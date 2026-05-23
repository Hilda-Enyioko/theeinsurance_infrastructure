from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Count, Sum

from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile


# Base: Resolve partner from authenticated user
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None

# Provider Analytics View
class ProviderSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        
        if not partner or partner.partner_type != 'provider':
            return Response({"error": "Access restricted to insurance providers."}, status=403)
        
        # Total Subscriptions
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
            "distributor_breakdown": list(distributor_breakdown)
        })


class ProviderSubscriptionsTrendsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        
        if not partner or partner.partner_type != 'provider':
            return Response({"error": "Access restricted to insurance providers."}, status=403)
        
        # Subscriptions per month for the last 6 months
        trends = (
            PolicySubscription.objects.filter(provider=partner)
            .extra(select={'month': "strftime('%%Y-%%m', created_at)"})
            .values('month')
            .annotate(count=Count('id'), revenue=Sum('amount_paid'))
            .order_by('month')
        )

        return Response({
            "trends": list(trends)
        })

# Distributor Analytics View
class DistributorSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        
        if not partner or partner.partner_type != 'distributor':
            return Response({"error": "Access restricted to insurance distributors."}, status=403)
        
        # Total Subscriptions
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

        total_customers = (
            CustomerProfile.objects.filter(partner=partner).count()
        )

        return Response({
            "summary": {**summary, "total_customers": total_customers},
            "top_providers": list(top_providers),
            "top_plans": list(top_plans)
        })
    
class DistributorSubscriptionsTrendsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        
        if not partner or partner.partner_type != 'distributor':
            return Response({"error": "Access restricted to insurance distributors."}, status=403)
        
        # Subscriptions facilitated per month for the last 6 months
        trends = (
            PolicySubscription.objects.filter(distributor=partner)
            .extra(select={'month': "strftime('%%Y-%%m', created_at)"})
            .values('month')
            .annotate(count=Count('id'), commissions=Sum('distributor_commission'))
            .order_by('month')
        )

        return Response({
            "trends": list(trends)
        })