"""
Nomba Authentication Service.

Handles OAuth 2.0 token lifecycle for all Nomba API calls:
  - Obtain a token (client_credentials grant)
  - Cache it for 25 minutes (token lives 30 min; we refresh 5 min early)
  - Refresh when expired
  - Expose a single public function: get_nomba_token()

All Nomba service modules (payments, subscriptions) import only
get_nomba_token() and never manage credentials themselves.
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Cache key and TTL
_CACHE_KEY_TOKEN        = "nomba:access_token"
_CACHE_KEY_REFRESH      = "nomba:refresh_token"
_CACHE_KEY_EXPIRES_AT   = "nomba:token_expires_at"
_TOKEN_CACHE_TTL        = 25 * 60


class NombaAuthError(Exception):
    """Raised when Nomba token acquisition or refresh fails."""
    pass


def _auth_headers() -> dict:
    """
    Headers required for Nomba auth endpoints.
    accountId must be the PARENT account ID — always, for auth calls.
    """
    return {
        "Content-Type": "application/json",
        "accountId": settings.NOMBA_ACCOUNT_ID,
    }


def _fetch_new_token() -> dict:
    """
    Request a fresh access_token from Nomba using client_credentials grant.
    Returns the full data dict: {access_token, refresh_token, expiresAt}.
    """
    payload = {
        "grant_type": "client_credentials",
        "client_id": settings.NOMBA_CLIENT_ID,
        "client_secret": settings.NOMBA_CLIENT_SECRET,
    }

    try:
        response = requests.post(
            f"{settings.NOMBA_BASE_URL}/auth/token/issue",
            json=payload,
            headers=_auth_headers(),
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        logger.error("Nomba token request timed out.")
        raise NombaAuthError("Nomba auth timed out.")
    except requests.RequestException as e:
        logger.error("Nomba token request failed: %s", str(e))
        raise NombaAuthError(f"Nomba auth request failed: {str(e)}")

    if data.get("code") != "00":
        logger.error("Nomba auth returned non-00 code: %s", data)
        raise NombaAuthError(f"Nomba auth error: {data.get('description', 'unknown')}")

    return data["data"]


def _refresh_token(refresh_token: str, current_access_token: str) -> dict:
    """
    Exchange a refresh_token for a new access_token without re-sending credentials.
    Returns the full data dict: {access_token, refresh_token, expiresAt}.
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        response = requests.post(
            f"{settings.NOMBA_BASE_URL}/auth/token/refresh",
            json=payload,
            headers={
                **_auth_headers(),
                "Authorization": f"Bearer {current_access_token}",
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        logger.error("Nomba token refresh timed out.")
        raise NombaAuthError("Nomba token refresh timed out.")
    except requests.RequestException as e:
        logger.error("Nomba token refresh failed: %s", str(e))
        raise NombaAuthError(f"Nomba token refresh failed: {str(e)}")

    if data.get("code") != "00":
        logger.error("Nomba token refresh returned non-00 code: %s", data)
        raise NombaAuthError(f"Nomba refresh error: {data.get('description', 'unknown')}")

    return data["data"]


def _cache_token(token_data: dict) -> None:
    """
    Store access_token and refresh_token in Django's cache.
    TTL is 25 minutes — 5 minutes shorter than Nomba's 30 minute expiry
    so we never serve a token that's about to expire.
    """
    cache.set(_CACHE_KEY_TOKEN,      token_data["access_token"],  _TOKEN_CACHE_TTL)
    cache.set(_CACHE_KEY_REFRESH,    token_data["refresh_token"], _TOKEN_CACHE_TTL)
    cache.set(_CACHE_KEY_EXPIRES_AT, token_data["expiresAt"],     _TOKEN_CACHE_TTL)
    logger.debug("Nomba token cached. Expires at: %s", token_data["expiresAt"])


def get_nomba_token() -> str:
    """
    Public interface. Returns a valid Nomba access_token.

    Strategy:
      1. Return cached token if present (cache TTL enforces the 25-min window).
      2. If a refresh_token is cached but access_token is gone, try to refresh.
      3. Otherwise fetch a brand-new token with client_credentials.

    All callers — checkout initiation, renewal charges, webhook verification
    helpers — use only this function. They never touch credentials directly.
    """
    access_token = cache.get(_CACHE_KEY_TOKEN)
    if access_token:
        logger.debug("Nomba token served from cache.")
        return access_token

    # Access token expired or was never fetched — check for refresh token
    refresh = cache.get(_CACHE_KEY_REFRESH)
    if refresh:
        logger.info("Nomba access token expired; attempting refresh.")
        try:
            # We need the old access token for the refresh header.
            # If it's gone from cache, fall through to a fresh fetch.
            old_access = cache.get(_CACHE_KEY_TOKEN, "")
            token_data = _refresh_token(refresh, old_access)
            _cache_token(token_data)
            return token_data["access_token"]
        except NombaAuthError:
            logger.warning("Nomba refresh failed; falling back to full re-auth.")

    # No valid cached token and refresh failed (or never existed) — full auth
    logger.info("Fetching new Nomba token via client_credentials.")
    token_data = _fetch_new_token()
    _cache_token(token_data)
    return token_data["access_token"]
