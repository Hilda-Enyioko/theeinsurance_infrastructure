from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Count, Sum
from drf_spectacular.utils import extend_schema

from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile
from accounts.permissions import IsProviderAdmin, IsDistributorAdmin
from core.throttles import PartnerRateThrottle


# Helper
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Provider Analytics
class ProviderSummaryView(APIView):
    """
    Aggregated subscription and revenue summary for a provider.
    IsProviderAdmin enforces: authenticated + partner_admin + provider type.
    No inline partner_type check needed.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get Provider Analytics Summary",
        description=(
            "Calculates and returns aggregated metrics for the authenticated insurance provider, "
            "including total revenue, payouts, subscription status breakdown, top-performing plans, "
            "and performance across different distributors."
        ),
        responses={
            200: {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "object",
                        "properties": {
                            "total_policies": {"type": "integer", "example": 1540},
                            "total_revenue": {"type": "string", "example": "7700000.00"},
                            "total_payout": {"type": "string", "example": "6160000.00"},
                        }
                    },
                    "status_breakdown": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string", "example": "active"},
                                "count": {"type": "integer", "example": 1200}
                            }
                        }
                    },
                    "top_plans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "plan__name": {"type": "string", "example": "Comprehensive Auto Shield"},
                                "total_sold": {"type": "integer", "example": 450},
                                "revenue": {"type": "string", "example": "2250000.00"}
                            }
                        }
                    },
                    "distributor_breakdown": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "distributor__name": {"type": "string", "example": "Nomba Finance"},
                                "total_sold": {"type": "integer", "example": 310},
                                "commission": {"type": "string", "example": "310000.00"}
                            }
                        }
                    }
                }
            },
            401: {"description": "Unauthorized access."},
            403: {"description": "Permission denied. Requires Provider Admin privileges."}
        },
        tags=["Analytics - Provider"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        subscriptions = PolicySubscription.objects.filter(provider=partner)

        summary = subscriptions.aggregate(
            total_policies=Count('id'),
            total_revenue=Sum('amount_paid'),
            total_payout=Sum('provider_payout'),
        )

        status_breakdown = (
            subscriptions.values('status')
            .annotate(count=Count('id'))
            .order_by('status')
        )

        top_plans = (
            subscriptions.values('plan__name')
            .annotate(total_sold=Count("id"), revenue=Sum("amount_paid"))
            .order_by("-total_sold")[:5]
        )

        distributor_breakdown = (
            subscriptions.filter(distributor__isnull=False)
            .values('distributor__name')
            .annotate(total_sold=Count("id"), commission=Sum("distributor_commission"))
            .order_by("-total_sold")
        )

        return Response({
            "summary": summary,
            "status_breakdown": list(status_breakdown),
            "top_plans": list(top_plans),
            "distributor_breakdown": list(distributor_breakdown),
        })


class ProviderSubscriptionsTrendsView(APIView):
    """
    Monthly subscription trends for a provider over the last 6 months.
    IsProviderAdmin enforced at class level.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get Provider Subscription Trends",
        description="Returns month-on-month aggregations of volumes and revenue generated over time.",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "trends": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "month": {"type": "string", "example": "2026-06"},
                                "count": {"type": "integer", "example": 240},
                                "revenue": {"type": "string", "example": "1200000.00"}
                            }
                        }
                    }
                }
            },
            401: {"description": "Unauthorized access."},
            403: {"description": "Permission denied. Requires Provider Admin privileges."}
        },
        tags=["Analytics - Provider"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)

        trends = (
            PolicySubscription.objects.filter(provider=partner)
            .extra(select={'month': "strftime('%%Y-%%m', created_at)"})
            .values('month')
            .annotate(count=Count('id'), revenue=Sum('amount_paid'))
            .order_by('month')
        )

        return Response({"trends": list(trends)})


# Distributor Analytics
class DistributorSummaryView(APIView):
    """
    Aggregated facilitation and commission summary for a distributor.
    IsDistributorAdmin enforces: authenticated + partner_admin + distributor type.
    No inline partner_type check needed.
    """
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get Distributor Analytics Summary",
        description=(
            "Calculates and returns metrics for the authenticated partner distributor, "
            "including total policies sold, commissions earned, user acquisition count, "
            "and performance segmented by top insurance providers."
        ),
        responses={
            200: {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "object",
                        "properties": {
                            "total_policies_facilitated": {"type": "integer", "example": 820},
                            "total_commissions_earned": {"type": "string", "example": "820000.00"},
                            "total_customers": {"type": "integer", "example": 750}
                        }
                    },
                    "top_providers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "provider__name": {"type": "string", "example": "Leadway Assurance"},
                                "total_sold": {"type": "integer", "example": 500}
                            }
                        }
                    },
                    "top_plans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "plan__name": {"type": "string", "example": "Third Party Premium"},
                                "plan__category__name": {"type": "string", "example": "Auto"},
                                "total_sold": {"type": "integer", "example": 380}
                            }
                        }
                    }
                }
            },
            401: {"description": "Unauthorized access."},
            403: {"description": "Permission denied. Requires Distributor Admin privileges."}
        },
        tags=["Analytics - Distributor"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        subscriptions = PolicySubscription.objects.filter(distributor=partner)

        summary = subscriptions.aggregate(
            total_policies_facilitated=Count('id'),
            total_commissions_earned=Sum('distributor_commission'),
        )

        top_providers = (
            subscriptions.values('provider__name')
            .annotate(total_sold=Count("id"))
            .order_by("-total_sold")[:5]
        )

        top_plans = (
            subscriptions.values('plan__name', 'plan__category__name')
            .annotate(total_sold=Count("id"))
            .order_by("-total_sold")[:5]
        )

        total_customers = CustomerProfile.objects.filter(partner=partner).count()

        return Response({
            "summary": {**summary, "total_customers": total_customers},
            "top_providers": list(top_providers),
            "top_plans": list(top_plans),
        })


class DistributorSubscriptionsTrendsView(APIView):
    """
    Monthly facilitation trends for a distributor over the last 6 months.
    IsDistributorAdmin enforced at class level.
    """
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get Distributor Facilitation Trends",
        description="Returns month-on-month breakdown of customer conversions and corresponding commissions earned.",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "trends": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "month": {"type": "string", "example": "2026-06"},
                                "count": {"type": "integer", "example": 145},
                                "commissions": {"type": "string", "example": "145000.00"}
                            }
                        }
                    }
                }
            },
            401: {"description": "Unauthorized access."},
            403: {"description": "Permission denied. Requires Distributor Admin privileges."}
        },
        tags=["Analytics - Distributor"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)

        trends = (
            PolicySubscription.objects.filter(distributor=partner)
            .extra(select={'month': "strftime('%%Y-%%m', created_at)"})
            .values('month')
            .annotate(count=Count('id'), commissions=Sum('distributor_commission'))
            .order_by('month')
        )

        return Response({"trends": list(trends)})
