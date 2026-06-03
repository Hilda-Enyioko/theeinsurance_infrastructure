from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Claim, ClaimDocument, REQUIRED_CLAIM_DOCUMENTS
from .serializers import (
    ClaimSerializer,
    ClaimCreateSerializer,
    ClaimDocumentSerializer,
    ClaimReviewSerializer,
)
from accounts.models import CustomerProfile
from subscriptions.models import PolicySubscription


# Helpers
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Customer Claims
class CustomerClaimListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        claims = Claim.objects.filter(customer=profile)
        serializer = ClaimSerializer(claims, many=True)
        return Response({"claims": serializer.data})

    def post(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        serializer = ClaimCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        subscription = serializer.validated_data["subscription"]

        # verify subscription belongs to this customer
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

        required_docs = REQUIRED_CLAIM_DOCUMENTS.get(claim_type, [])

        return Response({
            "message": "Claim submitted. Please upload supporting documents.",
            "claim_id": str(claim.id),
            "claim_reference": claim.claim_reference,
            "required_documents": required_docs,
        }, status=201)


class CustomerClaimDetailView(APIView):
    permission_classes = [IsAuthenticated]

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
    permission_classes = [IsAuthenticated]

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
    """Provider admins view all claims for their plans."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response(
                {"error": "Access restricted to insurance providers."},
                status=403
            )

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
    """Provider admins review and update claim status."""
    permission_classes = [IsAuthenticated]

    def get(self, request, claim_id):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response(
                {"error": "Access restricted to insurance providers."},
                status=403
            )

        try:
            claim = Claim.objects.get(id=claim_id, provider=partner)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimSerializer(claim)
        return Response(serializer.data)

    def patch(self, request, claim_id):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response(
                {"error": "Access restricted to insurance providers."},
                status=403
            )

        try:
            claim = Claim.objects.get(id=claim_id, provider=partner)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        claim.status = serializer.validated_data["status"]
        claim.provider_review_note = serializer.validated_data.get(
            "review_note", ""
        )
        if serializer.validated_data.get("approved_amount"):
            claim.approved_amount = serializer.validated_data["approved_amount"]

        # track who reviewed it
        try:
            claim.reviewed_by_provider = request.user.partner_admin_profile
        except Exception:
            pass

        claim.save()

        return Response({
            "message": f"Claim {claim.status}.",
            "claim_reference": claim.claim_reference,
            "status": claim.status,
        })


# TheeInsurance Staff Review

class StaffClaimListView(APIView):
    """TheeInsurance super admins view and triage all claims."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.role != "super_admin":
            return Response({"error": "Access restricted."}, status=403)

        claims = Claim.objects.all()

        status_filter = request.query_params.get("status")
        if status_filter:
            claims = claims.filter(status=status_filter)

        serializer = ClaimSerializer(claims, many=True)
        return Response({"claims": serializer.data})


class StaffClaimReviewView(APIView):
    """TheeInsurance staff do initial review before forwarding to provider."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, claim_id):
        if request.user.role != "super_admin":
            return Response({"error": "Access restricted."}, status=403)

        try:
            claim = Claim.objects.get(id=claim_id)
        except Claim.DoesNotExist:
            return Response({"error": "Claim not found."}, status=404)

        serializer = ClaimReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        claim.status = serializer.validated_data["status"]
        claim.theeinsurance_review_note = serializer.validated_data.get(
            "review_note", ""
        )
        claim.reviewed_by_theeinsurance = request.user
        claim.save()

        return Response({
            "message": f"Claim updated to {claim.status}.",
            "claim_reference": claim.claim_reference,
            "status": claim.status,
        })