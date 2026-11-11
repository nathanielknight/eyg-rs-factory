"""What a dev run's agent starts from: a fixed prompt, and the ticket and its
notes as files it can read and grep.

Bump PROMPT_VERSION whenever the prompt's or the files' meaning changes; each
DevRun records the version and the rendered prompt.
"""

from ticket.models import Ticket, TicketNote

PROMPT_VERSION = "dev-3"

TEMPLATE = """\
You are working on an interpreter for EYG, in Rust, in the git repository in
the current directory (a clone of `main` at {base_sha}). You are in a sandboxed
VM with no network access beyond the Anthropic API.

# What you're given

- `{in_dir}/ticket.md`: the ticket: what to do and how to test it.
- `{in_dir}/notes/`: the ticket's notes, oldest first, one file each: from Nat,
  from earlier attempts at this ticket, and CI results for earlier
  submissions. It may be empty. Read them before you start.

# What you hand back

When your session ends, the factory reads these files. Nothing else you leave
in the VM survives, apart from your commits.

- `{out_dir}/NOTES.md`: anything the next attempt should know: findings, dead
  ends, what's left to do. Write it as you go; if you run out of time, what's
  there is kept.
- `{out_dir}/SUBMIT.md`: create this only when the work is done and the tests
  pass. Its existence submits HEAD for validation, and its contents are your
  summary of what changed and why (it goes in the merge commit).
- `{out_dir}/REVIEW`: create this next to SUBMIT.md if a human should look at
  the change before it merges.

If you can't finish, don't create SUBMIT.md; say where you got to in NOTES.md.

# How to work

- Commit your work to the current branch as you go. Only committed work is
  submitted or kept.
- A CI run checks submissions with the ticket's conformance script, a
  held-out test suite you can't see, and lints (clippy, cargo geiger, cargo
  deny). If it fails, its results are in the notes for the next attempt.
- Don't change the test harness or the toolchain files, and don't disable or
  ignore tests; submissions that do are rejected or held for review.
- Changes to Cargo.toml, Cargo.lock or build.rs are held for human review.
  The VM can't reach a package registry, so new dependencies won't build.
"""

TICKET = """\
# Ticket {ticket_id}: {title}

{description}

# Testing

Test your work with:

    {test_command}

Separately, CI runs this conformance script from the root of the repository:

{conformance_script}
"""


def _indent(text: str) -> str:
    "As a markdown code block."
    return "\n".join(f"    {line}" for line in text.strip().splitlines())


def render(base_sha: str, in_dir: str, out_dir: str) -> str:
    return TEMPLATE.format(base_sha=base_sha, in_dir=in_dir, out_dir=out_dir)


def render_ticket(ticket: Ticket) -> str:
    return TICKET.format(
        ticket_id=ticket.pk,
        title=ticket.title,
        description=ticket.description,
        test_command=ticket.test_command,
        conformance_script=_indent(ticket.conformance_script or "(none)"),
    )


def note_files(notes: list[TicketNote]) -> dict[str, str]:
    "File name -> contents; the names sort oldest first."
    return {
        f"{index:03d}-{note.source}.md": (
            f"_{note.created_at:%Y-%m-%d %H:%M} · {note.get_source_display()}_\n\n"
            f"{note.content.rstrip()}\n"
        )
        for index, note in enumerate(notes, start=1)
    }
