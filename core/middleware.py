"""
Middleware to enforce and validate partner scopes using API keys.
"""

from django.http import JsonResponse
from django.urls import resolve, Resolver404
from .models import Partner

EXEMPT_VIEW_NAMES = [
    "partner-onboard",
    "partner-kyc",
    "partner-me",
    "login",
    "token-refresh",
    "payments:callback",
    "payments:nomba-callback",
    "partner-api-key-regenerate",
    "partner-api-key-retrieve",
]


class PartnerScopeMiddleware:
    """
    Middleware that validates the 'X-Partner-Key' header for incoming
    API requests and attaches the corresponding Partner instance to the request.
    """

    def __init__(self, get_response):
        """Initialize the middleware with the next response handler."""
        self.get_response = get_response

    def __call__(self, request):
        """
        Process the request to ensure API key validity on non-exempt endpoints.
        """
        if request.path.startswith("/api/v1/"):
            exempt_paths = (
                "/staff/",
                "/service-account/",
                "/nomba/webhook/",
                "/interswitch/webhook/",
            )
            if any(path in request.path for path in exempt_paths):
                return self.get_response(request)

            try:
                match = resolve(request.path)
                view_name = match.view_name
            except Resolver404:
                view_name = None

            if view_name not in EXEMPT_VIEW_NAMES:
                api_key = request.headers.get("X-Partner-Key")

                if not api_key:
                    return JsonResponse(
                        {"error": "X-Partner-Key header is required."},
                        status=401,
                    )

                try:
                    partner = Partner.objects.get(api_key=api_key, is_active=True)
                    request.partner = partner
                except Partner.DoesNotExist:
                    return JsonResponse(
                        {"error": "Invalid or inactive partner key."},
                        status=401,
                    )

        return self.get_response(request)
