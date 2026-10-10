from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsServiceAccount
from webhooks import builders as b
from webhooks.events import E
from webhooks.services import emit
from .models import Claim, ClaimPayment
from .serializers import ClaimSerializer


class AiResultSerializer(serializers.Serializer):
    score = serializers.FloatField(min_value=0, max_value=1)
    flags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    summary = serializers.CharField(required=False, allow_blank=True, default="")
    recommendation = serializers.ChoiceField(choices=["forward", "flag"])


class ReceiptSerializer(serializers.Serializer):
    receipt_url = serializers.URLField(max_length=500)


StatusResponse = inline_serializer(
    name="ServiceClaimStatusResponse",
    fields={
        "status": serializers.CharField(),
        "replayed": serializers.BooleanField(required=False),
    },
)

ErrorResponse = inline_serializer(
    name="ServiceClaimErrorResponse",
    fields={"error": serializers.CharField()},
)


class ServiceClaimAiResultView(APIView):
    permission_classes = [IsServiceAccount]

    @extend_schema(
        request=AiResultSerializer,
        responses={200: StatusResponse, 404: ErrorResponse},
        tags=["Service: Claims"],
        summary="Store AI check result (n8n)",
    )
    def post(self, request, claim_id):
        ser = AiResultSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ai = ser.validated_data

        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id).first()
            if not claim:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status != "ai_check_pending":   # replays and late callbacks are no-ops
                return Response({"status": claim.status, "replayed": True})

            claim.ai_result = ai
            flagged = bool(ai["flags"]) or ai["recommendation"] == "flag"
            claim.status = "flagged" if flagged else "forwarded"
            claim.save()

            emit(E.CLAIM_AI_CHECKED, partner=claim.provider, aggregate_id=claim.id,
                 data=b.claim_ai_checked(claim))
            if flagged:
                emit(E.CLAIM_FLAGGED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_flagged(claim))
            else:
                emit(E.CLAIM_FORWARDED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_forwarded(claim))
        return Response({"status": claim.status})


class ServiceClaimReceiptView(APIView):
    permission_classes = [IsServiceAccount]

    @extend_schema(
        request=ReceiptSerializer,
        responses={200: StatusResponse, 404: ErrorResponse, 409: ErrorResponse},
        tags=["Service: Claims"],
        summary="Attach settlement receipt (n8n)",
    )
    def post(self, request, claim_id):
        ser = ReceiptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id).first()
            if not claim:
                return Response({"error": "Claim not found."}, status=404)
            payment = claim.payments.filter(status=ClaimPayment.COMPLETED).first()
            if not payment:
                return Response({"error": "No completed payment for this claim."}, status=409)
            if payment.receipt_url:
                return Response({"status": claim.status, "replayed": True})

            payment.receipt_url = ser.validated_data["receipt_url"]
            payment.save(update_fields=["receipt_url"])
            emit(E.RECEIPT_GENERATED, partner=claim.provider, aggregate_id=claim.id,
                 data=b.receipt_generated(claim, payment), discriminator=payment.reference)
        return Response({"status": claim.status})


class ServiceClaimNotifiedView(APIView):
    permission_classes = [IsServiceAccount]

    @extend_schema(
        request=None,
        responses={200: StatusResponse, 404: ErrorResponse, 409: ErrorResponse},
        tags=["Service: Claims"],
        summary="Mark customer and provider as notified (n8n)",
    )
    def post(self, request, claim_id):
        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id).first()
            if not claim:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status not in ("paid", "rejected"):
                return Response({"error": f"Claim in '{claim.status}' is not final."}, status=409)
            if claim.parties_notified_at:
                return Response({"status": claim.status, "replayed": True})

            claim.parties_notified_at = timezone.now()
            claim.save(update_fields=["parties_notified_at", "updated_at"])
            emit(E.CLAIM_PARTIES_NOTIFIED, partner=claim.provider, aggregate_id=claim.id,
                 data=b.claim_parties_notified(claim))
        return Response({"status": claim.status})


class ServiceClaimDetailView(APIView):
    permission_classes = [IsServiceAccount]

    @extend_schema(
        responses={
            200: ClaimSerializer,
            404: inline_serializer("ServiceClaimNotFound", {"error": serializers.CharField()}),
        },
        tags=["Service: Claims"],
        summary="Get a claim (n8n)",
    )
    def get(self, request, claim_id):
        claim = (Claim.objects
                 .select_related("customer__user", "provider", "subscription__plan")
                 .prefetch_related("documents", "payments")
                 .filter(id=claim_id).first())
        if claim is None:
            return Response({"error": "Claim not found."}, status=404)
        return Response(ClaimSerializer(claim).data)
