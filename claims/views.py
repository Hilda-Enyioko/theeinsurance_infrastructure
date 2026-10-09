from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as s
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.emails import on_commit_send
from accounts.permissions import IsCustomer, IsHumanStaff, IsProviderAdmin, IsSuperAdmin
from accounts.utils import get_partner_from_user
from core.docs import PARTNER_KEY_AUTH, Tag, error, validation_error
from core.throttles import PartnerRateThrottle
from webhooks import builders as b
from webhooks.events import E
from webhooks.services import emit

from . import emails as claim_emails
from .docs import (AI_RESULT_EXAMPLE, CLAIM_CREATED_201, CLAIM_EXAMPLE, CLAIM_STAFF_EXAMPLE,
                   CREATE_REQ_EXAMPLE, DOC_EXAMPLE, PAYMENT_EXAMPLE)
from .models import Claim, ClaimDocument, ClaimPayment, REQUIRED_CLAIM_DOCUMENTS
from .serializers import (ClaimCreateSerializer, ClaimCustomerSerializer, ClaimDocumentSerializer,
                          ClaimReviewSerializer, ClaimSerializer)
from .settlement import start_claim_payout

DOC_UPLOAD_STATUSES = ("submitted", "more_info_required")
PROVIDER_REVIEWABLE = ("forwarded", "more_info_required")
PROVIDER_DECISIONS = ("approved", "rejected", "more_info_required")
STAFF_TRANSITIONS = {
    "flagged": {"forwarded", "rejected", "more_info_required"},
    "ai_check_pending": {"forwarded", "flagged"},   # manual override if AI is down
}

NO_PROFILE = "Customer profile not found."
SETTLEMENT_MISSING = {
    "error": "The customer has no settlement account on file. Request more information so they can add one.",
    "code": "customer_settlement_missing",
}

CLAIM_ID = OpenApiParameter("claim_id", OpenApiTypes.UUID, OpenApiParameter.PATH, description="Claim UUID.")
STATUS_PARAM = OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                                enum=[c[0] for c in Claim.STATUS_CHOICES], description="Filter by workflow status.")
ClaimListCustomer = inline_serializer("CustomerClaimList", {"claims": ClaimCustomerSerializer(many=True)})
ClaimListFull = inline_serializer("ClaimList", {"claims": ClaimSerializer(many=True)})
ReviewResponse = inline_serializer("ClaimReviewResponse", {
    "message": s.CharField(), "claim_reference": s.CharField(), "status": s.CharField()})


def _claims():
    return (Claim.objects.select_related("customer__user", "provider", "subscription__plan")
            .prefetch_related("documents", "payments"))


def _customer_profile(request):
    return request.user.customer_profiles.filter(partner=request.partner).first()


# ============================================================ CUSTOMER
class CustomerClaimListCreateView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="customer_claims_list",
        summary="List my claims",
        description="All claims the customer has filed with the partner in `X-Partner-Key`, newest first. "
                    "Includes `documents`, `payments` (masked account, receipt link) and `settlement_on_file`.",
        auth=PARTNER_KEY_AUTH,
        parameters=[STATUS_PARAM],
        responses={200: ClaimListCustomer, 404: error("Customer profile not found for this partner.", NO_PROFILE)},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"claims": [CLAIM_EXAMPLE]})],
        tags=[Tag.CLAIMS],
    )
    def get(self, request):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)
        qs = _claims().filter(customer=profile).order_by("-submitted_at")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"claims": ClaimCustomerSerializer(qs, many=True).data})

    @extend_schema(
        summary="Submit a claim",
        description=(
            "**Step 1 of the claims flow.** Files a claim against one of the customer's `active` subscriptions. "
            "The claim starts as `submitted`.\n\n"
            "Validation: the subscription must belong to the caller, `claim_type` must match the plan category "
            "(motor vs travel), and `incident_date` must be within the coverage period and not in the future.\n\n"
            "**What the app does next**\n"
            "1. Upload every file in `required_documents` via `next_steps.upload_documents`.\n"
            "2. If `settlement_on_file` is `false`, collect bank details and call `PATCH /auth/profile/`. "
            "A provider cannot approve the claim without them.\n\n"
            "Once the last required document is uploaded the claim moves to `ai_check_pending` automatically."
        ),
        auth=PARTNER_KEY_AUTH,
        request=ClaimCreateSerializer,
        responses={
            201: inline_serializer("ClaimCreated", {
                "message": s.CharField(), "claim_id": s.UUIDField(), "claim_reference": s.CharField(),
                "required_documents": s.ListField(child=s.CharField()), "settlement_on_file": s.BooleanField(),
                "next_steps": s.DictField(child=s.CharField(allow_null=True))}),
            400: validation_error("claim_type", "This claim type is not valid for motor insurance."),
            404: error("Customer profile not found for this partner.", NO_PROFILE),
        },
        examples=[OpenApiExample("Request", request_only=True, value=CREATE_REQ_EXAMPLE),
                  OpenApiExample("Created", response_only=True, status_codes=["201"], value=CLAIM_CREATED_201)],
        tags=[Tag.CLAIMS],
    )
    def post(self, request):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)

        serializer = ClaimCreateSerializer(data=request.data, context={"profile": profile})
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        sub = d["subscription"]

        with transaction.atomic():
            claim = Claim.objects.create(
                subscription=sub, customer=profile, provider=sub.provider, claim_type=d["claim_type"],
                incident_date=d["incident_date"], incident_description=d["incident_description"],
                claimed_amount=d["claimed_amount"], status="submitted")
            emit(E.CLAIM_SUBMITTED, partner=claim.provider, aggregate_id=claim.id, data=b.claim_submitted(claim))

        return Response({
            "message": "Claim submitted. Please upload supporting documents.",
            "claim_id": str(claim.id),
            "claim_reference": claim.claim_reference,
            "required_documents": REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, []),
            "settlement_on_file": profile.has_settlement,
            "next_steps": {
                "upload_documents": f"POST /api/v1/claims/{claim.id}/documents/",
                "add_settlement_account": None if profile.has_settlement else "PATCH /api/v1/auth/profile/",
            },
        }, status=201)


class CustomerClaimDetailView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="customer_claims_retrieve",
        summary="Get a claim",
        description="Single claim owned by the caller. Another customer's claim returns 404, never 403.",
        auth=PARTNER_KEY_AUTH,
        parameters=[CLAIM_ID],
        responses={200: ClaimCustomerSerializer,
                   404: error("Claim not found, or customer profile missing.", "Claim not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=CLAIM_EXAMPLE)],
        tags=[Tag.CLAIMS],
    )
    def get(self, request, claim_id):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)
        claim = _claims().filter(id=claim_id, customer=profile).first()
        if claim is None:
            return Response({"error": "Claim not found."}, status=404)
        return Response(ClaimCustomerSerializer(claim).data)


class ClaimDocumentUploadView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Upload a claim document",
        description=(
            "`multipart/form-data`. One call per document. Uploading the same `document_type` again "
            "**replaces** the earlier file.\n\n"
            "Allowed while the claim is `submitted` or `more_info_required`; otherwise 409. "
            "`document_type` must be one of the claim type's `required_documents`.\n\n"
            "**When the last required document arrives for a `submitted` claim** it moves to "
            "`ai_check_pending` and the AI check is requested. For a `more_info_required` claim the status does "
            "not change; call `POST /claims/{claim_id}/resubmit/` when done."
        ),
        auth=PARTNER_KEY_AUTH,
        parameters=[CLAIM_ID],
        request={"multipart/form-data": inline_serializer("ClaimDocumentUploadRequest", {
            "document_type": s.CharField(), "file": s.FileField()})},
        responses={
            200: inline_serializer("ClaimDocumentUploaded", {
                "message": s.CharField(), "missing_documents": s.ListField(child=s.CharField()),
                "all_documents_uploaded": s.BooleanField()}),
            400: error("Missing field, invalid type, or type not required for this claim.",
                       "This document is not required. Required: ['claim_form', 'medical_report']"),
            404: error("Claim not found or profile missing.", "Claim not found."),
            409: error("Claim is not accepting documents.", "Claim in 'forwarded' is no longer accepting documents."),
        },
        examples=[OpenApiExample("Partial", response_only=True, status_codes=["200"], value={
                      "message": "Document uploaded successfully.",
                      "missing_documents": ["flight_documents", "travel_insurance_certificate"],
                      "all_documents_uploaded": False}),
                  OpenApiExample("Complete (AI check requested)", response_only=True, status_codes=["200"], value={
                      "message": "Document uploaded successfully.", "missing_documents": [],
                      "all_documents_uploaded": True})],
        tags=[Tag.CLAIMS],
    )
    def post(self, request, claim_id):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)

        document_type, file = request.data.get("document_type"), request.FILES.get("file")
        if not document_type:
            return Response({"error": "document_type is required."}, status=400)
        if not file:
            return Response({"error": "file is required."}, status=400)
        valid_types = [c[0] for c in ClaimDocument.DOCUMENT_TYPE_CHOICES]
        if document_type not in valid_types:
            return Response({"error": f"Invalid document type. Valid types: {valid_types}"}, status=400)

        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id, customer=profile).first()
            if claim is None:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status not in DOC_UPLOAD_STATUSES:
                return Response({"error": f"Claim in '{claim.status}' is no longer accepting documents."}, status=409)

            required = REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, [])
            if document_type not in required:
                return Response({"error": f"This document is not required. Required: {required}"}, status=400)

            ClaimDocument.objects.update_or_create(claim=claim, document_type=document_type, defaults={"file": file})
            uploaded = set(claim.documents.values_list("document_type", flat=True))
            missing = [d for d in required if d not in uploaded]

            if not missing and claim.status == "submitted":   # first time complete -> lock + AI check
                claim.status = "ai_check_pending"
                claim.save(update_fields=["status", "updated_at"])
                emit(E.CLAIM_AI_CHECK_REQUESTED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_ai_check_requested(claim))

        return Response({"message": "Document uploaded successfully.",
                         "missing_documents": missing, "all_documents_uploaded": not missing})

    @extend_schema(
        summary="Claim document checklist",
        description="Required, uploaded and still-missing documents for this claim.",
        auth=PARTNER_KEY_AUTH,
        parameters=[CLAIM_ID],
        responses={
            200: inline_serializer("ClaimDocumentsChecklist", {
                "required_documents": s.ListField(child=s.CharField()),
                "uploaded_documents": ClaimDocumentSerializer(many=True),
                "missing_documents": s.ListField(child=s.CharField())}),
            404: error("Claim not found or profile missing.", "Claim not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "required_documents": ["claim_form", "flight_documents"], "uploaded_documents": [DOC_EXAMPLE],
            "missing_documents": ["flight_documents"]})],
        tags=[Tag.CLAIMS],
    )
    def get(self, request, claim_id):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)
        claim = Claim.objects.filter(id=claim_id, customer=profile).first()
        if claim is None:
            return Response({"error": "Claim not found."}, status=404)
        required = REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, [])
        uploaded = claim.documents.all()
        have = {d.document_type for d in uploaded}
        return Response({"required_documents": required,
                         "uploaded_documents": ClaimDocumentSerializer(uploaded, many=True).data,
                         "missing_documents": [d for d in required if d not in have]})


class ClaimResubmitView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Resubmit a claim after a request for more information",
        description=(
            "Call after uploading what the reviewer asked for. Requires status `more_info_required` and every "
            "required document present.\n\n"
            "The claim returns to whoever asked: `forwarded` (provider is emailed) if it had already been forwarded, "
            "otherwise `flagged` for TheeInsurance staff."
        ),
        auth=PARTNER_KEY_AUTH,
        parameters=[CLAIM_ID],
        request=None,
        responses={200: ReviewResponse,
                   400: error("Required documents still missing.", "Missing documents: ['flight_documents']"),
                   404: error("Claim not found.", "Claim not found."),
                   409: error("Claim is not awaiting information.", "Claim in 'forwarded' is not awaiting more information.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "message": "Claim resubmitted.", "claim_reference": "CLM-9F3A1B7C2D4E5F60", "status": "forwarded"})],
        tags=[Tag.CLAIMS],
    )
    def post(self, request, claim_id):
        profile = _customer_profile(request)
        if profile is None:
            return Response({"error": NO_PROFILE}, status=404)
        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id, customer=profile).first()
            if claim is None:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status != "more_info_required":
                return Response({"error": f"Claim in '{claim.status}' is not awaiting more information."}, status=409)
            required = REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, [])
            missing = [d for d in required if d not in set(claim.documents.values_list("document_type", flat=True))]
            if missing:
                return Response({"error": f"Missing documents: {missing}"}, status=400)

            claim.status = "forwarded" if claim.forwarded_at else "flagged"
            claim.save(update_fields=["status", "updated_at"])
            disc = f"resubmit:{claim.updated_at.isoformat()}"
            if claim.status == "forwarded":
                emit(E.CLAIM_FORWARDED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_forwarded(claim), discriminator=disc)
                on_commit_send(claim_emails.send_claim_forwarded, claim.id)
            else:
                emit(E.CLAIM_FLAGGED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_flagged(claim), discriminator=disc)
        return Response({"message": "Claim resubmitted.", "claim_reference": claim.claim_reference,
                         "status": claim.status})


# ============================================================ PROVIDER
class ProviderClaimListView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="provider_claims_list",
        summary="List claims for my plans",
        description="JWT only (no `X-Partner-Key`). Providers only see claims that have cleared the AI/staff gate "
                    "(`forwarded_at` set), never `submitted`, `ai_check_pending` or unreviewed `flagged` claims. "
                    "Includes `ai_result`.",
        parameters=[STATUS_PARAM,
                    OpenApiParameter("claim_type", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False,
                                     enum=[c[0] for c in Claim.CLAIM_TYPE_CHOICES])],
        responses={200: ClaimListFull},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={"claims": [CLAIM_STAFF_EXAMPLE]})],
        tags=[Tag.CLAIMS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        qs = _claims().filter(provider=partner, forwarded_at__isnull=False).order_by("-submitted_at")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("claim_type"):
            qs = qs.filter(claim_type=request.query_params["claim_type"])
        return Response({"claims": ClaimSerializer(qs, many=True).data})


class ProviderClaimReviewView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="provider_claims_retrieve",
        summary="Get a claim for review",
        parameters=[CLAIM_ID],
        responses={200: ClaimSerializer, 404: error("Not found, or not yet forwarded to this provider.", "Claim not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=CLAIM_STAFF_EXAMPLE)],
        tags=[Tag.CLAIMS],
    )
    def get(self, request, claim_id):
        partner = get_partner_from_user(request.user)
        claim = _claims().filter(id=claim_id, provider=partner, forwarded_at__isnull=False).first()
        if claim is None:
            return Response({"error": "Claim not found."}, status=404)
        return Response(ClaimSerializer(claim).data)

    @extend_schema(
        summary="Approve, reject or request more information",
        description=(
            "Allowed from `forwarded` or `more_info_required`. `partner_viewer` accounts get 403.\n\n"
            "- **approved**: requires `approved_amount` (≤ `claimed_amount`) and a customer settlement account on file "
            "(else 409 `customer_settlement_missing`). The payout starts automatically to the customer's account; "
            "when Nomba confirms, the claim becomes `paid`.\n"
            "- **rejected / more_info_required**: `review_note` is required and is shown to the customer.\n\n"
            "**Emails:** the customer is emailed for every decision; the distributor (if the claim came through one) "
            "gets an FYI on approve and reject."
        ),
        parameters=[CLAIM_ID],
        request=ClaimReviewSerializer,
        responses={
            200: ReviewResponse,
            400: validation_error("review_note", "A note is required when rejecting a claim or requesting more information."),
            403: error("Viewer role or no partner profile.", "You are not allowed to review claims."),
            404: error("Not found, or not yet forwarded to this provider.", "Claim not found."),
            409: error("Wrong status, or customer has no settlement account.",
                       "Claim in 'paid' cannot be reviewed."),
        },
        examples=[OpenApiExample("Approve", request_only=True, value={
                      "status": "approved", "approved_amount": "120000.00", "review_note": "Approved per policy section 4."}),
                  OpenApiExample("Reject", request_only=True, value={
                      "status": "rejected", "review_note": "Incident falls under a policy exclusion."}),
                  OpenApiExample("Request info", request_only=True, value={
                      "status": "more_info_required", "review_note": "Please upload a clearer police report."}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={
                      "message": "Claim approved.", "claim_reference": "CLM-9F3A1B7C2D4E5F60", "status": "approved"}),
                  OpenApiExample("No settlement account", response_only=True, status_codes=["409"], value={
                      "error": "The customer has no settlement account on file. Request more information so they can add one.",
                      "code": "customer_settlement_missing"})],
        tags=[Tag.CLAIMS],
    )
    def patch(self, request, claim_id):
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None or profile.role == "partner_viewer":
            return Response({"error": "You are not allowed to review claims."}, status=403)
        partner = profile.partner

        serializer = ClaimReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        new_status, approved_amount = data["status"], data.get("approved_amount")
        if new_status not in PROVIDER_DECISIONS:
            return Response({"error": f"status must be one of {list(PROVIDER_DECISIONS)}."}, status=400)

        with transaction.atomic():
            claim = (Claim.objects.select_for_update(of=("self",)).select_related("customer")
                     .filter(id=claim_id, provider=partner, forwarded_at__isnull=False).first())
            if claim is None:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status not in PROVIDER_REVIEWABLE:
                return Response({"error": f"Claim in '{claim.status}' cannot be reviewed."}, status=409)
            if approved_amount is not None and approved_amount > claim.claimed_amount:
                return Response({"error": "approved_amount cannot exceed claimed_amount."}, status=400)
            if new_status == "approved" and not claim.customer.has_settlement:
                return Response(SETTLEMENT_MISSING, status=409)

            claim.status = new_status
            claim.provider_review_note = data["review_note"]
            if approved_amount is not None:
                claim.approved_amount = approved_amount
            claim.reviewed_by_provider = profile
            claim.save()

            disc = f"{claim.status}:{claim.updated_at.isoformat()}"
            emit(E.PROVIDER_CLAIM_DECISION, partner=partner, aggregate_id=claim.id,
                 data=b.provider_claim_decision(claim, claim.status), discriminator=disc)
            if claim.status == "approved":
                emit(E.CLAIM_APPROVED, partner=partner, aggregate_id=claim.id, data=b.claim_approved(claim))
                start_claim_payout(claim)
            elif claim.status == "rejected":
                emit(E.CLAIM_REJECTED, partner=partner, aggregate_id=claim.id, data=b.claim_rejected(claim))
            emit(E.CLAIM_STATUS_UPDATED, partner=partner, aggregate_id=claim.id,
                 data=b.claim_status_updated(claim), discriminator=disc)   # legacy event
            on_commit_send(claim_emails.send_claim_decision, claim.id, claim.status, claim.provider_review_note)

        return Response({"message": f"Claim {claim.status}.", "claim_reference": claim.claim_reference,
                         "status": claim.status})


class ProviderClaimRetryPayoutView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Retry a failed payout",
        description="For an `approved` claim whose last payout `failed`. Starts a new payment attempt against the "
                    "customer's current settlement account. 409 if a payout is already pending or completed.",
        parameters=[CLAIM_ID],
        request=None,
        responses={202: inline_serializer("PayoutRetried", {
                       "message": s.CharField(), "payment_reference": s.CharField(), "status": s.CharField()}),
                   403: error("Viewer role.", "You are not allowed to retry payouts."),
                   404: error("Claim not found.", "Claim not found."),
                   409: error("Not retryable.", "A payout is already pending or completed.")},
        examples=[OpenApiExample("Accepted", response_only=True, status_codes=["202"], value={
            "message": "Payout re-initiated.", "payment_reference": "CLMPAY-CLM-9F3A1B7C2D4E5F60-2",
            "status": "pending"})],
        tags=[Tag.CLAIMS],
    )
    def post(self, request, claim_id):
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None or profile.role == "partner_viewer":
            return Response({"error": "You are not allowed to retry payouts."}, status=403)
        with transaction.atomic():
            claim = (Claim.objects.select_for_update(of=("self",)).select_related("customer")
                     .filter(id=claim_id, provider=profile.partner).first())
            if claim is None:
                return Response({"error": "Claim not found."}, status=404)
            if claim.status != "approved":
                return Response({"error": "Only approved claims awaiting payment can be retried."}, status=409)
            if claim.payments.filter(status__in=[ClaimPayment.PENDING, ClaimPayment.COMPLETED]).exists():
                return Response({"error": "A payout is already pending or completed."}, status=409)
            if not claim.customer.has_settlement:
                return Response(SETTLEMENT_MISSING, status=409)
            payment = start_claim_payout(claim)
        return Response({"message": "Payout re-initiated.", "payment_reference": payment.reference,
                         "status": payment.status}, status=202)


# ============================================================ TheeInsurance STAFF
class StaffClaimListView(APIView):
    permission_classes = [IsSuperAdmin, IsHumanStaff]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="staff_claims_list",
        summary="List all claims",
        description="Every claim across all partners, including `ai_result`. Triage queue: `?status=flagged`.",
        parameters=[STATUS_PARAM],
        responses={200: ClaimListFull},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={"claims": [{**CLAIM_STAFF_EXAMPLE, "status": "flagged", "forwarded_at": None,
                                                    "ai_result": {**AI_RESULT_EXAMPLE, "score": 0.81,
                                                                  "flags": ["amount_mismatch"],
                                                                  "recommendation": "flag"}}]})],
        tags=[Tag.CLAIMS],
    )
    def get(self, request):
        qs = _claims().order_by("-submitted_at")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"claims": ClaimSerializer(qs, many=True).data})


class StaffClaimReviewView(APIView):
    permission_classes = [IsSuperAdmin, IsHumanStaff]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="staff_claims_retrieve",
        summary="Get any claim",
        parameters=[CLAIM_ID],
        responses={200: ClaimSerializer, 404: error("Claim not found.", "Claim not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=CLAIM_STAFF_EXAMPLE)],
        tags=[Tag.CLAIMS],
    )
    def get(self, request, claim_id):
        claim = _claims().filter(id=claim_id).first()
        if claim is None:
            return Response({"error": "Claim not found."}, status=404)
        return Response(ClaimSerializer(claim).data)

    @extend_schema(
        summary="Triage a flagged claim",
        description=(
            "Allowed transitions:\n"
            "- `flagged` → `forwarded` | `rejected` | `more_info_required`\n"
            "- `ai_check_pending` → `forwarded` | `flagged` (manual override when the AI service is down)\n\n"
            "`review_note` is required for `rejected` / `more_info_required` and **is shown to the customer**.\n\n"
            "**Emails:** `forwarded` notifies the provider (and distributor FYI); `rejected` / `more_info_required` "
            "notify the customer."
        ),
        parameters=[CLAIM_ID],
        request=ClaimReviewSerializer,
        responses={200: ReviewResponse,
                   400: validation_error("review_note", "A note is required when rejecting a claim or requesting more information."),
                   404: error("Claim not found.", "Claim not found."),
                   409: error("Transition not allowed.", "Cannot move a 'paid' claim to 'forwarded'.")},
        examples=[OpenApiExample("Forward", request_only=True, value={"status": "forwarded", "review_note": "AI flag reviewed, OK."}),
                  OpenApiExample("Reject", request_only=True, value={"status": "rejected", "review_note": "Documents do not match the incident."}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={
                      "message": "Claim updated to forwarded.", "claim_reference": "CLM-9F3A1B7C2D4E5F60",
                      "status": "forwarded"})],
        tags=[Tag.CLAIMS],
    )
    def patch(self, request, claim_id):
        serializer = ClaimReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status, note = serializer.validated_data["status"], serializer.validated_data["review_note"]

        with transaction.atomic():
            claim = Claim.objects.select_for_update().filter(id=claim_id).first()
            if claim is None:
                return Response({"error": "Claim not found."}, status=404)
            if new_status not in STAFF_TRANSITIONS.get(claim.status, set()):
                return Response({"error": f"Cannot move a '{claim.status}' claim to '{new_status}'."}, status=409)

            claim.status = new_status
            claim.theeinsurance_review_note = note
            claim.reviewed_by_theeinsurance = request.user
            if new_status == "forwarded" and claim.forwarded_at is None:
                claim.forwarded_at = timezone.now()
            claim.save()

            if new_status == "forwarded":
                emit(E.CLAIM_FORWARDED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_forwarded(claim), discriminator="staff")
                on_commit_send(claim_emails.send_claim_forwarded, claim.id)
            elif new_status == "flagged":
                emit(E.CLAIM_FLAGGED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_flagged(claim), discriminator="staff")
            elif new_status == "rejected":
                emit(E.CLAIM_REJECTED, partner=claim.provider, aggregate_id=claim.id,
                     data=b.claim_rejected(claim), discriminator="staff")
                on_commit_send(claim_emails.send_claim_decision, claim.id, "rejected", note)
            elif new_status == "more_info_required":
                on_commit_send(claim_emails.send_claim_decision, claim.id, "more_info_required", note)

        return Response({"message": f"Claim updated to {claim.status}.",
                         "claim_reference": claim.claim_reference, "status": claim.status})
