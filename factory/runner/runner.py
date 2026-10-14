"""IncusRunner: the Runner protocol over the Incus REST API.

Every run gets a throwaway VM named run-<run_id>. Writes are ordered so a
crash at any point can be recovered from the stored row: the row exists before
any Incus call, the instance name before the instance, the operation ID before
the wait, and the terminal status last.
"""

import hashlib
import json
import logging
import posixpath
import secrets
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .incus import (
    CHUNK,
    EXEC_OUTPUT_KEY,
    EXEC_RETURN_KEY,
    KILLED_RETURN_CODE,
    IncusClient,
    IncusError,
    Operation,
    OutputRejected,
)
from .models import LIVE_STATUSES, Invocation
from .models import InvocationStatus as S
from .types import RunId, RunResult, RunSpec, RunStatus, source_from_json

log = logging.getLogger(__name__)

INSTANCE_PREFIX = "run-"


def new_run_id() -> RunId:
    "A UUIDv7 string: time-ordered, so runs sort by creation."
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10))
    value = (ms & (2**48 - 1)) << 80 | 0x7 << 76 | (rand >> 68 & 0xFFF) << 64
    value |= 0b10 << 62 | rand & (2**62 - 1)
    h = f"{value:032x}"
    return RunId(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}")


def instance_name(run_id: str) -> str:
    return f"{INSTANCE_PREFIX}{run_id}"


class Cancelled(Exception):
    "The run was cancelled while run() was working on it."


class IncusRunner:
    def __init__(
        self,
        client: IncusClient,
        run_root: Path,
        *,
        boot_timeout_s: float = 120,
        boot_poll_s: float = 2,
        step_timeout_s: float = 300,
        now: Callable[[], datetime] = timezone.now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.run_root = Path(run_root)
        self.boot_timeout_s = boot_timeout_s
        self.boot_poll_s = boot_poll_s
        self.step_timeout_s = step_timeout_s  # launch, unpack, stop, delete
        self.now = now
        self.sleep = sleep
        self.monotonic = monotonic

    # -- Runner protocol ---------------------------------------------------

    def create(self, spec: RunSpec) -> RunId:
        for i in spec.inputs:
            if not Path(i.host_path).is_file():
                raise FileNotFoundError(f"input {i.host_path} is not a file")
        run_id = new_run_id()
        # Every result points at real (possibly empty) logs, however early
        # the run stops.
        run_dir = self.run_dir(run_id)
        (run_dir / "artifacts").mkdir(parents=True)
        for stream in ("stdout", "stderr"):
            (run_dir / f"{stream}.log").touch()
        Invocation.objects.create(
            id=run_id,
            name=spec.name,
            source=spec.source.to_json(),
            profiles=list(spec.profiles),
            argv=list(spec.argv),
            env=dict(spec.env),
            uid=spec.uid,
            gid=spec.gid,
            cwd=spec.cwd,
            timeout_s=spec.timeout_s,
            max_log_bytes=spec.max_log_bytes,
            keep_on_failure=spec.keep_on_failure,
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
            inputs=[
                {
                    "host_path": str(i.host_path),
                    "guest_path": i.guest_path,
                    "mode": i.mode,
                    "uid": i.uid,
                    "gid": i.gid,
                    "unpack": i.unpack,
                }
                for i in spec.inputs
            ],
            outputs=[
                {
                    "guest_path": o.guest_path,
                    "max_bytes": o.max_bytes,
                    "required": o.required,
                }
                for o in spec.outputs
            ],
        )
        return run_id

    def run(self, run_id: RunId) -> RunResult:
        inv = Invocation.objects.get(pk=run_id)
        if not inv.is_live:
            return inv.to_result()

        outcome = _Outcome()
        try:
            try:
                if inv.status == S.PENDING:
                    self._launch(inv)
                    self._wait_for_agent(inv)
                    self._push_inputs(inv)
                    self._start_command(inv)
                op = self._wait_command(inv, outcome)
                outcome.op = op
                self._collect_logs(inv, op, outcome)
                if outcome.status == S.COMPLETED:
                    self._pull_outputs(inv, outcome)
            except Cancelled:
                outcome.fail(S.CANCELLED, "cancelled")
            except IncusError as e:
                outcome.fail(S.INFRA_ERROR, str(e))
            except Exception as e:
                log.exception("run %s failed unexpectedly", run_id)
                outcome.fail(S.INFRA_ERROR, f"internal error: {type(e).__name__}: {e}")
            return self._finalise(inv, outcome)
        finally:
            self._teardown(inv)

    def cancel(self, run_id: RunId) -> None:
        updated = Invocation.objects.filter(pk=run_id, status__in=LIVE_STATUSES).update(
            status=S.CANCELLED, exit_code=None, error="cancelled", ended_at=self.now()
        )
        if not updated:
            return
        inv = Invocation.objects.get(pk=run_id)
        # A blocked run() sees its operation fail and tears down; if nothing is
        # running this one, the sweeper deletes the stopped instance.
        if inv.instance_name:
            try:
                self.client.wait_ok(
                    self.client.stop(inv.instance_name), self.step_timeout_s, "stop"
                )
            except IncusError as e:
                log.warning("cancel %s: stop failed: %s", run_id, e)

    def status(self, run_id: RunId) -> RunStatus:
        return Invocation.objects.get(pk=run_id).to_status()

    def sweep(self) -> list[str]:
        """Delete run-* instances with no live run record. Returns their names.

        Instances deliberately kept by keep_on_failure are left alone.
        """
        swept = []
        for inst in self.client.list_instances():
            name = inst["name"]
            if not name.startswith(INSTANCE_PREFIX):
                continue
            inv = Invocation.objects.filter(
                pk=name.removeprefix(INSTANCE_PREFIX)
            ).first()
            if inv is not None and (inv.is_live or inv.instance_kept):
                continue
            log.info("sweeping orphaned instance %s", name)
            self._destroy(name)
            swept.append(name)
        return swept

    # -- lifecycle steps ---------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.run_root / run_id

    def _save(self, inv: Invocation, **fields) -> None:
        "Write fields to a live row; raise Cancelled if it's no longer live."
        for k, v in fields.items():
            setattr(inv, k, v)
        if not Invocation.objects.filter(pk=inv.pk, status__in=LIVE_STATUSES).update(
            **fields
        ):
            raise Cancelled()

    def _check_cancelled(self, inv: Invocation) -> None:
        if not Invocation.objects.filter(pk=inv.pk, status__in=LIVE_STATUSES).exists():
            raise Cancelled()

    def _launch(self, inv: Invocation) -> None:
        name = instance_name(inv.id)
        if inv.instance_name:
            # Re-entry after a crash before the command started: start over.
            self._destroy(inv.instance_name)
        self._save(inv, instance_name=name, started_at=self.now())

        op_id = self.client.create_instance(
            name, source_from_json(inv.source), inv.profiles
        )
        self.client.wait_ok(op_id, self.step_timeout_s, "launch")
        config = self.client.get_instance(name).get("config", {})
        self._save(inv, image_fingerprint=config.get("volatile.base_image", ""))

    def _wait_for_agent(self, inv: Invocation) -> None:
        deadline = self.monotonic() + self.boot_timeout_s
        last_error = "no response"
        while True:
            self._check_cancelled(inv)
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise IncusError(
                    f"guest agent not ready after {self.boot_timeout_s}s: {last_error}"
                )
            try:
                op_id = self.client.exec(inv.instance_name, ["true"])
                op = self.client.wait(op_id, min(remaining, 10))
                if op.succeeded and op.metadata.get(EXEC_RETURN_KEY) == 0:
                    return
                last_error = op.err or f"operation status {op.status_code}"
            except IncusError as e:
                last_error = str(e)
            self.sleep(self.boot_poll_s)

    def _push_inputs(self, inv: Invocation) -> None:
        records = [dict(r) for r in inv.inputs]
        for record in records:
            self._check_cancelled(inv)
            self.client.push_file(
                inv.instance_name,
                record["guest_path"],
                _read_hashing(Path(record["host_path"]), record),
                mode=record["mode"],
                uid=record["uid"],
                gid=record["gid"],
            )
            if record["unpack"]:
                # Ownership of the extracted files comes from the tarball.
                # Recorded output isn't optional: without it, Incus 6.0
                # reports tar's return code as -1.
                guest_dir = posixpath.dirname(record["guest_path"])
                op_id = self.client.exec(
                    inv.instance_name,
                    ["tar", "-xf", record["guest_path"], "-C", guest_dir],
                )
                op = self.client.wait_ok(
                    op_id, self.step_timeout_s, f"unpack {record['guest_path']}"
                )
                if op.metadata.get(EXEC_RETURN_KEY) != 0:
                    raise IncusError(
                        f"unpack {record['guest_path']}: tar exited {op.metadata.get(EXEC_RETURN_KEY)}"
                    )
        self._save(inv, inputs=records)

    def _start_command(self, inv: Invocation) -> None:
        self._check_cancelled(inv)
        op_id = self.client.exec(
            inv.instance_name,
            inv.argv,
            env=inv.env,
            uid=inv.uid,
            gid=inv.gid,
            cwd=inv.cwd,
            record_output=True,
        )
        self._save(
            inv, operation_id=op_id, command_started_at=self.now(), status=S.RUNNING
        )

    def _wait_command(self, inv: Invocation, outcome: "_Outcome") -> Operation:
        """Wait for the command until its deadline (re-entrant after a crash)."""
        deadline = inv.command_started_at + timedelta(seconds=inv.timeout_s)
        while True:
            remaining = (deadline - self.now()).total_seconds()
            op = self.client.wait(inv.operation_id, max(0, min(remaining, 300)))
            if op.finished or remaining <= 0:
                break
        if not op.finished:
            outcome.fail(S.TIMEOUT, f"command exceeded timeout_s={inv.timeout_s}")
            try:
                self.client.wait_ok(
                    self.client.stop(inv.instance_name), self.step_timeout_s, "stop"
                )
                op = self.client.get_operation(inv.operation_id)
            except IncusError as e:
                outcome.add_error(f"force-stop after timeout: {e}")
            return op
        # A cancel() under us shows up below as a killed command; _finalise
        # keeps the cancelled status, and we still collect the partial logs.
        if not op.succeeded:
            outcome.fail(
                S.INFRA_ERROR, f"exec: {op.err or f'operation status {op.status_code}'}"
            )
            return op
        code = op.metadata.get(EXEC_RETURN_KEY)
        if not isinstance(code, int):
            outcome.fail(
                S.INFRA_ERROR, f"exec metadata has no {EXEC_RETURN_KEY!r} code"
            )
            return op
        if code == KILLED_RETURN_CODE:
            # Incus 6.0 reports both a signal death and the instance going
            # away as a successful op returning -1. Only the first is the
            # command's own doing.
            try:
                state = self.client.get_instance(inv.instance_name).get("status")
            except IncusError as e:
                state = f"unknown ({e})"
            if state != "Running":
                outcome.fail(
                    S.INFRA_ERROR, f"instance {state} while the command ran (exit -1)"
                )
                return op
        outcome.status, outcome.exit_code = S.COMPLETED, code
        return op

    def _collect_logs(
        self, inv: Invocation, op: Operation, outcome: "_Outcome"
    ) -> None:
        paths = (op.metadata or {}).get(EXEC_OUTPUT_KEY) or {}
        if not paths:
            # Not finished cleanly (e.g. timeout): find the files by op ID.
            try:
                for p in self.client.list_exec_logs(inv.instance_name):
                    base = posixpath.basename(p.split("?", 1)[0])
                    if inv.operation_id in base:
                        if base.endswith(".stdout"):
                            paths["1"] = p
                        elif base.endswith(".stderr"):
                            paths["2"] = p
            except IncusError as e:
                outcome.add_error(f"listing exec logs: {e}")
        for fd, stream in (("1", "stdout"), ("2", "stderr")):
            dest = Path(getattr(inv, f"{stream}_path"))
            try:
                if fd not in paths:
                    raise IncusError(f"no recorded {stream} log")
                outcome.truncated[stream] = self.client.download_log(
                    paths[fd], dest, inv.max_log_bytes
                )
            except IncusError as e:
                if outcome.status == S.COMPLETED:
                    # Don't hand back a "completed" run with its logs missing.
                    outcome.fail(S.INFRA_ERROR, f"collecting {stream}: {e}")
                else:
                    outcome.add_error(f"collecting {stream}: {e}")

    def _pull_outputs(self, inv: Invocation, outcome: "_Outcome") -> None:
        artifacts = self.run_dir(inv.id) / "artifacts"
        problems = []
        records = []
        for index, out in enumerate(inv.outputs):
            # Fixed host-side name; never the guest's.
            dest = artifacts / str(index)
            record = {k: out[k] for k in ("guest_path", "max_bytes", "required")}
            try:
                size, sha = self.client.pull_file(
                    inv.instance_name, out["guest_path"], dest, out["max_bytes"]
                )
                record.update(host_path=str(dest), size=size, sha256=sha)
            except OutputRejected as e:
                record["missing_reason"] = str(e)
                if out["required"]:
                    problems.append(f"{out['guest_path']}: {e}")
            records.append(record)
        outcome.outputs = records
        if problems:
            outcome.status = S.ARTIFACT_ERROR  # exit_code is kept for diagnosis
            outcome.add_error("required outputs: " + "; ".join(problems))

    def _finalise(self, inv: Invocation, outcome: "_Outcome") -> RunResult:
        run_dir = self.run_dir(inv.id)
        if outcome.op is not None:
            (run_dir / "incus-op.json").write_text(
                json.dumps(outcome.op.raw, indent=2, default=str)
            )
        collected = dict(
            stdout_truncated=outcome.truncated.get("stdout", False),
            stderr_truncated=outcome.truncated.get("stderr", False),
            outputs=outcome.outputs if outcome.outputs is not None else inv.outputs,
        )
        terminal = dict(
            status=outcome.status,
            exit_code=outcome.exit_code
            if outcome.status in (S.COMPLETED, S.ARTIFACT_ERROR)
            else None,
            error=outcome.error,
            ended_at=self.now(),
        )
        rows = Invocation.objects.filter(pk=inv.pk)
        if not rows.filter(status__in=LIVE_STATUSES).update(**collected, **terminal):
            # Cancelled under us: keep cancel()'s status, add what we collected.
            rows.update(**collected)
        inv.refresh_from_db()
        return inv.to_result()

    def _teardown(self, inv: Invocation) -> None:
        inv.refresh_from_db()
        if not inv.instance_name:
            return
        if inv.instance_kept:
            try:
                self.client.wait_ok(
                    self.client.stop(inv.instance_name), self.step_timeout_s, "stop"
                )
            except IncusError as e:
                log.info("teardown %s: stop: %s", inv.instance_name, e)
            return
        self._destroy(inv.instance_name)

    def _destroy(self, name: str) -> None:
        "Force-stop and delete an instance, tolerating either already being done."
        try:
            self.client.wait_ok(self.client.stop(name), self.step_timeout_s, "stop")
        except IncusError as e:
            log.info("destroy %s: stop: %s", name, e)
        try:
            self.client.wait_ok(self.client.delete(name), self.step_timeout_s, "delete")
        except IncusError as e:
            log.warning("destroy %s: delete: %s", name, e)


class _Outcome:
    "What run() has learned so far; written to the row by _finalise."

    def __init__(self):
        self.status = S.INFRA_ERROR
        self.exit_code: int | None = None
        self.error = ""
        self.op: Operation | None = None
        self.truncated: dict[str, bool] = {}
        self.outputs: list[dict] | None = None

    def fail(self, status: str, error: str) -> None:
        self.status = status
        self.exit_code = None
        self.add_error(error)

    def add_error(self, error: str) -> None:
        self.error = f"{self.error}; {error}" if self.error else error


def _read_hashing(path: Path, record: dict) -> Iterator[bytes]:
    "Stream a file, recording its size and sha256 in `record` once read."
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            yield chunk
    record["size"] = size
    record["sha256"] = digest.hexdigest()


def get_runner() -> IncusRunner:
    "The runner configured by settings, over the local Incus socket."
    client = IncusClient.unix(
        settings.RUNNER_INCUS_SOCKET,
        settings.RUNNER_INCUS_PROJECT,
        call_timeout=settings.RUNNER_HTTP_TIMEOUT_S,
    )
    return IncusRunner(
        client,
        settings.RUNNER_RUN_ROOT,
        boot_timeout_s=settings.RUNNER_BOOT_TIMEOUT_S,
    )
