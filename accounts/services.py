from django.db import transaction
from django.utils import timezone

from accounts import emails
from webhooks.events import E
from webhooks.services import emit
from .models import CustomerKYC, PartnerKYC


class KYCReviewError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _guard(kyc, approve, note):
    if kyc.status != "pending":
        raise KYCReviewError(f"KYC has already been {kyc.status}.", 409)
    if not approve and not note:
        raise KYCReviewError("A note is required when rejecting.", 400)


@transaction.atomic
def review_partner_kyc(kyc_pk, *, approve, note="", reviewer_email):
    note = (note or "").strip()
    kyc = PartnerKYC.objects.select_for_update().select_related("partner").get(pk=kyc_pk)
    _guard(kyc, approve, note)

    kyc.status = "approved" if approve else "rejected"
    kyc.review_note, kyc.reviewed_by, kyc.reviewed_at = note, reviewer_email, timezone.now()
    kyc.save()

    partner = kyc.partner
    if approve:
        partner.is_active = True
        partner.save(update_fields=["is_active"])
    emit(E.KYC_APPROVED if approve else E.KYC_REJECTED, partner=partner, aggregate_id=partner.id,
         discriminator=kyc.reviewed_at.isoformat(),
         data={"partner_id": str(partner.id), "partner_name": partner.name, "status": kyc.status, "note": note})
    if approve:
        emails.on_commit_send(emails.send_kyc_approved, partner)
    else:
        emails.on_commit_send(emails.send_kyc_rejected, partner, note)
    return kyc


@transaction.atomic
def review_customer_kyc(kyc_pk, *, approve, note="", reviewer_email=""):
    note = (note or "").strip()
    kyc = CustomerKYC.objects.select_for_update().select_related("customer__user").get(pk=kyc_pk)
    _guard(kyc, approve, note)

    kyc.status = "approved" if approve else "rejected"
    kyc.review_note, kyc.reviewed_at = note, timezone.now()
    kyc.save()

    user = kyc.customer.user
    emails.on_commit_send(emails.send_customer_kyc_decision, user.email, user.first_name, approve, note)
    return kyc
