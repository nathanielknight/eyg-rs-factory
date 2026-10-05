from django.contrib import admin

from .models import Ticket, TicketNote


class TicketNoteInline(admin.TabularInline):
    model = TicketNote
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "state", "created_at", "modified_at")
    list_filter = ("state",)
    search_fields = ("title", "description")
    readonly_fields = ("created_at", "modified_at")
    inlines = [TicketNoteInline]


@admin.register(TicketNote)
class TicketNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "content", "created_at")
    readonly_fields = ("created_at",)
