from django.shortcuts import render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from decimal import Decimal

from .models import PolicySubscription
from .serializers import PolicySubscriptionSerializer, PolicySubscriptionCreateSerializer
from accounts.models import CustomerProfile
from plans.models import DistributorProviderAccess
from webhooks.views import dispatch_webhook


# Helpers
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


def calculate_financials(plan, distributor):
    """
    Calculate the revenue split for a subscription.
    Platform fee is 10% of premium.
    Distributor commission comes from DistributorProfile.
    Provider payout is what remains.
    """
    premium = plan.premium
    platform_fee = round(premium * Decimal("0.10"), 2)

    distributor_commission = Decimal("0.00")
    if distributor:
        try:
            rate = distributor.distributor_profile.commission_rate / Decimal("100")
            distributor_commission = round(premium * rate, 2)
        except Exception:
            distributor_commission = Decimal("0.00")

    provider_payout = round(premium - platform_fee - distributor_commission, 2)

    return {
        "amount_paid": premium,
        "platform_fee": platform_fee,
        "distributor_commission": distributor_commission,
        "provider_payout": provider_payout,
    }


# Customer Subscriptions
class CustomerSubscriptionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        subscriptions = PolicySubscription.objects.filter(customer=profile)

        # filter by status if provided
        status_filter = request.query_params.get("status")
        if status_filter:
            subscriptions = subscriptions.filter(status=status_filter)

        serializer = PolicySubscriptionSerializer(subscriptions, many=True)
        return Response({"subscriptions": serializer.data})


class CustomerSubscriptionCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # verify customer profile exists for this partner
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        serializer = PolicySubscriptionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        plan = serializer.validated_data["plan"]
        start_date = serializer.validated_data["start_date"]
        end_date = start_date + relativedelta(months=plan.duration_months)

        # verify distributor has access to this plan's provider
        partner = request.partner
        distributor = None

        if partner.partner_type == "distributor":
            has_access = DistributorProviderAccess.objects.filter(
                distributor=partner,
                provider=plan.provider,
                is_active=True,
            ).exists()
            if not has_access:
                return Response(
                    {"error": "This plan is not available on your platform."},
                    status=403
                )
            distributor = partner

        # calculate revenue split
        financials = calculate_financials(plan, distributor)

        # create subscription in pending state
        subscription = PolicySubscription.objects.create(
            customer=profile,
            plan=plan,
            provider=plan.provider,
            distributor=distributor,
            start_date=start_date,
            end_date=end_date,
            status="pending",
            **financials,
        )

        return Response({
            "message": "Subscription initiated. Complete payment to activate.",
            "subscription_id": str(subscription.id),
            "amount": str(subscription.amount_paid),
        }, status=201)


class CustomerSubscriptionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, subscription_id, profile):
        try:
            return PolicySubscription.objects.get(
                id=subscription_id, customer=profile
            )
        except PolicySubscription.DoesNotExist:
            return None

    def get(self, request, subscription_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        subscription = self.get_object(subscription_id, profile)
        if not subscription:
            return Response({"error": "Subscription not found."}, status=404)

        serializer = PolicySubscriptionSerializer(subscription)
        return Response(serializer.data)

    def delete(self, request, subscription_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        subscription = self.get_object(subscription_id, profile)
        if not subscription:
            return Response({"error": "Subscription not found."}, status=404)

        if subscription.status != "active":
            return Response(
                {"error": "Only active subscriptions can be cancelled."},
                status=400
            )

        subscription.status = "cancelled"
        subscription.save()

        # fire webhook to both provider and distributor
        payload = {
            "subscription_id": str(subscription.id),
            "plan": subscription.plan.name,
            "customer_email": subscription.customer.user.email,
            "status": "cancelled",
        }
        dispatch_webhook(subscription.provider, "subscription.cancelled", payload)
        if subscription.distributor:
            dispatch_webhook(
                subscription.distributor, "subscription.cancelled", payload
            )

        return Response({"message": "Subscription cancelled successfully."})


# Payment Verification
class PaymentVerificationView(APIView):
    """
    Called after payment gateway confirms payment.
    Activates the subscription and fires webhooks.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, subscription_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            subscription = PolicySubscription.objects.get(
                id=subscription_id, customer=profile, status="pending"
            )
        except PolicySubscription.DoesNotExist:
            return Response(
                {"error": "Pending subscription not found."}, status=404
            )

        payment_reference = request.data.get("payment_reference")
        if not payment_reference:
            return Response(
                {"error": "Payment reference is required."}, status=400
            )

        # activate subscription
        subscription.payment_reference = payment_reference
        subscription.payment_verified = True
        subscription.status = "active"
        subscription.save()

        # fire webhooks
        payload = {
            "subscription_id": str(subscription.id),
            "plan": subscription.plan.name,
            "customer_email": subscription.customer.user.email,
            "start_date": str(subscription.start_date),
            "end_date": str(subscription.end_date),
            "amount_paid": str(subscription.amount_paid),
            "status": "active",
        }
        dispatch_webhook(subscription.provider, "subscription.created", payload)
        if subscription.distributor:
            dispatch_webhook(
                subscription.distributor, "subscription.created", payload
            )

        return Response({
            "message": "Payment verified. Subscription is now active.",
            "subscription": PolicySubscriptionSerializer(subscription).data,
        })


# Partner Admin Subscription View
class PartnerSubscriptionListView(APIView):
    """
    Partner admins view all subscriptions relevant to them.
    Providers see subscriptions for their plans.
    Distributors see subscriptions facilitated through them.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        if partner.partner_type == "provider":
            subscriptions = PolicySubscription.objects.filter(provider=partner)
        else:
            subscriptions = PolicySubscription.objects.filter(distributor=partner)

        status_filter = request.query_params.get("status")
        if status_filter:
            subscriptions = subscriptions.filter(status=status_filter)

        serializer = PolicySubscriptionSerializer(subscriptions, many=True)
        return Response({"subscriptions": serializer.data})