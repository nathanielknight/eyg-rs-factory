from django.db import models, transaction


class TicketState(models.TextChoices):
    OPEN = "open", "Open"  # ready to be worked on
    DEVELOPING = "developing", "Developing"  # claimed by a dev run
    SUBMITTED = "submitted", "Submitted"  # awaiting verification
    VERIFYING = "verifying", "Verifying"  # verification run in progress
    NEEDS_REVIEW = "needs_review", "Needs review"  # waiting on a human
    DONE = "done", "Done"  # merged
    ERROR = "error", "Error"  # infra fault; manual retry
    CANCELLED = "cancelled", "Cancelled"


class InvalidTransition(Exception):
    pass


def _build_allowed_transitions() -> dict[TicketState, set[TicketState]]:
    T = TicketState
    terminal = {T.DONE, T.CANCELLED}
    allowed: dict[TicketState, set[TicketState]] = {
        T.OPEN: {T.DEVELOPING},
        T.DEVELOPING: {T.SUBMITTED, T.OPEN},
        T.SUBMITTED: {T.VERIFYING},
        T.VERIFYING: {T.DONE, T.OPEN, T.NEEDS_REVIEW},
        T.NEEDS_REVIEW: {T.SUBMITTED, T.OPEN},
        T.ERROR: {T.OPEN, T.SUBMITTED},
    }
    # Any non-terminal state may move to error or cancelled.
    for state in T:
        if state in terminal:
            continue
        allowed.setdefault(state, set()).add(T.CANCELLED)
        if state != T.ERROR:
            allowed[state].add(T.ERROR)
    return allowed


ALLOWED_TRANSITIONS = _build_allowed_transitions()


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
    )
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"#{self.pk or ''} {self.title}"

    def transition_to(self, new_state: str, note: str = "") -> None:
        """Move the ticket to `new_state`, enforcing the state machine.

        Raises InvalidTransition if not allowed. If `note` is given, a
        TicketNote is recorded atomically with the state change.
        """
        new_state = TicketState(new_state)
        current = TicketState(self.state)
        if new_state not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"{current.value} -> {new_state.value}")
        self.state = new_state


class TicketNote(models.Model):
    ticket = models.ForeignKey(to=Ticket, on_delete=models.CASCADE)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
