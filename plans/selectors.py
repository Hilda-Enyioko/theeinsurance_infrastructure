from django.db.models import Q
from .models import DistributorAccessGrant, InsurancePlan

ACCESS_ORDER = ["approved", "pending", "rejected", "revoked", "withdrawn"]


def plans_visible_to_partner(partner):
    """
    The plans a partner may SELL / show to its customers.
      provider    -> all its own active plans (public AND private)
      distributor -> active PUBLIC plans of active providers covered by an APPROVED grant:
                     provider-level (all current + future plans) or plan-level (that plan only)
    Subscriptions must also validate the chosen plan against this queryset.
    """
    base = InsurancePlan.objects.filter(is_active=True)
    if partner.partner_type == "provider":
        return base.filter(provider=partner)
    approved = DistributorAccessGrant.objects.filter(distributor=partner, status="approved")
    return (base.filter(visibility="public", provider__is_active=True)
            .filter(Q(provider_id__in=approved.filter(scope="provider").values("provider_id"))
                    | Q(id__in=approved.filter(scope="plan").values("plan_id"))))


def marketplace_plans():
    """What distributors may BROWSE: every active public plan from active providers."""
    return InsurancePlan.objects.filter(is_active=True, visibility="public", provider__is_active=True)


def distributor_access_index(distributor):
    idx = {"provider": {}, "plan": {}}
    for g in DistributorAccessGrant.objects.filter(distributor=distributor):
        if g.scope == "provider":
            idx["provider"][g.provider_id] = g.status
        else:
            idx["plan"][g.plan_id] = g.status
    return idx


def best_access(idx, *, provider_id, plan_id=None):
    cands = [(ACCESS_ORDER.index(st), via, st)
             for via, st in (("plan", idx["plan"].get(plan_id)), ("provider", idx["provider"].get(provider_id)))
             if st]
    if not cands:
        return {"status": "none", "via": None}
    _, via, st = min(cands)
    return {"status": st, "via": via}
