from django.test import Client

from ticket.models import NoteSource, TicketNote
from workflow import dev
from workflow.guest import factory_cli
from workflow.models import Submission

from .helpers import WorkflowTestCase


def bearer(token, **kwargs):
    return Client(headers={"Authorization": f"Bearer {token}"}, **kwargs)


class RunTokenTests(WorkflowTestCase):
    def test_token_reaches_only_its_own_ticket(self):
        dev_run, token = self.dispatch()
        other = self.make_ticket(title="other")
        client = bearer(token)
        self.assertEqual(client.get(f"/tickets/{dev_run.ticket_id}/").status_code, 200)
        self.assertEqual(client.get(f"/tickets/{other.pk}/").status_code, 404)
        self.assertEqual(client.get(f"/tickets/{other.pk}/notes/").status_code, 404)
        listed = client.get("/tickets/").json()["tickets"]
        self.assertEqual([t["id"] for t in listed], [dev_run.ticket_id])

    def test_agent_notes_are_attributed_to_the_run(self):
        dev_run, token = self.dispatch()
        resp = bearer(token).post(
            f"/tickets/{dev_run.ticket_id}/notes/",
            "Found the bug in env.rs",
            content_type="text/markdown",
        )
        self.assertEqual(resp.status_code, 201)
        note = TicketNote.objects.get()
        self.assertEqual((note.source, note.dev_run), (NoteSource.DEV_RUN, dev_run))

    def test_operator_notes_are_human(self):
        ticket = self.make_ticket()
        self.operator.post(
            f"/tickets/{ticket.pk}/notes/", "hint", content_type="text/markdown"
        )
        self.assertEqual(TicketNote.objects.get().source, NoteSource.HUMAN)

    def test_token_dies_with_the_run(self):
        dev_run, token = self.dispatch()
        dev.drive(self.runner, dev_run)
        resp = bearer(token).get(f"/tickets/{dev_run.ticket_id}/")
        self.assertEqual(resp.status_code, 401)

    def test_bad_token_is_401(self):
        self.assertEqual(bearer("nope").get("/tickets/").status_code, 401)

    def test_ci_network_is_refused_even_with_a_token(self):
        dev_run, token = self.dispatch()
        client = bearer(token, REMOTE_ADDR="10.197.0.23")
        self.assertEqual(client.get(f"/tickets/{dev_run.ticket_id}/").status_code, 403)

    def test_no_notes_after_submitting(self):
        dev_run, token = self.dispatch()
        bundle, head = self.agent_change(dev_run.base_sha, {"a.rs": "//\n"})
        self.submit(token, bundle, head, ticket=str(dev_run.ticket_id))
        resp = bearer(token).post(
            f"/tickets/{dev_run.ticket_id}/notes/", "x", content_type="text/markdown"
        )
        self.assertEqual(resp.status_code, 403)


class SubmitTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.dev_run, self.token = self.dispatch()
        self.bundle, self.head = self.agent_change(
            self.dev_run.base_sha, {"src/let.rs": "// let\n"}
        )

    def go(self, **fields):
        fields.setdefault("ticket", str(self.dev_run.ticket_id))
        return self.submit(self.token, self.bundle, self.head, **fields)

    def test_creates_a_pending_submission(self):
        resp = self.go()
        self.assertEqual(resp.status_code, 201, resp.content)
        sub = Submission.objects.get()
        self.assertEqual(sub.dev_run, self.dev_run)
        self.assertEqual(sub.head_sha, self.head)
        self.assertEqual(sub.base_sha, self.dev_run.base_sha)
        self.assertFalse(sub.validation_runs.exists())
        # Not fetched: the validate command does that.
        self.assertIsNone(self.repo.rev_parse(f"refs/factory/submissions/{sub.pk}"))

    def test_retry_is_idempotent_and_a_second_submission_conflicts(self):
        self.assertEqual(self.go().status_code, 201)
        again = self.go()
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["id"], Submission.objects.get().pk)
        other = self.go(submission_id="another-one")
        self.assertEqual(other.status_code, 409)

    def test_bad_bundle_is_rejected_with_gits_reason(self):
        self.bundle.write_bytes(b"not a bundle")
        resp = self.go()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("git bundle verify", resp.json()["error"])
        self.assertFalse(Submission.objects.exists())

    def test_bundle_size_is_capped(self):
        with self.settings(WORKFLOW_MAX_BUNDLE_BYTES=10):
            resp = self.go()
        self.assertEqual(resp.status_code, 413)

    def test_wrong_ticket_is_rejected(self):
        self.assertEqual(self.go(ticket="999").status_code, 400)

    def test_the_cli_upload_parses(self):
        body, content_type = factory_cli.multipart(
            {
                "submission_id": "cli-1",
                "ticket": str(self.dev_run.ticket_id),
                "summary": "Added let.",
                "review": "true",
                "head_sha": self.head,
            },
            "bundle",
            self.bundle,
        )
        resp = bearer(self.token).generic(
            "POST", "/submissions/", body, content_type=content_type
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(Submission.objects.get().review_required)

    def test_operator_cannot_submit(self):
        with self.bundle.open("rb") as f:
            resp = self.operator.post("/submissions/", {"bundle": f})
        self.assertEqual(resp.status_code, 403)
