"""Subscriptions: customer lifecycle (subscribe, documents, pay, renew, cancel) plus partner and staff views."""

import uuid
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as s
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCustomer, IsKYCVerifiedCustomer, IsPartnerAdmin, IsStaffMember
from accounts.utils import get_partner_from_user
from core.docs import MessageSerializer, Tag, error, validation_error
from core.throttles import PartnerRateThrottle
from payments.models import Transaction
from payments.serializers import TransactionSerializer
from payments.services import GatewayError, NotPayable, initiate_checkout
from plans.selectors import plans_visible_to_partner
from webhooks.services import dispatch_webhook

from . import docs as d
from .models import REQUIRED_DOCUMENTS, PolicySubscription, SubscriptionDocument
from .rules import can_cancel
from .serializers import (
    DistributorSubscriptionSerializer, PolicySubscriptionCreateSerializer, PolicySubscriptionSerializer,
    ProviderSubscriptionSerializer, StaffSubscriptionSerializer,
)

ALLOWED_UPLOAD_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ERR_NO_PROFILE = error("No customer profile under this X-Partner-Key.", "Customer profile not found.")
ERR_SUB_404 = error("Not found, or not yours.", "Subscription not found.")


# ------------------------------------------------------------------ helpers
def get_required_documents(plan):
    return REQUIRED_DOCUMENTS.get(plan.category.name, {}).get(plan.coverage_level, [])


def calculate_financials(plan, distributor):
    premium = plan.premium
    platform_fee = round(premium * Decimal("0.10"), 2)
    commission = Decimal("0.00")
    if distributor:
        try:
            commission = round(premium * (distributor.distributor_profile.commission_rate / Decimal("100")), 2)
        except Exception:
            commission = Decimal("0.00")
    return {"amount_paid": premium, "platform_fee": platform_fee, "distributor_commission": commission,
            "provider_payout": round(premium - platform_fee - commission, 2)}


def _profile(request):
    return request.user.customer_profiles.filter(partner=request.partner).first()


def _missing_docs(sub):
    uploaded = set(sub.documents.values_list("document_type", flat=True))
    return [doc for doc in get_required_documents(sub.plan) if doc not in uploaded]


def _uuid(name, value):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise s.ValidationError({name: ["Must be a valid UUID."]})


def _paginate(request, qs, serializer_cls):
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
        size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
    except ValueError:
        raise s.ValidationError({"page": ["page and page_size must be integers."]})
    items = qs[(page - 1) * size: page * size]
    return Response({"count": qs.count(), "page": page, "page_size": size,
                     "subscriptions": serializer_cls(items, many=True).data})


def _channel_filter(qs, value):
    if value == "direct":
        return qs.filter(distributor__isnull=True)
    if value == "distributor":
        return qs.filter(distributor__isnull=False)
    return qs


def _fire_cancel_webhooks(sub):
    payload = {"subscription_id": str(sub.id), "plan": sub.plan.name,
               "customer_email": sub.customer.user.email, "status": "cancelled"}

    def _go():
        dispatch_webhook(sub.provider, "subscription.cancelled", payload)
        if sub.distributor:
            dispatch_webhook(sub.distributor, "subscription.cancelled", payload)
    transaction.on_commit(_go)


# ================================================================ CUSTOMER
class CustomerSubscriptionListView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List my subscriptions",
        description="All of the logged-in customer's subscriptions with this partner, newest first. "
                    "`can_renew` / `can_cancel` tell the UI which buttons to show.",
        auth=d.CUSTOMER_AUTH, parameters=[d.STATUS_PARAM, *d.PAGING],
        responses={200: d.page_of("CustomerSubscriptionList", PolicySubscriptionSerializer), 404: ERR_NO_PROFILE},
        examples=[d.page_example(d.CUSTOMER_SUB)],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def get(self, request):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)
        qs = PolicySubscription.objects.filter(customer=profile).select_related(
            "customer__user", "plan", "provider", "distributor")
        if v := request.query_params.get("status"):
            qs = qs.filter(status=v)
        return _paginate(request, qs, PolicySubscriptionSerializer)


class CustomerSubscriptionCreateView(APIView):
    permission_classes = [IsKYCVerifiedCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Start a subscription",
        description=(
            "Step 1 of buying a plan. Requires **approved KYC**.\n\n"
            "The plan must be in the storefront of the partner whose `X-Partner-Key` is sent "
            "(a provider's own plans, or plans a distributor has approved access to).\n\n"
            "- If the plan needs documents, status is `pending_document`: upload each via `POST /subscriptions/{id}/documents/`.\n"
            "- If it needs none, status is `pending_payment`: go straight to `POST /subscriptions/{id}/pay/`.\n\n"
            "Idempotent: if an in-progress subscription for this plan exists, it is returned with **200** instead of creating a duplicate."
        ),
        auth=d.CUSTOMER_AUTH, request=PolicySubscriptionCreateSerializer,
        responses={201: d.SubscriptionInitiated, 200: d.SubscriptionInitiated,
                   400: validation_error("plan_id", "Plan not found or not available on this platform."),
                   403: d.KYC_REQUIRED, 404: ERR_NO_PROFILE,
                   409: error("Already subscribed, or a concurrent request raced us.",
                              "You already have an active subscription to this plan. Renew it instead.")},
        examples=[
            OpenApiExample("Request", request_only=True, value={"plan_id": d.PLAN_ID, "start_date": "2026-10-10"}),
            OpenApiExample("Created", response_only=True, status_codes=["201"], value={
                "message": "Subscription initiated. Please upload required documents.",
                "subscription_id": d.SUB_ID, "amount": "85000.00", "status": "pending_document",
                "required_documents": ["vehicle_license", "proof_of_ownership", "vehicle_registration",
                                       "inspection_report", "vehicle_photos"]}),
            OpenApiExample("Resumed", response_only=True, status_codes=["200"], value={
                "message": "You already have a subscription in progress for this plan. Resuming document upload.",
                "subscription_id": d.SUB_ID, "amount": "85000.00", "status": "pending_document",
                "required_documents": ["vehicle_license", "proof_of_ownership", "vehicle_registration"],
                "missing_documents": ["vehicle_registration"]}),
        ],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def post(self, request):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)

        ser = PolicySubscriptionCreateSerializer(data=request.data, context={"partner": request.partner})
        ser.is_valid(raise_exception=True)
        plan, start_date = ser.validated_data["plan"], ser.validated_data["start_date"]

        if PolicySubscription.objects.filter(customer=profile, plan=plan, status__in=["active", "grace_period"]).exists():
            return Response({"error": "You already have an active subscription to this plan. Renew it instead."}, status=409)

        existing = self._in_progress(profile, plan)
        if existing:
            return self._resume(existing)

        distributor = request.partner if request.partner.partner_type == "distributor" else None
        required = get_required_documents(plan)
        try:
            with transaction.atomic():
                sub = PolicySubscription.objects.create(
                    customer=profile, plan=plan, provider=plan.provider, distributor=distributor,
                    start_date=start_date, end_date=start_date + relativedelta(months=plan.duration_months),
                    status="pending_document" if required else "pending_payment",
                    **calculate_financials(plan, distributor))
        except IntegrityError:   # race: DB constraint is the backstop
            existing = self._in_progress(profile, plan)
            if existing:
                return self._resume(existing)
            return Response({"error": "Unable to initiate subscription right now. Please try again."}, status=409)

        return Response({
            "message": ("Subscription initiated. Please upload required documents." if required
                        else "Subscription initiated. No documents required; proceed to payment."),
            "subscription_id": str(sub.id), "amount": str(sub.amount_paid),
            "status": sub.status, "required_documents": required,
        }, status=201)

    @staticmethod
    def _in_progress(profile, plan):
        return PolicySubscription.objects.filter(
            customer=profile, plan=plan, status__in=["pending_document", "pending_payment"]).first()

    @staticmethod
    def _resume(sub):
        body = {"subscription_id": str(sub.id), "amount": str(sub.amount_paid), "status": sub.status,
                "required_documents": get_required_documents(sub.plan)}
        if sub.status == "pending_document":
            body["message"] = "You already have a subscription in progress for this plan. Resuming document upload."
            body["missing_documents"] = _missing_docs(sub)
        else:
            body["message"] = "You already have a subscription awaiting payment for this plan. Proceed to payment."
        return Response(body, status=200)


class SubscriptionDocumentUploadView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Upload a subscription document",
        description=(
            "`multipart/form-data`, one file per call. Allowed: pdf, jpg, jpeg, png, max 10 MB. "
            "Re-uploading a document type replaces the earlier file. When the last required document arrives, "
            "status moves to `pending_payment`."
        ),
        auth=d.CUSTOMER_AUTH,
        request={"multipart/form-data": inline_serializer("SubscriptionDocumentUpload", {
            "document_type": s.CharField(help_text="One of the codes in `required_documents`."),
            "file": s.FileField()})},
        responses={200: d.DocumentProgress,
                   400: error("Missing field, wrong type, bad file, or document not required.",
                              "This document is not required for this plan. Required: ['vehicle_license']"),
                   404: error("Not yours, or not awaiting documents.", "Subscription not found or not awaiting documents.")},
        examples=[
            OpenApiExample("Partial", response_only=True, status_codes=["200"], value={
                "message": "Document uploaded successfully.", "status": "pending_document",
                "missing_documents": ["inspection_report", "vehicle_photos"]}),
            OpenApiExample("Complete", response_only=True, status_codes=["200"], value={
                "message": "All documents uploaded. You can now proceed to payment.",
                "status": "pending_payment", "missing_documents": []}),
        ],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def post(self, request, subscription_id):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)
        sub = (PolicySubscription.objects.select_related("plan__category")
               .filter(id=subscription_id, customer=profile, status="pending_document").first())
        if sub is None:
            return Response({"error": "Subscription not found or not awaiting documents."}, status=404)

        document_type, file = request.data.get("document_type"), request.FILES.get("file")
        if not document_type:
            return Response({"error": "document_type is required."}, status=400)
        if not file:
            return Response({"error": "file is required."}, status=400)
        required = get_required_documents(sub.plan)
        if document_type not in {c[0] for c in SubscriptionDocument.DOCUMENT_TYPE_CHOICES}:
            return Response({"error": "Invalid document type."}, status=400)
        if document_type not in required:
            return Response({"error": f"This document is not required for this plan. Required: {required}"}, status=400)
        ext = "." + file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
        if ext not in ALLOWED_UPLOAD_EXT or file.size > MAX_UPLOAD_BYTES:
            return Response({"error": "File must be a pdf, jpg or png under 10 MB."}, status=400)

        SubscriptionDocument.objects.update_or_create(
            subscription=sub, document_type=document_type, defaults={"file": file})
        missing = _missing_docs(sub)
        if not missing:
            sub.status = "pending_payment"
            sub.save(update_fields=["status", "updated_at"])
            return Response({"message": "All documents uploaded. You can now proceed to payment.",
                            "status": "pending_payment", "missing_documents": []})
        return Response({"message": "Document uploaded successfully.", "status": "pending_document",
                         "missing_documents": missing})

    @extend_schema(
        summary="Document checklist",
        auth=d.CUSTOMER_AUTH, responses={200: d.DocumentChecklist, 404: ERR_SUB_404},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "required_documents": ["vehicle_license", "proof_of_ownership"],
            "uploaded_documents": [{"document_type": "vehicle_license",
                                    "file": "https://files.theeinsurance.com/subscriptions/documents/licence.pdf",
                                    "uploaded_at": "2026-10-06T10:05:00Z"}],
            "missing_documents": ["proof_of_ownership"], "ready_for_payment": False})],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def get(self, request, subscription_id):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)
        sub = PolicySubscription.objects.select_related("plan__category").filter(id=subscription_id, customer=profile).first()
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        uploaded = sub.documents.all()
        missing = _missing_docs(sub)
        return Response({
            "required_documents": get_required_documents(sub.plan),
            "uploaded_documents": [{"document_type": x.document_type, "file": x.file.url if x.file else None,
                                    "uploaded_at": x.uploaded_at} for x in uploaded],
            "missing_documents": missing, "ready_for_payment": not missing and sub.status == "pending_payment"})


class SubscriptionPayView(APIView):
    permission_classes = [IsKYCVerifiedCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Pay for a subscription (Paystack checkout)",
        description=(
            "Step 3. Only for `pending_payment` subscriptions. Returns a Paystack `payment_url`: redirect the customer there. "
            "Paystack sends them back to the configured callback page with `?reference=...`; call "
            "`GET /payments/verify/{reference}/` there.\n\n"
            "The amount is taken from the subscription, never from the client. Calling again within 30 minutes returns "
            "the same checkout link. **The subscription activates only after Paystack confirms the payment**; "
            "on success the customer, the provider and (if any) the distributor are emailed."
        ),
        auth=d.CUSTOMER_AUTH, request=None,
        responses={200: d.CheckoutResponse,
                   400: error("Not payable, or documents still missing.", "Please upload all required documents before proceeding to payment."),
                   403: d.KYC_REQUIRED, 404: ERR_SUB_404,
                   502: error("Paystack unreachable or rejected the request.", "Could not reach payment gateway.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=d.CHECKOUT_EXAMPLE)],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def post(self, request, subscription_id):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)
        sub = PolicySubscription.objects.select_related("plan__category").filter(id=subscription_id, customer=profile).first()
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        if sub.status == "pending_document":
            return Response({"error": "Please upload all required documents before proceeding to payment.",
                             "missing_documents": _missing_docs(sub)}, status=400)
        try:
            return Response(initiate_checkout(sub, Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION, request.user))
        except NotPayable as e:
            return Response({"error": str(e)}, status=400)
        except GatewayError as e:
            return Response({"error": str(e)}, status=502)


class SubscriptionRenewView(APIView):
    permission_classes = [IsKYCVerifiedCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Renew a subscription",
        description=(
            "Returns a Paystack checkout URL for the renewal (same flow as `/pay/`).\n\n"
            "Eligible when `can_renew` is true: `active` within 30 days of `end_date`, or `grace_period` / `expired` / `lapsed`. "
            "The price is the amount originally paid. After payment: if still covered, `end_date` is extended by the plan duration; "
            "if already expired, coverage restarts today. Renewal is refused if the plan was deactivated or this partner "
            "no longer has access to it."
        ),
        auth=d.CUSTOMER_AUTH, request=None,
        responses={200: d.CheckoutResponse,
                   400: error("Not eligible yet.", "Subscription is 'active' and is not eligible for renewal yet."),
                   403: d.KYC_REQUIRED, 404: ERR_SUB_404,
                   409: error("Plan no longer available.", "This plan is no longer available for renewal."),
                   502: error("Gateway error.", "Could not reach payment gateway.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={**d.CHECKOUT_EXAMPLE, "payment_type": "RENEWAL", "reference": "TII-1A2B3C4D5E6F7A8B"})],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def post(self, request, subscription_id):
        profile = _profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=404)
        sub = PolicySubscription.objects.select_related("plan").filter(id=subscription_id, customer=profile).first()
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        if not plans_visible_to_partner(request.partner).filter(id=sub.plan_id).exists():
            return Response({"error": "This plan is no longer available for renewal."}, status=409)
        try:
            return Response(initiate_checkout(sub, Transaction.PAYMENT_TYPE.RENEWAL, request.user))
        except NotPayable as e:
            return Response({"error": str(e)}, status=400)
        except GatewayError as e:
            return Response({"error": str(e)}, status=502)


class CustomerSubscriptionDetailView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    def _get(self, request, subscription_id):
        profile = _profile(request)
        if profile is None:
            return None
        return (PolicySubscription.objects.select_related("customer__user", "plan", "provider", "distributor")
                .filter(id=subscription_id, customer=profile).first())

    @extend_schema(
        summary="Get one subscription", auth=d.CUSTOMER_AUTH,
        responses={200: PolicySubscriptionSerializer, 404: ERR_SUB_404},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=d.CUSTOMER_SUB)],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def get(self, request, subscription_id):
        sub = self._get(request, subscription_id)
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        return Response(PolicySubscriptionSerializer(sub).data)

    @extend_schema(
        summary="Cancel a subscription",
        description=("Allowed for `pending_document`, `pending_payment`, `active` and `grace_period`. Any unpaid checkout is "
                     "voided and the provider/distributor receive a `subscription.cancelled` webhook. "
                     "**No automatic refund**: refunds are handled manually by TheeInsurance."),
        auth=d.CUSTOMER_AUTH, request=None,
        responses={200: MessageSerializer,
                   400: error("Status can't be cancelled.", "A 'cancelled' subscription cannot be cancelled."),
                   404: ERR_SUB_404},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={"message": "Subscription cancelled successfully."})],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def delete(self, request, subscription_id):
        sub = self._get(request, subscription_id)
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        if not can_cancel(sub):
            return Response({"error": f"A '{sub.status}' subscription cannot be cancelled."}, status=400)
        with transaction.atomic():
            sub.status, sub.cancelled_at = "cancelled", timezone.now()
            sub.save(update_fields=["status", "cancelled_at", "updated_at"])
            Transaction.objects.filter(subscription=sub, payment_status=Transaction.PAYMENT_STATUS.PENDING) \
                .update(payment_status=Transaction.PAYMENT_STATUS.FAILED)
            _fire_cancel_webhooks(sub)
        return Response({"message": "Subscription cancelled successfully."})


# ================================================================ PARTNER
class PartnerSubscriptionListView(APIView):
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Subscriptions associated with my organization",
        description=(
            "- **Provider:** every subscription to one of your plans, **including those sold through a distributor** "
            "(`channel=distributor`). Shows your `provider_payout`.\n"
            "- **Distributor:** every subscription sold through your platform. Shows your `distributor_commission`.\n\n"
            "Filter with `status`; providers can also filter `channel`."
        ),
        parameters=[d.STATUS_PARAM, d.CHANNEL_PARAM, *d.PAGING],
        responses={200: d.page_of("PartnerSubscriptionList", ProviderSubscriptionSerializer),
                   403: error("Not linked to a partner.", "This account is not linked to a partner.")},
        examples=[OpenApiExample("Provider", response_only=True, status_codes=["200"],
                                 value={"count": 1, "page": 1, "page_size": 20, "subscriptions": [d.PROVIDER_SUB]}),
                  OpenApiExample("Distributor", response_only=True, status_codes=["200"],
                                 value={"count": 1, "page": 1, "page_size": 20, "subscriptions": [d.DISTRIBUTOR_SUB]})],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        if partner is None:
            return Response({"error": "This account is not linked to a partner."}, status=403)
        qs = PolicySubscription.objects.select_related("customer__user", "plan", "provider", "distributor")
        if partner.partner_type == "provider":
            qs = _channel_filter(qs.filter(provider=partner), request.query_params.get("channel"))
            serializer_cls = ProviderSubscriptionSerializer
        else:
            qs = qs.filter(distributor=partner)
            serializer_cls = DistributorSubscriptionSerializer
        if v := request.query_params.get("status"):
            qs = qs.filter(status=v)
        return _paginate(request, qs, serializer_cls)


# ================================================================ STAFF
class StaffSubscriptionListView(APIView):
    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="All subscriptions on the platform",
        description="Every subscription with the full money split. Filters combine with AND.",
        parameters=[d.STATUS_PARAM, d.CHANNEL_PARAM,
                    OpenApiParameter("provider_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
                    OpenApiParameter("distributor_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
                    OpenApiParameter("customer_email", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Contains, case-insensitive."),
                    *d.PAGING],
        responses={200: d.page_of("StaffSubscriptionList", StaffSubscriptionSerializer),
                   400: validation_error("provider_id", "Must be a valid UUID.")},
        examples=[d.page_example(d.STAFF_SUB)],
        tags=[Tag.SUBSCRIPTIONS, Tag.STAFF],
    )
    def get(self, request):
        p = request.query_params
        qs = PolicySubscription.objects.select_related("customer__user", "plan", "provider", "distributor")
        if v := p.get("status"):
            qs = qs.filter(status=v)
        if v := p.get("provider_id"):
            qs = qs.filter(provider_id=_uuid("provider_id", v))
        if v := p.get("distributor_id"):
            qs = qs.filter(distributor_id=_uuid("distributor_id", v))
        if v := p.get("customer_email"):
            qs = qs.filter(customer__user__email__icontains=v)
        return _paginate(request, _channel_filter(qs, p.get("channel")), StaffSubscriptionSerializer)


class StaffSubscriptionDetailView(APIView):
    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="One subscription with its payment history",
        responses={200: inline_serializer("StaffSubscriptionDetail", {
            "subscription": StaffSubscriptionSerializer(), "transactions": TransactionSerializer(many=True)}),
                   404: error("Unknown id.", "Subscription not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "subscription": d.STAFF_SUB,
            "transactions": [{
                "id": "e2b6c1a0-3d4f-4b5a-9c8d-1f2e3a4b5c6d", "reference": "TII-9F3A7C1D2B4E6A80",
                "amount": "85000.00", "currency": "NGN", "payment_type": "NEW_SUBSCRIPTION",
                "payment_status": "SUCCESSFUL", "gateway": "PAYSTACK", "gateway_reference": "4099260516",
                "subscription_id": d.SUB_ID, "subscription_status": "active", "initiated_by": "chidi@example.com",
                "created_at": "2026-10-06T10:15:00Z", "updated_at": "2026-10-06T10:16:12Z"}]})],
        tags=[Tag.SUBSCRIPTIONS, Tag.STAFF],
    )
    def get(self, request, subscription_id):
        sub = (PolicySubscription.objects.select_related("customer__user", "plan", "provider", "distributor")
               .filter(id=subscription_id).first())
        if sub is None:
            return Response({"error": "Subscription not found."}, status=404)
        txns = Transaction.objects.filter(subscription=sub).select_related("subscription", "initiated_by")
        return Response({"subscription": StaffSubscriptionSerializer(sub).data,
                         "transactions": TransactionSerializer(txns, many=True).data})
