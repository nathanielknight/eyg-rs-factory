from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import Ticket, TicketNote, TicketState


def _summary(ticket: Ticket) -> dict:
    return {
        "id": ticket.pk,
        "title": ticket.title,
        "state": ticket.state,
        "created_at": ticket.created_at.isoformat(),
        "modified_at": ticket.modified_at.isoformat(),
    }


@require_GET
def ticket_list(request):
    tickets = Ticket.objects.order_by("modified_at")
    state = request.GET.get("state")
    if state is not None:
        if state not in TicketState.values:
            return JsonResponse(
                {"error": f"invalid state {state!r}", "valid": TicketState.values},
                status=400,
            )
        tickets = tickets.filter(state=state)
    return JsonResponse({"tickets": [_summary(t) for t in tickets]})


def _not_found(pk: int) -> JsonResponse:
    return JsonResponse({"error": f"ticket {pk} not found"}, status=404)


@require_GET
def ticket_detail(request, pk: int):
    # One query: the note count is a joined aggregate. verify_command is
    # deliberately withheld (it's the holdout check), so don't even load it.
    ticket = (
        Ticket.objects.defer("verify_command")
        .annotate(note_count=Count("ticketnote"))
        .filter(pk=pk)
        .first()
    )
    if ticket is None:
        return _not_found(pk)
    return JsonResponse(
        {
            **_summary(ticket),
            "description": ticket.description,
            "test_command": ticket.test_command,
            "note_count": ticket.note_count,
        }
    )


def _note_markdown(created_at, content: str) -> str:
    return f"_{created_at.isoformat()}_\n\n{content}"


def _list_notes(pk: int) -> HttpResponse:
    # One query: LEFT JOIN from the ticket, so a ticket with no notes yields a
    # single row of NULLs and a missing ticket yields no rows at all.
    rows = list(
        Ticket.objects.filter(pk=pk)
        .order_by("ticketnote__created_at", "ticketnote__pk")
        .values_list("ticketnote__pk", "ticketnote__created_at", "ticketnote__content")
    )
    if not rows:
        return _not_found(pk)
    body = "\n\n---\n\n".join(
        _note_markdown(created_at, content)
        for note_pk, created_at, content in rows
        if note_pk is not None
    )
    return HttpResponse(body, content_type="text/markdown; charset=utf-8")


def _add_note(request, pk: int) -> JsonResponse:
    content = request.body.decode("utf-8", errors="replace")
    if not content.strip():
        return JsonResponse({"error": "note body is empty"}, status=400)
    # Two queries: SQLite only checks foreign keys at commit, so an insert
    # against a missing ticket can't be relied on to fail; check first.
    if not Ticket.objects.filter(pk=pk).exists():
        return _not_found(pk)
    note = TicketNote.objects.create(ticket_id=pk, content=content)
    return JsonResponse(
        {
            "id": note.pk,
            "ticket": pk,
            "content": note.content,
            "created_at": note.created_at.isoformat(),
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def ticket_notes(request, pk: int):
    if request.method == "POST":
        return _add_note(request, pk)
    return _list_notes(pk)
