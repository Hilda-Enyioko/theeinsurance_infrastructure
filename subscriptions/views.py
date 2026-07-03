from rest_framework.views import APIView
from rest_framework.response import Response
from dateutil.relativedelta import relativedelta
from decimal import Decimal

# Import drf-spectacular tools
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

from .models import PolicySubscription, SubscriptionDocument, REQUIRED_DOCUMENTS
from .serializers import PolicySubscriptionSerializer, PolicySubscriptionCreateSerializer
from accounts.models import CustomerProfile
from accounts.permissions import IsCustomer, IsPartnerAdmin
from plans.models import DistributorProviderAccess
from webhooks.services import dispatch_webhook
from core.throttles import PartnerRateThrottle


# Helper Functions
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


def get_required_documents(plan):
    """Return required document list for a given plan."""
    category = plan.category.name
    coverage_level = plan.coverage_level
    return REQUIRED_DOCUMENTS.get(category, {}).get(coverage_level, [])


def calculate_financials(plan, distributor):
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
    """
    Customers list their own subscriptions.
    IsCustomer prevents partner_admins and staff from hitting this endpoint.
    Object ownership enforced via customer=profile filter.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List Customer Subscriptions",
        description="Allows a customer to view their active or past policy subscriptions.",
        parameters=[
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter subscriptions by status (e.g., active, pending_payment, cancelled)",
                required=False
            )
        ],
        responses={200: PolicySubscriptionSerializer(many=True)}
    )
    def get(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        subscriptions = PolicySubscription.objects.filter(customer=profile)

        status_filter = request.query_params.get("status")
        if status_filter:
            subscriptions = subscriptions.filter(status=status_filter)

        serializer = PolicySubscriptionSerializer(subscriptions, many=True)
        return Response({"subscriptions": serializer.data})


class CustomerSubscriptionCreateView(APIView):
    """
    Customers initiate a new subscription.
    IsCustomer enforced at class level.
    Distributor access check is enforced at queryset level before subscription creation.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Initiate a Subscription",
        description="Initiate a new insurance policy subscription. Returns details on required document uploads.",
        request=PolicySubscriptionCreateSerializer,
        responses={
            201: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT
        }
    )
    def post(self, request):
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

        financials = calculate_financials(plan, distributor)

        subscription = PolicySubscription.objects.create(
            customer=profile,
            plan=plan,
            provider=plan.provider,
            distributor=distributor,
            start_date=start_date,
            end_date=end_date,
            status="pending_document",
            **financials,
        )

        required_docs = get_required_documents(plan)

        return Response({
            "message": "Subscription initiated. Please upload required documents.",
            "subscription_id": str(subscription.id),
            "amount": str(subscription.amount_paid),
            "required_documents": required_docs,
        }, status=201)


# Document Upload
class SubscriptionDocumentUploadView(APIView):
    """
    Customers upload and review documents for a pending subscription.
    IsCustomer + customer=profile filter enforces ownership.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Upload Subscription Document",
        description="Uploads a single file required for the subscription evaluation.",
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'document_type': {'type': 'string', 'description': 'The code representing the document requirement.'},
                    'file': {'type': 'string', 'format': 'binary', 'description': 'The physical file payload.'}
                },
                'required': ['document_type', 'file']
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
    )
    def post(self, request, subscription_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            subscription = PolicySubscription.objects.get(
                id=subscription_id,
                customer=profile,
                status="pending_document",
            )
        except PolicySubscription.DoesNotExist:
            return Response(
                {"error": "Subscription not found or not awaiting documents."},
                status=404
            )

        document_type = request.data.get("document_type")
        file = request.FILES.get("file")

        if not document_type:
            return Response({"error": "document_type is required."}, status=400)
        if not file:
            return Response({"error": "file is required."}, status=400)

        required_docs = get_required_documents(subscription.plan)
        valid_types = [choice[0] for choice in SubscriptionDocument.DOCUMENT_TYPE_CHOICES]

        if document_type not in valid_types:
            return Response(
                {"error": f"Invalid document type. Valid types: {valid_types}"},
                status=400
            )

        if document_type not in required_docs:
            return Response(
                {"error": f"This document is not required for this plan. Required: {required_docs}"},
                status=400
            )

        SubscriptionDocument.objects.update_or_create(
            subscription=subscription,
            document_type=document_type,
            defaults={"file": file},
        )

        uploaded_types = list(
            subscription.documents.values_list("document_type", flat=True)
        )
        missing_docs = [doc for doc in required_docs if doc not in uploaded_types]

        if not missing_docs:
            subscription.status = "pending_payment"
            subscription.save()
            return Response({
                "message": "All documents uploaded. You can now proceed to payment.",
                "status": "pending_payment",
                "missing_documents": [],
            })

        return Response({
            "message": "Document uploaded successfully.",
            "status": "pending_document",
            "missing_documents": missing_docs,
        })

    @extend_schema(
        summary="View Subscription Document Checklist",
        description="Returns lists of uploaded, required, and missing documentation statuses.",
        responses={200: OpenApiTypes.OBJECT}
    )
    def get(self, request, subscription_id):
        """Returns uploaded documents and what is still missing."""
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            subscription = PolicySubscription.objects.get(
                id=subscription_id, customer=profile
            )
        except PolicySubscription.DoesNotExist:
            return Response({"error": "Subscription not found."}, status=404)

        required_docs = get_required_documents(subscription.plan)
        uploaded = subscription.documents.all()
        uploaded_types = [doc.document_type for doc in uploaded]
        missing_docs = [doc for doc in required_docs if doc not in uploaded_types]

        return Response({
            "required_documents": required_docs,
            "uploaded_documents": [
                {
                    "document_type": doc.document_type,
                    "file": doc.file.url if doc.file else None,
                    "uploaded_at": doc.uploaded_at,
                }
                for doc in uploaded
            ],
            "missing_documents": missing_docs,
            "ready_for_payment": len(missing_docs) == 0,
        })


# Subscription Detail
class CustomerSubscriptionDetailView(APIView):
    """
    Customers view or cancel a single subscription they own.
    IsCustomer + customer=profile filter enforces ownership.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    def get_object(self, subscription_id, profile):
        try:
            return PolicySubscription.objects.get(
                id=subscription_id, customer=profile
            )
        except PolicySubscription.DoesNotExist:
            return None

    @extend_schema(
        summary="Retrieve Subscription Detail",
        description="Fetch explicit object properties of a given customer subscription instance.",
        responses={200: PolicySubscriptionSerializer}
    )
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

    @extend_schema(
        summary="Cancel Subscription",
        description="Transition an active subscription directly into a 'cancelled' status.",
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
    )
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
    Customers submit payment reference to activate their subscription.
    IsCustomer + customer=profile filter enforces ownership.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Verify Payment Reference",
        description="Submit gateway confirmation hashes to transition status out of pending_payment and into active.",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'payment_reference': {'type': 'string', 'description': 'Transaction reference ID from payment gateway.'}
                },
                'required': ['payment_reference']
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
    )
    def post(self, request, subscription_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            subscription = PolicySubscription.objects.get(
                id=subscription_id, customer=profile
            )
        except PolicySubscription.DoesNotExist:
            return Response({"error": "Subscription not found."}, status=404)

        if subscription.status == "pending_document":
            required_docs = get_required_documents(subscription.plan)
            uploaded_types = list(
                subscription.documents.values_list("document_type", flat=True)
            )
            missing = [doc for doc in required_docs if doc not in uploaded_types]
            return Response({
                "error": "Please upload all required documents before proceeding to payment.",
                "missing_documents": missing,
            }, status=400)

        if subscription.status != "pending_payment":
            return Response(
                {"error": "This subscription is not awaiting payment."},
                status=400
            )

        payment_reference = request.data.get("payment_reference")
        if not payment_reference:
            return Response(
                {"error": "Payment reference is required."}, status=400
            )

        subscription.payment_reference = payment_reference
        subscription.payment_verified = True
        subscription.status = "active"
        subscription.save()

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
    Partner admins (both provider and distributor) view subscriptions scoped to them.
    IsPartnerAdmin enforces: authenticated + partner_admin role.
    Queryset is scoped by partner_type — providers see their policy subscriptions,
    distributors see subscriptions they facilitated.
    """
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Partner List Subscriptions",
        description="Allows providers or distributors to view corporate-relevant subscriptions depending on organizational context.",
        parameters=[
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter context scope via current state mapping context attributes.",
                required=False
            )
        ],
        responses={200: PolicySubscriptionSerializer(many=True)}
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)

        if partner.partner_type == "provider":
            subscriptions = PolicySubscription.objects.filter(provider=partner)
        else:
            subscriptions = PolicySubscription.objects.filter(distributor=partner)

        status_filter = request.query_params.get("status")
        if status_filter:
            subscriptions = subscriptions.filter(status=status_filter)

        serializer = PolicySubscriptionSerializer(subscriptions, many=True)
        return Response({"subscriptions": serializer.data})
