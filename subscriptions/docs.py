from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, inline_serializer
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers as s

from .models import PolicySubscription

SUB_ID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CUSTOMER_ID = "9b1d6c0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
PLAN_ID = "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
CUSTOMER_AUTH = [{"BearerAuth": [], "PartnerKey": []}]

_BASE = {
    "id": SUB_ID, "customer": CUSTOMER_ID, "customer_email": "chidi@example.com", "plan": PLAN_ID,
    "plan_name": "Motor Comprehensive Plus", "provider_name": "Sunrise Assurance Plc",
    "distributor_name": "QuickCover Ltd", "channel": "distributor",
    "start_date": "2026-10-10", "end_date": "2027-10-10", "status": "active", "amount_paid": "85000.00",
    "payment_reference": "TII-9F3A7C1D2B4E6A80", "payment_verified": True,
    "created_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T10:16:12Z",
}
CUSTOMER_SUB = {**_BASE, "can_renew": False, "can_cancel": True}
PROVIDER_SUB = {**_BASE, "provider_payout": "65875.00"}
DISTRIBUTOR_SUB = {**_BASE, "distributor_commission": "10625.00"}
STAFF_SUB = {**_BASE, "provider": "a7c2e9d4-5b3f-4e1a-9d8c-6b5a4f3e2d1c",
             "distributor": "3f6c1f4e-8a58-4b6e-9a53-2d6a7f0d9c11",
             "provider_payout": "65875.00", "distributor_commission": "10625.00", "platform_fee": "8500.00"}


def page_of(name, serializer):
    return inline_serializer(name, {
        "count": s.IntegerField(), "page": s.IntegerField(), "page_size": s.IntegerField(),
        "subscriptions": serializer(many=True)})


def page_example(item):
    return OpenApiExample("OK", response_only=True, status_codes=["200"],
                          value={"count": 1, "page": 1, "page_size": 20, "subscriptions": [item]})


STATUS_PARAM = OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY,
                                enum=[c[0] for c in PolicySubscription.STATUS_CHOICES])
PAGING = [OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, default=1),
          OpenApiParameter("page_size", OpenApiTypes.INT, OpenApiParameter.QUERY, default=20, description="Max 100.")]
CHANNEL_PARAM = OpenApiParameter("channel", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=["direct", "distributor"])

KYC_REQUIRED = OpenApiResponse(
    response=OpenApiTypes.OBJECT, description="Customer KYC is not approved.",
    examples=[OpenApiExample("KYC required", value={
        "error": "Complete KYC verification before purchasing a plan.",
        "code": "kyc_required", "kyc_status": "pending"})])

SubscriptionInitiated = inline_serializer("SubscriptionInitiated", {
    "message": s.CharField(), "subscription_id": s.UUIDField(), "amount": s.CharField(),
    "status": s.CharField(), "required_documents": s.ListField(child=s.CharField()),
    "missing_documents": s.ListField(child=s.CharField(), required=False)})

DocumentProgress = inline_serializer("DocumentProgress", {
    "message": s.CharField(), "status": s.CharField(), "missing_documents": s.ListField(child=s.CharField())})

DocumentChecklist = inline_serializer("DocumentChecklist", {
    "required_documents": s.ListField(child=s.CharField()),
    "uploaded_documents": inline_serializer("UploadedDocument", {
        "document_type": s.CharField(), "file": s.CharField(allow_null=True), "uploaded_at": s.DateTimeField()}, many=True),
    "missing_documents": s.ListField(child=s.CharField()), "ready_for_payment": s.BooleanField()})

CheckoutResponse = inline_serializer("CheckoutResponse", {
    "payment_url": s.URLField(help_text="Redirect the customer here (Paystack hosted checkout)."),
    "reference": s.CharField(help_text="Pass to GET /payments/verify/{reference}/ after the redirect back."),
    "amount": s.CharField(), "currency": s.CharField(), "subscription_id": s.UUIDField(),
    "payment_type": s.ChoiceField(["NEW_SUBSCRIPTION", "RENEWAL"])})

CHECKOUT_EXAMPLE = {"payment_url": "https://checkout.paystack.com/3ni8kdavz62431k",
                    "reference": "TII-9F3A7C1D2B4E6A80", "amount": "85000.00", "currency": "NGN",
                    "subscription_id": SUB_ID, "payment_type": "NEW_SUBSCRIPTION"}
