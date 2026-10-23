"""Validation runs: check a submission, merged onto current main, in CI.

Host-side first (fetch the bundle with fsck, gate checks, merge onto main),
then the CI harness in an ephemeral VM. Results go to the ticket as notes;
for the holdout set, counts only.

The merge commit CI tests, trailers and all, is the one the merge command
moves main to, so what merges is exactly what passed.
"""

import json
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from runner.models import Invocation, InvocationStatus
from runner.runner import IncusRunner
from runner.types import Input, Output, RunInProgress, RunSpec, source_from_json
from ticket.models import NoteSource, Ticket

from . import gate
from .git import SUBMISSION_REFS, VALIDATION_REFS, GitError, Identity, Repo
from .models import (
    LIVE_RUN_STATUSES,
    Check,
    Outcome,
    RunStatus,
    Submission,
    ValidationRun,
)
from .state import (
    SubmissionState,
    human_cutoff,
    latest_submission,
    latest_validation_run,
    refresh_ticket_state,
    submission_state,
    unfinished_tickets,
)
from .support import add_note, build_suite, notify_reviewers, sha256_hex

log = logging.getLogger(__name__)

HARNESS = Path(__file__).parent / "guest" / "ci_harness.py"
# Incus won't create parent directories on push, so inputs go straight into
# the CI user's home, and the harness unpacks the tree itself.
CI_UID = CI_GID = 1000
CI_DIR = "/home/agent"
WORK = f"{CI_DIR}/work"
SCRIPT = f"{CI_DIR}/conformance"
RESULTS = f"{CI_DIR}/results.json"


def to_validate(repo: Repo) -> list[Submission]:
    """Each ticket's latest submission, if it needs a validation run: it has
    none, the last one errored, one is live (to resume), or it passed against
    a main that has since moved."""
    main = repo.main_sha()
    work = []
    for ticket in unfinished_tickets():
        submission = latest_submission(ticket.pk)
        if submission is None:
            continue
        state = submission_state(submission, human_cutoff(ticket.pk))
        if state in (
            SubmissionState.PENDING,
            SubmissionState.RETRY,
            SubmissionState.VALIDATING,
        ):
            work.append(submission)
        elif state == SubmissionState.MERGEABLE:
            last = latest_validation_run(submission)
            # Already in main means a merge that crashed before recording
            # itself; the merge command finishes that.
            if last.base_sha != main and not repo.is_ancestor(last.merge_sha, main):
                work.append(submission)
    return work


def validate(runner: IncusRunner, repo: Repo, submission: Submission) -> ValidationRun:
    "Run (or resume) a validation of `submission` to the end."
    vr = submission.validation_runs.filter(status__in=LIVE_RUN_STATUSES).first()
    if vr is None:
        vr = ValidationRun.objects.create(
            submission=submission, base_sha=repo.main_sha()
        )
        refresh_ticket_state(submission.dev_run.ticket_id)
    if vr.invocation_id is None:
        try:
            prepare(runner, repo, vr)
        except (GitError, OSError) as e:
            log.exception("preparing %s failed", vr)
            _finish(vr, RunStatus.ERRORED, None, [], None, detail=f"prepare: {e}")
    vr.refresh_from_db()
    if vr.invocation_id is not None and vr.is_live:
        ValidationRun.objects.filter(pk=vr.pk, status=RunStatus.QUEUED).update(
            status=RunStatus.RUNNING
        )
        try:
            runner.run(vr.invocation_id)
        except RunInProgress:
            return vr
        collect(vr)
        vr.refresh_from_db()
    return vr


# -- host-side checks ---------------------------------------------------------


def prepare(runner: IncusRunner, repo: Repo, vr: ValidationRun) -> None:
    """Everything up to the VM: either fails the run, or attaches a pending
    invocation. Safe to repeat until then; nothing has run."""
    sub = vr.submission
    try:
        ref = f"{SUBMISSION_REFS}/{sub.pk}"
        head = repo.rev_parse(ref)
        if head is None:
            head = repo.fetch_bundle(Path(sub.bundle_path), ref)
    except GitError as e:
        return fail(vr, Check.FETCH, f"Fetching the bundle failed: {e.stderr}")
    if head != sub.head_sha:
        return fail(
            vr, Check.FETCH, f"The bundle's HEAD is {head}, not {sub.head_sha}."
        )

    result = gate.check(
        repo.changed_files(sub.base_sha, head),
        repo.added_lines(sub.base_sha, head),
        protected=settings.WORKFLOW_PROTECTED_PATHS,
        review_triggers=settings.WORKFLOW_REVIEW_TRIGGERS,
    )
    if result.rejected:
        paths = "\n".join(f"- `{p}`" for p in result.protected)
        return fail(
            vr,
            Check.GATE,
            f"The submission changes protected paths, so it can't be merged:\n\n{paths}",
        )
    if result.review:
        _require_review(sub, result.review_rules)

    merged = repo.merge_tree(vr.base_sha, head)
    if merged.tree is None:
        files = "\n".join(f"- `{p}`" for p in merged.conflicts)
        return fail(
            vr,
            Check.CONFLICT,
            f"The submission doesn't apply cleanly to main ({vr.base_sha[:12]}); "
            f"it needs a rebase. Conflicts:\n\n{files}",
        )
    merge_sha = repo.commit_tree(
        merged.tree,
        [vr.base_sha, head],
        merge_message(sub),
        Identity(settings.WORKFLOW_GIT_NAME, settings.WORKFLOW_GIT_EMAIL),
        # Fixed per run, so repeating this makes the same commit.
        vr.created_at.isoformat(),
    )
    repo.update_ref(f"{VALIDATION_REFS}/{vr.pk}", merge_sha)

    stage = Path(settings.WORKFLOW_ROOT) / "validation-runs" / str(vr.pk)
    stage.mkdir(parents=True, exist_ok=True)
    repo.archive(merge_sha, stage / "tree.tar")
    shutil.copy(HARNESS, stage / "ci_harness.py")
    # The ticket as it is now: its scripts can change between runs, and
    # oracle_version records which ones a run used.
    ticket = Ticket.objects.get(pk=sub.dev_run.ticket_id)
    script = ticket.conformance_script.replace("\r\n", "\n").encode()
    holdout = build_suite(settings.WORKFLOW_HOLDOUT_SUITES)
    (stage / "conformance").write_bytes(script)
    (stage / "holdout.json").write_bytes(holdout)
    config = {
        **settings.WORKFLOW_CI,
        "work_dir": WORK,
        "tree_tar": f"{CI_DIR}/tree.tar",
        "conformance_script": SCRIPT if script else "",
        "holdout": f"{CI_DIR}/holdout.json",
        "ticket_check": ticket.verify_command,
        "results": RESULTS,
    }
    (stage / "config.json").write_text(json.dumps(config, indent=2))

    def push(name, mode=0o600):
        return Input(
            stage / name, f"{CI_DIR}/{name}", mode=mode, uid=CI_UID, gid=CI_GID
        )

    inputs = [
        push("tree.tar"),
        push("ci_harness.py", 0o700),
        push("config.json"),
        push("holdout.json"),
    ]
    if script:
        inputs.append(push("conformance", 0o700))
    spec = RunSpec(
        name=f"validate submission {sub.pk}",
        source=source_from_json(settings.WORKFLOW_CI_SOURCE),
        profiles=list(settings.WORKFLOW_CI_PROFILES),
        inputs=inputs,
        argv=["python3", f"{CI_DIR}/ci_harness.py", f"{CI_DIR}/config.json"],
        env={"HOME": CI_DIR, "PATH": settings.WORKFLOW_CI["path"]},
        uid=CI_UID,
        gid=CI_GID,
        cwd=CI_DIR,
        timeout_s=settings.WORKFLOW_CI_TIMEOUT_S,
        outputs=[Output(RESULTS, max_bytes=4 * 1024 * 1024, required=True)],
        max_log_bytes=settings.WORKFLOW_CI_MAX_LOG_BYTES,
    )
    with transaction.atomic():
        run_id = runner.create(spec)
        ValidationRun.objects.filter(pk=vr.pk, invocation__isnull=True).update(
            invocation_id=run_id,
            merge_sha=merge_sha,
            oracle_version=sha256_hex(
                HARNESS.read_bytes(),
                script,
                b"\0",
                ticket.verify_command.encode(),
            ),
            holdout_version=sha256_hex(holdout),
        )


def merge_message(sub: Submission) -> str:
    dev_run = sub.dev_run
    return (
        f"Merge submission {sub.pk}: {dev_run.ticket.title}\n\n"
        f"{sub.summary.strip()}\n\n"
        f"Issue: {dev_run.ticket_id}\n"
        f"MR: {sub.pk}\n"
        f"Session: {dev_run.session_id}\n"
    )


def _require_review(sub: Submission, rules: list[str]) -> None:
    reason = ", ".join(
        (["agent_flag"] if sub.review_reason.startswith("agent_flag") else [])
        + [f"gate:{r}" for r in rules]
    )
    Submission.objects.filter(pk=sub.pk).update(
        review_required=True, review_reason=reason
    )
    sub.refresh_from_db()


# -- results --------------------------------------------------------------------


def fail(vr: ValidationRun, check: Check, note: str) -> None:
    "Fail a run before it got to CI."
    _finish(vr, RunStatus.FINISHED, Outcome.FAILED, [check], note, results=None)


def collect(vr: ValidationRun) -> bool:
    "Record how the CI run went, once it has ended. False if it hasn't."
    inv = Invocation.objects.get(pk=vr.invocation_id)
    if inv.is_live:
        return False
    match inv.status:
        case InvocationStatus.CANCELLED:
            _finish(vr, RunStatus.CANCELLED, None, [], None, detail=inv.error)
        case InvocationStatus.TIMEOUT:
            _finish(
                vr,
                RunStatus.FINISHED,
                Outcome.FAILED,
                [Check.TIMEOUT],
                f"Validation run {vr.pk} timed out after {inv.timeout_s}s.",
                detail=inv.error,
            )
        case InvocationStatus.COMPLETED if inv.exit_code == 0:
            try:
                results = json.loads(Path(_results_path(inv)).read_text())
                failed_checks(results)
            except (OSError, ValueError, KeyError, TypeError) as e:
                detail = f"unreadable CI results: {type(e).__name__}: {e}"
                _finish(vr, RunStatus.ERRORED, None, [], None, detail=detail)
                return True
            failed = failed_checks(results)
            outcome = Outcome.FAILED if failed else Outcome.PASSED
            _finish(
                vr,
                RunStatus.FINISHED,
                outcome,
                failed,
                results_note(vr, results, failed),
                results=results,
            )
        case _:
            detail = inv.error or f"harness exited {inv.exit_code}"
            _finish(vr, RunStatus.ERRORED, None, [], None, detail=detail)
    return True


def _results_path(inv: Invocation) -> str:
    [path] = [o["host_path"] for o in inv.outputs if o["guest_path"] == RESULTS]
    return path


def failed_checks(results: dict) -> list[str]:
    if not results["build"]["passed"]:
        return [Check.BUILD]
    phases = [
        (Check.CONFORMANCE, "conformance"),
        (Check.LINT, "lint"),
        (Check.TICKET, "ticket"),
        (Check.HOLDOUT, "holdout"),
    ]
    return [
        check
        for check, key in phases
        if results.get(key) is not None and not results[key]["passed"]
    ]


def _finish(vr, status, outcome, checks, note, *, results=None, detail="") -> None:
    sub = vr.submission
    ticket_id = sub.dev_run.ticket_id
    with transaction.atomic():
        updated = ValidationRun.objects.filter(
            pk=vr.pk, status__in=LIVE_RUN_STATUSES
        ).update(
            status=status,
            outcome=outcome,
            failed_checks=[str(c) for c in checks],
            results=results,
            detail=detail or (note or ""),
            ended_at=timezone.now(),
        )
        if not updated:
            return
        if note is not None:
            add_note(ticket_id, note, NoteSource.VALIDATION_RUN, validation_run=vr)
        elif status == RunStatus.ERRORED:
            add_note(
                ticket_id,
                f"Validation run {vr.pk} hit an infrastructure error ({detail}); "
                "it will be retried.",
                NoteSource.SYSTEM,
                validation_run=vr,
            )
    sub.refresh_from_db()
    if (
        outcome == Outcome.PASSED
        and sub.review_required
        and sub.review_decision is None
    ):
        notify_reviewers(
            f"Review needed: submission {sub.pk} for ticket {ticket_id}",
            f"Submission {sub.pk} passed validation and needs review "
            f"({sub.review_reason}).\n\n{sub.summary}\n",
        )
    refresh_ticket_state(ticket_id)


def results_note(vr: ValidationRun, results: dict, failed: list[str]) -> str:
    verdict = "failed" if failed else "passed"
    lines = [
        f"Validation run {vr.pk} {verdict}, on main {vr.base_sha[:12]}.",
        "",
    ]
    build = results["build"]
    if not build["passed"]:
        lines += ["**Build failed:**", "", _block(build["output"])]
        return "\n".join(lines)
    lines.append("- Build: passed")

    conf = results["conformance"]
    if conf is not None:
        lines.append(f"- Conformance: {'passed' if conf['passed'] else 'failed'}")

    lint = results["lint"]
    for name, check in lint["checks"].items():
        lines.append(f"- Lint `{name}`: {'passed' if check['passed'] else 'failed'}")

    if results.get("ticket") is not None:
        lines.append(
            f"- Ticket check: {'passed' if results['ticket']['passed'] else 'failed'}"
        )

    # Counts only: never cases, inputs or outputs.
    holdout = results["holdout"]
    lines.append(
        f"- Holdout: {holdout['total'] - holdout['failed']}/{holdout['total']} passed"
    )
    failing_areas = {
        area: c for area, c in sorted(holdout["by_area"].items()) if c["failed"]
    }
    for area, c in failing_areas.items():
        lines.append(f"  - {area}: {c['failed']} of {c['total']} failed")

    if conf is not None and not conf["passed"]:
        lines += ["", "**Conformance script output:**", "", _block(conf["output"])]
    for name, check in lint["checks"].items():
        if not check["passed"]:
            lines += ["", f"**`{name}` output:**", "", _block(check["output"])]
    if not failed:
        sub = vr.submission
        lines += [
            "",
            "Waiting on review before it merges."
            if sub.review_required and sub.review_decision is None
            else "It will merge if main hasn't moved; otherwise it'll be revalidated.",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _block(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}\n{text.rstrip()}\n{fence}"
