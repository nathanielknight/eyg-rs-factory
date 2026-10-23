from django.db import models
from django.db.models import Q


class TicketState(models.TextChoices):
    "Derived from the ticket's runs (see workflow.state); stored only as a cache."

    OPEN = "open", "Open"  # ready for a dev run
    DEVELOPING = "developing", "Developing"  # a dev run is live
    SUBMITTED = "submitted", "Submitted"  # a submission awaits validation
    VERIFYING = "verifying", "Verifying"  # a validation run is live
    NEEDS_REVIEW = "needs_review", "Needs review"  # passed; waiting on a human
    VALIDATED = "validated", "Validated"  # passed; waiting to merge
    DONE = "done", "Done"  # merged
    STALLED = "stalled", "Stalled"  # out of attempts; add a note to retry
    ERROR = "error", "Error"  # repeated infra faults; add a note to retry
    CANCELLED = "cancelled", "Cancelled"


class Ticket(models.Model):
    "The unit of work in the factory"

    title = models.CharField(
        max_length=1024,
        blank=False,
        help_text="Succinct description of what's to be done",
    )
    description = models.TextField(
        help_text="Detailed description of what's to be done."
    )
    test_command = models.TextField(
        help_text="A shell command for the agent to test with during development."
    )
    conformance_script = models.TextField(
        blank=True,
        default="",
        help_text=(
            "A script (with a #! line) run in CI from the root of the merged "
            "tree; its output goes into the ticket's notes."
        ),
    )
    verify_command = models.TextField(
        blank=True,
        default="",
        help_text="A shell command to be run in CI; held back from the dev agent.",
    )
    state = models.CharField(
        max_length=20,
        choices=TicketState.choices,
        default=TicketState.OPEN,
        db_index=True,
        editable=False,
        help_text="Cached; recomputed from the ticket's runs.",
    )
    cancelled_at = models.DateTimeField(
        null=True, blank=True, help_text="Set to stop work on the ticket."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"#{self.pk or ''} {self.title}"


class NoteSource(models.TextChoices):
    DEV_RUN = "dev_run", "Dev run"  # the agent, during a run
    VALIDATION_RUN = "validation_run", "Validation run"
    HUMAN = "human", "Human"
    SYSTEM = "system", "System"  # the orchestrator, e.g. "VM errored, retrying"


class TicketNote(models.Model):
    "Append-only: a dev run records the last note it saw, not a set of them."

    ticket = models.ForeignKey(to=Ticket, on_delete=models.PROTECT)
    content = models.TextField()
    source = models.CharField(
        max_length=20, choices=NoteSource.choices, default=NoteSource.HUMAN
    )
    dev_run = models.ForeignKey(
        "workflow.DevRun",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="notes",
    )
    validation_run = models.ForeignKey(
        "workflow.ValidationRun",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The FK matching the source is set; system notes may point at
            # either run, or neither.
            models.CheckConstraint(
                condition=Q(
                    source=NoteSource.DEV_RUN,
                    dev_run__isnull=False,
                    validation_run__isnull=True,
                )
                | Q(
                    source=NoteSource.VALIDATION_RUN,
                    dev_run__isnull=True,
                    validation_run__isnull=False,
                )
                | Q(
                    source=NoteSource.HUMAN,
                    dev_run__isnull=True,
                    validation_run__isnull=True,
                )
                | Q(source=NoteSource.SYSTEM),
                name="ticket_note_source_matches_run",
            ),
        ]
