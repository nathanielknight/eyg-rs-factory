"""The submit endpoint of the agent bridge.

It writes a bundle file and a Submission row, and runs `git bundle verify` so
the agent hears about a bad bundle while it can still fix it. It never
fetches the bundle or starts anything; the validate command does that.
"""

import re
import shutil
from pathlib import Path

from django.conf import settings
from django.core.files.uploadhandler import StopUpload, TemporaryFileUploadHandler
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .git import get_repo
from .models import Submission
from .state import refresh_ticket_state

SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
EXTERNAL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
# Room for the metadata fields and multipart framing around the bundle.
FORM_OVERHEAD = 256 * 1024


class CappedUploadHandler(TemporaryFileUploadHandler):
    "Stream uploads to disk, and give up on any file past the cap."

    def __init__(self, request, cap: int):
        super().__init__(request)
        self.cap = cap
        self.exceeded = False

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > self.cap:
            self.exceeded = True
            raise StopUpload(connection_reset=True)
        return super().receive_data_chunk(raw_data, start)


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _summary(sub: Submission) -> dict:
    return {
        "id": sub.pk,
        "dev_run": sub.dev_run_id,
        "submission_id": sub.external_id,
        "head_sha": sub.head_sha,
        "base_sha": sub.base_sha,
        "review_required": sub.review_required,
        "submitted_at": sub.submitted_at.isoformat(),
    }


@csrf_exempt
@require_POST
def submit(request):
    dev_run = request.dev_run
    if dev_run is None:
        return _error("submissions come from dev runs, with the run's token", 403)
    cap = settings.WORKFLOW_MAX_BUNDLE_BYTES
    if int(request.META.get("CONTENT_LENGTH") or 0) > cap + FORM_OVERHEAD:
        return _error(f"bundle exceeds {cap} bytes", 413)
    handler = CappedUploadHandler(request, cap)
    request.upload_handlers = [handler]

    post, files = request.POST, request.FILES
    if handler.exceeded:
        return _error(f"bundle exceeds {cap} bytes", 413)
    external_id = post.get("submission_id", "")
    if not EXTERNAL_ID.fullmatch(external_id):
        return _error("submission_id must be 1-64 letters, digits or dashes", 400)

    # A retry of a submit that already went through gets the same answer.
    existing = Submission.objects.filter(dev_run=dev_run).first()
    if existing is not None:
        if existing.external_id == external_id:
            return JsonResponse(_summary(existing), status=200)
        return _error("this run has already submitted", 409)

    if post.get("ticket") != str(dev_run.ticket_id):
        return _error(f"this run is for ticket {dev_run.ticket_id}", 400)
    head_sha = post.get("head_sha", "")
    if not SHA.fullmatch(head_sha):
        return _error("head_sha must be a full commit hash", 400)
    summary = post.get("summary", "")
    if not summary.strip():
        return _error("summary is empty", 400)
    review = post.get("review", "false")
    if review not in ("true", "false"):
        return _error("review must be true or false", 400)
    upload = files.get("bundle")
    if upload is None:
        return _error("no bundle uploaded", 400)

    dest_dir = Path(settings.WORKFLOW_ROOT) / "submissions" / str(dev_run.pk)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{external_id}.bundle"
    shutil.move(upload.temporary_file_path(), dest)
    if problem := get_repo().verify_bundle(dest):
        dest.unlink()
        return _error(f"git bundle verify: {problem}", 422)

    try:
        with transaction.atomic():
            sub = Submission.objects.create(
                dev_run=dev_run,
                external_id=external_id,
                bundle_path=str(dest),
                head_sha=head_sha,
                base_sha=dev_run.base_sha,
                summary=summary,
                review_required=review == "true",
                review_reason="agent_flag" if review == "true" else "",
            )
    except IntegrityError:
        # Lost a race with a concurrent submit from the same run.
        return _error("this run has already submitted", 409)
    refresh_ticket_state(dev_run.ticket_id)
    return JsonResponse(_summary(sub), status=201)
