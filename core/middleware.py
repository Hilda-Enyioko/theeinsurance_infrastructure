from django.http import JsonResponse
from .models import Partner


EXEMPT_PATHS = [
    "/api/v1/partner/onboard/",
    "/api/v1/partner/kyc/",
    "/api/v1/auth/login/",
    "/api/v1/auth/token/refresh/",
    "/api/v1/staff/auth/login/",
]


class PartnerScopeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/v1/"):
            if request.path not in EXEMPT_PATHS:
                api_key = request.headers.get("X-Partner-Key")

                if not api_key:
                    return JsonResponse(
                        {"error": "X-Partner-Key header is required."}, status=401
                    )

                try:
                    partner = Partner.objects.get(api_key=api_key, is_active=True)
                    request.partner = partner
                except Partner.DoesNotExist:
                    return JsonResponse(
                        {"error": "Invalid or inactive partner key."}, status=401
                    )

        response = self.get_response(request)
        return response