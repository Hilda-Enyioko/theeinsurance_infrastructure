"""
Rate Limiting and Throttling Core Module.
This module provides custom throttling classes for Django REST Framework (DRF)
to protect application endpoints
"""

from rest_framework.throttling import SimpleRateThrottle


class IPRateThrottle(SimpleRateThrottle):
    """
    Throttle by IP address.

    Used on public auth endpoints to prevent brute force attacks:
    - POST /auth/login/
    - POST /staff/auth/login/
    - POST /auth/register/
    - POST /partner/onboard/
    - POST /auth/token/refresh/

    Rate: 10 requests/hour per IP (configured in settings.DEFAULT_THROTTLE_RATES["auth"])

    In production behind a reverse proxy (nginx, Caddy), ensure
    SECURE_PROXY_SSL_HEADER and USE_X_FORWARDED_HOST are set so
    get_ident() reads the real client IP from X-Forwarded-For,
    not the proxy IP.
    """
    scope = "auth"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class PartnerRateThrottle(SimpleRateThrottle):
    """
    Throttle by partner (X-Partner-Key tenant).

    All authenticated and partner-scoped endpoints use this so each
    tenant has their own independent rate limit bucket. This means a
    misbehaving or compromised partner key cannot affect other tenants.

    Rate: 1000 requests/hour per partner (configured in settings.DEFAULT_THROTTLE_RATES["partner"])

    Falls back to IP-based keying if request.partner is not set —
    this handles the edge case of public endpoints where the middleware
    hasn't attached a partner (e.g. customer registration before login).
    """
    scope = "partner"

    def get_cache_key(self, request, view):
        partner = getattr(request, "partner", None)
        ident = str(partner.id) if partner else self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }
