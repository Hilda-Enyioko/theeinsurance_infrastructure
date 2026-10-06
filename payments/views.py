"""Paystack payment endpoints: verify (customer) and webhook (Paystack)."""

import json
import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCustomer
from core.docs import DetailSerializer, Tag, error
from core.throttles import PartnerRateThrottle

from .models import Transaction
from .serializers import TransactionSerializer
from .services import (
    PaymentError, SignatureVerificationError, paystack_verify, settle, verify_webhook_signature,
)

log = logging.getLogger(__name__)

SUB_ID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
TXN_EXAMPLE = {
    "id": "e2b6c1a0-3d4f-4b5a-9c8d-1f2e3a4b5c6d", "reference": "TII-9F3A7C1D2B4E6A80",
    "amount": "85000.00", "currency": "NGN", "payment_type": "NEW_SUBSCRIPTION",
    "payment_status": "SUCCESSFUL", "gateway": "PAYSTACK", "gateway_reference": "4099260516",
    "subscription_id": SUB_ID, "subscription_status": "active", "initiated_by": "chidi@example.com",
    "created_at": "2026-10-06T10:15:00Z", "updated_at": "2026-10-06T10:16:12Z",
}


class PaymentVerifyView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Verify a payment after Paystack redirects back",
        description=(
            "Call this from your callback page with the `reference` Paystack appended to the redirect URL "
            "(or the `reference` returned by `/pay/` or `/renew/`).\n\n"
            "If the payment is still `PENDING` we ask Paystack directly and update the subscription, so the UI "
            "doesn't have to wait for the webhook. Safe to poll every few seconds. When `payment_status` is "
            "`SUCCESSFUL`, `subscription_status` is `active`."
        ),
        auth=[{"BearerAuth": [], "PartnerKey": []}],
        parameters=[OpenApiParameter("reference", OpenApiTypes.STR, OpenApiParameter.PATH,
                                     description="Our transaction reference (`TII-...`).")],
        responses={200: TransactionSerializer,
                   404: error("Unknown reference, or it belongs to another customer.", "Transaction not found.")},
        examples=[OpenApiExample("Paid", response_only=True, status_codes=["200"], value=TXN_EXAMPLE),
                  OpenApiExample("Still pending", response_only=True, status_codes=["200"],
                                 value={**TXN_EXAMPLE, "payment_status": "PENDING", "gateway_reference": None,
                                        "subscription_status": "pending_payment"})],
        tags=[Tag.SUBSCRIPTIONS],
    )
    def get(self, request, reference):
        txn = (Transaction.objects.select_related("subscription", "initiated_by")
               .filter(reference=reference, subscription__customer__user=request.user).first())
        if txn is None:
            return Response({"error": "Transaction not found."}, status=status.HTTP_404_NOT_FOUND)
        if txn.payment_status == Transaction.PAYMENT_STATUS.PENDING:
            try:
                settle(reference, paystack_verify(reference), source="verify")
            except PaymentError as e:
                log.warning("Verify failed for %s: %s", reference, e)   # webhook will reconcile
            txn = Transaction.objects.select_related("subscription", "initiated_by").get(pk=txn.pk)
        return Response(TransactionSerializer(txn).data)


class PaystackWebhookView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = []   # protected by the signature, and Paystack retries in bursts

    @extend_schema(
        summary="Paystack webhook (server to server)",
        description=(
            "Called by Paystack, not by your frontend. Configure it in the Paystack dashboard as "
            "`/api/v1/payments/paystack/webhook/`.\n\n"
            "The raw body is verified against the `x-paystack-signature` header (HMAC-SHA512 with the secret key). "
            "Only `charge.success` changes state; other events are acknowledged and ignored. Processing is idempotent. "
            "An unexpected server error returns **500** so Paystack retries."
        ),
        auth=[],
        parameters=[OpenApiParameter("x-paystack-signature", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        request=inline_serializer("PaystackEvent", {"event": s.CharField(), "data": s.DictField()}),
        responses={200: DetailSerializer, 400: error("Invalid JSON.", "Invalid JSON.", key="detail"),
                   401: error("Bad or missing signature.", "Invalid signature.", key="detail")},
        examples=[OpenApiExample("charge.success", request_only=True, value={
            "event": "charge.success",
            "data": {"id": 4099260516, "status": "success", "reference": "TII-9F3A7C1D2B4E6A80",
                     "amount": 8500000, "currency": "NGN", "paid_at": "2026-10-06T10:16:10.000Z",
                     "customer": {"email": "chidi@example.com"}}}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={"detail": "Received."})],
        tags=[Tag.WEBHOOKS],
    )
    def post(self, request):
        raw = request.body   # read BEFORE touching request.data (stream can only be consumed once)
        try:
            verify_webhook_signature(raw, request.headers.get("x-paystack-signature", ""))
        except SignatureVerificationError:
            log.warning("Paystack webhook rejected: bad signature")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Response({"detail": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)

        if payload.get("event") == "charge.success":
            data = payload.get("data") or {}
            try:
                settle(data.get("reference", ""), data, source="webhook")
            except Exception:
                log.exception("Paystack webhook processing failed")
                return Response({"detail": "Processing error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"detail": "Received."})
