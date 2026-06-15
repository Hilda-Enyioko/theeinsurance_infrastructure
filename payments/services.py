"""
Payments Service Module.

Encapsulates all business logic for interacting with the Interswitch
Quickteller Pay gateway — initiation, verification, and webhook processing.
All views should remain thin and delegate to this module.
"""

import hashlib
import hmac
import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction as db_transaction

from .models import CallbackLog, Transaction

logger = logging.getLogger(__name__)


# Exceptions

class PaymentError(Exception):
    """Raised when payment initiation or verification fails."""
    pass


class SignatureVerificationError(Exception):
    """Raised when an incoming webhook signature cannot be verified."""
    pass


# Internal helpers

def _get_interswitch_headers() -> dict:
    """
    Build standard headers for all outbound Interswitch API calls.
    Client ID + HMAC-SHA512 signature are required by Quickteller Pay.
    """
    return {
        "Content-Type": "application/json",
        "clientid": settings.INTERSWITCH_CLIENT_ID,
        "Authorization": f"InterswitchAuth {settings.INTERSWITCH_CLIENT_ID}",
    }


def _kobo(amount: Decimal) -> int:
    """
    Convert Naira (Decimal) to kobo (int).
    Interswitch expects amounts in the lowest currency unit.
    """
    return int(amount * 100)


def _build_redirect_url(transaction: Transaction) -> str:
    """
    Construct the callback URL Interswitch will redirect the user to
    after payment attempt. Includes the internal reference for lookup.
    """
    base = settings.INTERSWITCH_REDIRECT_URL
    return f"{base}?ref={transaction.reference}"


# Signature verification

def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify that an incoming webhook was genuinely sent by Interswitch.
    Interswitch signs the raw request body with HMAC-SHA512 using your
    client secret as the key.
    """

    expected = hmac.new(
        key=settings.INTERSWITCH_CLIENT_SECRET.encode(),
        msg=payload_bytes,
        digestmod=hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise SignatureVerificationError(
            "Webhook signature mismatch — possible spoofed request."
        )

    return True


# Core service functions
def initiate_payment(transaction: Transaction) -> dict:
    """
    Initiate a Quickteller Pay redirect session for a given Transaction.

    Calls the Interswitch payment initiation endpoint and returns the
    payment URL that the frontend should redirect the user to.
    """
    
    payload = {
        "merchantCode": settings.INTERSWITCH_MERCHANT_CODE,
        "payableCode": settings.INTERSWITCH_PAYABLE_CODE,
        "amount": _kobo(transaction.amount),
        "transactionReference": transaction.reference,
        "currencyCode": "566",  # NGN ISO 4217 numeric
        "customerEmail": transaction.initiated_by.email,
        "customerName": transaction.initiated_by.get_full_name(),
        "redirectUrl": _build_redirect_url(transaction),
        "displayName": "TheeInsurance Portal",
    }

    try:
        response = requests.post(
            settings.INTERSWITCH_BASE_URL + "/api/v2/purchases",
            json=payload,
            headers=_get_interswitch_headers(),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        logger.error("Interswitch initiation timed out for ref: %s", transaction.reference)
        raise PaymentError("Payment gateway timed out. Please try again.")
    except requests.RequestException as e:
        logger.error("Interswitch initiation error for ref %s: %s", transaction.reference, str(e))
        raise PaymentError("Could not reach payment gateway.")

    payment_url = data.get("paymentUrl") or data.get("redirectUrl")
    if not payment_url:
        logger.error("No payment URL in Interswitch response: %s", data)
        raise PaymentError("Gateway did not return a payment URL.")

    logger.info("Payment initiated: ref=%s", transaction.reference)
    return {
        "payment_url": payment_url,
        "reference": transaction.reference,
    }


def verify_transaction(reference: str) -> dict:
    """
    Query Interswitch to verify the status of a transaction by reference.

    Called after redirect (user lands on callback URL) and optionally
    after receiving a webhook, as a second source of truth.
    """
    
    try:
        response = requests.get(
            settings.INTERSWITCH_BASE_URL + f"/api/v2/purchases/{reference}",
            headers=_get_interswitch_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.Timeout:
        logger.error("Interswitch verification timed out for ref: %s", reference)
        raise PaymentError("Verification timed out.")
    except requests.RequestException as e:
        logger.error("Interswitch verification error for ref %s: %s", reference, str(e))
        raise PaymentError("Could not verify transaction with gateway.")


def process_webhook(payload: dict, raw_body: bytes, signature: str) -> CallbackLog:
    """
    Process an incoming Interswitch webhook callback.

    Verifies the signature, checks for duplicates, validates the amount,
    updates Transaction and Subscription status, and creates an immutable
    CallbackLog — all within a single atomic transaction.
    """
    # 1. Verify signature first — reject spoofed requests immediately
    verify_webhook_signature(raw_body, signature)

    transaction_ref = payload.get("transactionReference", "")
    response_code = payload.get("responseCode", "")
    gateway_ref = payload.get("retrievalReferenceNumber", "")

    is_success = response_code == "00"  # Interswitch success code

    with db_transaction.atomic():
        # 2. Fetch the internal transaction
        try:
            txn = Transaction.objects.select_for_update().get(reference=transaction_ref)
        except Transaction.DoesNotExist:
            logger.warning("Webhook received for unknown reference: %s", transaction_ref)
            return CallbackLog.objects.create(
                transaction_reference=transaction_ref,
                raw_payload=payload,
                response_code=response_code,
                status=CallbackLog.Status.FLAGGED,
                transaction=None,
                is_duplicate=False,
                is_amount_mismatch=False,
            )

        # 3. Idempotency — reject duplicates
        is_duplicate = txn.payment_status != Transaction.PAYMENT_STATUS.PENDING
        
        # 4. Amount validation
        incoming_amount = Decimal(payload.get("amount", 0)) / 100  # kobo → naira
        is_amount_mismatch = incoming_amount != txn.amount

        # 5. Determine callback log status
        if is_duplicate:
            log_status = CallbackLog.Status.FLAGGED
        elif is_amount_mismatch:
            log_status = CallbackLog.Status.FLAGGED
        elif is_success:
            log_status = CallbackLog.Status.SUCCESS
        else:
            log_status = CallbackLog.Status.FAILED

        # 6. Update transaction only if it's still PENDING and amounts match
        if not is_duplicate and not is_amount_mismatch:
            txn.payment_status = (
                Transaction.PAYMENT_STATUS.SUCCESSFUL
                if is_success
                else Transaction.PAYMENT_STATUS.FAILED
            )
            txn.gateway_reference = gateway_ref
            txn.gateway_response = payload
            txn.save(update_fields=["payment_status", "gateway_reference", "gateway_response", "updated_at"])

            # 7. Update subscription status on success
            if is_success:
                _activate_subscription(txn)

        # 8. Create immutable audit log
        log = CallbackLog.objects.create(
            transaction_reference=transaction_ref,
            raw_payload=payload,
            response_code=response_code,
            status=log_status,
            transaction=txn,
            is_duplicate=is_duplicate,
            is_amount_mismatch=is_amount_mismatch,
        )

    # 9. Fire n8n webhook outside the atomic block — DB is committed by here
    if is_success and not is_duplicate and not is_amount_mismatch:
        _fire_n8n_webhook(txn)

    return log


# Side-effect helpers (called from process_webhook)

def _activate_subscription(txn: Transaction) -> None:
    """
    Activate or renew the PolicySubscription linked to a successful transaction.
    Import is local to avoid circular imports with the subscriptions app.
    """

    try:
        sub = txn.subscription
        sub.status = 'active'
        sub.payment_verified = True
        sub.payment_reference = txn.gateway_reference or txn.reference
        sub.save(update_fields=["status", "payment_verified", "payment_reference", "updated_at"])
        logger.info("Subscription %s activated for txn %s", sub.id, txn.reference)

    except Exception as e:
        logger.error("Failed to activate subscription for txn %s: %s", txn.reference, str(e))


def _fire_n8n_webhook(txn: Transaction) -> None:
    """
    Notify n8n of a successful payment to trigger automation tracks:
      Track A — customer notification
      Track B — AI plan recommendation
      Track D — provider analytics
    """
    payload = {
        "event": "payment.successful",
        "transaction_reference": txn.reference,
        "subscription_id": str(txn.subscription.id),
        "amount": str(txn.amount),
        "currency": txn.currency,
        "initiated_by": txn.initiated_by.email if txn.initiated_by else None,
        "payment_type": txn.payment_type,
    }

    try:
        response = requests.post(
            settings.N8N_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("n8n webhook fired for txn %s", txn.reference)
    except requests.RequestException as e:
        # Non-fatal — n8n failure should never block the payment confirmation
        logger.error("n8n webhook failed for txn %s: %s", txn.reference, str(e))
