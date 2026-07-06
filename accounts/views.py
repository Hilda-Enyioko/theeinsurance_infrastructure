"""
Authentication, KYC, and Core System Administration Views.

This module provides Django REST Framework (DRF) API endpoints handling:
- Multi-tenant Partner onboarding and Know Your Customer (KYC) submissions.
- Customer account registration and KYC linking under designated partners.
- Centralized user login and token management (SimpleJWT).
- High-level administrative operations by internal TheeInsurance staff.
"""

import secrets
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.contrib.auth import authenticate
from django.utils import timezone
from datetime import timedelta
from drf_spectacular.utils import (
    extend_schema,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)

from accounts.permissions import IsSuperAdmin, IsPartnerAdmin, IsCustomer
from core.throttles import IPRateThrottle, PartnerRateThrottle
from .models import CustomUser, PartnerKYC, CustomerProfile, ServiceAccountCredential
from core.models import Partner
from webhooks.services import dispatch_webhook

from .serializers import (
    CustomerRegistrationSerializer,
    PartnerOnboardingSerializer,
    LoginSerializer,
    CustomerKYCSerializer,
    PartnerKYCSerializer,
    PartnerMeSerializer,
)


# Helper functions
def get_partner_from_user(user):
    """
    Extract the associated partner entity from a user instance 
    if they are a partner admin.
    """
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


def generate_tokens(user, partner_id=None):
    """
    Generate SimpleJWT access and refresh tokens 
    embedded with role and tenant claims.
    """
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.role
    if partner_id:
        refresh["partner_id"] = str(partner_id)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


# Partner Onboarding

class PartnerOnboardingView(APIView):
    """
    Public — new partners self-register.
    IPRateThrottle: prevents automated partner account creation.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Self-register New Partner",
        description="Accepts corporate registration parameters, spins up an inactive Partner profile, and issues an API key for subsequent configuration steps.",
        request=PartnerOnboardingSerializer,
        responses={
            201: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "partner_id": {"type": "string", "format": "uuid"},
                    "api_key": {"type": "string", "description": "Shown once only."}
                }
            },
            400: {"description": "Validation errors."}
        },
        tags=["Partner Management"]
    )
    def post(self, request):
        serializer = PartnerOnboardingSerializer(data=request.data)
        if serializer.is_valid():
            partner = serializer.save()
            return Response({
                "message": "Partner account created. Submit KYC to activate your account.",
                "partner_id": str(partner.id),
                "api_key": partner.api_key,  # shown once only
            }, status=201)
        return Response(serializer.errors, status=400)


# Partner KYC

class PartnerKYCView(APIView):
    """
    Partner admins submit and view their own KYC.
    PartnerRateThrottle: scoped to the partner tenant.
    """
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Submit Partner KYC",
        description="Allows corporate administrators to upload operational documentation for corporate review.",
        request={
            "multipart/form-data": PartnerKYCSerializer,
        },
        responses={
            201: {
                "type": "object",
                "properties": {"message": {"type": "string"}}
            },
            400: {"description": "KYC already submitted or validation errors."}
        },
        tags=["Partner Management"],
    )
    def post(self, request):
        partner = get_partner_from_user(request.user)

        if hasattr(partner, 'kyc'):
            return Response({"error": "KYC already submitted."}, status=400)

        serializer = PartnerKYCSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(partner=partner)

            dispatch_webhook(
                partner,
                "kyc.submitted",
                {
                    "partner_id": str(partner.id),
                    "partner_name": partner.name,
                    "partner_type": partner.partner_type,
                }
            )

            return Response({
                "message": "KYC submitted. Your account will be reviewed shortly."
            }, status=201)

        return Response(serializer.errors, status=400)

    @extend_schema(
        summary="Retrieve Partner KYC Status",
        description="Allows authenticated corporate administrators to pull down their own current KYC submission state.",
        responses={
            200: PartnerKYCSerializer,
            404: {"description": "No KYC documentation submitted yet."}
        },
        tags=["Partner Management"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)

        if not hasattr(partner, 'kyc'):
            return Response({"error": "No KYC submitted yet."}, status=404)

        serializer = PartnerKYCSerializer(partner.kyc)
        return Response(serializer.data)


# Customer Registration

class CustomerRegisterView(APIView):
    """
    Public — customers self-register under a partner.
    IPRateThrottle: prevents automated account creation / credential stuffing.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Customer Registration",
        description="Uses tenant context attached to the request (via upstream middleware parsing) to bind the newly registered customer account to the parent organization.",
        request=CustomerRegistrationSerializer,
        responses={
            201: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "tokens": {
                        "type": "object",
                        "properties": {
                            "refresh": {"type": "string"},
                            "access": {"type": "string"}
                        }
                    }
                }
            },
            400: {"description": "Validation errors."}
        },
        tags=["Customer Management"]
    )
    def post(self, request):
        serializer = CustomerRegistrationSerializer(
            data=request.data,
            context={"partner": request.partner}
        )
        if serializer.is_valid():
            user = serializer.save()
            tokens = generate_tokens(user, partner_id=request.partner.id)
            return Response({
                "message": "Registration successful.",
                "tokens": tokens,
            }, status=201)
        return Response(serializer.errors, status=400)


# Customer KYC

class CustomerKYCView(APIView):
    """
    Customers submit and view their own KYC.
    PartnerRateThrottle: scoped to the partner tenant.
    """
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Submit Customer KYC",
        description="Allows registered consumers to submit required verification and identification metrics.",
        request={
            "multipart/form-data": CustomerKYCSerializer,
        },
        responses={
            201: {
                "type": "object",
                "properties": {"message": {"type": "string"}}
            },
            400: {"description": "KYC already submitted or validation errors."},
            404: {"description": "Customer profile not found within the current tenant scope."}
        },
        tags=["Customer Management"]
    )
    def post(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        if hasattr(profile, 'kyc'):
            return Response({"error": "KYC already submitted."}, status=400)

        serializer = CustomerKYCSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(customer=profile)
            return Response({"message": "KYC submitted successfully."}, status=201)
        return Response(serializer.errors, status=400)

    @extend_schema(
        summary="Retrieve Customer KYC Details",
        description="Allows an authenticated consumer to inspect their background check and identity records inside the scope of the current partner.",
        responses={
            200: CustomerKYCSerializer,
            404: {"description": "Customer profile or KYC records not found."}
        },
        tags=["Customer Management"]
    )
    def get(self, request):
        try:
            profile = request.user.customer_profiles.get(partner=request.partner)
        except CustomerProfile.DoesNotExist:
            return Response({"error": "Customer profile not found."}, status=404)

        if not hasattr(profile, 'kyc'):
            return Response({"error": "No KYC submitted yet."}, status=404)

        serializer = CustomerKYCSerializer(profile.kyc)
        return Response(serializer.data)


# Login

class LoginView(APIView):
    """
    Shared login for customers and partner admins.
    IPRateThrottle: strict IP-based limit — primary brute force surface.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Shared Portal Authentication Login",
        description="Verifies credentials for customers and partner admins, returning signed authentication payloads appended with role authorizations and multi-tenant scoping claims.",
        request=LoginSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "tokens": {
                        "type": "object",
                        "properties": {
                            "refresh": {"type": "string"},
                            "access": {"type": "string"}
                        }
                    },
                    "role": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "first_name": {"type": "string"}
                }
            },
            400: {"description": "Validation errors."},
            401: {"description": "Invalid email or password."},
            403: {"description": "Account is inactive."}
        },
        tags=["Authentication"]
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )

        if not user:
            return Response({"error": "Invalid email or password."}, status=401)

        if not user.is_active:
            return Response({"error": "Account is inactive."}, status=403)

        partner = get_partner_from_user(user)
        partner_id = partner.id if partner else None
        tokens = generate_tokens(user, partner_id=partner_id)

        return Response({
            "tokens": tokens,
            "role": user.role,
            "email": user.email,
            "first_name": user.first_name,
        })


class PartnerMeView(APIView):
    """
    GET /partner/me/
    Returns partner_type and partner_name for the authenticated partner admin.
    Called by the partner portal immediately after login.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get authenticated partner details",
        description="Retrieves the `partner_type` and `partner_name` for the currently logged-in partner admin.",
        responses={
            200: PartnerMeSerializer,
            403: OpenApiResponse(
                description="The authenticated user is not a partner admin or lacks a profile."
            ),
        },
        tags=["Partner Management"]
    )
    def get(self, request):
        user = request.user

        if user.role != "partner_admin" or not hasattr(
            user, "partner_admin_profile"
        ):
            return Response(
                {"detail": "This account is not linked to a partner."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = PartnerMeSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)


# Token Refresh

class TokenRefreshView(APIView):
    """
    IPRateThrottle: token refresh can also be abused for token farming.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Refresh Access Token",
        description="Validates submitted refresh states to hand out fresh access hashes, shielding downstream views from re-authentication requirements.",
        request={
            "application/json": {
                "type": "object",
                "required": ["refresh"],
                "properties": {
                    "refresh": {"type": "string", "description": "The refresh token string."}
                }
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "access": {"type": "string"}
                }
            },
            400: {"description": "Refresh token is missing."},
            401: {"description": "Token is invalid or expired."}
        },
        tags=["Authentication"]
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token is required."}, status=400)

        try:
            refresh = RefreshToken(refresh_token)
            return Response({"access": str(refresh.access_token)})
        except TokenError as e:
            return Response({"error": str(e)}, status=401)


# TheeInsurance Staff Login

class StaffLoginView(APIView):
    """
    Staff-only login.
    IPRateThrottle: same brute force protection as customer login.
    This is the highest-value credential surface in the system.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Internal Staff Login",
        description="Enforces strict structural checks to confirm the user possesses elevated global administrative roles (`super_admin`) before dispatching tokens.",
        request=LoginSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "tokens": {
                        "type": "object",
                        "properties": {
                            "refresh": {"type": "string"},
                            "access": {"type": "string"}
                        }
                    },
                    "role": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "first_name": {"type": "string"}
                }
            },
            400: {"description": "Validation errors."},
            401: {"description": "Invalid email or password."},
            403: {"description": "Account is inactive or user lacks super_admin role."}
        },
        tags=["Staff Administration Operations"]
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        user = authenticate(
            request,
            username=serializer.validated_data['email'],
            password=serializer.validated_data['password']
        )

        if not user:
            return Response({'error': 'Invalid email or password.'}, status=401)

        if not user.is_active:
            return Response({'error': 'Account is inactive.'}, status=403)

        if user.role != 'super_admin':
            return Response(
                {'error': 'Access restricted to TheeInsurance staff.'},
                status=403
            )

        tokens = generate_tokens(user)

        return Response({
            "tokens": tokens,
            "role": user.role,
            "email": user.email,
            "first_name": user.first_name,
        })


class StaffPartnerKYCReviewView(APIView):
    """
    TheeInsurance staff list and review partner KYC submissions.
    """
    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List or Retrieve Partner KYC Submissions",
        description="Allows internal staff members to query incoming organizational applications. Handles both full lists filtered by status and singular lookups based on path parameters.",
        parameters=[
            OpenApiParameter(
                name="partner_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                required=False,
                description="UUID of the specific partner to inspect."
            ),
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                default="pending",
                description="Filter list lookup results by submission status (e.g., pending, approved, rejected)."
            )
        ],
        responses={
            200: {
                "type": "object",
                "description": "Returns single kyc_submission object or a list under kyc_submissions key."
            },
            404: {"description": "KYC submission not found for the designated partner."}
        },
        tags=["Staff Administration Operations"]
    )
    def get(self, request, partner_id=None):
        if partner_id:
            try:
                k = PartnerKYC.objects.select_related('partner').get(partner__id=partner_id)
                data = {
                    "partner_id": str(k.partner.id),
                    "partner_name": k.partner.name,
                    "partner_type": k.partner.partner_type,
                    "rc_number": k.rc_number,
                    "status": k.status,
                    "submitted_at": k.submitted_at,
                    "review_note": getattr(k, 'review_note', ''),
                }
                return Response({"kyc_submission": data})
            except PartnerKYC.DoesNotExist:
                return Response({"error": "KYC submission not found for this partner."}, status=404)

        status_filter = request.query_params.get("status", "pending")
        kyc_list = PartnerKYC.objects.filter(status=status_filter).select_related('partner')

        data = [
            {
                "partner_id": str(k.partner.id),
                "partner_name": k.partner.name,
                "partner_type": k.partner.partner_type,
                "rc_number": k.rc_number,
                "status": k.status,
                "submitted_at": k.submitted_at,
            }
            for k in kyc_list
        ]

        return Response({"kyc_submissions": data})

    @extend_schema(
        summary="Review Partner KYC Submission",
        description="Issue corporate state confirmations (approve/reject), record internal review notes, and toggle the partner's production operational state.",
        parameters=[
            OpenApiParameter(
                name="partner_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                required=True,
                description="UUID of the partner under review."
            )
        ],
        request={
            "application/json": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"type": "string", "enum": ["approve", "reject"]},
                    "note": {"type": "string", "description": "Reason or context for the review status decision."}
                }
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "partner": {"type": "string"},
                    "is_active": {"type": "boolean"}
                }
            },
            400: {"description": "Invalid action value supplied."},
            404: {"description": "KYC record not found."}
        },
        tags=["Staff Administration Operations"]
    )
    def patch(self, request, partner_id):
        try:
            kyc = PartnerKYC.objects.get(partner__id=partner_id)
        except PartnerKYC.DoesNotExist:
            return Response({"error": "KYC not found."}, status=404)

        action = request.data.get("action")
        note = request.data.get("note", "")

        if action not in ["approve", "reject"]:
            return Response(
                {"error": "Action must be 'approve' or 'reject'."},
                status=400
            )

        kyc.status = "approved" if action == "approve" else "rejected"
        kyc.review_note = note
        kyc.reviewed_by = request.user.email
        kyc.reviewed_at = timezone.now()
        kyc.save()

        if action == "approve":
            kyc.partner.is_active = True
            kyc.partner.save()

        event = "kyc.approved" if action == "approve" else "kyc.rejected"
        dispatch_webhook(
            kyc.partner,
            event,
            {
                "partner_id": str(kyc.partner.id),
                "partner_name": kyc.partner.name,
                "status": kyc.status,
                "note": note,
            }
        )

        return Response({
            "message": f"Partner KYC {kyc.status}.",
            "partner": kyc.partner.name,
            "is_active": kyc.partner.is_active,
        })


# Staff — Distributor Provider Access

class StaffDistributorAccessView(APIView):
    """
    TheeInsurance staff grant or revoke distributor access to a provider.
    PartnerRateThrottle applied.
    """
    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Grant Distributor-Provider Access Line",
        description="Handles creation and activation of secure business access lines between downstream insurance 'distributors' and core policy 'providers'.",
        request={
            "application/json": {
                "type": "object",
                "required": ["distributor_id", "provider_id"],
                "properties": {
                    "distributor_id": {"type": "string", "format": "uuid"},
                    "provider_id": {"type": "string", "format": "uuid"}
                }
            }
        },
        responses={
            201: {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "created": {"type": "boolean"}
                }
            },
            400: {"description": "Missing parameters."},
            404: {"description": "Designated partner nodes not found."}
        },
        tags=["Staff Administration Operations"]
    )
    def post(self, request):
        from plans.models import DistributorProviderAccess

        distributor_id = request.data.get("distributor_id")
        provider_id = request.data.get("provider_id")

        if not distributor_id or not provider_id:
            return Response(
                {"error": "distributor_id and provider_id are required."},
                status=400
            )

        try:
            distributor = Partner.objects.get(
                id=distributor_id, partner_type="distributor"
            )
            provider = Partner.objects.get(
                id=provider_id, partner_type="provider"
            )
        except Partner.DoesNotExist:
            return Response({"error": "Partner not found."}, status=404)

        access, created = DistributorProviderAccess.objects.get_or_create(
            distributor=distributor,
            provider=provider,
            defaults={"is_active": True},
        )

        if not created:
            access.is_active = True
            access.save()

        return Response({
            "message": f"Access granted: {distributor.name} → {provider.name}",
            "created": created,
        }, status=201)

    @extend_schema(
        summary="Revoke Distributor-Provider Access Line",
        description="Disables systemic relationship cross-lines between downstream insurance 'distributors' and core policy 'providers'. Logs out active synchronization lines.",
        request={
            "application/json": {
                "type": "object",
                "required": ["distributor_id", "provider_id"],
                "properties": {
                    "distributor_id": {"type": "string", "format": "uuid"},
                    "provider_id": {"type": "string", "format": "uuid"}
                }
            }
        },
        responses={
            200: {"type": "object", "properties": {"message": {"type": "string"}}},
            404: {"description": "Access relationship record not found."}
        },
        tags=["Staff Administration Operations"]
    )
    def delete(self, request):
        from plans.models import DistributorProviderAccess

        distributor_id = request.data.get("distributor_id")
        provider_id = request.data.get("provider_id")

        try:
            access = DistributorProviderAccess.objects.get(
                distributor__id=distributor_id,
                provider__id=provider_id,
            )
            access.is_active = False
            access.save()
            return Response({"message": "Access revoked."})
        except DistributorProviderAccess.DoesNotExist:
            return Response({"error": "Access record not found."}, status=404)


class StaffServiceAccountCreateView(APIView):
    """
    Staff-only endpoint to provision a service account (e.g. for n8n).
    """
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Provision Machine-to-Machine Service Account",
        description="Staff-only endpoint to provision system service accounts (e.g., n8n, runtime schedulers). The raw client_secret is returned ONLY once in this response and cannot be recovered if lost.",
        request={
            "application/json": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "example": "n8n automation workflows"}
                }
            }
        },
        responses={
            201: {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "client_secret": {"type": "string", "description": "Returned once only."},
                    "name": {"type": "string"},
                    "warning": {"type": "string"}
                }
            },
            400: {"description": "Name field missing."},
            409: {"description": "Service account with a matching generated routing identifier already exists."}
        },
        tags=["Staff Service Account Infrastructure Management"]
    )
    def post(self, request):
        name = request.data.get("name", "").strip()
        if not name:
            return Response(
                {"detail": "A 'name' is required, e.g. 'n8n automation'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slug = name.lower().replace(" ", "-")
        email = f"svc-{slug}@internal.theeinsurance.local"

        if CustomUser.objects.filter(email=email).exists():
            return Response(
                {"detail": f"A service account named '{name}' already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        user = CustomUser.objects.create_user(
            email=email,
            password=None,
            role="service_account",
            first_name=name,
            last_name="Service Account",
        )
        user.set_unusable_password()
        user.save()

        cred = ServiceAccountCredential(user=user, name=name)
        raw_secret = secrets.token_urlsafe(32)
        cred.set_secret(raw_secret)
        cred.save()

        return Response(
            {
                "client_id": cred.client_id,
                "client_secret": raw_secret,  # shown once — copy it now
                "name": cred.name,
                "warning": "This secret will not be shown again. Store it securely.",
            },
            status=status.HTTP_201_CREATED,
        )


class StaffServiceAccountListView(APIView):
    """
    Staff-only. Lists all service account credentials for audit purposes.
    """
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Audit Service Account Credentials",
        description="Staff-only view. Returns structural configuration fields, registration records, metadata states, and last usage statistics. Never surfaces the private hashed client secret details.",
        responses={
            200: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "client_id": {"type": "string"},
                        "name": {"type": "string"},
                        "is_active": {"type": "boolean"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "last_used_at": {"type": "string", "format": "date-time", "nullable": True},
                        "email": {"type": "string"}
                    }
                }
            }
        },
        tags=["Staff Service Account Infrastructure Management"]
    )
    def get(self, request):
        creds = ServiceAccountCredential.objects.select_related("user").order_by("-created_at")
        data = [
            {
                "client_id": c.client_id,
                "name": c.name,
                "is_active": c.is_active,
                "created_at": c.created_at,
                "last_used_at": c.last_used_at,
                "email": c.user.email,
            }
            for c in creds
        ]
        return Response(data, status=status.HTTP_200_OK)


class StaffServiceAccountRevokeView(APIView):
    """
    Staff-only. Deactivates a service account credential.
    """
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Revoke Service Account Permissions",
        description="Deactivates a service account credential structural link. Preserves audit history logs while rendering both credential structures and parent internal routing system users inactive.",
        parameters=[
            OpenApiParameter(
                name="client_id",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                required=True,
                description="The target API client_id hash key string."
            )
        ],       
        request=None,
        responses={
            200: {"type": "object", "properties": {"detail": {"type": "string"}}},
            404: {"description": "Target identifier string matched no active record."}
        },
        tags=["Staff Service Account Infrastructure Management"]
    )
    def post(self, request, client_id):
        try:
            cred = ServiceAccountCredential.objects.select_related("user").get(client_id=client_id)
        except ServiceAccountCredential.DoesNotExist:
            return Response({"detail": "Service account not found."}, status=status.HTTP_404_NOT_FOUND)

        cred.is_active = False
        cred.save(update_fields=["is_active"])

        cred.user.is_active = False
        cred.user.save(update_fields=["is_active"])

        return Response({"detail": f"Service account '{cred.name}' revoked."}, status=status.HTTP_200_OK)


class ServiceAccountTokenView(APIView):
    """
    POST /auth/service-account/token/
    Client-credentials style token exchange for service accounts.
    """
    permission_classes = []
    authentication_classes = []
    
    SERVICE_ACCOUNT_TOKEN_LIFETIME = timedelta(days=10)

    @extend_schema(
        summary="Service Account M2M Token Exchange",
        description="Client-credentials authentication style token mapping workspace. Handshakes inbound script applications using signed tokens back-dropped from initial custom-issued out-of-band credential mappings.",
        request={
            "application/json": {
                "type": "object",
                "required": ["client_id", "client_secret"],
                "properties": {
                    "client_id": {"type": "string"},
                    "client_secret": {"type": "string"}
                }
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "access": {"type": "string"},
                    "refresh": {"type": "string"},
                    "expires_in": {"type": "integer"}
                }
            },
            400: {"description": "Missing client credentials parameters."},
            401: {"description": "Invalid secret pairing credentials, or service node disabled."}
        },
        tags=["Authentication"]
    )
    def post(self, request):
        client_id = request.data.get("client_id")
        client_secret = request.data.get("client_secret")

        if not client_id or not client_secret:
            return Response(
                {"error": "client_id and client_secret are required."}, status=400
            )

        try:
            cred = ServiceAccountCredential.objects.select_related("user").get(
                client_id=client_id, is_active=True
            )
        except ServiceAccountCredential.DoesNotExist:
            return Response({"error": "Invalid credentials."}, status=401)

        if not cred.check_secret(client_secret):
            return Response({"error": "Invalid credentials."}, status=401)

        if not cred.user.is_active:
            return Response({"error": "Service account is inactive."}, status=401)

        cred.last_used_at = timezone.now()
        cred.save(update_fields=["last_used_at"])

        refresh = RefreshToken.for_user(cred.user)
        refresh["role"] = cred.user.role
        refresh["service_account"] = True
        refresh.set_exp(lifetime=self.SERVICE_ACCOUNT_TOKEN_LIFETIME)

        access = refresh.access_token
        access["role"] = cred.user.role
        access["service_account"] = True
        access.set_exp(lifetime=self.SERVICE_ACCOUNT_TOKEN_LIFETIME)

        return Response({
            "access": str(access),
            "refresh": str(refresh),
            "expires_in": int(self.SERVICE_ACCOUNT_TOKEN_LIFETIME.total_seconds()),
        }, status=200)
