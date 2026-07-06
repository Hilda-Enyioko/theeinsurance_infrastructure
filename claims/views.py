from rest_framework.views import APIView
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiParameter, OpenApiTypes
from webhooks.services import dispatch_webhook
from accounts.permissions import IsSuperAdmin, IsProviderAdmin, IsCustomer
from rest_framework import serializers

from .models import Claim, ClaimDocument, REQUIRED_CLAIM_DOCUMENTS
from .serializers import (
    ClaimSerializer,
    ClaimCreateSerializer,
    ClaimDocumentSerializer,
    ClaimReviewSerializer,
)
from accounts.models import CustomerProfile
from core.throttles import PartnerRateThrottle


# Helpers
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Customer Claims
class CustomerClaimListCreateView(APIView):
    """
    Customers list their own claims and submit new ones.
    IsCustomer ensures partner_admins and staff cannot hit this endpoint.
    Object ownership is enforced via customer=profile in all queries.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List Customer Claims",
        description="Retrieves a list of all insurance claims submitted by the authenticated customer for the active partner.",
        responses={
            200: ClaimSerializer(many=True),
            404: {"description": "Customer profile not found for this partner context."},
        },
        tags=["Customer Claims"]
    )
    def get(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        claims = Claim.objects.filter(customer=profile)
        serializer = ClaimSerializer(claims, many=True)
        return Response({"claims": serializer.data})

    @extend_schema(
        summary="Submit New Claim",
        description="Submits a new insurance claim against an active policy subscription. Returns required document types for the specific claim.",
        request=ClaimCreateSerializer,
        responses={
            201: {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "example": "Claim submitted. Please upload supporting documents."},
                    "claim_id": {"type": "string", "format": "uuid"},
                    "claim_reference": {"type": "string", "example": "CLM-12345678"},
                    "required_documents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "example": ["MEDICAL_REPORT", "RECEIPT"]
                    },
                }
            },
            400: {"description": "Validation error or subscription mismatch."},
            404: {"description": "Customer profile or Subscription context not found."},
        },
        tags=["Customer Claims"]
    )
    def post(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        serializer = ClaimCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        subscription = serializer.validated_data["subscription"]

        if subscription.customer != profile:
            return Response({"error": "Subscription not found."}, status=404)

        claim_type = serializer.validated_data["claim_type"]

        claim = Claim.objects.create(
            subscription=subscription,
            customer=profile,
            provider=subscription.provider,
            claim_type=claim_type,
            incident_date=serializer.validated_data["incident_date"],
            incident_description=serializer.validated_data["incident_description"],
            claimed_amount=serializer.validated_data["claimed_amount"],
            status="submitted",
        )

        dispatch_webhook(
            subscription.provider,
            "claim.submitted",
            {
                "claim_id": str(claim.id),
                "claim_reference": claim.claim_reference,
                "claim_type": claim.claim_type,
                "customer_email": profile.user.email,
                "claimed_amount": str(claim.claimed_amount),
                "subscription_id": str(subscription.id),
            }
        )

        required_docs = REQUIRED_CLAIM_DOCUMENTS.get(claim_type, [])

        return Response({
            "message": "Claim submitted. Please upload supporting documents.",
            "claim_id": str(claim.id),
            "claim_reference": claim.claim_reference,
            "required_documents": required_docs,
        }, status=201)


class CustomerClaimDetailView(APIView):
    """
    Customers view a single claim they own.
    IsCustomer + customer=profile filter enforces ownership.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get Customer Claim Detail",
        description="Retrieves granular details of a single claim owned by the authenticated customer.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of the claim")
        ],
        responses={
            200: ClaimSerializer,
            404: {"description": "Claim or customer profile context not found."},
        },
        tags=["Customer Claims"],
    )
    def get(self, request, claim_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            claim = Claim.objects.get(id=claim_id, customer=profile)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimSerializer(claim)
        return Response(serializer.data)


# Claim Document Upload
class ClaimDocumentUploadView(APIView):
    """
    Customers upload and view documents for their claims.
    IsCustomer + customer=profile filter enforces ownership.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Upload Claim Document",
        description="Uploads a required multi-part file attachment for a pending claim. Only allowed while claim is in 'submitted' status.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of the claim")
        ],
        request={
            "multipart/form-data": {
                "type": "object",
                "required": ["document_type", "file"],
                "properties": {
                    "document_type": {
                        "type": "string",
                        "description": "The exact document classification type identifier."
                    },
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "File attachment payload."
                    }
                }
            }
        },
        responses={
            200: inline_serializer(
                name="ClaimDocumentsUploadResponse",
                fields={
                    "required_documents": serializers.ListField(child=serializers.CharField()),
                    "uploaded_documents": ClaimDocumentSerializer(many=True),
                    "missing_documents": serializers.ListField(child=serializers.CharField()),
                }
            ),
            400: {"description": "Missing file elements or document validation type failure."},
            404: {"description": "Claim not found or closed for documentation entries."},
        },
        tags=["Claim Documents"]
    )
    def post(self, request, claim_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            claim = Claim.objects.get(
                id=claim_id,
                customer=profile,
                status="submitted",
            )
        except Claim.DoesNotExist:
            return Response(
                {"error": "Claim not found or no longer accepting documents."},
                status=404
            )

        document_type = request.data.get("document_type")
        file = request.FILES.get("file")

        if not document_type:
            return Response({"error": "document_type is required."}, status=400)
        if not file:
            return Response({"error": "file is required."}, status=400)

        valid_types = [choice[0] for choice in ClaimDocument.DOCUMENT_TYPE_CHOICES]
        if document_type not in valid_types:
            return Response(
                {"error": f"Invalid document type. Valid types: {valid_types}"},
                status=400
            )

        required_docs = REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, [])
        if document_type not in required_docs:
            return Response(
                {"error": f"This document is not required. Required: {required_docs}"},
                status=400
            )

        ClaimDocument.objects.update_or_create(
            claim=claim,
            document_type=document_type,
            defaults={"file": file},
        )

        uploaded_types = list(
            claim.documents.values_list("document_type", flat=True)
        )
        missing_docs = [doc for doc in required_docs if doc not in uploaded_types]

        return Response({
            "message": "Document uploaded successfully.",
            "missing_documents": missing_docs,
            "all_documents_uploaded": len(missing_docs) == 0,
        })

    @extend_schema(
        summary="View Uploaded Claim Documents Checklist",
        description="Fetches a list of uploaded supporting elements, required checklist guidelines, and remaining documents for processing validation.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of the claim")
        ],
        responses={
            200: inline_serializer(
                name="ClaimDocumentsChecklistResponse",
                fields={
                    "required_documents": serializers.ListField(child=serializers.CharField()),
                    "uploaded_documents": ClaimDocumentSerializer(many=True),
                    "missing_documents": serializers.ListField(child=serializers.CharField()),
                }
            ),
            404: {"description": "Claim contextual workspace profile missing."},
        },
        tags=["Claim Documents"]
    )
    def get(self, request, claim_id):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        try:
            claim = Claim.objects.get(id=claim_id, customer=profile)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        required_docs = REQUIRED_CLAIM_DOCUMENTS.get(claim.claim_type, [])
        uploaded = claim.documents.all()
        uploaded_types = [doc.document_type for doc in uploaded]
        missing_docs = [doc for doc in required_docs if doc not in uploaded_types]

        return Response({
            "required_documents": required_docs,
            "uploaded_documents": ClaimDocumentSerializer(uploaded, many=True).data,
            "missing_documents": missing_docs,
        })


# Provider Claim Review
class ProviderClaimListView(APIView):
    """
    Provider admins view all claims submitted against their plans.
    IsProviderAdmin enforces: authenticated + partner_admin + provider type.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Provider List Claims",
        description="Enables authorized provider insurance account administrators to view claims targeted at their specific products.",
        parameters=[
            OpenApiParameter(name="status", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter claims by current workflow status state", required=False),
            OpenApiParameter(name="claim_type", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter claims by explicit category type identifier", required=False),
        ],
        responses={200: ClaimSerializer(many=True)},
        tags=["Provider Claim Management"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        claims = Claim.objects.filter(provider=partner)

        status_filter = request.query_params.get("status")
        if status_filter:
            claims = claims.filter(status=status_filter)

        claim_type_filter = request.query_params.get("claim_type")
        if claim_type_filter:
            claims = claims.filter(claim_type=claim_type_filter)

        serializer = ClaimSerializer(claims, many=True)
        return Response({"claims": serializer.data})


class ProviderClaimReviewView(APIView):
    """
    Provider admins review and update claim status.
    IsProviderAdmin at class level covers both GET and PATCH.
    Object ownership enforced via provider=partner filter.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Provider Claim Details View",
        description="Fetches a dedicated isolated operational view summary of an inbound claim for underwriting assessment.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of target claim")
        ],
        responses={
            200: ClaimSerializer,
            404: {"description": "Claim reference data not found within provider administration domain."},
        },
        tags=["Provider Claim Management"]
    )
    def get(self, request, claim_id):
        partner = get_partner_from_user(request.user)

        try:
            claim = Claim.objects.get(id=claim_id, provider=partner)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimSerializer(claim)
        return Response(serializer.data)

    @extend_schema(
        summary="Provider Claim Review Action Evaluation",
        description="Allows a provider underwriting expert to approve, reject, or comment on an active claim entry.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of target claim")
        ],
        request=ClaimReviewSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "example": "Claim approved."},
                    "claim_reference": {"type": "string", "example": "CLM-48218414"},
                    "status": {"type": "string", "example": "approved"},
                }
            },
            400: {"description": "Invalid decision metadata structures or invalid input constraints."},
            404: {"description": "Claim context target key missing."},
        },
        tags=["Provider Claim Management"]
    )
    def patch(self, request, claim_id):
        partner = get_partner_from_user(request.user)

        try:
            claim = Claim.objects.get(id=claim_id, provider=partner)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        claim.status = serializer.validated_data["status"]
        claim.provider_review_note = serializer.validated_data.get("review_note", "")
        if serializer.validated_data.get("approved_amount"):
            claim.approved_amount = serializer.validated_data["approved_amount"]

        try:
            claim.reviewed_by_provider = request.user.partner_admin_profile
        except Exception:
            pass

        claim.save()

        dispatch_webhook(
            claim.provider,
            "claim.status_updated",
            {
                "claim_id": str(claim.id),
                "claim_reference": claim.claim_reference,
                "status": claim.status,
                "customer_email": claim.customer.user.email,
                "approved_amount": str(claim.approved_amount) if claim.approved_amount else None,
            }
        )

        return Response({
            "message": f"Claim {claim.status}.",
            "claim_reference": claim.claim_reference,
            "status": claim.status,
        })


# TheeInsurance Staff Claim Review
class StaffClaimListView(APIView):
    """
    TheeInsurance super admins view and triage all claims across all partners.
    """
    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Staff Core General List Claims",
        description="Allows platform super administrators to browse claims entries filed across the ecosystem matrix.",
        parameters=[
            OpenApiParameter(name="status", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter claims by processing status", required=False)
        ],
        responses={200: ClaimSerializer(many=True)},
        tags=["TheeInsurance Platform Core Operations"]
    )
    def get(self, request):
        claims = Claim.objects.all()

        status_filter = request.query_params.get("status")
        if status_filter:
            claims = claims.filter(status=status_filter)

        serializer = ClaimSerializer(claims, many=True)
        return Response({"claims": serializer.data})


class StaffClaimReviewView(APIView):
    """
    TheeInsurance staff do initial triage before forwarding to provider.
    """
    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Staff Core Single Detail Fetch",
        description="Retrieves any single ecosystem claim entry profile mapping across partners.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of operational claim target")
        ],
        responses={
            200: ClaimSerializer,
            404: {"description": "Claim reference key not located in global records mapping."},
        },
        tags=["TheeInsurance Platform Core Operations"]
    )
    def get(self, request, claim_id):
        try:
            claim = Claim.objects.get(id=claim_id)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimSerializer(claim)
        return Response(serializer.data)

    @extend_schema(
        summary="Staff Central Core Initial Processing Review",
        description="Allows platform core specialists to triage claim parameters or execute priority status structural updates before deep carrier routing validation cycles occur.",
        parameters=[
            OpenApiParameter(name="claim_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH, description="UUID of operational claim target")
        ],
        request=ClaimReviewSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "example": "Claim updated to under_review."},
                    "claim_reference": {"type": "string", "example": "CLM-77112239"},
                    "status": {"type": "string", "example": "under_review"},
                }
            },
            400: {"description": "Triage input structural validation exception."},
            404: {"description": "Claim entry identifier element lookup mismatch."},
        },
        tags=["TheeInsurance Platform Core Operations"]
    )
    def patch(self, request, claim_id):
        try:
            claim = Claim.objects.get(id=claim_id)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        claim.status = serializer.validated_data["status"]
        claim.theeinsurance_review_note = serializer.validated_data.get("review_note", "")
        claim.reviewed_by_theeinsurance = request.user
        claim.save()

        return Response({
            "message": f"Claim updated to {claim.status}.",
            "claim_reference": claim.claim_reference,
            "status": claim.status,
        })
