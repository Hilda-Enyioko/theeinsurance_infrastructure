import json
import logging
import time
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import ServiceWebhookEndpoint, Webhook
from .envelope import build_envelope
from .events import CATALOGUE, N8N, PARTNER
from .models import OutboundEvent, WebhookDelivery
from .signing import sign

log = logging.getLogger(__name__)
BACKOFF = [60, 300, 1800, 7200, 21600]   # 1m, 5m, 30m, 2h, 6h, then FAILED (6 attempts total)


# ── Emission (idempotent) ─────────────────────────────────────────────────────

def emit(event_type, *, partner, aggregate_id, data, discriminator=""):
    """
    Idempotent: the same (event_type, aggregate_id, discriminator) is only
    ever stored and delivered once. Call inside the same transaction as the
    state change; delivery runs after commit.
    """
    key = f"{event_type}:{aggregate_id}:{discriminator}"
    existing = OutboundEvent.objects.filter(idempotency_key=key).first()
    if existing:
        return existing

    envelope = build_envelope(event_type, data, partner_id=partner.id, correlation_id=aggregate_id)
    try:
        with transaction.atomic():
            event = OutboundEvent.objects.create(
                id=envelope["id"], event_type=event_type,
                idempotency_key=key, envelope=envelope,
            )
    except IntegrityError:   # lost a race with a concurrent emit
        return OutboundEvent.objects.get(idempotency_key=key)

    transaction.on_commit(lambda: deliver_event(event.id))
    return event


def dispatch_webhook(partner, event_type, payload):
    """
    DEPRECATED shim so old call sites (subscriptions, payments) keep working.
    Not idempotent. Migrate callers to emit().
    """
    if event_type not in CATALOGUE:
        log.warning("dispatch_webhook: unknown event %s (not in catalogue)", event_type)
        return None
    try:
        return emit(event_type, partner=partner, aggregate_id=partner.id,
                    data=payload, discriminator=uuid.uuid4().hex)
    except Exception:
        log.exception("dispatch_webhook failed for %s", event_type)
        return None


# ── Delivery ──────────────────────────────────────────────────────────────────

def _n8n_headers():
    return {"Authorization": f"Bearer {settings.N8N_OUTBOUND_TOKEN}"}


def _targets(event):
    """Yield (target_key, url, signing_secret, extra_headers)."""
    audiences = CATALOGUE[event.event_type].audiences

    if N8N in audiences:
        endpoints = ServiceWebhookEndpoint.objects.filter(
            event=event.event_type, is_active=True,
            service_account__is_active=True,      # revoked accounts get nothing
        )
        for ep in endpoints:
            yield f"n8n:{ep.id}", ep.url, settings.N8N_SIGNING_SECRET, _n8n_headers()

    if PARTNER in audiences:
        hooks = Webhook.objects.filter(
            partner_id=event.envelope["partner_id"], is_active=True,
            events__event=event.event_type,
        )
        for wh in hooks:
            if not wh.secret_encrypted:   # legacy row created before encryption
                log.warning("webhook %s has no signing secret; skipped", wh.id)
                continue
            yield f"partner:{wh.id}", wh.url, wh.secret, {}


def deliver_event(event_id):
    """Never raises: a delivery problem must not break the request that emitted."""
    try:
        event = OutboundEvent.objects.get(id=event_id)
        for target, url, secret, extra in _targets(event):
            d, _ = WebhookDelivery.objects.get_or_create(
                event=event, target=target, defaults={"url": url})
            if d.status != WebhookDelivery.DELIVERED:
                _attempt(d, secret, extra)
    except Exception:
        log.exception("deliver_event failed for %s", event_id)


def _attempt(d, secret, extra_headers):
    body = json.dumps(d.event.envelope, separators=(",", ":")).encode()
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-TheeInsurance-Event": d.event.event_type,
        "X-TheeInsurance-Event-Id": d.event.id,
        "X-TheeInsurance-Timestamp": str(ts),
        "X-TheeInsurance-Signature": sign(secret, ts, body),
        **extra_headers,
    }
    d.attempts += 1
    try:
        r = requests.post(d.url, data=body, headers=headers, timeout=10)
        d.last_status_code = r.status_code
        ok = 200 <= r.status_code < 300
        d.last_error = "" if ok else r.text[:500]
    except requests.RequestException as exc:
        ok, d.last_error = False, str(exc)[:500]

    if ok:
        d.status = WebhookDelivery.DELIVERED
        d.delivered_at, d.next_retry_at = timezone.now(), None
    elif d.attempts <= len(BACKOFF):
        d.next_retry_at = timezone.now() + timedelta(seconds=BACKOFF[d.attempts - 1])
    else:
        d.status, d.next_retry_at = WebhookDelivery.FAILED, None
        log.error("webhook delivery exhausted: %s -> %s", d.event_id, d.target)
    d.save()


def _secret_for(d):
    kind, ident = d.target.split(":", 1)
    if kind == "n8n":
        return settings.N8N_SIGNING_SECRET, _n8n_headers()
    return Webhook.objects.get(id=ident).secret, {}


def retry_due():
    """Run every minute from a worker/cron. Run ONE worker only."""
    due = WebhookDelivery.objects.filter(
        status=WebhookDelivery.PENDING, next_retry_at__lte=timezone.now()
    ).select_related("event")
    for d in due:
        try:
            secret, extra = _secret_for(d)
        except Webhook.DoesNotExist:
            d.status, d.last_error, d.next_retry_at = WebhookDelivery.FAILED, "webhook deleted", None
            d.save()
            continue
        _attempt(d, secret, extra)
