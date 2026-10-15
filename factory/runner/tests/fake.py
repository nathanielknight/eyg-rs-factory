"""An in-memory stand-in for IncusClient, with a fake clock."""

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runner.incus import IncusError, Operation, OutputRejected


@dataclass
class Command:
    "Scripted behaviour for the main command."

    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    files: dict[str, bytes] = field(default_factory=dict)  # written into the guest
    hang: bool = False  # never finishes on its own
    vm_dies: bool = False  # the instance goes away while it runs


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self) -> datetime:
        return datetime(2026, 10, 13, tzinfo=UTC) + timedelta(seconds=self.t)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeIncus:
    def __init__(self, clock: Clock, command: Command | None = None):
        self.clock = clock
        self.command = command or Command()
        self.instances: dict[str, dict] = {}
        self.ops: dict[str, dict] = {}
        self.logs: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.ids = itertools.count(1)
        self.boot_failures = 0  # agent-ready probes that fail before success
        self.fail_create = False
        self.fail_push = False
        self.exec_metadata_has_output = True

    # -- helpers -----------------------------------------------------------

    def _op(
        self, status_code=200, metadata=None, err="", running=False, instance=None
    ) -> str:
        op_id = f"op{next(self.ids)}"
        self.ops[op_id] = {
            "id": op_id,
            "status_code": 103 if running else status_code,
            "err": err,
            "metadata": metadata or {},
            "instance": instance,
        }
        return op_id

    def _operation(self, op_id) -> Operation:
        if op_id not in self.ops:
            raise IncusError(f"operation {op_id} not found")
        d = self.ops[op_id]
        return Operation.from_json({k: v for k, v in d.items() if k != "instance"})

    def _running(self, name) -> dict:
        inst = self.instances.get(name)
        if inst is None:
            raise IncusError(f"instance {name} not found")
        if inst["status"] != "Running":
            raise IncusError(f"instance {name} is not running")
        return inst

    # -- IncusClient surface -----------------------------------------------

    def create_instance(self, name, source, profiles):
        self.calls.append(("create", name, source, tuple(profiles)))
        if self.fail_create:
            raise IncusError("create failed")
        if name in self.instances:
            raise IncusError(f"instance {name} already exists")
        self.instances[name] = {
            "status": "Running",
            "config": {"volatile.base_image": "abc123"},
            "files": {},
            "profiles": profiles,
        }
        return self._op()

    def get_instance(self, name):
        return {"name": name, **self.instances[name]}

    def list_instances(self):
        return [{"name": n, "status": i["status"]} for n, i in self.instances.items()]

    def get_operation(self, op_id):
        return self._operation(op_id)

    def wait(self, op_id, timeout_s):
        op = self._operation(op_id)
        if not op.finished:
            self.clock.sleep(timeout_s)
        return op

    def wait_ok(self, op_id, timeout_s, what):
        op = self.wait(op_id, timeout_s)
        if not op.succeeded:
            raise IncusError(f"{what}: {op.err}")
        return op

    def exec(self, name, argv, *, env=None, uid=0, gid=0, cwd="/", record_output=True):
        self.calls.append(("exec", name, tuple(argv), uid, gid, cwd))
        inst = self._running(name)
        if argv == ["true"]:
            if self.boot_failures:
                self.boot_failures -= 1
                raise IncusError("VM agent isn't currently running")
            return self._op(metadata={"return": 0})
        if argv[0] == "tar":
            return self._op(metadata={"return": 0})
        c = self.command
        if c.hang:
            # Partial output is recorded; the metadata only says where once
            # the op finishes (when stop() kills it).
            op_id = self._op(running=True, instance=name)
            prefix = f"/1.0/instances/{name}/logs/exec-output/exec_{op_id}"
            self.logs[prefix + ".stdout"], self.logs[prefix + ".stderr"] = (
                c.stdout,
                c.stderr,
            )
            return op_id
        if c.vm_dies:
            inst["status"] = "Stopped"
        inst["files"].update({p: (b, "file") for p, b in c.files.items()})
        op_id = f"op{next(self.ids)}"
        out = f"/1.0/instances/{name}/logs/exec-output/exec_{op_id}.stdout"
        err = f"/1.0/instances/{name}/logs/exec-output/exec_{op_id}.stderr"
        self.logs[out], self.logs[err] = c.stdout, c.stderr
        metadata = {"return": -1 if c.vm_dies else c.exit_code}
        if self.exec_metadata_has_output:
            metadata["output"] = {"1": out, "2": err}
        self.ops[op_id] = {
            "id": op_id,
            "status_code": 200,
            "err": "",
            "metadata": metadata,
            "instance": name,
        }
        return op_id

    def list_exec_logs(self, name):
        prefix = f"/1.0/instances/{name}/logs/exec-output/"
        return [p for p in self.logs if p.startswith(prefix)]

    def download_log(self, api_path, dest: Path, max_bytes):
        data = self.logs[api_path]
        dest.write_bytes(data[:max_bytes])
        return len(data) > max_bytes

    def push_file(self, name, guest_path, content, *, mode=0o644, uid=0, gid=0):
        self.calls.append(("push", name, guest_path, mode, uid, gid))
        if self.fail_push:
            raise IncusError("push failed")
        self._running(name)["files"][guest_path] = (b"".join(content), "file")

    def pull_file(self, name, guest_path, dest: Path, max_bytes):
        import hashlib

        files = self._running(name)["files"]
        if guest_path not in files:
            raise OutputRejected("not found")
        data, kind = files[guest_path]
        if kind != "file":
            raise OutputRejected(f"not a regular file (type {kind!r})")
        if len(data) > max_bytes:
            raise OutputRejected(f"exceeds max_bytes {max_bytes}")
        dest.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def stop(self, name, force=True):
        self.calls.append(("stop", name))
        inst = self.instances.get(name)
        if inst is None:
            raise IncusError(f"instance {name} not found")
        if inst["status"] == "Stopped":
            return self._op(status_code=400, err="The instance is already stopped")
        inst["status"] = "Stopped"
        # Like Incus 6.0: a killed command's op succeeds, returning -1.
        for op_id, op in self.ops.items():
            if op["instance"] == name and op["status_code"] == 103:
                metadata = {"return": -1}
                if self.exec_metadata_has_output:
                    prefix = f"/1.0/instances/{name}/logs/exec-output/exec_{op_id}"
                    metadata["output"] = {
                        "1": prefix + ".stdout",
                        "2": prefix + ".stderr",
                    }
                op.update(status_code=200, metadata=metadata)
        return self._op()

    def delete(self, name):
        self.calls.append(("delete", name))
        if name not in self.instances:
            raise IncusError(f"instance {name} not found")
        del self.instances[name]
        return self._op()
