"""
Payments Service Module.

Encapsulates all business logic for interacting with the Interswitch
Quickteller Pay gateway — initiation, verification, and webhook processing.
All views should remain thin and delegate to this module.
"""

import hashlib
import hmac
import base64
import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone
from django.db.models import Q
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import CallbackLog, Transaction
from core.nomba_auth import get_nomba_token, NombaAuthError    # noqa: E402
from core.models import ServiceWebhookEndpoint
from subscriptions.models import NombaTokenStore

logger = logging.getLogger(__name__)


# Exceptions
# ----------------------------------------------------------------------------------
class PaymentError(Exception):
    """Raised when payment initiation or verification fails."""
    pass


class SignatureVerificationError(Exception):
    """Raised when an incoming webhook signature cannot be verified."""
    pass

# ------------------------------------------------------------------------------------

# Internal helpers
# ------------------------------------------------------------------------------------
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

# Get N8N Service Webhook URL
def _get_service_webhook_url(event: str) -> str | None:
    endpoint = ServiceWebhookEndpoint.objects.filter(event=event, is_active=True).first()
    return endpoint.url if endpoint else None

# Check Nomba gateway eligibility
@extend_schema_field(serializers.BooleanField())
def nomba_payment_available(sub) -> bool:
    """
    Informational only — no longer used to gate whether Nomba can be used
    as the payment gateway (see _build_nomba_split for the actual behavior
    when a provider/distributor Nomba account is missing).

    True only if every party that needs a payout for this subscription
    (the provider, and the distributor if one is involved) has a Nomba
    sub-account on file. Kept around for the frontend to optionally show
    "split payout will apply" vs. "full amount settles to platform"
    messaging — it no longer blocks checkout or renewal.
    """
    if not sub.provider.nomba_account_id:
        return False
    if sub.distributor and not sub.distributor.nomba_account_id:
        return False
    return True

# --------------------------------------------------------------------------------------

# Signature verification
# ---------------------------------------------------------------------------------------
def verify_interswitch_signature(payload_bytes: bytes, signature_header: str) -> bool:
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

def verify_nomba_signature(payload: dict, timestamp: str, signature_header: str) -> bool:
    """
    Verify that an incoming webhook was genuinely sent by Nomba.

    Unlike Interswitch, Nomba does not sign the raw request body. It builds
    a colon-delimited string from specific payload fields plus the
    `nomba-timestamp` header value, HMAC-SHA256's that string with your
    signature key, and base64-encodes the digest. Compare against the
    `nomba-signature` header.
    """
    data = payload.get("data", {})
    merchant = data.get("merchant", {})
    transaction = data.get("transaction", {})

    response_code = transaction.get("responseCode", "")
    if response_code == "null":
        response_code = ""

    hashing_payload = ":".join([
        payload.get("event_type", ""),
        payload.get("requestId", ""),
        merchant.get("userId", ""),
        merchant.get("walletId", ""),
        transaction.get("transactionId", ""),
        transaction.get("type", ""),
        transaction.get("time", ""),
        response_code,
        timestamp,
    ])

    mac = hmac.HMAC(
        key=settings.NOMBA_SIGNATURE_KEY.encode(),
        msg=hashing_payload.encode(),
        digestmod=hashlib.sha256,
    )
    expected = base64.b64encode(mac.digest()).decode()

    if not hmac.compare_digest(expected, signature_header):
        raise SignatureVerificationError(
            "Webhook signature mismatch — possible spoofed request."
        )

    return True

# ----------------------------------------------------------------------------------------


# Store Tokenised Cards
# -----------------------------------------------------------------------------------
def _store_nomba_token(txn: Transaction, token_key: str) -> NombaTokenStore | None:
    """
    Persist Nomba card tokenization details for recurring charges.

    Validates that the customer explicitly consented to recurring charges
    via the associated PolicySubscription before saving to the database.
    """
    try:
        sub = txn.subscription
    except AttributeError:
        logger.error("Transaction %s is not linked to a subscription.", txn.reference)
        return None

    # Consent check
    if not sub.auto_charge_enabled:
        logger.warning(
            "Nomba token key received for transaction %s, but subscription %s "
            "does not have auto-charge enabled. Token discarded.",
            txn.reference, sub.id
        )
        return None

    # Parse details out of the saved gateway payload safely if available
    gateway_response = txn.gateway_response or {}
    response_data = gateway_response.get("data", {})
    transaction_data = response_data.get("transaction", {})

    card_type = response_data.get("cardType") or transaction_data.get("cardType", "")
    card_pan = response_data.get("bin") or transaction_data.get("pan", "")

    # Create or update the token storage record
    token_record, created = NombaTokenStore.objects.update_or_create(
        policy=sub,
        defaults={
            "token_key": token_key,
            "card_type": card_type,
            "card_pan": card_pan,
            "customer_consented_to_auto_charge": True,
            "consent_recorded_at": timezone.now(),
            "nomba_order_reference": txn.gateway_reference or "",
        }
    )

    logger.info(
        "Successfully stored Nomba token for subscription %s (Created: %s)",
        sub.id, created
    )
    return token_record
# -----------------------------------------------------------------------------------


# Payout Automated Splits
# -----------------------------------------------------------------------------------
def _build_nomba_split(sub, total_amount: Decimal) -> dict | None:
    """
    Build a Nomba splitRequest so TheeInsurance's own sub-account only ever
    receives its platform fee — the provider (and distributor, if involved)
    are paid out directly via the same transaction.

    - Direct subscription (no distributor): 2-way split — platform, provider.
    - Distributor-originated subscription:   3-way split — platform, distributor, provider.

    If the provider (or distributor, when one is involved) has no Nomba
    sub-account on file, there is no valid payee to split to. Rather than
    blocking the charge, we return None so the caller omits splitRequest
    entirely — Nomba then settles the full amount into TheeInsurance's own
    platform account (settings.NOMBA_SUB_ACCOUNT_ID). This is a deliberate
    fallback rather than a fallback to the Interswitch gateway.
    """
    provider_account = sub.provider.nomba_account_id
    distributor_account = sub.distributor.nomba_account_id if sub.distributor else None

    has_provider = bool(provider_account)
    has_distributor = (not sub.distributor) or bool(distributor_account)

    if not has_provider or not has_distributor:
        logger.info(
            "No complete Nomba split available for subscription %s "
            "(provider_account_present=%s, distributor_account_present=%s) — "
            "full amount will settle to the platform account, no split applied.",
            sub.id, has_provider, has_distributor,
        )
        return None

    platform_account = settings.NOMBA_SUB_ACCOUNT_ID
    platform_cut = sub.platform_fee
    distributor_cut = sub.distributor_commission  # Decimal('0.00') when no distributor
    provider_cut = sub.provider_payout

    if (platform_cut + distributor_cut + provider_cut) != total_amount:
        raise PaymentError(
            f"Split amounts don't sum to the charge amount for subscription {sub.id}."
        )

    split_list = [
        {"accountId": platform_account, "value": f"{platform_cut:.2f}"},
        {"accountId": provider_account, "value": f"{provider_cut:.2f}"},
    ]
    if sub.distributor:
        split_list.append(
            {"accountId": distributor_account, "value": f"{distributor_cut:.2f}"}
        )

    return {
        "splitType": "AMOUNT",
        "splitList": split_list,
    }

# -----------------------------------------------------------------------------------

# Process Success Payments
# -----------------------------------------------------------------------------------
def _process_successful_payment(txn, gateway_ref, payload, gateway):
    txn.payment_status = Transaction.PAYMENT_STATUS.SUCCESSFUL
    txn.gateway_reference = gateway_ref
    txn.gateway_response = payload
    txn.save(update_fields=[
        "payment_status",
        "gateway_reference",
        "gateway_response",
        "updated_at"
    ])

    _activate_subscription(txn)

    log = CallbackLog.objects.create(
        transaction_reference=txn.reference,
        raw_payload=payload,
        response_code=payload.get("responseCode", ""),
        status=CallbackLog.Status.SUCCESS,
        transaction=txn,
        gateway=gateway,
        is_duplicate=False,
        is_amount_mismatch=False,
    )

    db_transaction.on_commit(lambda: _fire_n8n_payment_webhook(txn))

    return log
# --------------------------------------------------------------------------------------

# Initiate Payment and Checkout
# ---------------------------------------------------------------------------------------
def initiate_payment(transaction: Transaction) -> dict:
    """
    Initiate a Quickteller Pay redirect session for a given Transaction.

    Calls the Interswitch payment initiation endpoint and returns the
    payment URL that the frontend should redirect the user to.
    """

    user = transaction.initiated_by
    customer_name = f"{user.first_name} {user.last_name}".strip()

    payload = {
        "merchantCode": settings.INTERSWITCH_MERCHANT_CODE,
        "payableCode": settings.INTERSWITCH_PAYABLE_CODE,
        "amount": _kobo(transaction.amount),
        "transactionReference": transaction.reference,
        "currencyCode": "566",  # NGN ISO 4217 numeric
        "customerEmail": transaction.initiated_by.email,
        "customerName": customer_name,
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


# Nomba Checkout

def initiate_nomba_checkout(subscription_id: str, customer_consented: bool) -> dict:
    """
    Create a Nomba checkout order for a policy subscription.

    Called by the partner's backend after the customer has confirmed they
    want automated renewal charges. The partner passes consent explicitly;
    we refuse to tokenize without it.

    Nomba is used regardless of whether the provider/distributor has a
    Nomba sub-account on file — _build_nomba_split() below decides whether
    to attach a splitRequest or let the full amount settle to TheeInsurance's
    own platform account. There is no fallback to Interswitch here.

    Returns checkoutLink and orderReference for the partner to hand to
    their frontend.
    """
    from subscriptions.models import PolicySubscription

    # 1. Load and validate the subscription
    try:
        sub = PolicySubscription.objects.select_related(
            'plan', 'customer'
        ).get(id=subscription_id)
    except PolicySubscription.DoesNotExist:
        raise PaymentError(f"Subscription {subscription_id} not found.")

    if sub.status != 'pending_payment':
        raise PaymentError(
            f"Subscription is '{sub.status}'. "
            "Only subscriptions in 'pending_payment' status can be checked out."
        )

    # 2. Consent gate — never tokenize without explicit customer consent
    if not customer_consented:
        raise PaymentError(
            "Customer consent to automated charges is required to proceed."
        )

    # 3. Create an internal Transaction record before calling Nomba
    with db_transaction.atomic():
        txn = Transaction.objects.create(
            amount=sub.plan.premium,
            currency='NGN',
            initiated_by=sub.customer.user,
            subscription=sub,
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
            gateway=Transaction.GATEWAY.NOMBA,
        )

    # 4. Build the Nomba checkout order payload
    order_payload = {
        "order": {
            "orderReference":   txn.reference,
            "customerId":       str(sub.customer.user.id),
            "customerEmail":    sub.customer.user.email,
            "amount":           str(sub.plan.premium),
            "currency":         "NGN",
            "accountId":        settings.NOMBA_SUB_ACCOUNT_ID,
            "callbackUrl":      settings.NOMBA_CALLBACK_URL,
            "orderMetaData": {
                "subscriptionId": str(sub.id),
                "policyNumber":   str(sub.id),
            },
        },
        "tokenizeCard": True,
    }

    split = _build_nomba_split(sub, sub.plan.premium)
    if split is not None:
        order_payload["order"]["splitRequest"] = split
    # else: no splitRequest key at all — full amount stays with our
    # platform sub-account (settings.NOMBA_SUB_ACCOUNT_ID).

    # 5. Call Nomba
    try:
        token = get_nomba_token()
        response = requests.post(
            f"{settings.NOMBA_BASE_URL}/checkout/order",
            json=order_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "accountId":     settings.NOMBA_ACCOUNT_ID,
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except NombaAuthError as e:
        logger.error("Nomba auth failed during checkout for txn %s: %s", txn.reference, str(e))
        raise PaymentError("Could not authenticate with payment gateway.")
    except requests.Timeout:
        logger.error("Nomba checkout timed out for txn %s", txn.reference)
        raise PaymentError("Payment gateway timed out. Please try again.")
    except requests.RequestException as e:
        logger.error("Nomba checkout request failed for txn %s: %s", txn.reference, str(e))
        raise PaymentError("Could not reach payment gateway.")

    if data.get("code") != "00":
        logger.error("Nomba checkout non-00 for txn %s: %s", txn.reference, data)
        raise PaymentError(f"Gateway error: {data.get('description', 'unknown')}")

    nomba_data = data["data"]
    checkout_link     = nomba_data["checkoutLink"]
    order_reference   = nomba_data["orderReference"]

    # 6. Store the Nomba order reference on the transaction for reconciliation
    txn.gateway_reference = order_reference
    txn.save(update_fields=["gateway_reference", "updated_at"])

    logger.info(
        "Nomba checkout created: txn=%s order_reference=%s",
        txn.reference, order_reference
    )

    return {
        "checkout_link":     checkout_link,
        "order_reference":   order_reference,
        "transaction_ref":   txn.reference,
        "amount":            str(sub.plan.premium),
        "currency":          "NGN",
    }

# -------------------------------------------------------------------------------------

# -------------------------------------------------------------------------------------
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



def verify_nomba_transaction(order_reference: str) -> dict:
    """
    Query Nomba to verify the status of a checkout transaction by orderReference.

    Uses /v1/transactions/accounts/single, which works in both sandbox and
    production (unlike /v1/checkout/transaction, which is production-only
    and has a different response shape with no top-level `status` field).

    Nomba returns HTTP 200 even for "transaction not found" (code: "01",
    data: null) rather than a 4xx/5xx — raise_for_status() alone won't catch
    that, so we check `code` explicitly and raise PaymentError with the
    actual description instead of silently returning an empty data dict.

    Called from NombaCallbackView when the customer lands on the redirect
    before the webhook has landed, as a second source of truth — mirrors
    verify_transaction() for Interswitch.
    """
    try:
        token = get_nomba_token()
        response = requests.get(
            f"{settings.NOMBA_BASE_URL}/transactions/accounts/single",
            params={"orderReference": order_reference},
            headers={
                "Authorization": f"Bearer {token}",
                "accountId": settings.NOMBA_ACCOUNT_ID,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
    except NombaAuthError as e:
        logger.error("Nomba auth failed during verification for order_ref %s: %s", order_reference, str(e))
        raise PaymentError("Could not authenticate with payment gateway.")
    except requests.Timeout:
        logger.error("Nomba verification timed out for order_ref: %s", order_reference)
        raise PaymentError("Verification timed out.")
    except requests.RequestException as e:
        logger.error("Nomba verification error for order_ref %s: %s", order_reference, str(e))
        raise PaymentError("Could not verify transaction with gateway.")

    if result.get("code") != "00":
        description = result.get("description", "Unknown error")
        logger.warning(
            "Nomba verification non-00 for order_ref %s: code=%s description=%s",
            order_reference, result.get("code"), description,
        )
        raise PaymentError(f"Verification failed: {description}")

    return result

# --------------------------------------------------------------------------------------


# Webhook Processing Functions
# --------------------------------------------------------------------------------------
def process_interswitch_webhook(payload: dict, raw_body: bytes, signature: str) -> CallbackLog:
    """
    Process an incoming Interswitch Transaction webhook callback.

    Verifies the signature, checks for duplicates, validates the amount,
    and — on a clean success — delegates to _process_successful_payment.
    Non-success / duplicate / mismatched callbacks are still logged for
    audit purposes but don't touch the Transaction or fire n8n.
    """
    verify_interswitch_signature(raw_body, signature)

    transaction_ref = payload.get("transactionReference", "")
    response_code = payload.get("responseCode", "")
    gateway_ref = payload.get("retrievalReferenceNumber", "")
    is_success = response_code == "00"  # Interswitch success code

    with db_transaction.atomic():
        try:
            txn = Transaction.objects.select_for_update().get(reference=transaction_ref)
        except Transaction.DoesNotExist:
            logger.warning("Interswitch webhook received for unknown reference: %s", transaction_ref)
            return CallbackLog.objects.create(
                transaction_reference=transaction_ref,
                raw_payload=payload,
                response_code=response_code,
                status=CallbackLog.Status.FLAGGED,
                transaction=None,
                gateway="interswitch",
                is_duplicate=False,
                is_amount_mismatch=False,
            )

        is_duplicate = txn.payment_status != Transaction.PAYMENT_STATUS.PENDING

        incoming_amount = Decimal(str(payload.get("amount", 0))) / 100
        is_amount_mismatch = incoming_amount != txn.amount

        if is_duplicate or is_amount_mismatch:
            return CallbackLog.objects.create(
                transaction_reference=transaction_ref,
                raw_payload=payload,
                response_code=response_code,
                status=CallbackLog.Status.FLAGGED,
                transaction=txn,
                gateway="interswitch",
                is_duplicate=is_duplicate,
                is_amount_mismatch=is_amount_mismatch,
            )

        if is_success:
            return _process_successful_payment(txn, gateway_ref, payload, gateway="interswitch")

        # Clean failure — not a duplicate, not a mismatch, just declined
        txn.payment_status = Transaction.PAYMENT_STATUS.FAILED
        txn.gateway_reference = gateway_ref
        txn.gateway_response = payload
        txn.save(update_fields=["payment_status", "gateway_reference", "gateway_response", "updated_at"])

        return CallbackLog.objects.create(
            transaction_reference=transaction_ref,
            raw_payload=payload,
            response_code=response_code,
            status=CallbackLog.Status.FAILED,
            transaction=txn,
            gateway="interswitch",
            is_duplicate=False,
            is_amount_mismatch=False,
        )


def process_nomba_webhook(payload: dict, signature: str) -> CallbackLog:
    """
    Process an incoming Nomba Transaction webhook callback.

    Verifies the signature (Nomba's composite-string HMAC, not raw body),
    checks for duplicates, and — on success — delegates to
    _process_successful_payment, then persists the Nomba tokenKey for
    future automated charges.

    Transaction lookup uses data.order, not data.transaction:
      - data.order.orderReference = OUR merchant reference (Transaction.reference)
      - data.order.orderId        = NOMBA's own ID (Transaction.gateway_reference)
    Same naming convention Nomba uses on the checkout redirect. data.transaction.merchantTxRef
    is unreliable (empty/truncated in practice) and should not be used for lookup.

    Note: no amount-mismatch check here — Nomba's checkout flow doesn't
    carry the same amount-tampering surface as Interswitch's redirect flow.
    Revisit if that assumption changes.
    """
    timestamp = payload.get("_nomba_timestamp", "")
    verify_nomba_signature(payload, timestamp, signature)

    data = payload.get("data", {})
    transaction = data.get("transaction", {})
    order = data.get("order", {})

    order_reference = order.get("orderReference", "")
    order_id = order.get("orderId", "")
    response_code = transaction.get("responseCode", "")
    gateway_ref = transaction.get("transactionId", "")
    is_success = payload.get("event_type") == "payment_success"

    with db_transaction.atomic():
        txn = (
            Transaction.objects.select_for_update()
            .filter(Q(reference=order_reference) | Q(gateway_reference=order_id))
            .first()
        )

        if not txn:
            logger.warning(
                "Nomba webhook received for unknown order (orderReference=%s, orderId=%s)",
                order_reference, order_id,
            )
            return CallbackLog.objects.create(
                transaction_reference=order_reference or order_id,
                raw_payload=payload,
                response_code=response_code,
                status=CallbackLog.Status.FLAGGED,
                transaction=None,
                gateway="nomba",
                is_duplicate=False,
                is_amount_mismatch=False,
            )

        is_duplicate = txn.payment_status != Transaction.PAYMENT_STATUS.PENDING

        if is_duplicate:
            return CallbackLog.objects.create(
                transaction_reference=txn.reference,
                raw_payload=payload,
                response_code=response_code,
                status=CallbackLog.Status.FLAGGED,
                transaction=txn,
                gateway="nomba",
                is_duplicate=True,
                is_amount_mismatch=False,
            )

        if is_success:
            log = _process_successful_payment(txn, gateway_ref, payload, gateway="nomba")

            token_key = data.get("tokenizedCardData", {}).get("tokenKey") or data.get("tokenKey")
            if token_key and token_key != "N/A":
                _store_nomba_token(txn, token_key)

            return log

        # Failure / reversal event
        txn.payment_status = Transaction.PAYMENT_STATUS.FAILED
        txn.gateway_reference = gateway_ref
        txn.gateway_response = payload
        txn.save(update_fields=["payment_status", "gateway_reference", "gateway_response", "updated_at"])

        return CallbackLog.objects.create(
            transaction_reference=txn.reference,
            raw_payload=payload,
            response_code=response_code,
            status=CallbackLog.Status.FAILED,
            transaction=txn,
            gateway="nomba",
            is_duplicate=False,
            is_amount_mismatch=False,
        )

# ---------------------------------------------------------------------------------------


# Policy Renewal Engine
# -------------------------------------------------------------------------------------

def charge_policy_renewal(subscription_id: str) -> dict:
    """
    Charge a policy renewal using a stored Nomba tokenized card.

    Called by:
      - The renewal scheduler (Django management command or Celery beat)
      - n8n via the service account endpoint on scheduled renewal dates
      - Dunning retry attempts (n8n calls this again on day 1, 3, 7)

    Flow:
      1. Load the PolicySubscription and its NombaTokenStore
      2. Guard against double-charging (idempotency at the DB level)
      3. Create a new RENEWAL Transaction record
      4. POST to Nomba /v1/checkout/tokenized-card-payment
      5. On success  → activate/renew subscription, fire n8n payment.successful
      6. On failure  → mark transaction FAILED, fire n8n charge.failed for dunning

    Nomba is used regardless of whether the provider/distributor has a
    Nomba sub-account on file — _build_nomba_split() decides whether to
    attach a splitRequest or let the full amount settle to TheeInsurance's
    own platform account. There is no fallback to Interswitch here.

    Returns a dict with outcome details for the caller (n8n or scheduler).
    """
    from subscriptions.models import PolicySubscription, NombaTokenStore

    # 1. Load subscription
    try:
        sub = PolicySubscription.objects.select_related(
            'plan', 'customer'
        ).get(id=subscription_id)
    except PolicySubscription.DoesNotExist:
        raise PaymentError(f"Subscription {subscription_id} not found.")

    # 2. Guard: only charge active or grace-period subscriptions
    if sub.status not in ('active', 'grace_period'):
        raise PaymentError(
            f"Subscription {subscription_id} is '{sub.status}'. "
            "Only active or grace_period subscriptions can be renewed."
        )

    # 3. Guard: skip if auto-charge is disabled (customer revoked consent)
    if not sub.auto_charge_enabled:
        raise PaymentError(
            f"Auto-charge is disabled for subscription {subscription_id}. "
            "Skipping renewal charge."
        )

    # 4. Load the stored token
    try:
        token_store = NombaTokenStore.objects.get(policy=sub)
    except NombaTokenStore.DoesNotExist:
        raise PaymentError(
            f"No stored Nomba token for subscription {subscription_id}. "
            "Customer must complete a checkout to enable auto-renewal."
        )

    # 5. Idempotency guard at DB level:
    #    If a PENDING renewal transaction already exists for this subscription,
    #    a previous attempt is still in flight — don't fire a second charge.
    in_flight = Transaction.objects.filter(
        subscription=sub,
        payment_type=Transaction.PAYMENT_TYPE.RENEWAL,
        payment_status=Transaction.PAYMENT_STATUS.PENDING,
        gateway=Transaction.GATEWAY.NOMBA,
    ).exists()

    if in_flight:
        logger.warning(
            "Renewal charge skipped for subscription %s — a PENDING renewal "
            "transaction already exists. Possible duplicate trigger.",
            subscription_id,
        )
        raise PaymentError(
            f"A renewal charge is already in progress for subscription {subscription_id}."
        )

    # 6. Create the renewal Transaction record before calling Nomba
    #    we have an audit trail even if the network call fails.
    with db_transaction.atomic():
        txn = Transaction.objects.create(
            amount=sub.plan.premium,
            currency='NGN',
            initiated_by=sub.customer.user,
            subscription=sub,
            payment_type=Transaction.PAYMENT_TYPE.RENEWAL,
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
            gateway=Transaction.GATEWAY.NOMBA,
        )

    # 7. Build Nomba tokenized charge payload
    charge_payload = {
        "order": {
            "orderReference": txn.reference,
            "customerEmail":  sub.customer.email,
            "amount":         str(sub.plan.premium),
            "currency":       "NGN",
            "accountId":      settings.NOMBA_SUB_ACCOUNT_ID,
            "callbackUrl":    settings.NOMBA_CALLBACK_URL,
            "orderMetaData": {
                "subscriptionId": str(sub.id),
                "chargeType":     "AUTO_RENEWAL",
            },
        },
        "tokenKey": token_store.token_key,
    }

    split = _build_nomba_split(sub, sub.plan.premium)
    if split is not None:
        charge_payload["order"]["splitRequest"] = split
    # else: no splitRequest key at all — full amount stays with our
    # platform sub-account (settings.NOMBA_SUB_ACCOUNT_ID).

    # 8. Call Nomba
    try:
        access_token = get_nomba_token()
        response = requests.post(
            f"{settings.NOMBA_BASE_URL}/checkout/tokenized-card-payment",
            json=charge_payload,
            headers={
                "Authorization":    f"Bearer {access_token}",
                "accountId":        settings.NOMBA_ACCOUNT_ID,
                "Content-Type":     "application/json",
                "X-Idempotency-Key": txn.reference,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

    except NombaAuthError as e:
        logger.error("Nomba auth failed during renewal for txn %s: %s", txn.reference, str(e))
        _mark_renewal_failed(txn, error="auth_failure")
        raise PaymentError("Could not authenticate with payment gateway.")

    except requests.Timeout:
        logger.error("Nomba renewal charge timed out for txn %s", txn.reference)
        _mark_renewal_failed(txn, error="timeout")
        raise PaymentError("Payment gateway timed out.")

    except requests.RequestException as e:
        logger.error("Nomba renewal request failed for txn %s: %s", txn.reference, str(e))
        _mark_renewal_failed(txn, error=str(e))
        raise PaymentError("Could not reach payment gateway.")

    # 9. Handle Nomba response
    response_code = data.get("code")
    nomba_data    = data.get("data", {})
    charge_status = nomba_data.get("status")

    if response_code == "00" and charge_status is True:
        # Success — activate the subscription and notify n8n
        with db_transaction.atomic():
            txn.payment_status    = Transaction.PAYMENT_STATUS.SUCCESSFUL
            txn.gateway_reference = txn.reference   # Nomba doesn't return a separate ref here
            txn.gateway_response  = data
            txn.save(update_fields=[
                "payment_status", "gateway_reference", "gateway_response", "updated_at"
            ])
            _activate_subscription(txn)

        db_transaction.on_commit(lambda: _fire_n8n_payment_webhook(txn))

        logger.info(
            "Auto-renewal successful: subscription=%s txn=%s amount=%s",
            sub.id, txn.reference, sub.plan.premium,
        )
        return {
            "outcome":          "success",
            "transaction_ref":  txn.reference,
            "subscription_id":  str(sub.id),
            "amount":           str(sub.plan.premium),
        }

    else:
        # Nomba returned non-00 or status=False — card declined or gateway error
        logger.warning(
            "Auto-renewal charge failed: subscription=%s txn=%s code=%s message=%s",
            sub.id, txn.reference, response_code, nomba_data.get("message", ""),
        )
        _mark_renewal_failed(txn, error=nomba_data.get("message", "declined"), raw=data)

        return {
            "outcome":          "failed",
            "transaction_ref":  txn.reference,
            "subscription_id":  str(sub.id),
            "failure_reason":   nomba_data.get("message", "declined"),
        }

# ----------------------------------------------------------------------------------------


# Failed Policy Renewal Processing
# ----------------------------------------------------------------------------------------
def _mark_renewal_failed(
    txn: Transaction,
    error: str = "",
    raw: dict | None = None,
) -> None:
    """
    Mark a renewal Transaction as FAILED, put the subscription into
    grace_period, and fire the charge.failed webhook to n8n so it can
    begin the dunning schedule (day 1 → day 3 → day 7).
    """
    from subscriptions.models import PolicySubscription

    txn.payment_status   = Transaction.PAYMENT_STATUS.FAILED
    txn.gateway_response = raw or {"error": error}
    txn.save(update_fields=["payment_status", "gateway_response", "updated_at"])

    # Put subscription into grace period — it's still technically active
    # but flagged for dunning. n8n decides when to lapse it.
    try:
        sub = txn.subscription
        sub.status = 'grace_period'
        sub.save(update_fields=["status", "updated_at"])
    except Exception as e:
        logger.error(
            "Could not set grace_period for subscription linked to txn %s: %s",
            txn.reference, str(e),
        )

    # Fire charge.failed to n8n outside the atomic block (called directly,
    # not via on_commit, because this function may be called outside a transaction)
    _fire_n8n_dunning_webhook(txn, error=error)

# ---------------------------------------------------------------------------------------

# Activate policy subscription
# ----------------------------------------------------------------------------------------
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
# --------------------------------------------------------------------------------------------


# N8N Transaction Webhooks

# N8N payment webhook
# -------------------------------------------------------------------------------------------
def _fire_n8n_payment_webhook(txn: Transaction) -> None:
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
            settings.N8N_WEBHOOK_PAYMENT_URL,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("n8n webhook fired for txn %s", txn.reference)
    except requests.RequestException as e:
        # Non-fatal — n8n failure should never block the payment confirmation
        logger.error("n8n webhook failed for txn %s: %s", txn.reference, str(e))

# ----------------------------------------------------------------------------------

def _fire_n8n_dunning_webhook(txn: Transaction, error: str = "") -> None:
    """
    Notify n8n that a renewal charge failed.

    n8n receives this and starts the dunning workflow:
      - Day 1: retry charge + notify customer
      - Day 3: retry charge + escalate notification
      - Day 7: final retry; if still failed, lapse subscription

    Payload contract (share with automation engineer):
      {
        "event":              "charge.failed",
        "transaction_ref":    "TII-XXXXXXXX",
        "subscription_id":    "<uuid>",
        "customer_email":     "customer@example.com",
        "amount":             "5000.00",
        "currency":           "NGN",
        "failure_reason":     "declined" | "timeout" | "auth_failure" | ...,
        "retry_endpoint":     "POST /payments/nomba/renewal/charge/",
        "retry_payload":      { "subscription_id": "<uuid>" }
      }
    """
    try:
        sub = txn.subscription
    except Exception:
        logger.error("Could not load subscription for dunning webhook, txn %s", txn.reference)
        return

    url = _get_service_webhook_url("charge.failed") or settings.N8N_WEBHOOK_DUNNING_URL
    payload = {
        "event":           "charge.failed",
        "transaction_ref": txn.reference,
        "subscription_id": str(sub.id),
        "customer_email":  sub.customer.email if sub.customer else None,
        "amount":          str(txn.amount),
        "currency":        txn.currency,
        "failure_reason":  error or "unknown",
        "retry_endpoint":  "POST /api/v1/payments/nomba/renewal/charge/",
        "retry_payload":   {"subscription_id": str(sub.id)},
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("n8n dunning webhook fired for txn %s", txn.reference)
    except requests.RequestException as e:
        logger.error("n8n dunning webhook failed for txn %s: %s", txn.reference, str(e))
# -------------------------------------------------------------------------------------------
