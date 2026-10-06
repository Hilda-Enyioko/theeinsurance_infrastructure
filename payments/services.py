"""Payments via Paystack. All business logic lives here; views stay thin."""

import hashlib
import hmac
import logging
from datetime import timedelta
from decimal import Decimal

import requests
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from accounts import emails
from subscriptions.models import PolicySubscription
from subscriptions.rules import RENEWABLE_STATUSES, can_renew
from webhooks.services import dispatch_webhook

from .models import CallbackLog, Transaction

log = logging.getLogger(__name__)

PENDING_TTL = timedelta(minutes=30)


class PaymentError(Exception):
    """Base class."""


class NotPayable(PaymentError):
    """Business-rule failure (HTTP 400/409)."""


class GatewayError(PaymentError):
    """Paystack unreachable or rejected the request (HTTP 502)."""


class SignatureVerificationError(Exception):
    pass


# Paystack client
def _kobo(amount) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1")))


def _headers():
    return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}


def verify_webhook_signature(raw_body: bytes, signature: str) -> None:
    """Paystack signs the RAW body with HMAC-SHA512 using your secret key (x-paystack-signature)."""
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise SignatureVerificationError("Webhook signature mismatch.")


def _call(method, path, **kwargs):
    try:
        resp = requests.request(method, f"{settings.PAYSTACK_BASE_URL}{path}",
                                headers=_headers(), timeout=30, **kwargs)
        resp.raise_for_status()
        body = resp.json()
    except requests.Timeout:
        raise GatewayError("Payment gateway timed out. Please try again.")
    except requests.RequestException:
        log.exception("Paystack %s %s failed", method, path)
        raise GatewayError("Could not reach payment gateway.")
    if not body.get("status") or "data" not in body:
        raise GatewayError(body.get("message") or "Gateway rejected the request.")
    return body["data"]


def paystack_initialize(txn: Transaction, email: str, metadata: dict) -> dict:
    return _call("POST", "/transaction/initialize", json={
        "email": email,
        "amount": _kobo(txn.amount),
        "currency": txn.currency,
        "reference": txn.reference,
        "callback_url": settings.PAYSTACK_CALLBACK_URL,
        "metadata": metadata,
    })


def paystack_verify(reference: str) -> dict:
    return _call("GET", f"/transaction/verify/{reference}")


# initiate
def _checkout_payload(txn: Transaction) -> dict:
    return {
        "payment_url": txn.authorization_url,
        "reference": txn.reference,
        "amount": str(txn.amount),
        "currency": txn.currency,
        "subscription_id": str(txn.subscription_id),
        "payment_type": txn.payment_type,
    }


def initiate_checkout(sub: PolicySubscription, payment_type: str, user) -> dict:
    """Create (or reuse) a PENDING transaction and return the Paystack checkout URL."""
    if payment_type == Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION:
        if sub.status != "pending_payment":
            raise NotPayable(f"Subscription is '{sub.status}'. Only 'pending_payment' subscriptions can be paid.")
    elif not can_renew(sub):
        raise NotPayable(f"Subscription is '{sub.status}' and is not eligible for renewal yet.")

    with db_transaction.atomic():
        locked = PolicySubscription.objects.select_for_update().get(pk=sub.pk)
        txn = Transaction.objects.filter(
            subscription=locked, payment_type=payment_type,
            payment_status=Transaction.PAYMENT_STATUS.PENDING).first()
        if txn:
            fresh = timezone.now() - txn.created_at < PENDING_TTL
            if fresh and txn.authorization_url:
                return _checkout_payload(txn)
            if fresh:
                raise NotPayable("A payment is already being set up. Try again in a moment.")
            txn.payment_status = Transaction.PAYMENT_STATUS.FAILED   # stale; Paystack can still flip it later
            txn.save(update_fields=["payment_status", "updated_at"])
        txn = Transaction.objects.create(
            subscription=locked, initiated_by=user, amount=locked.amount_paid, currency="NGN",
            payment_type=payment_type, gateway=Transaction.GATEWAY.PAYSTACK)

    try:
        data = paystack_initialize(txn, user.email, {
            "subscription_id": str(sub.id), "payment_type": payment_type, "plan": sub.plan.name})
    except GatewayError:
        txn.payment_status = Transaction.PAYMENT_STATUS.FAILED
        txn.save(update_fields=["payment_status", "updated_at"])
        raise

    txn.authorization_url = data["authorization_url"]
    txn.access_code = data.get("access_code", "")
    txn.save(update_fields=["authorization_url", "access_code", "updated_at"])
    return _checkout_payload(txn)


#  settle (webhook + verify both land here)
def _log(reference, payload, status, txn, *, duplicate=False, mismatch=False):
    CallbackLog.objects.get_or_create(
        transaction_reference=reference,
        response_code=str(payload.get("status", ""))[:20],
        status=status,
        defaults=dict(raw_payload=payload, transaction=txn, gateway=CallbackLog.Gateway.PAYSTACK,
                      is_duplicate=duplicate, is_amount_mismatch=mismatch),
    )


def _apply_success(txn: Transaction, sub: PolicySubscription) -> bool:
    today = timezone.localdate()
    if txn.payment_type == Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION:
        if sub.status != "pending_payment":
            log.warning("Paid txn %s but subscription %s is '%s'. Manual refund review needed.",
                        txn.reference, sub.id, sub.status)
            return False
        sub.payment_verified = True
    else:
        if sub.status not in RENEWABLE_STATUSES:
            log.warning("Paid renewal %s but subscription %s is '%s'. Manual refund review needed.",
                        txn.reference, sub.id, sub.status)
            return False
        months = relativedelta(months=sub.plan.duration_months)
        if sub.end_date >= today:
            sub.end_date = sub.end_date + months
        else:
            sub.start_date, sub.end_date = today, today + months
    sub.status = "active"
    sub.payment_reference = txn.reference
    sub.save()
    return True


def _fire_partner_webhooks(sub, event, payload):
    def _go():
        try:
            dispatch_webhook(sub.provider, event, payload)
            if sub.distributor:
                dispatch_webhook(sub.distributor, event, payload)
        except Exception:
            log.exception("Partner webhook '%s' failed for subscription %s", event, sub.id)
    db_transaction.on_commit(_go)


@db_transaction.atomic
def settle(reference: str, data: dict, source: str):
    """
    Apply a Paystack transaction result to our ledger. `data` is Paystack's `data` object
    (same shape from the charge.success webhook and from /transaction/verify).
    Idempotent: safe to call repeatedly from either path.
    """
    txn = Transaction.objects.select_for_update(of=("self",)).filter(reference=reference).first()
    if txn is None:
        log.warning("Paystack %s for unknown reference %s", source, reference)
        _log(reference, data, CallbackLog.Status.FLAGGED, None)
        return None

    gw_status = data.get("status")

    if txn.payment_status == Transaction.PAYMENT_STATUS.SUCCESSFUL:
        _log(reference, data, CallbackLog.Status.FLAGGED, txn, duplicate=True)
        return txn

    if gw_status == "success":
        amount_ok = int(data.get("amount") or -1) == _kobo(txn.amount)
        if not amount_ok or data.get("currency", txn.currency) != txn.currency:
            log.error("Amount/currency mismatch on %s: got %s %s", reference, data.get("amount"), data.get("currency"))
            _log(reference, data, CallbackLog.Status.FLAGGED, txn, mismatch=True)
            return txn

        txn.payment_status = Transaction.PAYMENT_STATUS.SUCCESSFUL   # also rescues a txn we had marked FAILED
        txn.gateway_reference = str(data.get("id", ""))[:100]
        txn.gateway_response = data
        txn.save(update_fields=["payment_status", "gateway_reference", "gateway_response", "updated_at"])

        sub = (PolicySubscription.objects.select_for_update(of=("self",))
               .select_related("plan", "provider", "distributor", "customer__user")
               .get(pk=txn.subscription_id))
        applied = _apply_success(txn, sub)
        _log(reference, data, CallbackLog.Status.SUCCESS if applied else CallbackLog.Status.FLAGGED, txn)

        if applied:
            renewal = txn.payment_type == Transaction.PAYMENT_TYPE.RENEWAL
            _fire_partner_webhooks(sub, "subscription.renewed" if renewal else "subscription.created", {
                "subscription_id": str(sub.id), "plan": sub.plan.name,
                "customer_email": sub.customer.user.email,
                "start_date": str(sub.start_date), "end_date": str(sub.end_date),
                "amount_paid": str(sub.amount_paid), "status": "active",
                "transaction_reference": txn.reference,
            })
            emails.on_commit_send(emails.send_payment_emails, txn.pk)
        return txn

    if gw_status in ("failed", "reversed") and txn.payment_status == Transaction.PAYMENT_STATUS.PENDING:
        txn.payment_status = Transaction.PAYMENT_STATUS.FAILED
        txn.gateway_response = data
        txn.save(update_fields=["payment_status", "gateway_response", "updated_at"])
        _log(reference, data, CallbackLog.Status.FAILED, txn)
    # "abandoned" / "ongoing" / "pending": leave PENDING; the customer can still finish or retry
    return txn
