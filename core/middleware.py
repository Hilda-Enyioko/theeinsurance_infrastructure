from django.http import JsonResponse
from django.urls import resolve, Resolver404
from .models import Partner


EXEMPT_VIEW_NAMES = [
    "partner-onboard",
    "partner-kyc",
    "login",
    "token-refresh",
]


class PartnerScopeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/v1/"):
            if any(path in request.path for path in ("/staff/", "/service-account/")):
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
