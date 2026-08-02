"""Authentication, KYC, and Core System Administration Views.

This module handles authentication, onboarding, KYC processing, team
management, and service account tokens for the insurance platform.
"""

from datetime import timedelta
import secrets

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt import tokens as jwt_tokens
from rest_framework_simplejwt.exceptions import TokenError
from .utils import get_partner_from_user, is_full_partner_admin

from accounts.permissions import (
    IsCustomer,
    IsPartnerAdmin,
    IsStaffMember,
    IsSuperAdmin,
)
from core.models import Partner
from core.throttles import IPRateThrottle, PartnerRateThrottle
from webhooks.services import dispatch_webhook

from .models import (
    CustomerProfile,
    CustomUser,
    PartnerAdmin,
    PartnerKYC,
    ServiceAccountCredential,
    Staff,
    StaffLoginEvent,
)
from .serializers import (
    CustomerKYCSerializer,
    CustomerRegistrationSerializer,
    LoginSerializer,
    PartnerKYCSerializer,
    PartnerOnboardingSerializer,
    PartnerTeamInviteSerializer,
    PartnerTeamMemberSerializer,
    StaffCreateSerializer,
    StaffSerializer,
)


# Helper functions

def generate_tokens(user, partner_id=None):
    """Generate simple JWT access and refresh tokens for an authenticated user."""
    refresh = jwt_tokens.RefreshToken.for_user(user)
    refresh["role"] = user.role
    if partner_id:
        refresh["partner_id"] = str(partner_id)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


def _client_meta(request):
    """Extract metadata details from the incoming client request."""
    return {
        "ip_address": request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
    }


def _client_meta(request):
    """Extract metadata details from the incoming client request."""
    return {
        "ip_address": request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
    }


# Partner Onboarding
class PartnerOnboardingView(APIView):
    """Handles new insurance partner registration and initial key setup."""

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
        """Submit a new partner registration entry."""
        serializer = PartnerOnboardingSerializer(data=request.data)
        if serializer.is_valid():
            partner = serializer.save()
            return Response(
                {
                    "message": (
                        "Partner account created. Submit KYC to activate your account."
                    ),
                    "partner_id": str(partner.id),
                    "api_key": getattr(partner, "_raw_api_key", None),
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Partner KYC
class PartnerKYCView(APIView):
    """Handles fetching and submitting organizational Know Your Customer entries."""

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
        """Submit an organization's initial KYC records."""
        partner = get_partner_from_user(request.user)
        profile = getattr(request.user, "partner_admin_profile", None)

        if profile is None or not is_full_partner_admin(request.user):
            return Response(
                {
                    "error": (
                        "Only a partner_admin may submit KYC for the organization."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if hasattr(partner, "kyc"):
            return Response(
                {"error": "KYC already submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
                },
            )

            return Response(
                {
                    "message": (
                        "KYC submitted. Your account will be reviewed shortly."
                    )
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        """Retrieve the organizational KYC information status."""
        partner = get_partner_from_user(request.user)

        if not hasattr(partner, "kyc"):
            return Response(
                {"error": "No KYC submitted yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PartnerKYCSerializer(partner.kyc)
        return Response(serializer.data, status=status.HTTP_200_OK)


# Partner Team Management
class PartnerTeamView(APIView):
    """Allows team members to list or invite organizational roster accounts."""

    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request):
        """Fetch all verified members tied to the user's partner space."""
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None:
            return Response(
                {"error": "No partner admin profile found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        members = PartnerAdmin.objects.filter(
            partner=profile.partner
        ).select_related("user", "invited_by")
        return Response(
            PartnerTeamMemberSerializer(members, many=True).data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        """Invite a new manager role to the user's operational partner space."""
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None:
            return Response(
                {"error": "No partner admin profile found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PartnerTeamInviteSerializer(
            data=request.data, context={"inviter": profile}
        )
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            member = serializer.save()
        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            PartnerTeamMemberSerializer(member).data,
            status=status.HTTP_201_CREATED,
        )


class PartnerTeamMemberDeactivateView(APIView):
    """Allows partner admins to securely deactivate team profiles internally."""

    permission_classes = [IsPartnerAdmin]

    def post(self, request, member_id):
        """Deactivate a specified team account while maintaining org structural safety."""
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None or not is_full_partner_admin(request.user):
            return Response(
                {"error": "Only a partner_admin can deactivate team members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            member = PartnerAdmin.objects.select_related("user").get(
                id=member_id, partner=profile.partner
            )
        except PartnerAdmin.DoesNotExist:
            return Response(
                {"error": "Team member not found in your organization."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if member.user_id == request.user.id:
            return Response(
                {"error": "You cannot deactivate your own account here."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if member.role == "partner_admin":
            remaining = (
                PartnerAdmin.objects.filter(
                    partner=profile.partner,
                    role="partner_admin",
                    user__is_active=True,
                )
                .exclude(id=member.id)
                .count()
            )
            if remaining == 0:
                return Response(
                    {
                        "error": (
                            "Cannot deactivate the organization's last active"
                            " partner_admin."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        member.user.is_active = False
        member.user.save(update_fields=["is_active"])
        return Response(
            {"message": f"{member.user.email} has been deactivated."},
            status=status.HTTP_200_OK,
        )


# Customer Registration
class CustomerRegisterView(APIView):
    """Endpoint for new consumer registration under distinct distribution contexts."""

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
        """Process payload verification and user context provisioning."""
        serializer = CustomerRegistrationSerializer(
            data=request.data, context={"partner": request.partner}
        )
        if serializer.is_valid():
            user = serializer.save()
            tokens = generate_tokens(user, partner_id=request.partner.id)
            return Response(
                {"message": "Registration successful.", "tokens": tokens},
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Customer KYC
class CustomerKYCView(APIView):
    """Enforces profile verification tracking processes for end customers."""

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
        """Submit KYC validation profile files for processing verification."""
        try:
            profile = request.user.customer_profiles.get(
                partner=request.partner
            )
        except CustomerProfile.DoesNotExist:
            return Response(
                {"error": "Customer profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if hasattr(profile, "kyc"):
            return Response(
                {"error": "KYC already submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = CustomerKYCSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(customer=profile)
            return Response(
                {"message": "KYC submitted successfully."},
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        """Fetch submission metadata status regarding an end user."""
        try:
            profile = request.user.customer_profiles.get(
                partner=request.partner
            )
        except CustomerProfile.DoesNotExist:
            return Response(
                {"error": "Customer profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not hasattr(profile, "kyc"):
            return Response(
                {"error": "No KYC submitted yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CustomerKYCSerializer(profile.kyc)
        return Response(serializer.data, status=status.HTTP_200_OK)


# Login
class LoginView(APIView):
    """Shared authentication entry gate for customer and partner accounts."""

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
        """Authenticate login targets using raw basic email identities."""
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )

        if not user:
            return Response(
                {"error": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"error": "Account is inactive."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.role in ("super_admin", "support_admin", "service_account"):
            return Response(
                {
                    "error": (
                        "Staff accounts must log in via the staff login"
                        " endpoint."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        partner = get_partner_from_user(user)
        partner_id = partner.id if partner else None
        tokens = generate_tokens(user, partner_id=partner_id)

        return Response(
            {
                "tokens": tokens,
                "role": user.role,
                "email": user.email,
                "first_name": user.first_name,
            },
            status=status.HTTP_200_OK,
        )


# Token Refresh
class TokenRefreshView(APIView):
    """Generates standard access tokens via valid refresh lifecycles."""

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
        """Process rotation payload parameters securely."""
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"error": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh = jwt_tokens.RefreshToken(refresh_token)
            return Response(
                {"access": str(refresh.access_token)}, status=status.HTTP_200_OK
            )
        except TokenError as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_401_UNAUTHORIZED
            )


# Staff Login
class StaffLoginView(APIView):
    """Staff-only identity entrypoint tracking authorization verification events."""

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
        """Verify explicit internal administrator identities and log audit trends."""
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data["email"]
        meta = _client_meta(request)
        staff = Staff.objects.filter(email=email).first()

        user = authenticate(
            request,
            username=email,
            password=serializer.validated_data["password"],
        )

        if not user or user.role not in ("super_admin", "support_admin"):
            if staff:
                StaffLoginEvent.objects.create(
                    staff=staff,
                    successful=False,
                    failure_reason="Invalid credentials or insufficient role.",
                    **meta,
                )
            return Response(
                {"error": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            if staff:
                StaffLoginEvent.objects.create(
                    staff=staff,
                    successful=False,
                    failure_reason="Account inactive.",
                    **meta,
                )
            return Response(
                {"error": "Account is inactive."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tokens = generate_tokens(user)

        if staff:
            StaffLoginEvent.objects.create(staff=staff, successful=True, **meta)

        return Response(
            {
                "tokens": tokens,
                "role": user.role,
                "email": user.email,
                "first_name": user.first_name,
            },
            status=status.HTTP_200_OK,
        )


# Staff Account Management
class StaffAccountView(APIView):
    """Core administrative interface ensuring restricted execution of system changes."""

    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request):
        """List active functional configurations of systemic personnel structures."""
        staff = Staff.objects.select_related("created_by").order_by(
            "-date_joined"
        )
        return Response(
            StaffSerializer(staff, many=True).data, status=status.HTTP_200_OK
        )

    def post(self, request):
        """Provision systemic manager records with verified accountability traces."""
        try:
            created_by = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response(
                {"error": "Your account is not recognized as a Staff record."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = StaffCreateSerializer(
            data=request.data, context={"created_by": created_by}
        )
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            staff_record = serializer.save()
        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            StaffSerializer(staff_record).data, status=status.HTTP_201_CREATED
        )


class StaffAccountDeactivateView(APIView):
    """Super Admin endpoint safely managing system access teardown routines."""

    permission_classes = [IsSuperAdmin]

    def post(self, request, staff_id):
        """Invalidate structural processing authority keys safely."""
        try:
            by_user = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response(
                {"error": "Your account is not recognized as a Staff record."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            target = Staff.objects.get(pk=staff_id)
        except Staff.DoesNotExist:
            return Response(
                {"error": "Staff account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if target.pk == by_user.pk:
            return Response(
                {"error": "You cannot deactivate your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target.deactivate(by=by_user)
        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {"message": f"{target.email} has been deactivated."},
            status=status.HTTP_200_OK,
        )


class StaffPartnerKYCReviewView(APIView):
    """Backoffice administration pipeline review workflows for partner approvals."""

    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request, partner_id=None):
        """Aggregate review verification profiles or specific targets pending checks."""
        if partner_id:
            try:
                k = PartnerKYC.objects.select_related("partner").get(
                    partner__id=partner_id
                )
                data = {
                    "partner_id": str(k.partner.id),
                    "partner_name": k.partner.name,
                    "partner_type": k.partner.partner_type,
                    "rc_number": k.rc_number,
                    "status": k.status,
                    "submitted_at": k.submitted_at,
                    "review_note": getattr(k, "review_note", ""),
                }
                return Response(
                    {"kyc_submission": data}, status=status.HTTP_200_OK
                )
            except PartnerKYC.DoesNotExist:
                return Response(
                    {"error": "KYC submission not found for this partner."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        status_filter = request.query_params.get("status", "pending")
        kyc_list = PartnerKYC.objects.filter(status=status_filter).select_related(
            "partner"
        )

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

        return Response({"kyc_submissions": data}, status=status.HTTP_200_OK)

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
        """Update individual status records signaling network account activations."""
        try:
            kyc = PartnerKYC.objects.get(partner__id=partner_id)
        except PartnerKYC.DoesNotExist:
            return Response(
                {"error": "KYC not found."}, status=status.HTTP_404_NOT_FOUND
            )

        action = request.data.get("action")
        note = request.data.get("note", "")

        if action not in ["approve", "reject"]:
            return Response(
                {"error": "Action must be 'approve' or 'reject'."},
                status=status.HTTP_400_BAD_REQUEST,
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
            },
        )

        return Response(
            {
                "message": f"Partner KYC {kyc.status}.",
                "partner": kyc.partner.name,
                "is_active": kyc.partner.is_active,
            },
            status=status.HTTP_200_OK,
        )


# Staff — Distributor Access (provider-level and plan-level)
class StaffDistributorAccessView(APIView):
    """Processing distributor access (provider-level and plan-level)"""

    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request):
        """Enumerate outstanding active authorization grant conditions across partners."""
        from plans.models import DistributorAccessGrant

        status_filter = request.query_params.get("status", "pending")
        grants = DistributorAccessGrant.objects.filter(
            status=status_filter
        ).select_related("distributor", "provider", "plan")

        data = [
            {
                "id": str(g.id),
                "distributor": g.distributor.name,
                "provider": g.provider.name,
                "scope": g.scope,
                "plan": g.plan.name if g.plan_id else None,
                "status": g.status,
                "requested_at": g.requested_at,
            }
            for g in grants
        ]
        return Response({"access_grants": data}, status=status.HTTP_200_OK)

    def post(self, request):
        """Force provision a downstream shared risk deployment path directly."""
        from plans.models import DistributorAccessGrant, InsurancePlan

        distributor_id = request.data.get("distributor_id")
        provider_id = request.data.get("provider_id")
        scope = request.data.get("scope", "provider")
        plan_id = request.data.get("plan_id")

        if not distributor_id or not provider_id:
            return Response(
                {"error": "distributor_id and provider_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            distributor = Partner.objects.get(
                id=distributor_id, partner_type="distributor"
            )
            provider = Partner.objects.get(
                id=provider_id, partner_type="provider"
            )
        except Partner.DoesNotExist:
            return Response(
                {"error": "Partner not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        plan = None
        if scope == "plan":
            try:
                plan = InsurancePlan.objects.get(id=plan_id, provider=provider)
            except InsurancePlan.DoesNotExist:
                return Response(
                    {"error": "Plan not found for this provider."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        try:
            grant = DistributorAccessGrant.objects.grant_directly(
                distributor=distributor,
                provider=provider,
                scope=scope,
                plan=plan,
                by=request.user,
            )
        except ValidationError as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {
                "message": f"Access granted: {distributor.name} → {provider.name}",
                "scope": grant.scope,
                "status": grant.status,
            },
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request, grant_id):
        """Update or terminate authorization mapping configurations manually."""
        from plans.models import DistributorAccessGrant

        try:
            grant = DistributorAccessGrant.objects.get(id=grant_id)
        except DistributorAccessGrant.DoesNotExist:
            return Response(
                {"error": "Access grant not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        action = request.data.get("action")
        note = request.data.get("note", "")

        try:
            if action == "approve":
                DistributorAccessGrant.objects.approve(grant, by=request.user)
            elif action == "reject":
                DistributorAccessGrant.objects.reject(
                    grant, by=request.user, note=note
                )
            elif action == "revoke":
                DistributorAccessGrant.objects.revoke(grant, by=request.user)
            else:
                return Response(
                    {
                        "error": (
                            "action must be 'approve', 'reject', or 'revoke'."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                "message": f"Access grant {grant.status}.",
                "distributor": grant.distributor.name,
                "provider": grant.provider.name,
                "scope": grant.scope,
            },
            status=status.HTTP_200_OK,
        )


class StaffServiceAccountCreateView(APIView):
    """Provisions non-interactive service account profiles for automation pipelines."""

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
        """Generate specialized machine accounts along with transient secret tokens."""
        name = request.data.get("name", "").strip()
        if not name:
            return Response(
                {"detail": "A 'name' is required, e.g. 'n8n automation'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            created_by = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response(
                {"detail": "Your account is not recognized as a Staff record."},
                status=status.HTTP_403_FORBIDDEN,
            )

        slug = name.lower().replace(" ", "-")
        email = f"svc-{slug}@internal.theeinsurance.local"

        if CustomUser.objects.filter(email=email).exists():
            return Response(
                {"detail": f"A service account named '{name}' already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        staff = Staff.objects.create_staff(
            created_by=created_by,
            email=email,
            first_name=name,
            last_name="Service Account",
            role="service_account",
            password=None,
        )

        cred = ServiceAccountCredential(staff=staff, name=name)
        raw_secret = secrets.token_urlsafe(32)
        cred.set_secret(raw_secret)
        cred.save()

        return Response(
            {
                "client_id": cred.client_id,
                "client_secret": raw_secret,
                "name": cred.name,
                "warning": (
                    "This secret will not be shown again. Store it securely."
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class StaffServiceAccountListView(APIView):
    """Allows Super Admins to audit all provisioned service accounts."""

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
        """Fetch service accounts along with their last operational trace indicators."""
        creds = ServiceAccountCredential.objects.select_related("staff").order_by(
            "-created_at"
        )
        data = [
            {
                "client_id": c.client_id,
                "name": c.name,
                "is_active": c.is_active,
                "created_at": c.created_at,
                "last_used_at": c.last_used_at,
                "email": c.staff.email,
            }
            for c in creds
        ]
        return Response(data, status=status.HTTP_200_OK)


class StaffServiceAccountRevokeView(APIView):
    """Instantly kills target service account profile sever execution paths."""

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
        """Revoke a target configuration permanently."""
        try:
            cred = ServiceAccountCredential.objects.select_related("staff").get(
                client_id=client_id
            )
        except ServiceAccountCredential.DoesNotExist:
            return Response(
                {"detail": "Service account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        cred.is_active = False
        cred.save(update_fields=["is_active"])

        cred.staff.is_active = False
        cred.staff.save(update_fields=["is_active"])

        return Response(
            {"detail": f"Service account '{cred.name}' revoked."},
            status=status.HTTP_200_OK,
        )


# Service Account Token Setup
class ServiceAccountTokenView(APIView):
    """Automation service account view"""

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
        """Authenticate explicit service account credentials pairing inputs securely."""
        client_id = request.data.get("client_id")
        client_secret = request.data.get("client_secret")

        if not client_id or not client_secret:
            return Response(
                {"error": "client_id and client_secret are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cred = ServiceAccountCredential.objects.select_related("staff").get(
                client_id=client_id, is_active=True
            )
        except ServiceAccountCredential.DoesNotExist:
            return Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not cred.check_secret(client_secret):
            return Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not cred.staff.is_active:
            return Response(
                {"error": "Service account is inactive."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        cred.last_used_at = timezone.now()
        cred.save(update_fields=["last_used_at"])

        refresh = jwt_tokens.RefreshToken.for_user(cred.staff)
        refresh["role"] = cred.staff.role
        refresh["service_account"] = True
        refresh.set_exp(lifetime=self.SERVICE_ACCOUNT_TOKEN_LIFETIME)

        access = refresh.access_token
        access["role"] = cred.staff.role
        access["service_account"] = True
        access.set_exp(lifetime=self.SERVICE_ACCOUNT_TOKEN_LIFETIME)

        return Response(
            {
                "access": str(access),
                "refresh": str(refresh),
                "expires_in": int(
                    self.SERVICE_ACCOUNT_TOKEN_LIFETIME.total_seconds()
                ),
            },
            status=status.HTTP_200_OK,
        )
