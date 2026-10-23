"Small shared pieces: notes, suites, locks and notifications."

import fcntl
import hashlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail

from ticket.models import NoteSource, TicketNote

log = logging.getLogger(__name__)


def add_note(ticket_id: int, content: str, source: NoteSource, **runs) -> TicketNote:
    return TicketNote.objects.create(
        ticket_id=ticket_id, content=content, source=source, **runs
    )


def sha256_hex(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


# -- suites -----------------------------------------------------------------


def _suite_files(path: Path) -> list[Path]:
    return sorted(path.rglob("*.json")) if path.is_dir() else [path]


def build_suite(paths: list[Path]) -> bytes:
    """One JSON list of every case in `paths`, each with an id and an area.

    The bytes are canonical, so their hash is the suite's version.
    """
    cases = []
    for path in paths:
        for file in _suite_files(path):
            for index, case in enumerate(json.loads(file.read_text())):
                cases.append(
                    {
                        **case,
                        "id": f"{file.stem}/{index}",
                        "area": case.get("area") or file.stem,
                    }
                )
    return json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()


# -- locks ------------------------------------------------------------------


class LockBusy(Exception):
    pass


@contextmanager
def batch_lock(name: str) -> Iterator[None]:
    """One process at a time per batch job; freed if the process dies.

    The runner's claims already stop two workers driving one run; this stops
    two dispatchers each starting a new one.
    """
    locks = Path(settings.WORKFLOW_ROOT) / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    fd = os.open(locks / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(name) from None
        yield
    finally:
        os.close(fd)


# -- notifications ----------------------------------------------------------


def notify_reviewers(subject: str, body: str) -> None:
    if not settings.WORKFLOW_REVIEWERS:
        log.warning("no WORKFLOW_REVIEWERS to notify: %s", subject)
        return
    try:
        send_mail(subject, body, None, settings.WORKFLOW_REVIEWERS)
    except Exception:
        # The submission still shows as needing review in the admin.
        log.exception("notifying reviewers failed: %s", subject)
