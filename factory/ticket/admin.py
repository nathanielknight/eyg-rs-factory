from django.contrib import admin

from workflow.state import refresh_ticket_state

from .models import NoteSource, Ticket, TicketNote


class TicketNoteInline(admin.TabularInline):
    "Notes are append-only: add them here, but don't edit them."

    model = TicketNote
    extra = 0
    fields = ("content", "source", "dev_run", "validation_run", "created_at")
    readonly_fields = ("source", "dev_run", "validation_run", "created_at")
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "state", "created_at", "modified_at")
    list_filter = ("state",)
    search_fields = ("title", "description")
    readonly_fields = ("state", "created_at", "modified_at")
    inlines = [TicketNoteInline]

    def has_delete_permission(self, request, obj=None):
        return False

    def save_formset(self, request, form, formset, change):
        for note in formset.save(commit=False):
            note.source = NoteSource.HUMAN
            note.save()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Cancelling, or a human note, can change the derived state.
        refresh_ticket_state(form.instance.pk)


@admin.register(TicketNote)
class TicketNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "source", "content", "created_at")
    list_filter = ("source",)
    readonly_fields = ("source", "dev_run", "validation_run", "created_at")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.source = NoteSource.HUMAN
        super().save_model(request, obj, form, change)
        refresh_ticket_state(obj.ticket_id)
