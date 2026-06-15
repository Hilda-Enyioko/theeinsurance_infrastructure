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

from .models import Transaction
from .serializers import InitiatePaymentSerializer, TransactionSerializer
from .services import (
    PaymentError,
    SignatureVerificationError,
    initiate_payment,
    process_webhook,
    verify_transaction,
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


class PaymentWebhookView(APIView):
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
            process_webhook(
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
