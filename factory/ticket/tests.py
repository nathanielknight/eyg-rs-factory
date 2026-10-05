from django.test import TestCase
from django.urls import reverse

from .models import Ticket, TicketNote, TicketState


def make_ticket(**kwargs) -> Ticket:
    defaults = {
        "title": "Implement let",
        "description": "Add let bindings",
        "test_command": "cargo test",
        "verify_command": "SECRET-holdout-suite --strict",
    }
    return Ticket.objects.create(**{**defaults, **kwargs})


class TicketListTests(TestCase):
    def test_lists_tickets(self):
        t = make_ticket()
        resp = self.client.get(reverse("tickets:list"))
        self.assertEqual(resp.status_code, 200)
        [item] = resp.json()["tickets"]
        # Stable fields match
        self.assertEqual(item["id"], t.pk)
        self.assertEqual(item["title"], t.title)
        self.assertEqual(item["state"], t.state)
        # They have the same fields
        self.assertEqual(
            set(item), {"id", "title", "state", "created_at", "modified_at"}
        )

    def test_filters_by_state(self):
        open = make_ticket(title="a")
        done = make_ticket(title="b", state=TicketState.DONE)

        resp = self.client.get(reverse("tickets:list"), {"state": "done"})
        self.assertEqual([t["id"] for t in resp.json()["tickets"]], [done.pk])

        unfiltered_resp = self.client.get(reverse("tickets:list"))
        self.assertEqual(
            set(t["id"] for t in unfiltered_resp.json()["tickets"]),
            set([done.pk, open.pk]),
        )

    def test_one_query(self):
        make_ticket()
        make_ticket()
        with self.assertNumQueries(1):
            self.client.get(reverse("tickets:list"))

    def test_invalid_state_is_400(self):
        resp = self.client.get(reverse("tickets:list"), {"state": "bogus-8cu1Ocl26NikXaEYF-hyeZDXZ8tQIWW39JpZOtjSzpw"})
        self.assertEqual(resp.status_code, 400)


class TicketDetailTests(TestCase):
    def test_detail_omits_verify_command(self):
        t = make_ticket()
        resp = self.client.get(reverse("tickets:detail", args=[t.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("verify_command", resp.json())
        self.assertNotIn(t.verify_command, resp.content.decode())

    def test_detail_includes_note_count(self):
        t = make_ticket()
        TicketNote.objects.create(ticket=t, content="one")
        TicketNote.objects.create(ticket=t, content="two")
        resp = self.client.get(reverse("tickets:detail", args=[t.pk]))
        self.assertEqual(resp.json()["note_count"], 2)

    def test_one_query(self):
        t = make_ticket()
        TicketNote.objects.create(ticket=t, content="one")
        with self.assertNumQueries(1):
            self.client.get(reverse("tickets:detail", args=[t.pk]))

    def test_missing_ticket_is_404(self):
        with self.assertNumQueries(1):
            resp = self.client.get(reverse("tickets:detail", args=[999]))
        self.assertEqual(resp.status_code, 404)


class TicketNotesTests(TestCase):
    def test_post_then_get_notes(self):
        t = make_ticket()
        url = reverse("tickets:notes", args=[t.pk])
        for body in ["# First\n\nhello", "second *note*"]:
            resp = self.client.post(url, body, content_type="text/markdown")
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.json()["content"], body)

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/markdown"))
        parts = resp.content.decode().split("\n\n---\n\n")
        self.assertEqual(len(parts), 2)
        self.assertIn("# First\n\nhello", parts[0])
        self.assertIn("second *note*", parts[1])
        for part, note in zip(parts, t.ticketnote_set.order_by("pk")):
            self.assertIn(note.created_at.isoformat(), part)

    def test_get_notes_one_query(self):
        t = make_ticket()
        TicketNote.objects.create(ticket=t, content="one")
        TicketNote.objects.create(ticket=t, content="two")
        with self.assertNumQueries(1):
            resp = self.client.get(reverse("tickets:notes", args=[t.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_get_notes_for_ticket_without_notes(self):
        t = make_ticket()
        resp = self.client.get(reverse("tickets:notes", args=[t.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")

    def test_notes_for_missing_ticket_is_404(self):
        url = reverse("tickets:notes", args=[999])
        self.assertEqual(self.client.get(url).status_code, 404)
        resp = self.client.post(url, "hi", content_type="text/markdown")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(TicketNote.objects.count(), 0)

    def test_empty_note_is_400(self):
        t = make_ticket()
        url = reverse("tickets:notes", args=[t.pk])
        resp = self.client.post(url, "  \n", content_type="text/markdown")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(t.ticketnote_set.count(), 0)

    def test_post_works_without_csrf_token(self):
        from django.test import Client

        t = make_ticket()
        client = Client(enforce_csrf_checks=True)
        resp = client.post(
            reverse("tickets:notes", args=[t.pk]), "hi", content_type="text/markdown"
        )
        self.assertEqual(resp.status_code, 201)
