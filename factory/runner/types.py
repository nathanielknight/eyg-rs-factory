"""The caller-facing interface of the guest runner: put files into a fresh VM,
run one command, capture its output and pull out named artifacts.

Nothing here knows about tickets, agents or CI; those are callers.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, NewType, Protocol

RunId = NewType("RunId", str)  # opaque, runner-generated (UUIDv7)

TerminalStatus = Literal[
    "completed", "timeout", "cancelled", "infra_error", "artifact_error"
]
Status = Literal["pending", "running"] | TerminalStatus


@dataclass(frozen=True)
class SnapshotSource:
    "Clone a snapshot of a (never-started) golden instance."

    instance: str
    snapshot: str

    def to_json(self) -> dict:
        return {
            "type": "snapshot",
            "instance": self.instance,
            "snapshot": self.snapshot,
        }


@dataclass(frozen=True)
class ImageSource:
    "Launch from a local image."

    fingerprint: str

    def to_json(self) -> dict:
        return {"type": "image", "fingerprint": self.fingerprint}


Source = SnapshotSource | ImageSource


def source_from_json(data: dict) -> Source:
    match data["type"]:
        case "snapshot":
            return SnapshotSource(instance=data["instance"], snapshot=data["snapshot"])
        case "image":
            return ImageSource(fingerprint=data["fingerprint"])
    raise ValueError(f"unknown source type {data['type']!r}")


@dataclass(frozen=True)
class Input:
    host_path: Path  # a single file; trees are passed as tarballs
    guest_path: str  # absolute
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    unpack: bool = (
        False  # if true, guest extracts the tarball at guest_path's directory
    )


@dataclass(frozen=True)
class Output:
    guest_path: str  # a single regular file, never a directory
    max_bytes: int
    required: bool = False  # missing required output -> status=artifact_error


@dataclass(frozen=True)
class RunSpec:
    name: str  # human label; not an identifier
    source: Source
    profiles: list[str]  # network, limits; chosen by the caller
    inputs: list[Input]
    argv: list[str]
    env: dict[str, str]
    uid: int
    gid: int
    cwd: str
    timeout_s: int  # wall clock for the command only
    outputs: list[Output]
    max_log_bytes: int  # cap per stream for stdout and stderr
    keep_on_failure: bool = False  # leave the instance stopped for debugging

    def __post_init__(self):
        if not self.argv:
            raise ValueError("argv must not be empty")
        for path in [
            self.cwd,
            *(i.guest_path for i in self.inputs),
            *(o.guest_path for o in self.outputs),
        ]:
            if not path.startswith("/"):
                raise ValueError(f"guest path {path!r} is not absolute")
        if self.timeout_s <= 0 or self.max_log_bytes < 0:
            raise ValueError(
                "timeout_s must be positive and max_log_bytes non-negative"
            )
        if any(o.max_bytes < 0 for o in self.outputs):
            raise ValueError("output max_bytes must be non-negative")


@dataclass(frozen=True)
class RunResult:
    run_id: RunId
    status: TerminalStatus
    exit_code: int | None  # set only when the command ran to completion
    stdout_path: Path  # host path, possibly truncated
    stderr_path: Path
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: dict[str, Path] = field(default_factory=dict)  # guest_path -> host path
    error: str | None = None  # infra or artifact detail
    image_fingerprint: str = ""
    instance_name: str = ""  # run-<run_id>
    # None only for a run cancelled before it launched.
    started_at: datetime | None = None
    command_started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class RunStatus:
    run_id: RunId
    status: Status
    created_at: datetime
    started_at: datetime | None
    command_started_at: datetime | None
    ended_at: datetime | None


class Runner(Protocol):
    def create(self, spec: RunSpec) -> RunId: ...  # records the invocation as pending
    def run(self, run_id: RunId) -> RunResult: ...  # blocking, re-entrant
    def cancel(self, run_id: RunId) -> None: ...  # force-stops the instance
    def status(self, run_id: RunId) -> RunStatus: ...  # current status and timestamps
