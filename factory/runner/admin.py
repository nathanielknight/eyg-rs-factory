from django.contrib import admin

from .models import Invocation


@admin.register(Invocation)
class InvocationAdmin(admin.ModelAdmin):
    "Read-only: only the runner's lifecycle methods write invocations."

    list_display = ("id", "name", "status", "exit_code", "created_at", "ended_at")
    list_filter = ("status",)
    search_fields = ("id", "name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
