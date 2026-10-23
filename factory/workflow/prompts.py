"""The instructions a dev run's agent starts from.

Bump PROMPT_VERSION whenever the template's meaning changes; each DevRun
records the version and the rendered text.
"""

from ticket.models import Ticket, TicketNote

PROMPT_VERSION = "dev-2"

TEMPLATE = """\
You are working on an interpreter for EYG, in Rust, in the git repository in
the current directory (a clone of `main` at {base_sha}). You are in a sandboxed
VM with no network access beyond the Anthropic API and the factory.

# Ticket {ticket_id}: {title}

{description}

# Testing

Test your work with:

    {test_command}

Separately, a CI run checks submissions with this ticket's conformance
script, run from the root of the repository:

{conformance_script}

as well as a held-out test suite you can't see and lints (clippy, cargo
geiger, cargo deny). The conformance script's output, if it fails, will be
in the ticket's notes.

# Notes on this ticket

{notes}

# How to work

- Commit your work to the current branch as you go.
- The `factory` command talks to the factory:
  - `factory note TEXT` adds a note to the ticket. Use it for anything the
    next attempt should know: findings, dead ends, what's left to do.
  - `factory submit --summary TEXT` submits HEAD for validation, once. Add
    `--review` if a human should look at it before it merges. Leave any
    notes first: once you've submitted, you can't add more.
- Submit only when the tests pass and the work is done. If you can't finish,
  leave a note saying where you got to instead of submitting.
- Don't change the test harness or the toolchain files, and don't disable or
  ignore tests; submissions that do are rejected or held for review.
- Changes to Cargo.toml, Cargo.lock or build.rs are held for human review.
  The VM can't reach a package registry, so new dependencies won't build.
"""


def _note(note: TicketNote) -> str:
    return f"## {note.created_at:%Y-%m-%d %H:%M} ({note.get_source_display()})\n\n{note.content}"


def _indent(text: str) -> str:
    "As a markdown code block."
    return "\n".join(f"    {line}" for line in text.strip().splitlines())


def render(ticket: Ticket, notes: list[TicketNote], base_sha: str) -> str:
    return TEMPLATE.format(
        base_sha=base_sha,
        ticket_id=ticket.pk,
        title=ticket.title,
        description=ticket.description,
        test_command=ticket.test_command,
        conformance_script=_indent(ticket.conformance_script or "(none)"),
        notes="\n\n".join(_note(n) for n in notes) if notes else "(none yet)",
    )
