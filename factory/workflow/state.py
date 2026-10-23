"""Ticket and submission state, derived from the run records.

Nothing here is stored except Ticket.state, which is a cache for the admin
and the ticket list. Anything that writes run records calls
refresh_ticket_state afterwards.

Retry budgets count from the ticket's latest human note: adding a note is how
Nat hands a stalled or erroring ticket back to the factory.
"""

from datetime import datetime
from enum import StrEnum

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ticket.models import NoteSource, Ticket, TicketNote, TicketState

from .models import (
    LIVE_RUN_STATUSES,
    DevRun,
    Merge,
    Outcome,
    ReviewDecision,
    RunStatus,
    Submission,
    ValidationRun,
)


class SubmissionState(StrEnum):
    PENDING = "pending"  # no validation run yet
    VALIDATING = "validating"
    RETRY = "retry"  # the last validation errored; it will be re-run
    ERROR = "error"  # validation keeps erroring; waits on a human note
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    MERGEABLE = "mergeable"  # passed and cleared; merge, or revalidate if stale
    MERGED = "merged"


# Still wants something from the factory or a human; anything else is a dead
# end, and the ticket goes back to dev runs.
IN_FLIGHT = {
    SubmissionState.PENDING,
    SubmissionState.VALIDATING,
    SubmissionState.RETRY,
    SubmissionState.ERROR,
    SubmissionState.NEEDS_REVIEW,
    SubmissionState.MERGEABLE,
}


def human_cutoff(ticket_id: int) -> datetime | None:
    "When the ticket last got a human note; retry budgets count from here."
    return (
        TicketNote.objects.filter(ticket_id=ticket_id, source=NoteSource.HUMAN)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )


def _since(cutoff: datetime | None) -> Q:
    return Q(created_at__gt=cutoff) if cutoff else Q()


def latest_validation_run(submission: Submission) -> ValidationRun | None:
    return (
        submission.validation_runs.select_related("merge")
        .order_by("-created_at", "-pk")
        .first()
    )


def submission_state(
    submission: Submission, cutoff: datetime | None = None
) -> SubmissionState:
    S = SubmissionState
    if Merge.objects.filter(validation_run__submission=submission).exists():
        return S.MERGED
    if submission.review_decision == ReviewDecision.REJECTED:
        return S.REJECTED
    latest = latest_validation_run(submission)
    if latest is None:
        return S.PENDING
    if latest.is_live:
        return S.VALIDATING
    if latest.status == RunStatus.CANCELLED:
        return S.CANCELLED
    if latest.status == RunStatus.ERRORED:
        errors = submission.validation_runs.filter(
            _since(cutoff), status=RunStatus.ERRORED
        ).count()
        return S.RETRY if errors < settings.WORKFLOW_MAX_INFRA_ERRORS else S.ERROR
    if latest.outcome == Outcome.FAILED:
        return S.FAILED
    if submission.review_required and submission.review_decision is None:
        return S.NEEDS_REVIEW
    return S.MERGEABLE


def latest_submission(ticket_id: int) -> Submission | None:
    return (
        Submission.objects.filter(dev_run__ticket_id=ticket_id)
        .order_by("-submitted_at", "-pk")
        .first()
    )


def derive_ticket_state(ticket: Ticket) -> TicketState:
    T = TicketState
    if Merge.objects.filter(
        validation_run__submission__dev_run__ticket=ticket
    ).exists():
        return T.DONE
    if ticket.cancelled_at is not None:
        return T.CANCELLED
    if ticket.dev_runs.filter(status__in=LIVE_RUN_STATUSES).exists():
        return T.DEVELOPING

    cutoff = human_cutoff(ticket.pk)
    submission = latest_submission(ticket.pk)
    if submission is not None:
        match submission_state(submission, cutoff):
            case SubmissionState.PENDING | SubmissionState.RETRY:
                return T.SUBMITTED
            case SubmissionState.VALIDATING:
                return T.VERIFYING
            case SubmissionState.ERROR:
                return T.ERROR
            case SubmissionState.NEEDS_REVIEW:
                return T.NEEDS_REVIEW
            case SubmissionState.MERGEABLE:
                return T.VALIDATED

    runs = list(
        ticket.dev_runs.filter(_since(cutoff))
        .order_by("-created_at", "-pk")
        .values_list("status", flat=True)
    )
    trailing_errors = 0
    for status in runs:
        if status != RunStatus.ERRORED:
            break
        trailing_errors += 1
    if trailing_errors >= settings.WORKFLOW_MAX_INFRA_ERRORS:
        return T.ERROR
    attempts = sum(1 for s in runs if s not in (RunStatus.ERRORED, RunStatus.CANCELLED))
    if attempts >= settings.WORKFLOW_MAX_ATTEMPTS:
        return T.STALLED
    return T.OPEN


def refresh_ticket_state(ticket_id: int) -> TicketState:
    "Recompute and cache a ticket's state; bumps modified_at if it changed."
    ticket = Ticket.objects.get(pk=ticket_id)
    state = derive_ticket_state(ticket)
    if state != ticket.state:
        Ticket.objects.filter(pk=ticket_id).update(
            state=state, modified_at=timezone.now()
        )
    return state


def unfinished_tickets():
    "Tickets that might still have work in them. Done is forever; cancelled isn't."
    return (
        Ticket.objects.filter(cancelled_at__isnull=True)
        .exclude(state=TicketState.DONE)
        .order_by("created_at", "pk")
    )


def live_dev_runs():
    return DevRun.objects.filter(status__in=LIVE_RUN_STATUSES).select_related(
        "ticket", "invocation"
    )
