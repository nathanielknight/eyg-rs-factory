"""Dev runs: dispatch a ticket to an agent VM, then collect what came of it.

A dev run is a function call: files go in, the agent runs, files come out.
Nothing in the VM talks to the factory.

In: a clone of main, a fixed prompt, and the ticket and its notes as files
under IN. Out, under OUT: the agent's NOTES.md, a SUBMIT.md if it wants its
work submitted (and a REVIEW if a human should look first), and a bundle of
its commits that the driver script makes once the agent stops. The driver
keeps the agent to its budget itself, so a run that overruns still hands its
notes and commits back; the runner's own timeout is only a backstop.

stdout is Claude's stream-json transcript, captured host-side by the runner.
WORKFLOW_DEV_AGENT swaps Claude for another program (run on the prompt's
path), e.g. to exercise the workflow without spending a session.

The Claude token goes into the VM as a file, never into the Invocation row,
and the host copy is deleted once the run ends. Everything that comes back is
untrusted: the bundle is only verified here, and fetched with fsck and gated
by the validate command.
"""

import logging
import shlex
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from runner.models import Invocation, InvocationStatus
from runner.runner import IncusRunner
from runner.types import Input, Output, RunInProgress, RunSpec, source_from_json
from ticket.models import NoteSource, Ticket, TicketState

from . import prompts
from .git import GitError, Repo
from .models import LIVE_RUN_STATUSES, DevRun, Outcome, RunStatus, Submission
from .state import live_dev_runs, refresh_ticket_state, unfinished_tickets
from .support import add_note

log = logging.getLogger(__name__)

HOME = "/home/agent"
WORK = f"{HOME}/work"
AGENT_UID = AGENT_GID = 1000
AGENT_PATH = f"{HOME}/.local/bin:{HOME}/.cargo/bin:/usr/local/bin:/usr/bin:/bin"

IN = f"{HOME}/in"
OUT = f"{HOME}/out"
# Written by the agent.
NOTES = f"{OUT}/NOTES.md"
SUBMIT = f"{OUT}/SUBMIT.md"
REVIEW = f"{OUT}/REVIEW"
# Written by the driver, after the agent stops.
BUNDLE = f"{OUT}/head.bundle"  # base..HEAD; absent if HEAD is still the base
UNCOMMITTED = f"{OUT}/uncommitted.txt"  # git status --porcelain

# How long the host waits past the agent's budget for the driver to package up.
PACKAGING_GRACE_S = 5 * 60
TIMEOUT_EXIT = 124  # timeout(1)'s, when the agent ran out its budget
SMALL_FILE_BYTES = 64 * 1024
NOTE_EXCERPT = 2000

DRIVER = f"""\
#!/bin/sh
set -eu
set -a; . {HOME}/secrets.env; set +a
rm -f {HOME}/secrets.env
mkdir -p {IN} {OUT}
tar -xf {HOME}/in.tar -C {IN}
cd {WORK}
git clone --quiet --branch main {HOME}/repo.bundle .
git remote remove origin
git config user.name "factory agent"
git config user.email agent@factory.invalid

# The agent, within its budget. From here on nothing stops the script: what
# the agent left is packaged however it ended.
set +e
if [ -e {HOME}/agent ]; then
    timeout -k 30 "$FACTORY_BUDGET_S" {HOME}/agent {HOME}/prompt.md
else
    timeout -k 30 "$FACTORY_BUDGET_S" \\
        claude -p "$(cat {HOME}/prompt.md)" \\
        --session-id "$FACTORY_SESSION_ID" \\
        --output-format stream-json --verbose \\
        --dangerously-skip-permissions
fi
rc=$?

mkdir -p {OUT}
cd {WORK}
if [ "$(git rev-parse HEAD)" != "$FACTORY_BASE_SHA" ]; then
    git bundle create --quiet {BUNDLE} "$FACTORY_BASE_SHA..HEAD" >&2
fi
git status --porcelain > {UNCOMMITTED}
exit $rc
"""


def _staging_dir(session_id: uuid.UUID) -> Path:
    return Path(settings.WORKFLOW_ROOT) / "dev-runs" / str(session_id)


def pick_ticket() -> Ticket | None:
    for ticket in unfinished_tickets():
        if refresh_ticket_state(ticket.pk) == TicketState.OPEN:
            return ticket
    return None


def _write_inputs(stage: Path, ticket: Ticket, notes: list) -> None:
    "The agent's IN directory, as a directory (for the record) and a tarball."
    root = stage / "in"
    (root / "notes").mkdir(parents=True)
    (root / "ticket.md").write_text(prompts.render_ticket(ticket))
    for name, text in prompts.note_files(notes).items():
        (root / "notes" / name).write_text(text)
    with tarfile.open(stage / "in.tar", "w") as tar:
        for path in sorted(root.iterdir()):
            tar.add(path, arcname=path.name)


def dispatch(runner: IncusRunner, repo: Repo, ticket: Ticket) -> DevRun:
    "Record a dev run for `ticket` and its pending VM run. Doesn't start it."
    session_id = uuid.uuid4()
    stage = _staging_dir(session_id)
    stage.mkdir(parents=True)
    bundle = stage / "repo.bundle"
    base_sha = repo.bundle_main(bundle)

    notes = list(ticket.ticketnote_set.order_by("pk"))
    prompt = prompts.render(base_sha, IN, OUT)
    _write_inputs(stage, ticket, notes)

    files = {
        "prompt.md": prompt,
        "run-dev.sh": DRIVER,
        "secrets.env": "CLAUDE_CODE_OAUTH_TOKEN="
        f"{shlex.quote(settings.WORKFLOW_CLAUDE_TOKEN)}\n",
    }
    for name, text in files.items():
        (stage / name).write_text(text)
    (stage / "secrets.env").chmod(0o600)
    if settings.WORKFLOW_DEV_AGENT:
        shutil.copy(settings.WORKFLOW_DEV_AGENT, stage / "agent")

    def push(name, guest, mode=0o644):
        return Input(stage / name, guest, mode=mode, uid=AGENT_UID, gid=AGENT_GID)

    agent = (
        [push("agent", f"{HOME}/agent", 0o755)] if settings.WORKFLOW_DEV_AGENT else []
    )

    spec = RunSpec(
        name=f"dev ticket {ticket.pk}",
        source=source_from_json(settings.WORKFLOW_AGENT_SOURCE),
        profiles=list(settings.WORKFLOW_AGENT_PROFILES),
        inputs=[
            push("repo.bundle", f"{HOME}/repo.bundle"),
            push("in.tar", f"{HOME}/in.tar"),
            push("prompt.md", f"{HOME}/prompt.md"),
            push("secrets.env", f"{HOME}/secrets.env", 0o600),
            push("run-dev.sh", f"{HOME}/run-dev.sh", 0o755),
            *agent,
        ],
        argv=[f"{HOME}/run-dev.sh"],
        env={
            "HOME": HOME,
            "PATH": AGENT_PATH,
            "FACTORY_BASE_SHA": base_sha,
            "FACTORY_SESSION_ID": str(session_id),
            "FACTORY_BUDGET_S": str(settings.WORKFLOW_DEV_TIMEOUT_S),
        },
        uid=AGENT_UID,
        gid=AGENT_GID,
        cwd=HOME,
        timeout_s=settings.WORKFLOW_DEV_TIMEOUT_S + PACKAGING_GRACE_S,
        outputs=[
            Output(NOTES, max_bytes=settings.WORKFLOW_MAX_NOTE_BYTES),
            Output(SUBMIT, max_bytes=SMALL_FILE_BYTES),
            Output(REVIEW, max_bytes=SMALL_FILE_BYTES),
            Output(BUNDLE, max_bytes=settings.WORKFLOW_MAX_BUNDLE_BYTES),
            Output(UNCOMMITTED, max_bytes=SMALL_FILE_BYTES),
        ],
        max_log_bytes=settings.WORKFLOW_DEV_MAX_LOG_BYTES,
    )
    # Both rows or neither; a stray staging dir is harmless.
    with transaction.atomic():
        run_id = runner.create(spec)
        dev_run = DevRun.objects.create(
            ticket=ticket,
            invocation_id=run_id,
            base_sha=base_sha,
            notes_through=notes[-1] if notes else None,
            prompt_version=prompts.PROMPT_VERSION,
            prompt=prompt,
            test_command=ticket.test_command,
            session_id=session_id,
        )
    refresh_ticket_state(ticket.pk)
    log.info("dispatched %s", dev_run)
    return dev_run


def drive(runner: IncusRunner, repo: Repo, dev_run: DevRun) -> bool:
    """Run (or resume) the dev run's VM to the end, then collect it.

    False if another worker holds it.
    """
    DevRun.objects.filter(pk=dev_run.pk, status=RunStatus.QUEUED).update(
        status=RunStatus.RUNNING
    )
    try:
        runner.run(dev_run.invocation_id)
    except RunInProgress:
        return False
    collect(repo, dev_run)
    return True


# -- what came back -----------------------------------------------------------


@dataclass
class Returned:
    "What a dev run handed back, read from the files the runner pulled out."

    notes: str = ""  # the agent's NOTES.md
    summary: str | None = None  # SUBMIT.md, if the bundle to go with it is sound
    review: bool = False
    bundle: Path | None = None
    head_sha: str = ""
    problems: list[str] = field(default_factory=list)  # for the ticket's notes


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) > NOTE_EXCERPT:
        text = text[:NOTE_EXCERPT] + "\n..."
    return f"```\n{text}\n```"


def read_returned(repo: Repo, inv: Invocation) -> Returned:
    """Nothing here is trusted. The runner only pulls files out of a run whose
    command exited on its own, so any other run returns nothing."""
    got = Returned()
    records = {o["guest_path"]: o for o in inv.outputs}

    def pulled(guest_path: str) -> Path | None:
        record = records.get(guest_path, {})
        if record.get("host_path"):
            return Path(record["host_path"])
        reason = record.get("missing_reason", "not found")
        if reason != "not found" and guest_path != UNCOMMITTED:
            name = guest_path.removeprefix(f"{OUT}/")
            got.problems.append(f"`{name}` was left behind: {reason}.")
        return None

    def text(guest_path: str) -> str | None:
        path = pulled(guest_path)
        return None if path is None else path.read_text(errors="replace")

    got.notes = (text(NOTES) or "").strip()
    summary = text(SUBMIT)
    bundle = pulled(BUNDLE)
    if summary is None:
        return got

    if bundle is None:
        got.problems.append(
            "It asked to submit, but there was no bundle of commits on top of "
            "its base to submit."
        )
        return got
    try:
        problem = repo.verify_bundle(bundle)
        head = None if problem else repo.bundle_heads(bundle).get("HEAD")
    except GitError as e:
        problem, head = e.stderr, None
    if not head:
        got.problems.append(
            "It asked to submit, but its bundle is no good: "
            f"{problem or 'it has no HEAD'}."
        )
        return got

    got.summary = summary.strip() or "(no summary given)"
    got.review = pulled(REVIEW) is not None
    got.bundle, got.head_sha = bundle, head
    uncommitted = (text(UNCOMMITTED) or "").strip()
    if uncommitted:
        got.problems.append(
            "It submitted with uncommitted changes, which were left out:\n\n"
            + _excerpt(uncommitted)
        )
    return got


def _verdict(inv: Invocation, submitted: bool) -> tuple[str, str | None, str]:
    "(status, outcome, detail) for a dev run whose invocation has ended."
    outcome = Outcome.PASSED if submitted else Outcome.FAILED
    match inv.status:
        case InvocationStatus.CANCELLED:
            return RunStatus.CANCELLED, None, inv.error
        case InvocationStatus.INFRA_ERROR:
            return RunStatus.ERRORED, None, inv.error
        case InvocationStatus.TIMEOUT:
            return RunStatus.FINISHED, outcome, "the VM overran its budget"
        case _ if inv.exit_code == TIMEOUT_EXIT:
            return RunStatus.FINISHED, outcome, "the agent ran out its budget"
        case _:
            return RunStatus.FINISHED, outcome, f"session exited {inv.exit_code}"


def collect(repo: Repo, dev_run: DevRun) -> bool:
    """Record how a dev run ended, once its VM run has: its notes, and its
    submission if it made one. False if it hasn't ended."""
    dev_run.refresh_from_db()
    inv = Invocation.objects.get(pk=dev_run.invocation_id)
    if inv.is_live or not dev_run.is_live:
        return not inv.is_live
    got = read_returned(repo, inv)
    status, outcome, detail = _verdict(inv, submitted=got.summary is not None)
    with transaction.atomic():
        updated = DevRun.objects.filter(
            pk=dev_run.pk, status__in=LIVE_RUN_STATUSES
        ).update(
            status=status,
            outcome=outcome,
            detail=detail,
            ended_at=inv.ended_at or timezone.now(),
            transcript_path=inv.stdout_path,
        )
        if updated:
            if got.notes:
                add_note(
                    dev_run.ticket_id, got.notes, NoteSource.DEV_RUN, dev_run=dev_run
                )
            if got.summary is not None:
                Submission.objects.create(
                    dev_run=dev_run,
                    bundle_path=str(got.bundle),
                    head_sha=got.head_sha,
                    base_sha=dev_run.base_sha,
                    summary=got.summary,
                    review_required=got.review,
                    review_reason="agent_flag" if got.review else "",
                )
            system = [f"Dev run {dev_run.pk}: {p}" for p in got.problems]
            if end := _end_note(dev_run, status, outcome, detail):
                system.append(end)
            for note in system:
                add_note(dev_run.ticket_id, note, NoteSource.SYSTEM, dev_run=dev_run)
    (_staging_dir(dev_run.session_id) / "secrets.env").unlink(missing_ok=True)
    refresh_ticket_state(dev_run.ticket_id)
    return True


def _end_note(dev_run, status, outcome, detail) -> str | None:
    match status, outcome:
        case RunStatus.ERRORED, _:
            return f"Dev run {dev_run.pk} hit an infrastructure error ({detail}); it will be retried."
        case RunStatus.FINISHED, Outcome.FAILED:
            return f"Dev run {dev_run.pk} ended without a submission: {detail}."
    return None


def collect_all(runner: IncusRunner, repo: Repo) -> None:
    """Finish off every live dev run: cancel those whose ticket was cancelled,
    record ended ones, and resume any whose worker died."""
    for dev_run in live_dev_runs():
        if dev_run.ticket.cancelled_at is not None:
            runner.cancel(dev_run.invocation_id)
        if not collect(repo, dev_run):
            drive(runner, repo, dev_run)


def dispatch_next(runner: IncusRunner, repo: Repo) -> DevRun | None:
    "Dispatch and drive one dev run, if there's room and a ticket for it."
    if live_dev_runs().count() >= settings.WORKFLOW_MAX_LIVE_DEV_RUNS:
        return None
    ticket = pick_ticket()
    if ticket is None:
        return None
    dev_run = dispatch(runner, repo, ticket)
    drive(runner, repo, dev_run)
    dev_run.refresh_from_db()
    return dev_run
