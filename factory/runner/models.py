from pathlib import Path

from django.db import models

from .types import RunId, RunResult, RunStatus


class InvocationStatus(models.TextChoices):
    PENDING = "pending", "Pending"  # created; nothing has touched Incus yet
    RUNNING = "running", "Running"  # the command has been started
    COMPLETED = "completed", "Completed"  # exited on its own; exit_code is set
    TIMEOUT = "timeout", "Timeout"
    CANCELLED = "cancelled", "Cancelled"
    INFRA_ERROR = "infra_error", "Infra error"
    ARTIFACT_ERROR = "artifact_error", "Artifact error"


LIVE_STATUSES = (InvocationStatus.PENDING, InvocationStatus.RUNNING)


class Invocation(models.Model):
    """One guest-runner run: its spec, what happened, and where the output went.

    Only the runner's lifecycle methods (create, run, cancel, sweep) write
    these rows. Workflow tables point at Invocation; it never points outward.
    """

    # Identity
    id = models.CharField(primary_key=True, max_length=36, editable=False)
    name = models.CharField(max_length=255, help_text="Human label; not an identifier.")

    # Spec
    source = models.JSONField(help_text='{"type": "snapshot"|"image", ...}')
    profiles = models.JSONField(default=list)
    argv = models.JSONField()
    env = models.JSONField(default=dict)
    uid = models.PositiveIntegerField()
    gid = models.PositiveIntegerField()
    cwd = models.CharField(max_length=4096)
    timeout_s = models.PositiveIntegerField()
    max_log_bytes = models.PositiveBigIntegerField()
    keep_on_failure = models.BooleanField(default=False)
    # Per input: host_path, guest_path, mode, uid, gid, unpack; size and
    # sha256 are filled in as the bytes are pushed.
    inputs = models.JSONField(default=list)
    # Per output: guest_path, max_bytes, required; after the run, host_path,
    # size and sha256, or missing_reason.
    outputs = models.JSONField(default=list)

    # Logs
    stdout_path = models.CharField(max_length=4096, blank=True, default="")
    stderr_path = models.CharField(max_length=4096, blank=True, default="")
    stdout_truncated = models.BooleanField(default=False)
    stderr_truncated = models.BooleanField(default=False)

    # Status
    status = models.CharField(
        max_length=20,
        choices=InvocationStatus.choices,
        default=InvocationStatus.PENDING,
        db_index=True,
    )
    exit_code = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    # Claim: the worker driving a live run, and when it last showed signs of
    # life. A lapsed heartbeat lets another run() take over, or sweep reap it.
    claim_token = models.CharField(max_length=32, blank=True, default="")
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    # Incus
    operation_id = models.CharField(max_length=64, blank=True, default="")
    instance_name = models.CharField(max_length=64, blank=True, default="")
    image_fingerprint = models.CharField(max_length=64, blank=True, default="")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    command_started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.id} {self.name} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def instance_kept(self) -> bool:
        "True if teardown deliberately left the instance for debugging."
        return (
            self.keep_on_failure
            and not self.is_live
            and self.status != InvocationStatus.COMPLETED
        )

    def to_status(self) -> RunStatus:
        return RunStatus(
            run_id=RunId(self.id),
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            command_started_at=self.command_started_at,
            ended_at=self.ended_at,
        )

    def to_result(self) -> RunResult:
        if self.is_live:
            raise ValueError(f"run {self.id} is still {self.status}")
        return RunResult(
            run_id=RunId(self.id),
            status=self.status,
            exit_code=self.exit_code,
            stdout_path=Path(self.stdout_path),
            stderr_path=Path(self.stderr_path),
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            artifacts={
                o["guest_path"]: Path(o["host_path"])
                for o in self.outputs
                if o.get("host_path")
            },
            error=self.error or None,
            image_fingerprint=self.image_fingerprint,
            instance_name=self.instance_name,
            started_at=self.started_at,
            command_started_at=self.command_started_at,
            ended_at=self.ended_at,
        )
