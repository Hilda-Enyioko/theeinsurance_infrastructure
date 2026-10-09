from dataclasses import dataclass

SCHEMA_VERSION = "2026-10-01"

N8N, PARTNER = "n8n", "partner"   # audiences


@dataclass(frozen=True)
class EventDef:
    type: str
    description: str
    audiences: tuple
    required: tuple   # keys that must exist in envelope["data"]


class E:
    # claims pipeline
    CLAIM_SUBMITTED = "claim.submitted"
    CLAIM_AI_CHECK_REQUESTED = "claim.ai_check_requested"
    CLAIM_AI_CHECKED = "claim.ai_checked"
    CLAIM_FLAGGED = "claim.flagged"
    CLAIM_FORWARDED = "claim.forwarded"
    PROVIDER_CLAIM_DECISION = "provider.claim_decision"
    CLAIM_APPROVED = "claim.approved"
    CLAIM_REJECTED = "claim.rejected"
    PAYMENT_INITIATED = "payment.initiated"
    PAYMENT_COMPLETED = "payment.completed"
    PAYMENT_FAILED = "payment.failed"
    RECEIPT_GENERATED = "receipt.generated"
    CLAIM_PARTIES_NOTIFIED = "claim.parties_notified"
    # plans and subscriptions events
    CLAIM_STATUS_UPDATED = "claim.status_updated"
    KYC_SUBMITTED = "kyc.submitted"
    KYC_APPROVED = "kyc.approved"
    KYC_REJECTED = "kyc.rejected"
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"
    SUBSCRIPTION_UPDATED = "subscription.updated"
    PAYMENT_SUCCESSFUL = "payment.successful"
    CHARGE_FAILED = "charge.failed"


_CLAIM = ("claim_id", "claim_reference", "claim_type", "status")

_DEFS = [
    # claims pipeline
    EventDef(E.CLAIM_SUBMITTED, "Claim submitted", (N8N, PARTNER), _CLAIM + ("claimed_amount", "customer")),
    EventDef(E.CLAIM_AI_CHECK_REQUESTED, "All documents uploaded; AI check needed", (N8N,), _CLAIM + ("documents", "subscription")),
    EventDef(E.CLAIM_AI_CHECKED, "AI check result stored", (N8N,), _CLAIM + ("ai",)),
    EventDef(E.CLAIM_FLAGGED, "Claim flagged for staff review", (N8N,), _CLAIM + ("ai",)),
    EventDef(E.CLAIM_FORWARDED, "Claim forwarded to provider", (N8N, PARTNER), _CLAIM + ("claimed_amount",)),
    EventDef(E.PROVIDER_CLAIM_DECISION, "Provider made a decision", (N8N,), _CLAIM + ("decision",)),
    EventDef(E.CLAIM_APPROVED, "Claim approved", (N8N, PARTNER), _CLAIM + ("approved_amount",)),
    EventDef(E.CLAIM_REJECTED, "Claim rejected", (N8N, PARTNER), _CLAIM + ("reason",)),
    EventDef(E.PAYMENT_INITIATED, "Claim payout started", (N8N,), _CLAIM + ("payment",)),
    EventDef(E.PAYMENT_COMPLETED, "Claim payout succeeded", (N8N, PARTNER), _CLAIM + ("payment",)),
    EventDef(E.PAYMENT_FAILED, "Claim payout failed", (N8N,), _CLAIM + ("payment",)),
    EventDef(E.RECEIPT_GENERATED, "Settlement receipt generated", (N8N,), _CLAIM + ("receipt",)),
    EventDef(E.CLAIM_PARTIES_NOTIFIED, "Customer and provider informed", (N8N,), _CLAIM),
    # plans and subscriptions events
    EventDef(E.CLAIM_STATUS_UPDATED, "Claim status updated (deprecated)", (PARTNER,), ()),
    EventDef(E.KYC_SUBMITTED, "KYC submitted", (PARTNER,), ("partner_id",)),
    EventDef(E.KYC_APPROVED, "KYC approved", (PARTNER,), ("partner_id", "status")),
    EventDef(E.KYC_REJECTED, "KYC rejected", (PARTNER,), ("partner_id", "status")),
    EventDef(E.SUBSCRIPTION_CREATED, "Subscription created", (PARTNER,), ()),
    EventDef(E.SUBSCRIPTION_CANCELLED, "Subscription cancelled", (PARTNER,), ()),
    EventDef(E.SUBSCRIPTION_UPDATED, "Subscription updated", (PARTNER,), ()),
    EventDef(E.PAYMENT_SUCCESSFUL, "Payment successful", (N8N,), ()),
    EventDef(E.CHARGE_FAILED, "Charge failed (dunning)", (N8N,), ()),
]

CATALOGUE = {d.type: d for d in _DEFS}


def choices(audience=None):
    return [(d.type, d.description) for d in CATALOGUE.values()
            if audience is None or audience in d.audiences]
