import logging

from django.db import transaction
from django.utils import timezone

from webhooks import builders as b
from webhooks.events import E
from webhooks.services import emit
from .models import ClaimPayment

log = logging.getLogger(__name__)


def start_claim_payout(claim):
    """Call inside the transaction that approved the claim."""
    ref = f"CLMPAY-{claim.claim_reference}"
    payment, created = ClaimPayment.objects.get_or_create(
        reference=ref,
        defaults={"claim": claim, "amount": claim.approved_amount},
    )
    if created:
        emit(E.PAYMENT_INITIATED, partner=claim.provider, aggregate_id=claim.id,
             data=b.payment_event(claim, payment), discriminator=ref)
        transaction.on_commit(lambda: submit_to_nomba(payment))
    return payment


def submit_to_nomba(payment):
    """
    INTEGRATION POINT: call your existing Nomba transfer/payout client here,
    using payment.reference as the merchant transaction reference.
    The result arrives via the Nomba webhook, which calls record_payment_result().
    """
    log.warning("submit_to_nomba not wired yet for %s", payment.reference)


def record_payment_result(reference, *, success, nomba_reference="", reason=""):
    """Call from your verified Nomba webhook handler. Safe to call twice."""
    with transaction.atomic():
        payment = (ClaimPayment.objects.select_for_update(of=("self",))
                   .select_related("claim", "claim__provider").get(reference=reference))
        if payment.status != ClaimPayment.PENDING:
            return payment   # replayed webhook

        claim = payment.claim
        payment.nomba_reference = nomba_reference
        if success:
            payment.status = ClaimPayment.COMPLETED
            payment.completed_at = timezone.now()
            claim.status = "paid"
            claim.save(update_fields=["status", "updated_at"])
            event_type = E.PAYMENT_COMPLETED
        else:
            payment.status = ClaimPayment.FAILED
            payment.failure_reason = reason[:255]
            event_type = E.PAYMENT_FAILED   # claim stays "approved"
        payment.save()

        emit(event_type, partner=claim.provider, aggregate_id=claim.id,
             data=b.payment_event(claim, payment), discriminator=reference)
    return payment
import logging

from django.db import transaction
from django.utils import timezone

from accounts.emails import on_commit_send
from webhooks import builders as b
from webhooks.events import E
from webhooks.services import emit
from . import emails as claim_emails
from .models import ClaimPayment

log = logging.getLogger(__name__)


def start_claim_payout(claim):
    """
    Call inside the transaction that approved the claim (or a retry).
    Snapshots the customer's settlement account onto the payment. Idempotent:
    returns the existing payment if one is pending or completed.
    """
    existing = claim.payments.filter(
        status__in=[ClaimPayment.PENDING, ClaimPayment.COMPLETED]).first()
    if existing:
        return existing

    customer = claim.customer
    ref = f"CLMPAY-{claim.claim_reference}-{claim.payments.count() + 1}"   # unique per attempt
    payment = ClaimPayment.objects.create(
        claim=claim, reference=ref, amount=claim.approved_amount,
        account_name=customer.settlement_account_name,
        account_number=customer.settlement_bank_account,
        bank_code=customer.settlement_bank_code,
    )
    emit(E.PAYMENT_INITIATED, partner=claim.provider, aggregate_id=claim.id,
         data=b.payment_event(claim, payment), discriminator=ref)
    transaction.on_commit(lambda: submit_to_nomba(payment))
    return payment


def submit_to_nomba(payment):
    """
    INTEGRATION POINT: call your Nomba transfer client with payment.reference as the merchant
    reference and payment.account_name / account_number / bank_code as the beneficiary.
    The result arrives via the Nomba webhook, which calls record_payment_result().
    """
    log.warning("submit_to_nomba not wired yet for %s", payment.reference)


def record_payment_result(reference, *, success, nomba_reference="", reason=""):
    """Call from your verified Nomba webhook handler. Safe to call twice."""
    with transaction.atomic():
        payment = (ClaimPayment.objects.select_for_update(of=("self",))
                   .select_related("claim", "claim__provider").get(reference=reference))
        if payment.status != ClaimPayment.PENDING:
            return payment   # replayed webhook

        claim = payment.claim
        payment.gateway_reference = nomba_reference          # was nomba_reference (no such field)
        if success:
            payment.status = ClaimPayment.COMPLETED
            payment.completed_at = timezone.now()
            claim.status = "paid"
            claim.save(update_fields=["status", "updated_at"])
            event_type = E.PAYMENT_COMPLETED
            on_commit_send(claim_emails.send_claim_paid, claim.id)
        else:
            payment.status = ClaimPayment.FAILED
            payment.failure_reason = reason[:255]
            event_type = E.PAYMENT_FAILED   # claim stays "approved"
            on_commit_send(claim_emails.send_claim_payout_failed, claim.id, payment.failure_reason)
        payment.save()

        emit(event_type, partner=claim.provider, aggregate_id=claim.id,
             data=b.payment_event(claim, payment), discriminator=reference)
    return payment
