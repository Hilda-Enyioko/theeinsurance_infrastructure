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
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

from .models import Transaction
from .serializers import InitiatePaymentSerializer, TransactionSerializer
from core.throttles import PartnerRateThrottle
from accounts.permissions import IsServiceAccount
from webhooks.services import dispatch_webhook
from .services import (
    PaymentError,
    SignatureVerificationError,
    initiate_payment,
    process_interswitch_webhook,
    process_nomba_webhook,
    verify_transaction,
    verify_nomba_transaction,
    initiate_nomba_checkout,
    charge_policy_renewal,
)

logger = logging.getLogger(__name__)


class InitiatePaymentView(APIView):
    """
    POST /payments/initiate/

    Validates the request, creates a PENDING Transaction, calls Interswitch
    to get a payment URL, and returns it to the frontend for redirect.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Initiate Interswitch Payment",
        description=(
            "Validates the request, creates a PENDING Transaction record, "
            "calls Interswitch to fetch a payment URL, and returns it to the frontend."
        ),
        request=InitiatePaymentSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "payment_url": {"type": "string", "format": "uri"},
                    "reference": {"type": "string"},
                },
            },
            400: {"description": "Validation error."},
            502: {"description": "Gateway error."},
        },
        tags=["Payments"]
    )
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
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Interswitch Payment Callback",
        description=(
            "Handles the user redirect landing from Interswitch. Verifies the status "
            "with the gateway if still pending, updates the database, and returns the transaction state."
        ),
        parameters=[
            OpenApiParameter(
                name="ref",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Internal transaction reference (e.g., TII-XXXX).",
            )
        ],
        responses={
            200: TransactionSerializer,
            400: {"description": "Missing transaction reference."},
            404: {"description": "Transaction not found."},
            502: {"description": "Gateway verification error."},
        },
        tags=["Payments"]
    )
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
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Interswitch Server Webhook",
        description=(
            "Receives server-to-server transaction status notifications from Interswitch. "
            "Requires an 'x-interswitch-signature' header. Always returns a 200 OK to stop retries."
        ),
        parameters=[
            OpenApiParameter(
                name="x-interswitch-signature",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="HMAC-SHA512 signature of the raw request body.",
            )
        ],
        request={"application/json": {"type": "object"}},
        responses={
            200: {"description": "Webhook received successfully (outcome is logged asynchronously)."},
            400: {"description": "Missing signature or invalid JSON payload."},
            401: {"description": "Invalid signature verification failure."},
        },
        tags=["Webhooks"],
        auth=[],
    )
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
        tags=["Payments"]
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


class NombaCallbackView(APIView):
    """
    GET /payments/nomba/callback/

    Nomba redirects the customer here after a checkout attempt (success or
    failure). No authentication — the customer arrives straight from
    Nomba's hosted checkout page and won't be carrying a JWT.

    Nomba's redirect query params are named confusingly relative to the
    create-order response:
      - `orderReference` on the redirect = OUR merchant reference
        (Transaction.reference), i.e. what we originally sent Nomba as
        orderReference when creating the checkout order.
      - `orderId` on the redirect = NOMBA's own order ID, which is what
        their create-order response called `orderReference` and what we
        stored as Transaction.gateway_reference.

    We match on either, since we can't fully trust which one Nomba will
    reliably send in all cases.

    If the webhook hasn't landed yet (still PENDING), re-verifies directly
    with Nomba as a second source of truth, same pattern as PaymentCallbackView.
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Nomba Payment Callback",
        description=(
            "Handles the user redirect landing from Nomba after checkout. "
            "Re-verifies with Nomba directly if still pending (webhook may not "
            "have landed yet), updates the database, and returns the transaction state."
        ),
        parameters=[
            OpenApiParameter(
                name="orderReference",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Our merchant reference (Transaction.reference), appended by Nomba on redirect.",
            ),
            OpenApiParameter(
                name="orderId",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Nomba's own order ID (Transaction.gateway_reference), appended by Nomba on redirect.",
            ),
        ],
        responses={
            200: TransactionSerializer,
            400: {"description": "Missing orderReference and orderId."},
            404: {"description": "Transaction not found."},
            502: {"description": "Gateway verification error."},
        },
        tags=["Payments"],
    )
    def get(self, request: Request) -> Response:
        order_reference = request.query_params.get('orderReference')
        order_id = request.query_params.get('orderId')

        if not order_reference and not order_id:
            return Response(
                {"detail": "Missing orderReference and orderId."},
                status=status.HTTP_400_BAD_REQUEST
            )

        from django.db.models import Q

        txn = Transaction.objects.filter(
            Q(reference=order_reference) | Q(gateway_reference=order_id)
        ).first()

        if not txn:
            return Response(
                {"detail": "Transaction not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        # Only re-verify with gateway if still PENDING
        # (webhook may have already updated it)
        if txn.payment_status == Transaction.PAYMENT_STATUS.PENDING:
            try:
                gateway_data = verify_nomba_transaction(txn.gateway_reference)
                nomba_data = gateway_data.get("data", {})
                gateway_status = nomba_data.get("status", "")

                txn.gateway_response = gateway_data
                txn.payment_status = (
                    Transaction.PAYMENT_STATUS.SUCCESSFUL
                    if gateway_status == "SUCCESS"
                    else Transaction.PAYMENT_STATUS.FAILED
                    if gateway_status == "FAILED"
                    else Transaction.PAYMENT_STATUS.PENDING
                )
                txn.save(update_fields=[
                    "payment_status",
                    "gateway_response",
                    "updated_at"
                ])

            except PaymentError as e:
                logger.warning(
                    "Could not verify Nomba txn %s on callback: %s", txn.reference, str(e)
                )
                # Return current state even if verification fails —
                # webhook will reconcile asynchronously

        return Response(
            TransactionSerializer(txn).data,
            status=status.HTTP_200_OK
        )


class NombaWebhookView(APIView):
    """
    POST /payments/nomba/webhook/

    Nomba server-to-server callback after checkout completion.
    No authentication required — Nomba won't send a JWT.
    Signature verification is mandatory and happens inside
    process_nomba_webhook().
    """

    permission_classes     = []
    authentication_classes = []
    throttle_classes       = [PartnerRateThrottle]

    @extend_schema(
        summary="Nomba Server Webhook",
        description=(
            "Handles checkout processing updates asynchronously from Nomba. "
            "Validates authenticity via custom timestamp composite cryptographic headers."
        ),
        parameters=[
            OpenApiParameter(
                name="nomba-signature",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="HMAC-SHA256 base64 string derived from composite structural context.",
            ),
            OpenApiParameter(
                name="nomba-timestamp",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Epoch time format token assigned to prevent payload mutation.",
            ),
        ],
        request={"application/json": {"type": "object"}},
        responses={
            200: {"description": "Payload received and verified."},
            400: {"description": "Missing custom layout headers or semantic parser JSON issues."},
            401: {"description": "Signature verification engine denied transaction execution context."},
        },
        tags=["Webhooks"],
        auth=[],
    )
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

        logger.info("Nomba webhook raw payload: %s", json.dumps(payload))
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


class NombaRenewalChargeView(APIView):
    """
    POST /payments/nomba/renewal/charge/
    """
    permission_classes = [IsAuthenticated]
    throttle_classes   = [PartnerRateThrottle]

    @extend_schema(
        summary="Trigger auto-renewal charge",
        description=(
            "Charges a subscription renewal using its stored Nomba tokenized card. "
            "Called by n8n on renewal dates and dunning retries (day 1, 3, 7). "
            "Requires service account JWT."
        ),
        request={
            "application/json": {
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "format": "uuid",
                    },
                },
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "outcome":         {"type": "string", "enum": ["success", "failed"]},
                    "transaction_ref": {"type": "string"},
                    "subscription_id": {"type": "string"},
                    "amount":          {"type": "string"},
                    "failure_reason":  {"type": "string"},
                },
            },
            400: {"description": "Missing or invalid subscription_id, or business rule violation."},
        },
        tags=["Payments"]
    )
    def post(self, request: Request) -> Response:
        subscription_id = request.data.get("subscription_id")

        if not subscription_id:
            return Response(
                {"error": "subscription_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = charge_policy_renewal(subscription_id)
            return Response(result, status=status.HTTP_200_OK)
        except PaymentError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.error("Unexpected error in NombaRenewalChargeView: %s", str(e))
            return Response(
                {"error": "An unexpected error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class DunningFinalFailureView(APIView):
    """
    POST /payments/dunning/final-failure/

    Called by n8n once the dunning schedule (day 1, 3, 7) is exhausted and
    the subscription still hasn't recovered. Marks the subscription
    cancelled and dispatches a subscription.cancelled webhook to the
    partner.
    """
    permission_classes = [IsServiceAccount]

    @extend_schema(
        summary="Handle Final Dunning Failure",
        description=(
            "Executes post-dunning lifecycle automation routines when recovery attempts fail. "
            "Terminates active access windows and transitions state explicitly to 'lapsed'."
        ),
        request={
            "application/json": {
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "string", "format": "uuid"},
                    "transaction_ref": {"type": "string", "description": "Last failing reference string context."},
                    "reason": {"type": "string", "default": "dunning_exhausted"},
                },
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "subscription_id": {"type": "string", "format": "uuid"},
                },
            },
            400: {"description": "Missing subscription identifier parameters."},
            404: {"description": "Target subscription reference does not exist contextually."},
        },
        tags=["Payments"]
    )
    def post(self, request):
        subscription_id = request.data.get("subscription_id")
        transaction_ref = request.data.get("transaction_ref", "")
        reason = request.data.get("reason", "dunning_exhausted")

        if not subscription_id:
            return Response({"error": "subscription_id is required."}, status=400)

        from subscriptions.models import PolicySubscription

        try:
            sub = PolicySubscription.objects.select_related(
                "customer", "plan", "customer__partner"
            ).get(id=subscription_id)
        except PolicySubscription.DoesNotExist:
            return Response({"error": "Subscription not found."}, status=404)

        # Idempotent — n8n may retry this call on network failure
        if sub.status == "lapsed":
            return Response(
                {"message": "Subscription already lapsed.", "subscription_id": str(sub.id)},
                status=200,
            )

        sub.status = "lapsed"
        sub.auto_charge_enabled = False
        sub.save(update_fields=["status", "auto_charge_enabled", "updated_at"])

        logger.info(
            "Subscription %s lapsed after dunning exhausted (txn=%s, reason=%s)",
            sub.id, transaction_ref, reason,
        )

        partner = getattr(sub.customer, "partner", None)
        if partner:
            dispatch_webhook(
                partner=partner,
                event_type="subscription.cancelled",
                payload={
                    "event": "subscription.cancelled",
                    "subscription_id": str(sub.id),
                    "customer_email": sub.customer.user.email,
                    "transaction_reference": transaction_ref,
                    "reason": reason,
                    "cancelled_at": timezone.now().isoformat(),
                },
            )
        else:
            logger.warning(
                "No partner found for subscription %s — could not dispatch subscription cancelled notification.",
                sub.id,
            )

        return Response({
            "message": "Subscription lapsed and partner notified.",
            "subscription_id": str(sub.id),
        }, status=200)
