"""
Middleware to enforce and validate partner scopes using API keys.
"""

from django.http import JsonResponse
from django.urls import resolve, Resolver404
from .models import Partner

EXEMPT_VIEW_NAMES = {
    # public
    "partner-onboard", "login", "token-refresh", "service-account-token",
    # partner portal: JWT only
    "partner-kyc", "partner-me", "partner-profile", "partner-api-key-regenerate",
    "partner-team", "partner-team-deactivate", "partner-subscription-list",
    "provider-plan-list-create", "provider-plan-detail",
    "provider-access-requests", "provider-access-request-review",
    "distributor-marketplace-providers", "distributor-marketplace-plans",
    "distributor-access-grants", "distributor-access-grant-withdraw",
    # Paystack
    "payments:paystack-webhook",
}
EXEMPT_PATH_PREFIXES = ("/api/v1/staff/",)

class PartnerScopeMiddleware:
    """
    Middleware that validates the 'X-Partner-Key' header for incoming
    API requests and attaches the corresponding Partner instance to the request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.partner = None
        
        if not request.path.startswith("/api/v1/"):
            return self.get_response(request)

        # Bypass check for exempt path prefixes
        if request.path.startswith(EXEMPT_PATH_PREFIXES):
            return self.get_response(request)

        # 2. Check if the resolved view is exempt
        try:
            match = resolve(request.path)
            view_name = match.view_name
        except Resolver404:
            view_name = None

        if view_name in EXEMPT_VIEW_NAMES:
            return self.get_response(request)

        # 3. Enforce and validate X-Partner-Key for non-exempt endpoints
        api_key = request.headers.get("X-Partner-Key") or request.META.get("HTTP_X_PARTNER_KEY")

        if not api_key:
            return JsonResponse(
                {"error": "X-Partner-Key header is required."},
                status=401,
            )

        try:
            key_hash = Partner.hash_key(api_key)
            request.partner = Partner.objects.get(api_key_hash=key_hash, is_active=True)
        except Partner.DoesNotExist:
            return JsonResponse(
                {"error": "Invalid or inactive partner key."},
                status=401,
            )

        return self.get_response(request)
