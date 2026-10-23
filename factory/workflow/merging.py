"""Merges: move main to a validation run's merge commit.

Only when the run passed, review is cleared, and main is still the base it
was validated on; the ref update itself checks that last part atomically. A
crash between moving main and recording the Merge is picked up next time by
noticing main already contains the commit.
"""

import logging

from django.db import transaction

from ticket.models import NoteSource

from .git import Repo
from .models import Merge, Outcome, RunStatus, ValidationRun
from .state import (
    SubmissionState,
    human_cutoff,
    latest_submission,
    latest_validation_run,
    refresh_ticket_state,
    submission_state,
    unfinished_tickets,
)
from .support import add_note

log = logging.getLogger(__name__)


def mergeable() -> list[ValidationRun]:
    runs = []
    for ticket in unfinished_tickets():
        submission = latest_submission(ticket.pk)
        if submission is None:
            continue
        state = submission_state(submission, human_cutoff(ticket.pk))
        if state == SubmissionState.MERGEABLE:
            runs.append(latest_validation_run(submission))
    return runs


def merge(repo: Repo, vr: ValidationRun) -> Merge | None:
    "Merge `vr`'s commit into main if it may; None if not (yet)."
    sub = vr.submission
    if not (
        vr.status == RunStatus.FINISHED
        and vr.outcome == Outcome.PASSED
        and vr.merge_sha
        and sub.review_cleared
    ):
        return None
    if existing := Merge.objects.filter(validation_run=vr).first():
        return existing

    main = repo.main_sha()
    if main != vr.base_sha and not repo.is_ancestor(vr.merge_sha, main):
        log.info("%s: main moved to %s; needs revalidation", vr, main[:12])
        return None
    if main == vr.base_sha and not repo.update_ref(
        repo.main_ref, vr.merge_sha, vr.base_sha
    ):
        log.info("%s: main moved while merging", vr)
        return None

    ticket_id = sub.dev_run.ticket_id
    with transaction.atomic():
        record, created = Merge.objects.get_or_create(
            validation_run=vr,
            defaults={"parent_sha": vr.base_sha, "result_sha": vr.merge_sha},
        )
        if created:
            add_note(
                ticket_id,
                f"Submission {sub.pk} merged into main as {vr.merge_sha[:12]}.",
                NoteSource.SYSTEM,
                validation_run=vr,
            )
    refresh_ticket_state(ticket_id)
    return record
