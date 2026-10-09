"""Claim lifecycle emails (Resend). Always call through accounts.emails.on_commit_send."""
from django.utils.html import escape

from accounts.emails import _naira, _partner_admin_emails, _send
from .models import Claim, ClaimPayment


def _load(claim_id):
    return (Claim.objects
            .select_related("customer__user", "provider", "subscription__plan", "subscription__distributor")
            .filter(pk=claim_id).first())


def _distributor_fyi(claim, subject, body):
    """Distributors are informed only; they never have to act on a claim."""
    if claim.subscription.distributor:
        _send(_partner_admin_emails(claim.subscription.distributor), subject, body)


def send_claim_forwarded(claim_id):
    """Claim cleared the AI/staff gate: provider must review, distributor is told."""
    claim = _load(claim_id)
    if claim is None:
        return
    user = claim.customer.user
    ref, plan = escape(claim.claim_reference), escape(claim.subscription.plan.name)
    kind = escape(claim.get_claim_type_display())
    who = escape(f"{user.first_name} {user.last_name}")

    _send(_partner_admin_emails(claim.provider), f"New claim to review: {claim.claim_reference}",
          f"<p>A <b>{kind}</b> claim ({ref}) from {who} under <b>{plan}</b> is ready for your review.</p>"
          f"<p>Claimed amount: <b>{_naira(claim.claimed_amount)}</b></p>"
          "<p>Please log in to review the documents and make a decision.</p>")
    _distributor_fyi(claim, f"Claim filed through your platform: {claim.claim_reference}",
                     f"<p>A <b>{kind}</b> claim ({ref}) for <b>{plan}</b> was filed through your platform and "
                     "has been forwarded to the provider for review.</p><p>No action is required from you.</p>")


def send_claim_decision(claim_id, status, note=""):
    """Customer is told about approved / rejected / more_info_required; distributor FYI on approve/reject."""
    claim = _load(claim_id)
    if claim is None:
        return
    user = claim.customer.user
    name, ref, plan, note = (escape(user.first_name), escape(claim.claim_reference),
                             escape(claim.subscription.plan.name), escape(note or ""))

    if status == "approved":
        last4 = claim.customer.settlement_bank_account[-4:]
        subject = f"Claim approved: {claim.claim_reference}"
        body = (f"<p>Hi {name},</p><p>Your claim <b>{ref}</b> has been approved for "
                f"<b>{_naira(claim.approved_amount)}</b>.</p>"
                f"<p>Payment will be sent to your settlement account ending <b>{last4}</b>. "
                "We'll email you again once it has been paid.</p>")
        dist_body = (f"<p>Claim {ref} for <b>{plan}</b>, filed through your platform, was approved for "
                     f"<b>{_naira(claim.approved_amount)}</b>.</p><p>No action is required from you.</p>")
    elif status == "rejected":
        subject = f"Claim update: {claim.claim_reference}"
        body = (f"<p>Hi {name},</p><p>We're sorry, your claim <b>{ref}</b> was not approved.</p>"
                f"<p>Reason: {note}</p>")
        dist_body = (f"<p>Claim {ref} for <b>{plan}</b>, filed through your platform, was rejected.</p>"
                     "<p>No action is required from you.</p>")
    elif status == "more_info_required":
        subject = f"More information needed: {claim.claim_reference}"
        body = (f"<p>Hi {name},</p><p>We need more information to continue with claim <b>{ref}</b>.</p>"
                f"<p>{note}</p><p>Please log in, upload what's missing and resubmit your claim.</p>")
        dist_body = None
    else:
        return

    _send([user.email], subject, body)
    if dist_body:
        _distributor_fyi(claim, f"Claim {claim.claim_reference}: {status.replace('_', ' ')}", dist_body)


def send_claim_paid(claim_id):
    claim = _load(claim_id)
    if claim is None:
        return
    user = claim.customer.user
    payment = claim.payments.filter(status=ClaimPayment.COMPLETED).first()
    amount = _naira(payment.amount if payment else claim.approved_amount)
    last4 = (payment.account_number if payment else "")[-4:]
    ref, plan = escape(claim.claim_reference), escape(claim.subscription.plan.name)

    _send([user.email], f"Claim paid: {claim.claim_reference}",
          f"<p>Hi {escape(user.first_name)},</p><p>Your claim <b>{ref}</b> has been paid. "
          f"<b>{amount}</b> was sent to your account ending <b>{last4}</b>.</p>")
    _distributor_fyi(claim, f"Claim paid: {claim.claim_reference}",
                     f"<p>Claim {ref} for <b>{plan}</b>, filed through your platform, has been paid out "
                     f"(<b>{amount}</b>).</p><p>No action is required from you.</p>")


def send_claim_payout_failed(claim_id, reason=""):
    claim = _load(claim_id)
    if claim is None:
        return
    _send(_partner_admin_emails(claim.provider), f"Payout failed: {claim.claim_reference}",
          f"<p>The payout for approved claim <b>{escape(claim.claim_reference)}</b> failed.</p>"
          f"<p>Reason: {escape(reason)}</p>"
          "<p>The claim remains approved. Log in and retry the payout once the issue is resolved.</p>")
