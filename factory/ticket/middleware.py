import hashlib
import hmac
import ipaddress

from django.conf import settings
from django.http import JsonResponse


class AgentBridgeAuthMiddleware:
    """Authenticate requests to the agent bridge API.

    Two kinds of bearer token: the shared secret (Nat's tools; acts as a
    human), or a live dev run's own token, which sets request.dev_run and
    limits the request to that run's ticket. Requests from untrusted networks
    (CI VMs) are refused whatever they carry.
    """

    protected_prefixes = ("/tickets/", "/submissions/")
    header = "Authorization"  # sent as: Authorization: Bearer <token>

    def __init__(self, get_response):
        self.get_response = get_response
        self.untrusted = [
            ipaddress.ip_network(n) for n in settings.WORKFLOW_UNTRUSTED_NETWORKS
        ]

    def __call__(self, request):
        request.dev_run = None
        if request.path.startswith(self.protected_prefixes):
            if self._untrusted(request.META.get("REMOTE_ADDR", "")):
                return JsonResponse({"error": "forbidden"}, status=403)
            supplied = request.headers.get(self.header, "").removeprefix("Bearer ")
            if not supplied:
                return _unauthorized()
            expected = settings.FACTORY_SHARED_SECRET
            if not (
                expected and hmac.compare_digest(supplied.encode(), expected.encode())
            ):
                request.dev_run = _live_dev_run(supplied)
                if request.dev_run is None:
                    return _unauthorized()
        return self.get_response(request)

    def _untrusted(self, addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in net for net in self.untrusted)


def _unauthorized() -> JsonResponse:
    return JsonResponse({"error": "unauthorized"}, status=401)


def _live_dev_run(token: str):
    from workflow.models import LIVE_RUN_STATUSES, DevRun

    digest = hashlib.sha256(token.encode()).hexdigest()
    return DevRun.objects.filter(
        token_hash=digest, status__in=LIVE_RUN_STATUSES
    ).first()
