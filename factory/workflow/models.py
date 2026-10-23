"""The development lifecycle: DevRun -> Submission -> ValidationRun -> Merge.

Each link is a straight shot that can stop at any step; dead ends are kept.
There are no links between attempts: a retry starts from main plus the
ticket's notes. Rows are written by the agent bridge (Submission) and the
batch commands (everything else), and are left alone once terminal.

VM details (instance name, image fingerprint, logs, exit code, timings) live
on runner.Invocation; the runs here point at it.
"""

from django.db import models
from django.db.models import Q

from runner.models import Invocation
from ticket.models import Ticket, TicketNote


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    FINISHED = "finished", "Finished"  # outcome is set
    ERRORED = "errored", "Errored"  # infrastructure failure, not the agent's
    CANCELLED = "cancelled", "Cancelled"


LIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


class Outcome(models.TextChoices):
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed"


class VMRun(models.Model):
    """A run in a VM, as the workflow sees it.

    status is the run's lifecycle; outcome is the verdict, set only when it
    finished. Keeping them apart lets infra errors be left out of agent stats.
    """

    invocation = models.OneToOneField(
        Invocation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)s",
        help_text="Null until a VM is needed (a validation run can end before).",
    )
    status = models.CharField(
        max_length=20,
        choices=RunStatus.choices,
        default=RunStatus.QUEUED,
        db_index=True,
    )
    outcome = models.CharField(
        max_length=20, choices=Outcome.choices, null=True, blank=True
    )
    detail = models.TextField(
        blank=True, default="", help_text="Why it failed or errored, for humans."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=Q(status=RunStatus.FINISHED, outcome__isnull=False)
                | (~Q(status=RunStatus.FINISHED) & Q(outcome__isnull=True)),
                name="%(app_label)s_%(class)s_outcome_iff_finished",
            ),
        ]

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_RUN_STATUSES

    # The VM side, from the runner's record.

    @property
    def vm_name(self) -> str:
        return self.invocation.instance_name if self.invocation else ""

    @property
    def image_fingerprint(self) -> str:
        return self.invocation.image_fingerprint if self.invocation else ""

    @property
    def budget_s(self) -> int | None:
        return self.invocation.timeout_s if self.invocation else None

    @property
    def exit_code(self) -> int | None:
        return self.invocation.exit_code if self.invocation else None

    @property
    def log_path(self) -> str:
        return self.invocation.stderr_path if self.invocation else ""


class DevRun(VMRun):
    "An agent working a ticket in a sandboxed VM. Converged if it submitted."

    ticket = models.ForeignKey(
        Ticket, on_delete=models.PROTECT, related_name="dev_runs"
    )

    # Exactly what the agent was given.
    base_sha = models.CharField(max_length=64, help_text="main at dispatch")
    notes_through = models.ForeignKey(
        TicketNote,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="The last note the run saw; notes are append-only.",
    )
    prompt_version = models.CharField(max_length=64)
    prompt = models.TextField(help_text="As rendered and given to the agent.")
    test_command = models.TextField()
    session_id = models.UUIDField(unique=True, help_text="The Claude session ID.")

    # The run's bearer token for the agent bridge, as sha256 hex. The token
    # itself only ever goes into the VM.
    token_hash = models.CharField(max_length=64, unique=True, editable=False)

    transcript_path = models.CharField(
        max_length=4096,
        blank=True,
        default="",
        help_text="stream-json, captured host-side.",
    )

    class Meta(VMRun.Meta):
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"dev run {self.pk} (ticket {self.ticket_id}, {self.status})"

    @property
    def converged(self) -> bool:
        return hasattr(self, "submission")


class ReviewDecision(models.TextChoices):
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class Submission(models.Model):
    """A dev run's proposed change: a git bundle plus the agent's summary.

    Pending until it has a validation run. The bundle is verified on upload
    but only fetched into the bare repo by the validate command.
    """

    dev_run = models.OneToOneField(
        DevRun, on_delete=models.PROTECT, related_name="submission"
    )
    external_id = models.CharField(
        max_length=64, unique=True, help_text="Client-chosen, for idempotent retries."
    )
    bundle_path = models.CharField(max_length=4096)
    head_sha = models.CharField(max_length=64)
    base_sha = models.CharField(max_length=64)
    summary = models.TextField()
    submitted_at = models.DateTimeField(auto_now_add=True)

    review_required = models.BooleanField(default=False)
    review_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text='"agent_flag" or "gate:<rule>"',
    )
    review_decision = models.CharField(
        max_length=20, choices=ReviewDecision.choices, null=True, blank=True
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"submission {self.pk} ({self.head_sha[:10]})"

    @property
    def review_cleared(self) -> bool:
        return (
            not self.review_required or self.review_decision == ReviewDecision.APPROVED
        )


class Check(models.TextChoices):
    "The step a failed validation run stopped at."

    FETCH = "fetch", "Fetch"
    GATE = "gate", "Gate"
    CONFLICT = "conflict", "Needs rebase"
    BUILD = "build", "Build"
    CONFORMANCE = "conformance", "Conformance"
    LINT = "lint", "Lint"
    TICKET = "ticket", "Ticket check"
    HOLDOUT = "holdout", "Holdout"
    TIMEOUT = "timeout", "Timeout"


class ValidationRun(VMRun):
    """A deterministic check of a submission merged onto a specific main.

    merge_sha is the merge commit (with trailers) that CI tested; merging
    moves main to exactly that commit.
    """

    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="validation_runs"
    )
    base_sha = models.CharField(max_length=64, help_text="main when validated")
    merge_sha = models.CharField(max_length=64, blank=True, default="")
    oracle_version = models.CharField(max_length=64)
    holdout_version = models.CharField(max_length=64)
    failed_checks = models.JSONField(
        default=list, blank=True, help_text="Check values, in the order run."
    )
    results = models.JSONField(
        null=True, blank=True, help_text="The CI harness's report (no holdout cases)."
    )

    class Meta(VMRun.Meta):
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"validation run {self.pk} (submission {self.submission_id}, {self.status})"
        )


class Merge(models.Model):
    validation_run = models.OneToOneField(
        ValidationRun, on_delete=models.PROTECT, related_name="merge"
    )
    parent_sha = models.CharField(max_length=64)
    result_sha = models.CharField(max_length=64)
    merged_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"merge {self.result_sha[:10]}"
