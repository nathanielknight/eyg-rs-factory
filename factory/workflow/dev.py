"""Dev runs: dispatch a ticket to an agent VM, then collect what came of it.

A dev run's VM gets a clone of main, the rendered prompt, the `factory` CLI
and a per-run token for the agent bridge. Its stdout is Claude's stream-json
transcript, captured host-side by the runner. WORKFLOW_DEV_AGENT swaps
Claude for another program (run on the prompt's path), e.g. to exercise the
workflow without spending a session.

Secrets (the run token, the Claude token) go into the VM as a file, never
into the Invocation row, and the host copy is deleted once the run ends.
"""

import hashlib
import logging
import secrets
import shlex
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from runner.models import Invocation, InvocationStatus
from runner.runner import IncusRunner
from runner.types import Input, RunInProgress, RunSpec, source_from_json
from ticket.models import NoteSource, Ticket, TicketState

from . import prompts
from .git import Repo
from .models import LIVE_RUN_STATUSES, DevRun, Outcome, RunStatus, Submission
from .state import live_dev_runs, refresh_ticket_state, unfinished_tickets
from .support import add_note

log = logging.getLogger(__name__)

GUEST = Path(__file__).parent / "guest"
HOME = "/home/agent"
WORK = f"{HOME}/work"
AGENT_UID = AGENT_GID = 1000
AGENT_PATH = f"{HOME}/.local/bin:{HOME}/.cargo/bin:/usr/local/bin:/usr/bin:/bin"

DRIVER = f"""\
#!/bin/sh
set -eu
set -a; . {HOME}/secrets.env; set +a
rm -f {HOME}/secrets.env
cd {WORK}
git clone --quiet --branch main {HOME}/repo.bundle .
git remote remove origin
git config user.name "factory agent"
git config user.email agent@factory.invalid
if [ -e {HOME}/agent ]; then exec {HOME}/agent {HOME}/prompt.md; fi
exec claude -p "$(cat {HOME}/prompt.md)" \\
    --session-id "$FACTORY_SESSION_ID" \\
    --output-format stream-json --verbose \\
    --dangerously-skip-permissions
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _staging_dir(session_id: uuid.UUID) -> Path:
    return Path(settings.WORKFLOW_ROOT) / "dev-runs" / str(session_id)


def pick_ticket() -> Ticket | None:
    for ticket in unfinished_tickets():
        if refresh_ticket_state(ticket.pk) == TicketState.OPEN:
            return ticket
    return None


def dispatch(runner: IncusRunner, repo: Repo, ticket: Ticket) -> DevRun:
    "Record a dev run for `ticket` and its pending VM run. Doesn't start it."
    session_id = uuid.uuid4()
    stage = _staging_dir(session_id)
    stage.mkdir(parents=True)
    bundle = stage / "repo.bundle"
    base_sha = repo.bundle_main(bundle)

    notes = list(ticket.ticketnote_set.order_by("pk"))
    prompt = prompts.render(ticket, notes, base_sha)
    token = secrets.token_urlsafe(32)

    files = {
        "prompt.md": prompt,
        "run-dev.sh": DRIVER,
        "secrets.env": "".join(
            f"{k}={shlex.quote(v)}\n"
            for k, v in {
                "CLAUDE_CODE_OAUTH_TOKEN": settings.WORKFLOW_CLAUDE_TOKEN,
                "FACTORY_TOKEN": token,
            }.items()
        ),
    }
    for name, text in files.items():
        (stage / name).write_text(text)
    (stage / "secrets.env").chmod(0o600)
    shutil.copy(GUEST / "factory_cli.py", stage / "factory")
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
            push("prompt.md", f"{HOME}/prompt.md"),
            push("secrets.env", f"{HOME}/secrets.env", 0o600),
            push("run-dev.sh", f"{HOME}/run-dev.sh", 0o755),
            push("factory", f"{HOME}/.local/bin/factory", 0o755),
            *agent,
        ],
        argv=[f"{HOME}/run-dev.sh"],
        env={
            "HOME": HOME,
            "PATH": AGENT_PATH,
            "FACTORY_URL": settings.WORKFLOW_AGENT_API_URL,
            "FACTORY_TICKET": str(ticket.pk),
            "FACTORY_BASE_SHA": base_sha,
            "FACTORY_SESSION_ID": str(session_id),
        },
        uid=AGENT_UID,
        gid=AGENT_GID,
        cwd=WORK,
        timeout_s=settings.WORKFLOW_DEV_TIMEOUT_S,
        outputs=[],
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
            token_hash=token_hash(token),
        )
    refresh_ticket_state(ticket.pk)
    log.info("dispatched %s", dev_run)
    return dev_run


def drive(runner: IncusRunner, dev_run: DevRun) -> bool:
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
    collect(dev_run)
    return True


def _verdict(dev_run: DevRun, inv: Invocation) -> tuple[str, str | None, str]:
    "(status, outcome, detail) for a dev run whose invocation has ended."
    converged = Submission.objects.filter(dev_run=dev_run).exists()
    outcome = Outcome.PASSED if converged else Outcome.FAILED
    match inv.status:
        case InvocationStatus.CANCELLED:
            return RunStatus.CANCELLED, None, inv.error
        case InvocationStatus.INFRA_ERROR:
            return RunStatus.ERRORED, None, inv.error
        case InvocationStatus.TIMEOUT:
            return RunStatus.FINISHED, outcome, "hit its wall-clock budget"
        case _:
            return RunStatus.FINISHED, outcome, f"session exited {inv.exit_code}"


def collect(dev_run: DevRun) -> bool:
    "Record how a dev run ended, once its VM run has. False if it hasn't."
    dev_run.refresh_from_db()
    inv = Invocation.objects.get(pk=dev_run.invocation_id)
    if inv.is_live or not dev_run.is_live:
        return not inv.is_live
    status, outcome, detail = _verdict(dev_run, inv)
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
            note = _end_note(dev_run, status, outcome, detail)
            if note:
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


def collect_all(runner: IncusRunner) -> None:
    """Finish off every live dev run: cancel those whose ticket was cancelled,
    record ended ones, and resume any whose worker died."""
    for dev_run in live_dev_runs():
        if dev_run.ticket.cancelled_at is not None:
            runner.cancel(dev_run.invocation_id)
        if not collect(dev_run):
            drive(runner, dev_run)


def dispatch_next(runner: IncusRunner, repo: Repo) -> DevRun | None:
    "Dispatch and drive one dev run, if there's room and a ticket for it."
    if live_dev_runs().count() >= settings.WORKFLOW_MAX_LIVE_DEV_RUNS:
        return None
    ticket = pick_ticket()
    if ticket is None:
        return None
    dev_run = dispatch(runner, repo, ticket)
    drive(runner, dev_run)
    dev_run.refresh_from_db()
    return dev_run
