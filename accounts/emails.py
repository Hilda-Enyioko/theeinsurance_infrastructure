import logging
import resend
from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)


def _partner_admin_emails(partner):
    from .models import PartnerAdmin
    return list(
        PartnerAdmin.objects.filter(partner=partner, role="partner_admin", user__is_active=True)
        .values_list("user__email", flat=True)
    )


def _send(to, subject, html):
    if not to:
        return
    if not settings.RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set; skipping email '%s' to %s", subject, to)
        return
    resend.api_key = settings.RESEND_API_KEY
    try:
        resend.Emails.send({"from": settings.DEFAULT_FROM_EMAIL, "to": to, "subject": subject, "html": html})
    except Exception:
        log.exception("Resend failed for '%s'", subject)   # never break the request over email


def _after_commit(fn, *args):
    transaction.on_commit(lambda: fn(*args))


def send_kyc_received(partner):
    _send(_partner_admin_emails(partner), "We've received your KYC",
          f"<p>Hi,</p><p>Thanks for submitting KYC for <b>{partner.name}</b>. "
          "Our team is reviewing it and we'll email you as soon as it's verified.</p>")


def send_kyc_approved(partner):
    _send(_partner_admin_emails(partner), "Your account is verified",
          f"<p>Good news, <b>{partner.name}</b> has been verified.</p>"
          "<p>Your <b>X-Partner-Key</b> is now active. Use the key issued when you signed up. "
          "If you've lost it, log in and regenerate it from your dashboard.</p>"
          "<p>You can now log in and complete your profile.</p>")


def send_kyc_rejected(partner, note):
    _send(_partner_admin_emails(partner), "Your KYC needs attention",
          f"<p>We couldn't verify <b>{partner.name}</b>.</p><p>Reason: {note}</p>"
          "<p>Please log in and resubmit your documents.</p>")


def on_commit_send(fn, *args):
    _after_commit(fn, *args)

def send_customer_kyc_decision(email, first_name, approved, note=""):
    if approved:
        subject, body = "Your identity is verified", (
            f"<p>Hi {first_name},</p><p>Your KYC has been verified. You can now purchase plans.</p>")
    else:
        subject, body = "Your KYC needs attention", (
            f"<p>Hi {first_name},</p><p>We couldn't verify your KYC.</p><p>Reason: {note}</p>"
            "<p>Please log in and resubmit.</p>")
    _send([email], subject, body)
