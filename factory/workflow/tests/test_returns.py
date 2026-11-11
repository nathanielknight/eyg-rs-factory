"What a dev run hands back: the files in its OUT directory, and the driver."

import os
import shutil
import subprocess
from pathlib import Path

from runner.tests.fake import Command
from ticket.models import NoteSource, TicketNote, TicketState
from workflow import dev
from workflow.models import Outcome, RunStatus, Submission
from workflow.state import refresh_ticket_state

from .helpers import WorkflowTestCase, git


def notes(ticket_id, source):
    qs = TicketNote.objects.filter(ticket_id=ticket_id, source=source).order_by("pk")
    return [n.content for n in qs]


class ReturnedTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.dev_run = self.dispatch()
        self.bundle, self.head = self.agent_change(
            self.dev_run.base_sha, {"src/let.rs": "// let\n"}
        )
        self.ticket_id = self.dev_run.ticket_id

    def test_submit_file_and_bundle_make_a_pending_submission(self):
        self.returns(submit="Added let.\n", bundle=self.bundle)
        dev_run = self.drive(self.dev_run)
        self.assertEqual(
            (dev_run.status, dev_run.outcome), (RunStatus.FINISHED, Outcome.PASSED)
        )
        sub = Submission.objects.get()
        self.assertEqual(sub.dev_run, dev_run)
        # The head is read from the bundle, not claimed by the agent.
        self.assertEqual(sub.head_sha, self.head)
        self.assertEqual(sub.base_sha, dev_run.base_sha)
        self.assertEqual(sub.summary, "Added let.")
        self.assertFalse(sub.review_required)
        self.assertFalse(sub.validation_runs.exists())
        # Not fetched: the validate command does that.
        self.assertIsNone(self.repo.rev_parse(f"refs/factory/submissions/{sub.pk}"))
        self.assertEqual(notes(self.ticket_id, NoteSource.SYSTEM), [])
        self.assertEqual(refresh_ticket_state(self.ticket_id), TicketState.SUBMITTED)

    def test_review_file_holds_the_submission_for_review(self):
        self.returns(submit="Added let.", bundle=self.bundle, review="")
        self.drive(self.dev_run)
        sub = Submission.objects.get()
        self.assertEqual((sub.review_required, sub.review_reason), (True, "agent_flag"))

    def test_notes_file_becomes_the_runs_note(self):
        self.returns(notes="Found the bug in env.rs\n", bundle=self.bundle)
        dev_run = self.drive(self.dev_run)
        note = TicketNote.objects.get(source=NoteSource.DEV_RUN)
        self.assertEqual(
            (note.content, note.dev_run), ("Found the bug in env.rs", dev_run)
        )
        # No SUBMIT.md, so the commits it left aren't a submission.
        self.assertEqual(dev_run.outcome, Outcome.FAILED)
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(refresh_ticket_state(self.ticket_id), TicketState.OPEN)

    def test_notes_reach_the_next_attempt(self):
        self.returns(notes="Start with the parser.")
        self.drive(self.dev_run)
        again = self.dispatch(self.dev_run.ticket)
        given = dev._staging_dir(again.session_id) / "in" / "notes"
        names = sorted(p.name for p in given.iterdir())
        self.assertEqual(names, ["001-dev_run.md", "002-system.md"])
        self.assertIn("Start with the parser.", (given / names[0]).read_text())

    def test_submit_without_commits_is_not_a_submission(self):
        self.returns(submit="Done!")
        dev_run = self.drive(self.dev_run)
        self.assertEqual(dev_run.outcome, Outcome.FAILED)
        self.assertFalse(Submission.objects.exists())
        problem, ended = notes(self.ticket_id, NoteSource.SYSTEM)
        self.assertIn("no bundle of commits", problem)
        self.assertIn("without a submission", ended)

    def test_bad_bundle_is_not_a_submission(self):
        self.returns(submit="Done!", bundle=b"not a bundle")
        dev_run = self.drive(self.dev_run)
        self.assertEqual(dev_run.outcome, Outcome.FAILED)
        self.assertFalse(Submission.objects.exists())
        self.assertIn(
            "its bundle is no good", notes(self.ticket_id, NoteSource.SYSTEM)[0]
        )

    def test_oversize_files_are_left_behind_and_said_so(self):
        with self.settings(WORKFLOW_MAX_BUNDLE_BYTES=10, WORKFLOW_MAX_NOTE_BYTES=10):
            dev_run = self.dispatch(self.make_ticket(title="other"))
        self.returns(submit="Done!", bundle=self.bundle, notes="x" * 11)
        self.drive(dev_run)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(TicketNote.objects.filter(source=NoteSource.DEV_RUN).exists())
        system = "\n".join(notes(dev_run.ticket_id, NoteSource.SYSTEM))
        self.assertIn("`NOTES.md` was left behind", system)
        self.assertIn("`head.bundle` was left behind", system)

    def test_uncommitted_changes_are_noted_on_a_submission(self):
        self.returns(submit="Done!", bundle=self.bundle, uncommitted=" M src/main.rs\n")
        self.drive(self.dev_run)
        self.assertTrue(Submission.objects.exists())
        [note] = notes(self.ticket_id, NoteSource.SYSTEM)
        self.assertIn("uncommitted changes", note)
        self.assertIn("M src/main.rs", note)

    def test_empty_summary_still_submits(self):
        self.returns(submit="\n", bundle=self.bundle)
        self.drive(self.dev_run)
        self.assertEqual(Submission.objects.get().summary, "(no summary given)")

    def test_running_out_the_budget_keeps_notes_and_says_so(self):
        self.incus.command = Command(
            exit_code=dev.TIMEOUT_EXIT, files={dev.NOTES: b"Half way through."}
        )
        dev_run = self.drive(self.dev_run)
        self.assertEqual(
            (dev_run.status, dev_run.outcome), (RunStatus.FINISHED, Outcome.FAILED)
        )
        self.assertEqual(
            notes(self.ticket_id, NoteSource.DEV_RUN), ["Half way through."]
        )
        self.assertIn("ran out its budget", notes(self.ticket_id, NoteSource.SYSTEM)[0])

    def test_host_timeout_is_longer_than_the_agents_budget(self):
        inv = self.dev_run.invocation
        self.assertEqual(inv.env["FACTORY_BUDGET_S"], "7200")
        self.assertGreater(inv.timeout_s, 7200)

    def test_a_dead_vm_returns_nothing(self):
        self.incus.command = Command(
            vm_dies=True, files={dev.SUBMIT: b"Done!", dev.NOTES: b"n"}
        )
        dev_run = self.drive(self.dev_run)
        self.assertEqual(dev_run.status, RunStatus.ERRORED)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(TicketNote.objects.filter(source=NoteSource.DEV_RUN).exists())

    def test_collecting_twice_submits_once(self):
        self.returns(submit="Added let.", bundle=self.bundle, notes="n")
        self.drive(self.dev_run)
        dev.collect(self.repo, self.dev_run)
        dev.collect_all(self.runner, self.repo)
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(TicketNote.objects.count(), 1)


class DriverTests(WorkflowTestCase):
    "The driver script itself, run for real with HOME moved to a temp dir."

    def run_driver(self, agent: str, budget: str | None = None):
        with self.settings(WORKFLOW_CLAUDE_TOKEN="sk-claude-secret"):
            dev_run = self.dispatch()
        inv = dev_run.invocation
        home = self.tmp / "home"
        (home / "work").mkdir(parents=True)
        for i in inv.inputs:
            dest = home / Path(i["guest_path"]).relative_to(dev.HOME)
            shutil.copy(i["host_path"], dest)
        (home / "agent").write_text(agent.replace(dev.HOME, str(home)))
        (home / "agent").chmod(0o755)
        script = home / "run-dev.sh"
        script.write_text(script.read_text().replace(dev.HOME, str(home)))
        env = {**os.environ, **inv.env, "HOME": str(home)}
        if budget:
            env["FACTORY_BUDGET_S"] = budget
        result = subprocess.run(["sh", str(script)], env=env, capture_output=True)
        return dev_run, home, result

    def hand_back(self, dev_run, home):
        "Feed what the driver left in OUT through the runner, as the VM would."
        files = {
            f"{dev.OUT}/{p.name}": p.read_bytes() for p in (home / "out").iterdir()
        }
        self.incus.command = Command(exit_code=0, files=files)
        return self.drive(dev_run)

    def test_packages_the_agents_commits_and_files(self):
        dev_run, home, result = self.run_driver(
            "#!/bin/sh\nset -e\n"
            'test "$CLAUDE_CODE_OAUTH_TOKEN" = sk-claude-secret\n'
            f"test ! -e {dev.HOME}/secrets.env\n"
            f"grep -q 'Implement let' {dev.IN}/ticket.md\n"
            f"test -d {dev.IN}/notes\n"
            f'grep -q "$FACTORY_BASE_SHA" "$1"\n'
            "echo '// let' > src/let.rs\n"
            "git add -A && git commit --quiet -m let\n"
            "echo scratch > scratch.txt\n"
            f"echo 'Added let.' > {dev.OUT}/SUBMIT.md\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")  # stdout is the agent's alone
        self.assertEqual(
            sorted(p.name for p in (home / "out").iterdir()),
            ["SUBMIT.md", "head.bundle", "uncommitted.txt"],
        )
        dev_run = self.hand_back(dev_run, home)
        sub = dev_run.submission
        self.assertEqual(sub.head_sha, git(home / "work", "rev-parse", "HEAD"))
        self.assertEqual(sub.summary, "Added let.")
        [note] = notes(dev_run.ticket_id, NoteSource.SYSTEM)
        self.assertIn("scratch.txt", note)

    def test_no_commits_means_no_bundle(self):
        dev_run, home, result = self.run_driver(
            f"#!/bin/sh\necho stuck > {dev.OUT}/NOTES.md\nexit 3\n"
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            sorted(p.name for p in (home / "out").iterdir()),
            ["NOTES.md", "uncommitted.txt"],
        )

    def test_agent_over_budget_is_stopped_and_still_packaged(self):
        dev_run, home, result = self.run_driver(
            f"#!/bin/sh\necho so far > {dev.OUT}/NOTES.md\n"
            "echo x > x.rs && git add -A && git commit --quiet -m wip\n"
            "sleep 60\n",
            budget="2",
        )
        self.assertEqual(result.returncode, dev.TIMEOUT_EXIT)
        self.assertTrue((home / "out" / "head.bundle").exists())
        self.assertEqual((home / "out" / "NOTES.md").read_text(), "so far\n")
