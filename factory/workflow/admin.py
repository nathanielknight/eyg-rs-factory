from django.contrib import admin
from django.utils import timezone

from .models import DevRun, Merge, ReviewDecision, Submission, ValidationRun
from .state import refresh_ticket_state


class ReadOnlyAdmin(admin.ModelAdmin):
    "Only the batch commands and the agent bridge write these."

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DevRun)
class DevRunAdmin(ReadOnlyAdmin):
    list_display = ("id", "ticket", "status", "outcome", "created_at", "ended_at")
    list_filter = ("status", "outcome")
    search_fields = ("ticket__title", "session_id")
    exclude = ("token_hash",)


@admin.register(ValidationRun)
class ValidationRunAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "submission",
        "status",
        "outcome",
        "failed_checks",
        "base_sha",
        "created_at",
    )
    list_filter = ("status", "outcome")


@admin.register(Merge)
class MergeAdmin(ReadOnlyAdmin):
    list_display = ("id", "validation_run", "parent_sha", "result_sha", "merged_at")


def _decide(submissions, decision: str) -> None:
    for sub in submissions.filter(review_decision__isnull=True):
        Submission.objects.filter(pk=sub.pk, review_decision__isnull=True).update(
            review_decision=decision, reviewed_at=timezone.now()
        )
        refresh_ticket_state(sub.dev_run.ticket_id)


@admin.action(description="Approve selected submissions")
def approve(modeladmin, request, queryset):
    _decide(queryset, ReviewDecision.APPROVED)


@admin.action(description="Reject selected submissions")
def reject(modeladmin, request, queryset):
    _decide(queryset, ReviewDecision.REJECTED)


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    "Review is the one thing a human changes here, once."

    list_display = (
        "id",
        "dev_run",
        "head_sha",
        "review_required",
        "review_reason",
        "review_decision",
        "submitted_at",
    )
    list_filter = ("review_required", "review_decision")
    actions = [approve, reject]

    def get_readonly_fields(self, request, obj=None):
        fields = [f.name for f in self.model._meta.fields]
        if obj is not None and obj.review_decision is None:
            fields.remove("review_decision")
        return fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if "review_decision" in form.changed_data and obj.review_decision:
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)
        refresh_ticket_state(obj.dev_run.ticket_id)
