from django.http import JsonResponse
from .models import Partner

class PartnerScopeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

        def __call__(self, request):
            if request.path.startswith("/api/v1/"):
                api_key = request.headers.get("X-API-Key")

                if not api_key:
                    return JsonResponse(
                        {"error": "API key required"}, status=401
                    )

                try:
                    partner = Partner.objects.get(api_key=api_key, is_active=True)
                    request.partner = partner  # Attach partner to request for later use
                except Partner.DoesNotExist:
                    return JsonResponse({"error": "Invalid API key"}, status=401)
                
            response = self.get_response(request)
            return response
            
