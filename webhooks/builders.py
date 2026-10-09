def _core(claim):
    user = claim.customer.user
    return {
        "claim_id": str(claim.id),
        "claim_reference": claim.claim_reference,
        "claim_type": claim.claim_type,
        "status": claim.status,
        "customer": {"id": str(claim.customer_id), "email": user.email,
                     "first_name": user.first_name},
        "provider": {"id": str(claim.provider_id), "name": claim.provider.name,
                     "admin_emails": list(claim.provider.admins
                         .filter(user__is_active=True)
                         .values_list("user__email", flat=True))},
    }


def _payment(p):
    return {
        "reference": p.reference,
        "amount": str(p.amount),
        "status": p.status,
        "nomba_reference": p.nomba_reference,
        "failure_reason": p.failure_reason,
    }


def claim_submitted(claim):
    return {**_core(claim),
            "claimed_amount": str(claim.claimed_amount),
            "incident_date": claim.incident_date.isoformat()}


def claim_ai_check_requested(claim):
    plan = getattr(claim.subscription, "plan", None)
    return {**_core(claim),
            "subscription": {"id": str(claim.subscription_id)},
            "plan": ({"id": str(plan.id), "name": plan.name,
                      "coverage_level": plan.coverage_level,
                      "coverage_amount": str(plan.coverage_amount),
                      "premium": str(plan.premium),
                      "duration_months": plan.duration_months} if plan else None),
            "claimed_amount": str(claim.claimed_amount),
            "incident_date": claim.incident_date.isoformat(),
            "incident_description": claim.incident_description,
            "documents": [{"type": d.document_type, "url": d.file.url}
                          for d in claim.documents.all()]}


def claim_ai_checked(claim):
    return {**_core(claim), "ai": claim.ai_result}


claim_flagged = claim_ai_checked


def claim_forwarded(claim):
    return {**_core(claim),
            "claimed_amount": str(claim.claimed_amount),
            "provider_id": str(claim.provider_id)}


def provider_claim_decision(claim, decision):
    return {**_core(claim), "decision": decision}


def claim_approved(claim):
    return {**_core(claim), "approved_amount": str(claim.approved_amount)}


def claim_rejected(claim):
    return {**_core(claim),
            "reason": claim.provider_review_note or claim.theeinsurance_review_note}


def claim_status_updated(claim):   # legacy partner event
    return {**_core(claim),
            "customer_email": claim.customer.user.email,
            "approved_amount": str(claim.approved_amount) if claim.approved_amount is not None else None}


def payment_event(claim, payment):  # initiated / completed / failed
    return {**_core(claim), "payment": _payment(payment)}


def receipt_generated(claim, payment):
    return {**_core(claim),
            "receipt": {"url": payment.receipt_url, "payment_reference": payment.reference}}


def claim_parties_notified(claim):
    return _core(claim)
