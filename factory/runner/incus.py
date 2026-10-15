"""A thin client for the parts of the Incus REST API the runner needs.

Talks to the local Unix socket, or to a remote over TLS with a client
certificate. No websockets: exec runs detached with record-output, so a
command outlives the connection that started it and can be re-attached to by
waiting on its operation ID.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .types import ImageSource, SnapshotSource, Source

# Operation status codes (https://linuxcontainers.org/incus/docs/main/rest-api/).
OP_SUCCESS = 200
OP_FAILURE = 400
OP_CANCELLED = 401
OP_FINISHED = (OP_SUCCESS, OP_FAILURE, OP_CANCELLED)

# Keys in a finished exec operation's metadata, checked against Incus 6.0.4.
# Log files are named exec_<operation id>.stdout/.stderr and stay readable
# after the instance is stopped.
EXEC_RETURN_KEY = "return"
EXEC_OUTPUT_KEY = "output"  # {"1": stdout log path, "2": stderr log path}
# What Incus 6.0 reports when the command dies from a signal or the instance
# is stopped under it; the operation itself still succeeds.
KILLED_RETURN_CODE = -1

CHUNK = 64 * 1024


class IncusError(Exception):
    "The Incus API refused a request, failed an operation, or was unreachable."


class OutputRejected(Exception):
    "A pulled file was missing, oversize or not a regular file."


@dataclass(frozen=True)
class Operation:
    id: str
    status_code: int
    err: str
    metadata: dict
    raw: dict

    @classmethod
    def from_json(cls, data: dict) -> "Operation":
        return cls(
            id=data["id"],
            status_code=data["status_code"],
            err=data.get("err") or "",
            metadata=data.get("metadata") or {},
            raw=data,
        )

    @property
    def finished(self) -> bool:
        return self.status_code in OP_FINISHED

    @property
    def succeeded(self) -> bool:
        return self.status_code == OP_SUCCESS


def _source_json(source: Source) -> dict:
    match source:
        case SnapshotSource(instance=instance, snapshot=snapshot):
            return {"type": "copy", "source": f"{instance}/{snapshot}"}
        case ImageSource(fingerprint=fingerprint):
            return {"type": "image", "fingerprint": fingerprint}
    raise TypeError(f"unknown source {source!r}")


class IncusClient:
    def __init__(self, http: httpx.Client, project: str, call_timeout: float = 30.0):
        self.http = http
        self.project = project
        self.call_timeout = call_timeout

    @classmethod
    def unix(
        cls, socket_path: str, project: str, call_timeout: float = 30.0
    ) -> "IncusClient":
        http = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url="http://incus",
            timeout=call_timeout,
        )
        return cls(http, project, call_timeout)

    @classmethod
    def remote(
        cls,
        url: str,
        client_cert: str,
        client_key: str,
        server_cert: str,
        project: str,
        call_timeout: float = 30.0,
    ) -> "IncusClient":
        import ssl

        ctx = ssl.create_default_context(cafile=server_cert)
        ctx.load_cert_chain(client_cert, client_key)
        http = httpx.Client(base_url=url, verify=ctx, timeout=call_timeout)
        return cls(http, project, call_timeout)

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        params = {"project": self.project, **kwargs.pop("params", {})}
        try:
            resp = self.http.request(method, path, params=params, **kwargs)
        except httpx.HTTPError as e:
            raise IncusError(f"{method} {path}: {type(e).__name__}: {e}") from e
        return resp

    def _json(self, method: str, path: str, **kwargs) -> dict:
        resp = self._request(method, path, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            raise IncusError(f"{method} {path}: HTTP {resp.status_code}, non-JSON body")
        if body.get("type") == "error" or resp.status_code >= 400:
            raise IncusError(
                f"{method} {path}: {body.get('error') or f'HTTP {resp.status_code}'}"
            )
        return body

    def _async(self, method: str, path: str, **kwargs) -> str:
        "Make a request that starts an operation; return the operation ID."
        body = self._json(method, path, **kwargs)
        op = body.get("metadata") or {}
        if "id" in op:
            return op["id"]
        # Fall back to the operation URL, e.g. /1.0/operations/<id>.
        return body["operation"].rsplit("/", 1)[-1]

    # -- operations --------------------------------------------------------

    def get_operation(self, op_id: str) -> Operation:
        return Operation.from_json(
            self._json("GET", f"/1.0/operations/{op_id}")["metadata"]
        )

    def wait(self, op_id: str, timeout_s: float) -> Operation:
        """Wait up to `timeout_s` for an operation; return it, finished or not.

        Raises IncusError only if the operation can't be read at all.
        """
        timeout_s = max(0, int(timeout_s))
        try:
            body = self._json(
                "GET",
                f"/1.0/operations/{op_id}/wait",
                params={"timeout": timeout_s},
                timeout=timeout_s + self.call_timeout,
            )
            return Operation.from_json(body["metadata"])
        except IncusError:
            # Some versions answer a wait timeout with an error; ask directly.
            return self.get_operation(op_id)

    def wait_ok(self, op_id: str, timeout_s: float, what: str) -> Operation:
        "Wait for an operation and raise IncusError unless it succeeded."
        op = self.wait(op_id, timeout_s)
        if not op.finished:
            raise IncusError(f"{what}: operation {op_id} not done after {timeout_s}s")
        if not op.succeeded:
            raise IncusError(
                f"{what}: {op.err or f'operation status {op.status_code}'}"
            )
        return op

    # -- instances ---------------------------------------------------------

    def create_instance(self, name: str, source: Source, profiles: list[str]) -> str:
        return self._async(
            "POST",
            "/1.0/instances",
            json={
                "name": name,
                "type": "virtual-machine",
                "start": True,
                "profiles": profiles,
                "source": _source_json(source),
            },
        )

    def get_instance(self, name: str) -> dict:
        return self._json("GET", f"/1.0/instances/{name}")["metadata"]

    def list_instances(self) -> list[dict]:
        return self._json("GET", "/1.0/instances", params={"recursion": 1})["metadata"]

    def stop(self, name: str, force: bool = True) -> str:
        return self._async(
            "PUT",
            f"/1.0/instances/{name}/state",
            json={"action": "stop", "force": force},
        )

    def delete(self, name: str) -> str:
        return self._async("DELETE", f"/1.0/instances/{name}")

    # -- exec and logs -----------------------------------------------------

    def exec(
        self,
        name: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        uid: int = 0,
        gid: int = 0,
        cwd: str = "/",
        record_output: bool = True,
    ) -> str:
        return self._async(
            "POST",
            f"/1.0/instances/{name}/exec",
            json={
                "command": argv,
                "environment": env or {},
                "user": uid,
                "group": gid,
                "cwd": cwd,
                "interactive": False,
                "wait-for-websocket": False,
                "record-output": record_output,
            },
        )

    def list_exec_logs(self, name: str) -> list[str]:
        "API paths of the recorded exec output files for an instance."
        return self._json("GET", f"/1.0/instances/{name}/logs/exec-output")["metadata"]

    def download_log(self, api_path: str, dest: Path, max_bytes: int) -> bool:
        """Stream a recorded log to `dest`, keeping at most `max_bytes`.

        Returns True if the log was truncated.
        """
        path = httpx.URL(api_path).path  # drop any ?project= the server added
        truncated = False
        with dest.open("wb") as f:
            try:
                with self.http.stream(
                    "GET", path, params={"project": self.project}
                ) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        raise IncusError(f"GET {path}: HTTP {resp.status_code}")
                    written = 0
                    for chunk in resp.iter_bytes(CHUNK):
                        room = max_bytes - written
                        if len(chunk) > room:
                            f.write(chunk[:room])
                            truncated = True
                            break
                        f.write(chunk)
                        written += len(chunk)
            except httpx.HTTPError as e:
                raise IncusError(f"GET {path}: {type(e).__name__}: {e}") from e
        return truncated

    # -- files -------------------------------------------------------------

    def push_file(
        self,
        name: str,
        guest_path: str,
        content: Iterable[bytes],
        *,
        mode: int = 0o644,
        uid: int = 0,
        gid: int = 0,
    ) -> None:
        self._json(
            "POST",
            f"/1.0/instances/{name}/files",
            params={"path": guest_path},
            content=content,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Incus-uid": str(uid),
                "X-Incus-gid": str(gid),
                "X-Incus-mode": f"{mode:04o}",
                "X-Incus-type": "file",
                "X-Incus-write": "overwrite",
            },
        )

    def pull_file(
        self, name: str, guest_path: str, dest: Path, max_bytes: int
    ) -> tuple[int, str]:
        """Fetch one regular file to `dest`; return (size, sha256 hex).

        Raises OutputRejected for a missing file, a symlink or directory, or
        anything over `max_bytes`; `dest` is removed in those cases.
        """
        url = f"/1.0/instances/{name}/files"
        try:
            with self.http.stream(
                "GET", url, params={"project": self.project, "path": guest_path}
            ) as resp:
                if resp.status_code == 404:
                    raise OutputRejected("not found")
                if resp.status_code >= 400:
                    resp.read()
                    raise OutputRejected(_error_text(resp))
                kind = resp.headers.get("X-Incus-type")
                if kind != "file":
                    raise OutputRejected(f"not a regular file (type {kind!r})")
                length = resp.headers.get("Content-Length")
                if length is not None and int(length) > max_bytes:
                    raise OutputRejected(
                        f"{length} bytes exceeds max_bytes {max_bytes}"
                    )
                digest = hashlib.sha256()
                size = 0
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise OutputRejected(f"exceeds max_bytes {max_bytes}")
                        digest.update(chunk)
                        f.write(chunk)
        except httpx.HTTPError as e:
            dest.unlink(missing_ok=True)
            raise OutputRejected(f"{type(e).__name__}: {e}") from e
        except OutputRejected:
            dest.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()


def _error_text(resp: httpx.Response) -> str:
    try:
        return json.loads(resp.content).get("error") or f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"
