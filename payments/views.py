"""
Payments Views Module.

Three endpoints covering the full Quickteller Pay redirect lifecycle:
  1. POST /payments/initiate/   — create Transaction, get payment URL
  2. GET  /payments/callback/   — user lands here after Interswitch redirect
  3. POST /payments/webhook/    — Interswitch server-to-server notification
"""

import logging
import json

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from .models import Transaction
from .serializers import InitiatePaymentSerializer, TransactionSerializer
from core.throttles import PartnerRateThrottle
from .services import (
    PaymentError,
    SignatureVerificationError,
    initiate_payment,
    process_interswitch_webhook,
    process_nomba_webhook,
    verify_transaction,
    initiate_nomba_checkout,
)

logger = logging.getLogger(__name__)


class InitiatePaymentView(APIView):
    """
    POST /payments/initiate/

    Validates the request, creates a PENDING Transaction, calls Interswitch
    to get a payment URL, and returns it to the frontend for redirect.

    Request body:
        subscription_id (UUID): The PolicySubscription to pay for.
        payment_type (str):     NEW_SUBSCRIPTION or RENEWAL.

    Response:
        200: { payment_url, reference }
        400: Validation error
        502: Gateway error
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [PartnerRateThrottle]

    def post(self, request: Request) -> Response:
        serializer = InitiatePaymentSerializer(
            data=request.data,
            context={'request': request}
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            transaction = serializer.save()
            result = initiate_payment(transaction)
        except PaymentError as e:
            logger.error(
                "Payment initiation failed for user %s: %s",
                request.user.id, str(e)
            )
            return Response(
                {"detail": str(e)},
                status=status.HTTP_502_BAD_GATEWAY
            )

        return Response(result, status=status.HTTP_200_OK)


class PaymentCallbackView(APIView):
    """
    GET /payments/callback/

    Interswitch redirects the user here after a payment attempt.
    Verifies the transaction status with Interswitch, updates the Transaction
    record, and returns the current state to the frontend.

    Query params:
        ref (str): Internal transaction reference (TII-XXXX).

    Response:
        200: TransactionSerializer data
        400: Missing reference
        404: Transaction not found
        502: Gateway verification error
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request: Request) -> Response:
        reference = request.query_params.get('ref')

        if not reference:
            return Response(
                {"detail": "Missing transaction reference."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            txn = Transaction.objects.get(reference=reference)
        except Transaction.DoesNotExist:
            return Response(
                {"detail": "Transaction not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        # Only re-verify with gateway if still PENDING
        # (webhook may have already updated it)
        if txn.payment_status == Transaction.PAYMENT_STATUS.PENDING:
            try:
                gateway_data = verify_transaction(reference)
                response_code = gateway_data.get("responseCode", "")

                txn.gateway_response = gateway_data
                txn.gateway_reference = gateway_data.get("retrievalReferenceNumber", "")
                txn.payment_status = (
                    Transaction.PAYMENT_STATUS.SUCCESSFUL
                    if response_code == "00"
                    else Transaction.PAYMENT_STATUS.FAILED
                )
                txn.save(update_fields=[
                    "payment_status",
                    "gateway_reference",
                    "gateway_response",
                    "updated_at"
                ])

            except PaymentError as e:
                logger.warning(
                    "Could not verify txn %s on callback: %s", reference, str(e)
                )
                # Return current state even if verification fails —
                # webhook will reconcile asynchronously

        return Response(
            TransactionSerializer(txn).data,
            status=status.HTTP_200_OK
        )


class InterswitchWebhookView(APIView):
    """
    POST /payments/webhook/

    Interswitch server-to-server callback. No authentication required
    (Interswitch won't send JWT), but signature verification is mandatory.

    This view is intentionally minimal — all logic lives in process_webhook().
    Always returns 200 to Interswitch even on duplicates/flags, to prevent
    Interswitch from retrying indefinitely.

    Headers:
        x-interswitch-signature: HMAC-SHA512 of raw body

    Response:
        200: Always (processing outcome is logged, not surfaced)
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [PartnerRateThrottle]

    def post(self, request: Request) -> Response:
        signature = request.headers.get('x-interswitch-signature', '')

        if not signature:
            logger.warning("Webhook received with no signature header.")
            return Response(
                {"detail": "Missing signature."},
                status=status.HTTP_400_BAD_REQUEST
            )

        raw_body = request.body

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response(
                {"detail": "Invalid JSON."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            process_interswitch_webhook(
                payload=payload,
                raw_body=raw_body,
                signature=signature,
            )
        except SignatureVerificationError:
            logger.warning("Webhook rejected — invalid signature.")
            return Response(
                {"detail": "Invalid signature."},
                status=status.HTTP_401_UNAUTHORIZED
            )
        except Exception as e:
            # Catch-all — log and return 200 anyway so Interswitch stops retrying
            logger.error("Unexpected error processing webhook: %s", str(e))

        return Response({"detail": "Received."}, status=status.HTTP_200_OK)


# Nomba
# -----------------------------------------------------------------------------------
class NombaCheckoutView(APIView):
    """
    POST /payments/nomba/checkout/

    Partner backend calls this after:
      1. Customer has selected a plan (subscription in 'pending_payment')
      2. Customer has explicitly consented to automated renewal charges

    Request body:
      {
        "subscription_id":    "<uuid>",
        "customer_consented": true
      }

    Response:
      {
        "checkout_link":   "https://checkout.nomba.com/pay/...",
        "order_reference": "<nomba-order-ref>",
        "transaction_ref": "TII-XXXXXXXX",
        "amount":          "5000.00",
        "currency":        "NGN"
      }

    The partner redirects their customer to checkout_link to complete payment.
    On success, Nomba fires a webhook to /payments/nomba/webhook/ containing
    the tokenKey for future automated charges.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes   = [PartnerRateThrottle]

    @extend_schema(
        summary="Initiate Nomba Checkout",
        description=(
            "Creates a Nomba checkout order for a pending subscription. "
            "Requires explicit customer consent to automated charges. "
            "Returns a checkout link for the partner to redirect their customer to."
        ),
        request={
            "application/json": {
                "type": "object",
                "required": ["subscription_id", "customer_consented"],
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the PolicySubscription in pending_payment status.",
                    },
                    "customer_consented": {
                        "type": "boolean",
                        "description": "Must be true. Confirms customer agreed to automated renewal charges.",
                    },
                },
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "checkout_link":   {"type": "string"},
                    "order_reference": {"type": "string"},
                    "transaction_ref": {"type": "string"},
                    "amount":          {"type": "string"},
                    "currency":        {"type": "string"},
                },
            },
            400: {"description": "Validation error or subscription not in correct state."},
            500: {"description": "Gateway error."},
        },
        tags=["Payments"],
        auth=["jwtAuth"],
    )
    def post(self, request: Request) -> Response:
        subscription_id    = request.data.get("subscription_id")
        customer_consented = request.data.get("customer_consented", False)

        if not subscription_id:
            return Response(
                {"error": "subscription_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(customer_consented, bool):
            return Response(
                {"error": "customer_consented must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = initiate_nomba_checkout(subscription_id, customer_consented)
            return Response(result, status=status.HTTP_200_OK)
        except PaymentError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.error("Unexpected error in NombaCheckoutView: %s", str(e))
            return Response(
                {"error": "An unexpected error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class NombaWebhookView(APIView):
    """
    POST /payments/nomba/webhook/

    Nomba server-to-server callback after checkout completion.
    No authentication required — Nomba won't send a JWT.
    Signature verification is mandatory and happens inside
    process_nomba_webhook().

    Nomba signs using a composite string (not raw body). The timestamp
    header is extracted here and injected into the payload dict before
    passing to the service, so the service has everything it needs for
    verification without an extra function parameter.

    Headers:
        nomba-signature:  HMAC-SHA256 base64 of composite string
        nomba-timestamp:  timestamp string used in signature construction

    Response:
        200: Always — outcome is logged, not surfaced to Nomba
    """

    permission_classes     = []
    authentication_classes = []
    throttle_classes       = [PartnerRateThrottle]

    def post(self, request: Request) -> Response:
        signature = request.headers.get("nomba-signature", "")
        timestamp = request.headers.get("nomba-timestamp", "")

        if not signature:
            logger.warning("Nomba webhook received with no signature header.")
            return Response(
                {"detail": "Missing signature."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not timestamp:
            logger.warning("Nomba webhook received with no timestamp header.")
            return Response(
                {"detail": "Missing timestamp."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return Response(
                {"detail": "Invalid JSON."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Inject timestamp so the service can build the composite
        # signature string without needing a separate parameter
        payload["_nomba_timestamp"] = timestamp

        try:
            process_nomba_webhook(
                payload=payload,
                signature=signature,
            )
        except SignatureVerificationError:
            logger.warning("Nomba webhook rejected — invalid signature.")
            return Response(
                {"detail": "Invalid signature."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error("Unexpected error processing Nomba webhook: %s", str(e))

        return Response({"detail": "Received."}, status=status.HTTP_200_OK)

