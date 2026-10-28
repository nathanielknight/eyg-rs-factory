import json
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import ProtectedError

from runner.models import Invocation
from runner.tests.fake import Command
from ticket.models import NoteSource, Ticket, TicketNote, TicketState
from workflow import dev, merging, validation
from workflow.git import VALIDATION_REFS
from workflow.models import (
    Check,
    DevRun,
    Merge,
    Outcome,
    ReviewDecision,
    RunStatus,
    Submission,
)
from workflow.state import refresh_ticket_state

from .helpers import WorkflowTestCase, git, passing_results


def notes(ticket_id, source=None):
    qs = TicketNote.objects.filter(ticket_id=ticket_id).order_by("pk")
    if source:
        qs = qs.filter(source=source)
    return [n.content for n in qs]


def state_of(ticket):
    return refresh_ticket_state(ticket.pk)


class DispatchTests(WorkflowTestCase):
    def test_records_what_the_agent_was_given(self):
        ticket = self.make_ticket()
        TicketNote.objects.create(ticket=ticket, content="Try the env first.")
        last = TicketNote.objects.create(
            ticket=ticket, content="Watch out for shadowing."
        )
        dev_run, _ = self.dispatch(ticket)

        self.assertEqual(dev_run.base_sha, self.repo.main_sha())
        self.assertEqual(dev_run.notes_through, last)
        self.assertIn("Watch out for shadowing.", dev_run.prompt)
        self.assertIn(ticket.description, dev_run.prompt)
        self.assertEqual(dev_run.test_command, "cargo test")
        self.assertEqual(dev_run.status, RunStatus.QUEUED)
        self.assertEqual(state_of(ticket), TicketState.DEVELOPING)

    def test_secrets_stay_out_of_the_database(self):
        with self.settings(WORKFLOW_CLAUDE_TOKEN="sk-claude-secret"):
            dev_run, token = self.dispatch()
        inv = Invocation.objects.get(pk=dev_run.invocation_id)
        row = json.dumps([inv.env, inv.argv, inv.inputs])
        self.assertNotIn(token, row)
        self.assertNotIn("sk-claude-secret", row)
        self.assertNotEqual(dev_run.token_hash, token)

    def test_runs_a_configured_agent_in_place_of_claude(self):
        script = self.tmp / "fake-agent.sh"
        script.write_text("#!/bin/sh\nfactory submit --summary fake\n")
        with self.settings(WORKFLOW_DEV_AGENT=str(script)):
            dev_run, _ = self.dispatch()
        inv = Invocation.objects.get(pk=dev_run.invocation_id)
        [agent] = [i for i in inv.inputs if i["guest_path"] == "/home/agent/agent"]
        self.assertEqual(Path(agent["host_path"]).read_text(), script.read_text())

    def test_runs_claude_by_default(self):
        dev_run, _ = self.dispatch()
        inv = Invocation.objects.get(pk=dev_run.invocation_id)
        guest_paths = [i["guest_path"] for i in inv.inputs]
        self.assertNotIn("/home/agent/agent", guest_paths)

    def test_secrets_file_is_deleted_once_collected(self):
        dev_run, _ = self.dispatch()
        secrets = dev._staging_dir(dev_run.session_id) / "secrets.env"
        self.assertTrue(secrets.exists())
        dev.drive(self.runner, dev_run)
        self.assertFalse(secrets.exists())

    def test_run_without_submission_has_not_converged(self):
        dev_run, _ = self.dispatch()
        self.incus.command = Command(exit_code=0)
        dev.drive(self.runner, dev_run)
        dev_run.refresh_from_db()
        self.assertEqual(
            (dev_run.status, dev_run.outcome), (RunStatus.FINISHED, Outcome.FAILED)
        )
        self.assertEqual(dev_run.transcript_path, dev_run.invocation.stdout_path)
        [note] = notes(dev_run.ticket_id, NoteSource.SYSTEM)
        self.assertIn("without a submission", note)
        self.assertEqual(state_of(dev_run.ticket), TicketState.OPEN)

    def test_infra_error_is_errored_not_failed(self):
        dev_run, _ = self.dispatch()
        self.incus.command = Command(vm_dies=True)
        dev.drive(self.runner, dev_run)
        dev_run.refresh_from_db()
        self.assertEqual(dev_run.status, RunStatus.ERRORED)
        self.assertIsNone(dev_run.outcome)

    def test_collect_is_idempotent(self):
        dev_run, _ = self.dispatch()
        dev.drive(self.runner, dev_run)
        dev.collect(dev_run)
        dev.collect_all(self.runner)
        self.assertEqual(len(notes(dev_run.ticket_id, NoteSource.SYSTEM)), 1)

    def test_dispatch_command_picks_oldest_open_ticket(self):
        first = self.make_ticket(title="first")
        self.make_ticket(title="second")
        out = StringIO()
        with (
            self.settings(WORKFLOW_CLAUDE_TOKEN="token"),
            mock.patch(
                "workflow.management.commands.dispatch.get_runner",
                return_value=self.runner,
            ),
        ):
            call_command("dispatch", stdout=out)
        self.assertEqual(DevRun.objects.get().ticket, first)

    def test_cancelled_ticket_stops_its_live_run(self):
        dev_run, _ = self.dispatch()
        dev_run.ticket.cancelled_at = self.clock.now()
        dev_run.ticket.save()
        dev.collect_all(self.runner)
        dev_run.refresh_from_db()
        self.assertEqual(dev_run.status, RunStatus.CANCELLED)
        self.assertEqual(state_of(dev_run.ticket), TicketState.CANCELLED)


class RetryBudgetTests(WorkflowTestCase):
    def finish(self, ticket, **command):
        dev_run, _ = self.dispatch(ticket)
        self.incus.command = Command(**command)
        dev.drive(self.runner, dev_run)

    def test_stalls_after_max_attempts_until_a_human_note(self):
        ticket = self.make_ticket()
        with self.settings(WORKFLOW_MAX_ATTEMPTS=2):
            self.finish(ticket)
            self.assertEqual(state_of(ticket), TicketState.OPEN)
            self.finish(ticket)
            self.assertEqual(state_of(ticket), TicketState.STALLED)
            self.assertIsNone(dev.pick_ticket())

            self.operator.post(
                f"/tickets/{ticket.pk}/notes/", "Try X.", content_type="text/markdown"
            )
            self.assertEqual(state_of(ticket), TicketState.OPEN)

    def test_repeated_infra_errors_need_a_human(self):
        ticket = self.make_ticket()
        with self.settings(WORKFLOW_MAX_INFRA_ERRORS=2):
            self.finish(ticket, vm_dies=True)
            self.assertEqual(state_of(ticket), TicketState.OPEN)
            self.finish(ticket, vm_dies=True)
            self.assertEqual(state_of(ticket), TicketState.ERROR)


class ValidationTests(WorkflowTestCase):
    def test_passing_submission_merges_with_trailers(self):
        dev_run, sub = self.submitted_run()
        self.assertEqual(
            (dev_run.status, dev_run.outcome), (RunStatus.FINISHED, Outcome.PASSED)
        )
        self.assertEqual(state_of(dev_run.ticket), TicketState.SUBMITTED)
        base = self.repo.main_sha()

        self.ci_results(passing_results())
        [pending] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, pending)
        self.assertEqual((vr.status, vr.outcome), (RunStatus.FINISHED, Outcome.PASSED))
        self.assertEqual(vr.base_sha, base)
        self.assertEqual(vr.image_fingerprint, "abc123")
        self.assertTrue(vr.oracle_version and vr.holdout_version)
        self.assertEqual(state_of(dev_run.ticket), TicketState.VALIDATED)

        [ready] = merging.mergeable()
        record = merging.merge(self.repo, ready)
        self.assertEqual(self.repo.main_sha(), vr.merge_sha)
        self.assertEqual((record.parent_sha, record.result_sha), (base, vr.merge_sha))
        message = git(self.repo.path, "log", "-1", "--format=%B", "main")
        self.assertIn(f"Issue: {dev_run.ticket_id}\n", message)
        self.assertIn(f"MR: {sub.pk}\n", message)
        self.assertIn(f"Session: {dev_run.session_id}", message)
        self.assertEqual(git(self.repo.path, "rev-parse", "main^2"), sub.head_sha)
        self.assertEqual(state_of(dev_run.ticket), TicketState.DONE)
        self.assertEqual(validation.to_validate(self.repo), [])
        self.assertEqual(merging.mergeable(), [])

    def test_ci_gets_the_merge_commit_and_the_tickets_scripts(self):
        dev_run, _ = self.submitted_run()
        Ticket.objects.filter(pk=dev_run.ticket_id).update(
            conformance_script="#!/bin/sh\nscripts/verify.py let\n",
            verify_command="cargo test --test let_hidden",
        )
        self.ci_results(passing_results())
        [sub] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, sub)
        inv = vr.invocation
        guest = {i["guest_path"]: i for i in inv.inputs}
        script = guest[validation.SCRIPT]
        self.assertEqual(script["mode"], 0o700)
        self.assertEqual(
            Path(script["host_path"]).read_text(),
            "#!/bin/sh\nscripts/verify.py let\n",
        )
        config = json.loads(
            Path(guest["/home/agent/config.json"]["host_path"]).read_text()
        )
        self.assertEqual(config["conformance_script"], validation.SCRIPT)
        self.assertEqual(config["ticket_check"], "cargo test --test let_hidden")
        self.assertEqual({i["uid"] for i in inv.inputs} | {inv.uid}, {1000})

        # The ticket's scripts are part of the oracle version.
        Ticket.objects.filter(pk=dev_run.ticket_id).update(conformance_script="")
        self.advance_main({"README": "moved\n"})
        [sub] = validation.to_validate(self.repo)
        again = validation.validate(self.runner, self.repo, sub)
        self.assertNotIn(
            validation.SCRIPT, [i["guest_path"] for i in again.invocation.inputs]
        )
        self.assertNotEqual(again.oracle_version, vr.oracle_version)
        self.assertEqual(inv.profiles[-1], "eyg-ci")
        self.assertEqual(
            self.repo.rev_parse(f"{VALIDATION_REFS}/{vr.pk}"), vr.merge_sha
        )

    def test_failure_notes_show_conformance_but_only_holdout_counts(self):
        dev_run, _ = self.submitted_run()
        self.ci_results(
            passing_results(
                conformance={
                    "passed": False,
                    "exit_code": 1,
                    "output": "let/3: stdout mismatch",
                },
                holdout={
                    "passed": False,
                    "total": 4,
                    "failed": 2,
                    "by_area": {
                        "builtins": {"total": 3, "failed": 2},
                        "effects": {"total": 1, "failed": 0},
                    },
                },
            )
        )
        [sub] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.outcome, Outcome.FAILED)
        self.assertEqual(vr.failed_checks, [Check.CONFORMANCE, Check.HOLDOUT])
        [note] = TicketNote.objects.filter(source=NoteSource.VALIDATION_RUN)
        self.assertEqual(note.validation_run, vr)
        self.assertIn("let/3: stdout mismatch", note.content)
        self.assertIn("Holdout: 2/4 passed", note.content)
        self.assertIn("builtins: 2 of 3 failed", note.content)
        self.assertNotIn("hidden", note.content)
        self.assertNotIn("effects", note.content)
        self.assertEqual(state_of(dev_run.ticket), TicketState.OPEN)

    def test_protected_path_rejects_at_the_gate(self):
        self.submitted_run(files={"rust-toolchain.toml": "[toolchain]\n"})
        [sub] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.failed_checks, [Check.GATE])
        self.assertIsNone(vr.invocation)
        self.assertIn("rust-toolchain.toml", notes(sub.dev_run.ticket_id)[-1])

    def test_cargo_changes_force_review_and_notify(self):
        dev_run, _ = self.submitted_run(files={"Cargo.toml": "[package]\nname = 'x'\n"})
        self.ci_results(passing_results())
        [sub] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, sub)
        sub.refresh_from_db()
        self.assertTrue(sub.review_required)
        self.assertEqual(sub.review_reason, "gate:Cargo.toml")
        self.assertEqual(vr.outcome, Outcome.PASSED)
        self.assertEqual(state_of(dev_run.ticket), TicketState.NEEDS_REVIEW)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(merging.mergeable(), [])

        sub.review_decision = ReviewDecision.APPROVED
        sub.save()
        [ready] = merging.mergeable()
        self.assertIsNotNone(merging.merge(self.repo, ready))

    def test_ignored_tests_force_review(self):
        self.submitted_run(files={"src/lib.rs": "#[ignore]\n#[test]\nfn t() {}\n"})
        self.ci_results(passing_results())
        [sub] = validation.to_validate(self.repo)
        validation.validate(self.runner, self.repo, sub)
        sub.refresh_from_db()
        self.assertEqual(sub.review_reason, "gate:ignore")

    def test_agent_review_flag_holds_the_merge(self):
        dev_run, sub = self.submitted_run(review="true")
        self.assertEqual(sub.review_reason, "agent_flag")
        self.ci_results(passing_results())
        validation.validate(self.runner, self.repo, sub)
        self.assertEqual(state_of(dev_run.ticket), TicketState.NEEDS_REVIEW)
        sub.review_decision = ReviewDecision.REJECTED
        sub.save()
        self.assertEqual(state_of(dev_run.ticket), TicketState.OPEN)

    def test_conflict_with_main_needs_rebase(self):
        dev_run, sub = self.submitted_run(files={"src/main.rs": "fn main() { a }\n"})
        self.advance_main({"src/main.rs": "fn main() { b }\n"})
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.failed_checks, [Check.CONFLICT])
        self.assertIn("needs a rebase", notes(dev_run.ticket_id)[-1])
        self.assertIn("src/main.rs", notes(dev_run.ticket_id)[-1])

    def test_main_moving_after_a_pass_means_revalidation(self):
        _, sub = self.submitted_run()
        self.ci_results(passing_results())
        first = validation.validate(self.runner, self.repo, sub)
        moved = self.advance_main({"README": "hi\n"})

        [ready] = merging.mergeable()
        self.assertIsNone(merging.merge(self.repo, ready))
        self.assertEqual(self.repo.main_sha(), moved)

        [again] = validation.to_validate(self.repo)
        second = validation.validate(self.runner, self.repo, again)
        self.assertEqual(second.base_sha, moved)
        self.assertNotEqual(second.merge_sha, first.merge_sha)
        merging.merge(self.repo, merging.mergeable()[0])
        self.assertEqual(self.repo.main_sha(), second.merge_sha)

    def test_merge_recovers_after_crashing_before_recording(self):
        self.submitted_run()
        self.ci_results(passing_results())
        [sub] = validation.to_validate(self.repo)
        vr = validation.validate(self.runner, self.repo, sub)
        self.repo.update_ref(self.repo.main_ref, vr.merge_sha, vr.base_sha)

        self.assertEqual(validation.to_validate(self.repo), [])
        [ready] = merging.mergeable()
        record = merging.merge(self.repo, ready)
        self.assertEqual(record.result_sha, vr.merge_sha)
        self.assertEqual(Merge.objects.count(), 1)

    def test_errored_ci_is_retried(self):
        dev_run, sub = self.submitted_run()
        self.incus.command = Command(vm_dies=True)
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.status, RunStatus.ERRORED)
        self.assertEqual(state_of(dev_run.ticket), TicketState.SUBMITTED)
        self.ci_results(passing_results())
        [again] = validation.to_validate(self.repo)
        self.assertEqual(
            validation.validate(self.runner, self.repo, again).outcome, Outcome.PASSED
        )

    def test_missing_results_is_an_infra_error(self):
        _, sub = self.submitted_run()
        self.incus.command = Command(exit_code=0)
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.status, RunStatus.ERRORED)

    def test_build_failure_skips_the_rest(self):
        _, sub = self.submitted_run()
        self.ci_results(
            {"build": {"passed": False, "exit_code": 101, "output": "error[E0425]"}}
        )
        vr = validation.validate(self.runner, self.repo, sub)
        self.assertEqual(vr.failed_checks, [Check.BUILD])
        self.assertIn("error[E0425]", notes(sub.dev_run.ticket_id)[-1])


class ConstraintTests(WorkflowTestCase):
    def test_note_source_must_match_its_run(self):
        ticket = self.make_ticket()
        with self.assertRaises(IntegrityError):
            TicketNote.objects.create(
                ticket=ticket, content="x", source=NoteSource.DEV_RUN
            )

    def test_outcome_only_when_finished(self):
        dev_run, _ = self.dispatch()
        with self.assertRaises(IntegrityError):
            DevRun.objects.filter(pk=dev_run.pk).update(outcome=Outcome.PASSED)

    def test_runs_are_protected_from_deletion(self):
        _, sub = self.submitted_run()
        with self.assertRaises(ProtectedError):
            sub.dev_run.ticket.delete()
        self.assertEqual(Submission.objects.count(), 1)
