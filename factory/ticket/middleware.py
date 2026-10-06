import hmac

from django.conf import settings
from django.http import JsonResponse


class SharedSecretAuthMiddleware:
    """Require a shared secret header on /tickets/ URLs."""

    protected_prefix = "/tickets/"
    header = "Authorization"  # sent as: Authorization: Bearer <secret>

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.protected_prefix):
            expected = settings.FACTORY_SHARED_SECRET
            supplied = request.headers.get(self.header, "").removeprefix("Bearer ")
            if not expected or not hmac.compare_digest(
                supplied.encode(), expected.encode()
            ):
                return JsonResponse({"error": "unauthorized"}, status=401)
        return self.get_response(request)
