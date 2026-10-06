"""Authentication, KYC and core system administration views."""

import secrets
from datetime import timedelta

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer,
)
from rest_framework import serializers as s
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt import tokens as jwt_tokens
from rest_framework_simplejwt.exceptions import TokenError

from accounts import emails
from accounts.docs import (
    ApiKeyResponse, KYCReviewResponse, LOGIN_EXAMPLES, LoginResponse, ONBOARD_201,
    ONBOARD_REQ_EXAMPLES, OnboardResponse, PROFILE_EXAMPLE, ServiceAccountCreated,
)
from accounts.permissions import (
    IsCustomer, IsHumanStaff, IsPartnerAdmin, IsStaffMember, IsSuperAdmin,
)
from accounts.utils import get_partner_from_user, is_full_partner_admin
from core.docs import (
    DetailSerializer, ErrorSerializer, MessageSerializer, SAMPLE_TOKENS, Tag,
    TokenPairSerializer, error, validation_error,
)
from core.models import Partner
from core.throttles import IPRateThrottle, PartnerRateThrottle
from webhooks.events import E
from webhooks.services import emit

from .models import (
    CustomerKYC, CustomerProfile, CustomUser, PartnerAdmin, PartnerKYC,
    ServiceAccountCredential, Staff, StaffLoginEvent,
)
from .serializers import (
    CustomerKYCSerializer, CustomerRegistrationSerializer, LoginSerializer,
    PartnerKYCSerializer, PartnerMeSerializer, PartnerOnboardingSerializer,
    PartnerPasswordConfirmSerializer, PartnerProfileSerializer,
    PartnerProfileUpdateSerializer, PartnerTeamInviteSerializer,
    PartnerTeamMemberSerializer, StaffCreateSerializer, StaffSerializer,
)

PARTNER_KEY_AUTH = [{"BearerAuth": [], "PartnerKey": []}]   # customer endpoints need both


# ---------------------------------------------------------------- helpers
def generate_tokens(user, partner_id=None):
    refresh = jwt_tokens.RefreshToken.for_user(user)
    refresh["role"] = user.role
    if partner_id:
        refresh["partner_id"] = str(partner_id)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def _client_meta(request):
    return {
        "ip_address": request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
    }


def _customer_kyc_status(user, partner):
    if user.role != "customer" or partner is None:
        return None
    kyc = CustomerKYC.objects.filter(customer__user=user, customer__partner=partner).first()
    return kyc.status if kyc else "not_submitted"


# ================================================================ 1. PARTNERS
class PartnerOnboardingView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Partner sign-up (Get Started)",
        description=(
            "Step 1 of partner onboarding.\n\n"
            "The partner chooses `partner_type` (`provider` or `distributor`):\n"
            "- **distributor** → `commission_rate` is **required**.\n"
            "- **provider** → `commission_rate` must be **omitted**.\n\n"
            "Creates the partner (inactive), its first `partner_admin` user, and issues the "
            "**X-Partner-Key** (shown once only; it stays inactive until staff approve KYC). "
            "JWT tokens are returned so the portal can go straight to `POST /partner/kyc/`."
        ),
        auth=[],
        request=PartnerOnboardingSerializer,
        responses={
            201: OnboardResponse,
            400: validation_error("commission_rate", "Required for distributors."),
            429: error("Rate limited.", "Request was throttled.", key="detail"),
        },
        examples=ONBOARD_REQ_EXAMPLES + [ONBOARD_201],
        tags=[Tag.PARTNERS],
    )
    def post(self, request):
        serializer = PartnerOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        partner = serializer.save()
        tokens = generate_tokens(serializer.admin_user, partner_id=partner.id)
        return Response(
            {
                "message": "Partner account created. Submit KYC to activate your X-Partner-Key.",
                "partner_id": str(partner.id),
                "partner_type": partner.partner_type,
                "is_active": partner.is_active,
                "api_key": getattr(partner, "_raw_api_key", None),
                "tokens": tokens,
                "next_step": "POST /api/partner/kyc/",
            },
            status=status.HTTP_201_CREATED,
        )


class PartnerKYCView(APIView):
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Submit partner KYC",
        description=(
            "Step 2 of onboarding. `multipart/form-data`.\n\n"
            "Required: `rc_number`, `tax_identification_number`, `cac_certificate`, `proof_of_address`. "
            "Providers must also send `naicom_licence_number` and `naicom_licence_doc`.\n\n"
            "On success a Resend email tells the partner the KYC is under review. "
            "If a previous submission was **rejected**, calling this again resubmits it (status returns to `pending`)."
        ),
        request={"multipart/form-data": PartnerKYCSerializer},
        responses={
            201: MessageSerializer,
            400: validation_error("rc_number", "partner kyc with this rc number already exists."),
            403: error("Caller is not a full partner_admin.", "Only a partner_admin may submit KYC for the organization."),
            409: error("KYC is pending review or already approved.", "KYC is already pending review."),
        },
        examples=[OpenApiExample("Created", response_only=True, status_codes=["201"],
                                 value={"message": "KYC submitted. We'll email you once it has been reviewed."})],
        tags=[Tag.PARTNERS],
    )
    def post(self, request):
        partner = get_partner_from_user(request.user)
        if not is_full_partner_admin(request.user) or partner is None:
            return Response({"error": "Only a partner_admin may submit KYC for the organization."},
                            status=status.HTTP_403_FORBIDDEN)

        existing = getattr(partner, "kyc", None)
        if existing and existing.status != "rejected":
            return Response({"error": f"KYC is already {existing.status}."}, status=status.HTTP_409_CONFLICT)

        serializer = PartnerKYCSerializer(instance=existing, data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            if existing:   # resubmission after rejection
                serializer.save(status="pending", review_note="", reviewed_by="", reviewed_at=None)
            else:
                serializer.save(partner=partner)
            emit(E.KYC_SUBMITTED, partner=partner, aggregate_id=partner.id,
                 data={"partner_id": str(partner.id), "partner_name": partner.name,
                       "partner_type": partner.partner_type})
            emails.on_commit_send(emails.send_kyc_received, partner)

        return Response({"message": "KYC submitted. We'll email you once it has been reviewed."},
                        status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Get my KYC status",
        responses={
            200: PartnerKYCSerializer,
            404: error("No KYC submitted yet.", "No KYC submitted yet."),
        },
        examples=[OpenApiExample("Pending", response_only=True, status_codes=["200"], value={
            "rc_number": "RC1234567", "naicom_licence_number": "", "tax_identification_number": "12345678-0001",
            "status": "pending", "review_note": "", "submitted_at": "2026-10-06T09:12:44Z", "reviewed_at": None})],
        tags=[Tag.PARTNERS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not hasattr(partner, "kyc"):
            return Response({"error": "No KYC submitted yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PartnerKYCSerializer(partner.kyc).data)


class PartnerMeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Who am I (partner portal bootstrap)",
        description="Call right after login to learn the partner's name and whether it is a provider or distributor.",
        responses={
            200: PartnerMeSerializer,
            403: error("Not a partner admin.", "This account is not linked to a partner.", key="detail"),
        },
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={"partner_type": "distributor", "partner_name": "QuickCover Ltd"})],
        tags=[Tag.PARTNERS],
    )
    def get(self, request):
        user = request.user
        if user.role != "partner_admin" or not hasattr(user, "partner_admin_profile"):
            return Response({"detail": "This account is not linked to a partner."}, status=status.HTTP_403_FORBIDDEN)
        return Response(PartnerMeSerializer(user).data)


class PartnerProfileView(APIView):
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Get partner profile",
        description="Includes settlement details and `kyc_status`. `commission_rate` is present for distributors only.",
        responses={200: PartnerProfileSerializer, 403: error("Not linked to a partner.", "This account is not linked to a partner.", key="detail")},
        examples=[OpenApiExample("Distributor", value=PROFILE_EXAMPLE, response_only=True, status_codes=["200"])],
        tags=[Tag.PARTNERS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        if partner is None:
            return Response({"detail": "This account is not linked to a partner."}, status=status.HTTP_403_FORBIDDEN)
        return Response(PartnerProfileSerializer(partner).data)

    @extend_schema(
        summary="Update partner profile / settlement account",
        description=(
            "Partial update. **Only available once KYC is `approved`**, otherwise 403 with `code: kyc_not_verified`.\n\n"
            "Editable: `phone_number`, `address`, `website`, `settlement` "
            "(`account_name`, `account_number` = 10-digit NUBAN, `bank_code`). "
            "`name`, `partner_type`, `commission_rate` are not editable here."
        ),
        request=PartnerProfileUpdateSerializer,
        responses={
            200: PartnerProfileSerializer,
            400: validation_error("settlement", "{'account_number': ['Must be a 10-digit NUBAN account number.']}"),
            403: OpenApiResponse(response=OpenApiTypes.OBJECT, description="KYC not verified, or caller is not a full partner_admin.",
                                 examples=[OpenApiExample("KYC not verified", value={
                                     "error": "Profile can only be updated after KYC is verified.",
                                     "code": "kyc_not_verified", "kyc_status": "pending"})]),
        },
        examples=[OpenApiExample("Add settlement account", request_only=True, value={
            "phone_number": "+2348012345678", "website": "https://quickcover.ng",
            "settlement": {"account_name": "QuickCover Ltd", "account_number": "0123456789", "bank_code": "058"}}),
            OpenApiExample("Updated", response_only=True, status_codes=["200"], value=PROFILE_EXAMPLE)],
        tags=[Tag.PARTNERS],
    )
    def patch(self, request):
        partner = get_partner_from_user(request.user)
        if partner is None or not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can update the profile."}, status=status.HTTP_403_FORBIDDEN)
        kyc_status = partner.kyc.status if hasattr(partner, "kyc") else "not_submitted"
        if kyc_status != "approved":
            return Response({"error": "Profile can only be updated after KYC is verified.",
                             "code": "kyc_not_verified", "kyc_status": kyc_status},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = PartnerProfileUpdateSerializer(partner, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        partner = serializer.save()
        partner.refresh_from_db()
        return Response(PartnerProfileSerializer(partner).data)


class PartnerAPIKeyRegenerateView(APIView):
    """Keys are stored hashed, so they can't be retrieved, only rotated."""
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Regenerate X-Partner-Key",
        description=(
            "Confirms the caller's password, then **rotates** the key. The old key stops working "
            "immediately (every integration using it gets 401). The new key is shown once only. "
            "Use this if the key is lost or compromised."
        ),
        request=PartnerPasswordConfirmSerializer,
        responses={200: ApiKeyResponse, 400: validation_error("password", "Incorrect password."),
                   403: error("Not linked to a partner.", "This account is not linked to a partner.", key="detail")},
        examples=[OpenApiExample("Request", request_only=True, value={"password": "S3curePassw0rd!"}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={
                      "api_key": "Zp9aQ2wS4dE6rF8tG0yH1uJ3iK5oL7-mN9bV1cX3zA5",
                      "warning": "This key will not be shown again. The previous key is now invalid, update every integration."})],
        tags=[Tag.PARTNERS],
    )
    def post(self, request):
        partner = get_partner_from_user(request.user)
        if partner is None:
            return Response({"detail": "This account is not linked to a partner."}, status=status.HTTP_403_FORBIDDEN)
        ser = PartnerPasswordConfirmSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        raw = secrets.token_urlsafe(32)
        partner.api_key_hash = Partner.hash_key(raw)      # was `partner.api_key` (field doesn't exist)
        partner.save(update_fields=["api_key_hash"])
        return Response({"api_key": raw,
                         "warning": "This key will not be shown again. The previous key is now invalid, "
                                    "update every integration."})


class PartnerTeamView(APIView):
    permission_classes = [IsPartnerAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List team members",
        responses={200: PartnerTeamMemberSerializer(many=True),
                   404: error("No partner admin profile.", "No partner admin profile found.")},
        tags=[Tag.PARTNERS],
    )
    def get(self, request):
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None:
            return Response({"error": "No partner admin profile found."}, status=status.HTTP_404_NOT_FOUND)
        members = PartnerAdmin.objects.filter(partner=profile.partner).select_related("user", "invited_by")
        return Response(PartnerTeamMemberSerializer(members, many=True).data)

    @extend_schema(
        summary="Invite a team member",
        description="Only a full `partner_admin` can invite. `role`: `partner_admin` | `support_partner_admin` | `partner_viewer`. "
                    "The invitee is created inactive.",
        request=PartnerTeamInviteSerializer,
        responses={201: PartnerTeamMemberSerializer, 400: validation_error("role", '"admin" is not a valid choice.'),
                   403: error("Inviter lacks permission.", "Only a partner_admin can invite new team members to the organization.")},
        examples=[OpenApiExample("Request", request_only=True, value={
            "email": "kemi@quickcover.ng", "first_name": "Kemi", "last_name": "Adeyemi", "role": "support_partner_admin"})],
        tags=[Tag.PARTNERS],
    )
    def post(self, request):
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None:
            return Response({"error": "No partner admin profile found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = PartnerTeamInviteSerializer(data=request.data, context={"inviter": profile})
        serializer.is_valid(raise_exception=True)
        try:
            member = serializer.save()
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=status.HTTP_403_FORBIDDEN)
        return Response(PartnerTeamMemberSerializer(member).data, status=status.HTTP_201_CREATED)


class PartnerTeamMemberDeactivateView(APIView):
    permission_classes = [IsPartnerAdmin]

    @extend_schema(
        summary="Deactivate a team member",
        parameters=[OpenApiParameter("member_id", OpenApiTypes.UUID, OpenApiParameter.PATH,
                                     description="`PartnerAdmin.id` from the team list.")],
        request=None,
        responses={200: MessageSerializer,
                   400: error("Self-deactivation or last active partner_admin.", "Cannot deactivate the organization's last active partner_admin."),
                   403: error("Not a full partner_admin.", "Only a partner_admin can deactivate team members."),
                   404: error("Member not in this organization.", "Team member not found in your organization.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"],
                                 value={"message": "kemi@quickcover.ng has been deactivated."})],
        tags=[Tag.PARTNERS],
    )
    def post(self, request, member_id):
        profile = getattr(request.user, "partner_admin_profile", None)
        if profile is None or not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can deactivate team members."}, status=status.HTTP_403_FORBIDDEN)
        try:
            member = PartnerAdmin.objects.select_related("user").get(id=member_id, partner=profile.partner)
        except PartnerAdmin.DoesNotExist:
            return Response({"error": "Team member not found in your organization."}, status=status.HTTP_404_NOT_FOUND)
        if member.user_id == request.user.id:
            return Response({"error": "You cannot deactivate your own account here."}, status=status.HTTP_400_BAD_REQUEST)
        if member.role == "partner_admin":
            remaining = (PartnerAdmin.objects.filter(partner=profile.partner, role="partner_admin", user__is_active=True)
                         .exclude(id=member.id).count())
            if remaining == 0:
                return Response({"error": "Cannot deactivate the organization's last active partner_admin."},
                                status=status.HTTP_400_BAD_REQUEST)
        member.user.is_active = False
        member.user.save(update_fields=["is_active"])
        return Response({"message": f"{member.user.email} has been deactivated."})


# ================================================================ SHARED AUTH (Partners + Customers)
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Login (partner admins and customers)",
        description=(
            "Returns JWT tokens carrying `role` and `partner_id` claims. Staff must use `/staff/auth/login/`.\n\n"
            "For customers the response includes `kyc_status` so the UI can gate purchasing "
            "(`not_submitted` | `pending` | `approved` | `rejected`)."
        ),
        auth=[],
        request=LoginSerializer,
        responses={
            200: LoginResponse,
            400: validation_error("email", "This field is required."),
            401: error("Wrong email or password.", "Invalid email or password."),
            403: error("Staff account used the wrong endpoint.", "Staff accounts must log in via the staff login endpoint."),
        },
        examples=[OpenApiExample("Request", request_only=True, value={"email": "ada@quickcover.ng", "password": "S3curePassw0rd!"})] + LOGIN_EXAMPLES,
        tags=[Tag.PARTNERS, Tag.CUSTOMERS],
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request, username=serializer.validated_data["email"],
                            password=serializer.validated_data["password"])
        if not user:
            return Response({"error": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({"error": "Account is inactive."}, status=status.HTTP_403_FORBIDDEN)
        if user.role in ("super_admin", "support_admin", "service_account"):
            return Response({"error": "Staff accounts must log in via the staff login endpoint."},
                            status=status.HTTP_403_FORBIDDEN)

        partner = get_partner_from_user(user)
        body = {"tokens": generate_tokens(user, partner_id=partner.id if partner else None),
                "role": user.role, "email": user.email, "first_name": user.first_name}
        kyc_status = _customer_kyc_status(user, partner)
        if kyc_status:
            body["kyc_status"] = kyc_status
        return Response(body)


class TokenRefreshView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Refresh access token",
        auth=[],
        request=inline_serializer("RefreshRequest", {"refresh": s.CharField()}),
        responses={200: inline_serializer("RefreshResponse", {"access": s.CharField()}),
                   400: error("Missing refresh token.", "Refresh token is required."),
                   401: error("Invalid or expired token.", "Token is invalid or expired.")},
        examples=[OpenApiExample("Request", request_only=True, value={"refresh": "eyJhbGciOiJIUzI1NiIs..."}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={"access": "eyJhbGciOiJIUzI1NiIs..."})],
        tags=[Tag.PARTNERS, Tag.CUSTOMERS],
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({"access": str(jwt_tokens.RefreshToken(refresh_token).access_token)})
        except TokenError as e:
            return Response({"error": str(e)}, status=status.HTTP_401_UNAUTHORIZED)


# ================================================================ 4. CUSTOMERS
class CustomerRegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Register a customer",
        description="Requires the `X-Partner-Key` header of the provider/distributor the customer is signing up through. "
                    "The customer is bound to that partner and can then browse its plans.",
        auth=[{"PartnerKey": []}],
        request=CustomerRegistrationSerializer,
        responses={
            201: inline_serializer("CustomerRegisterResponse", {"message": s.CharField(), "tokens": TokenPairSerializer}),
            400: validation_error("email", "An account with this email already exists."),
            401: error("Missing/invalid/inactive X-Partner-Key.", "Invalid or inactive partner key.", key="detail"),
        },
        examples=[OpenApiExample("Request", request_only=True, value={
            "email": "chidi@example.com", "password": "S3curePassw0rd!", "first_name": "Chidi", "last_name": "Eze",
            "phone_number": "+2348031234567", "date_of_birth": "1994-03-21", "gender": "male",
            "address": "7 Admiralty Way, Lekki, Lagos"}),
            OpenApiExample("Created", response_only=True, status_codes=["201"],
                           value={"message": "Registration successful.", "tokens": SAMPLE_TOKENS})],
        tags=[Tag.CUSTOMERS],
    )
    def post(self, request):
        serializer = CustomerRegistrationSerializer(data=request.data, context={"partner": request.partner})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({"message": "Registration successful.",
                         "tokens": generate_tokens(user, partner_id=request.partner.id)},
                        status=status.HTTP_201_CREATED)


class CustomerKYCView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [PartnerRateThrottle]

    def _profile(self, request):
        return request.user.customer_profiles.filter(partner=request.partner).first()

    @extend_schema(
        summary="Submit customer KYC",
        description=(
            "`multipart/form-data`. `id_type`: `nin` | `bvn` | `passport` | `drivers_licence` | `voters_card`. "
            "`selfie` is optional. Until KYC is **approved** the customer can browse plans but cannot subscribe. "
            "A **rejected** KYC can be resubmitted."
        ),
        auth=PARTNER_KEY_AUTH,
        request={"multipart/form-data": CustomerKYCSerializer},
        responses={201: MessageSerializer, 400: validation_error("id_document", "No file was submitted."),
                   404: error("Customer profile not found for this partner.", "Customer profile not found."),
                   409: error("KYC pending or approved.", "KYC is already pending.")},
        examples=[OpenApiExample("Created", response_only=True, status_codes=["201"],
                                 value={"message": "KYC submitted successfully. It will be reviewed shortly."})],
        tags=[Tag.CUSTOMERS],
    )
    def post(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=status.HTTP_404_NOT_FOUND)
        existing = getattr(profile, "kyc", None)
        if existing and existing.status != "rejected":
            return Response({"error": f"KYC is already {existing.status}."}, status=status.HTTP_409_CONFLICT)
        serializer = CustomerKYCSerializer(instance=existing, data=request.data)
        serializer.is_valid(raise_exception=True)
        if existing:
            serializer.save(status="pending", review_note="", reviewed_at=None)
        else:
            serializer.save(customer=profile)
        return Response({"message": "KYC submitted successfully. It will be reviewed shortly."},
                        status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Get my KYC",
        auth=PARTNER_KEY_AUTH,
        responses={200: CustomerKYCSerializer,
                   404: error("Profile or KYC not found.", "No KYC submitted yet.")},
        examples=[OpenApiExample("Rejected", response_only=True, status_codes=["200"], value={
            "id_type": "nin", "id_number": "12345678901", "status": "rejected",
            "review_note": "ID photo is blurry. Please re-upload.",
            "submitted_at": "2026-10-06T10:00:00Z", "reviewed_at": "2026-10-06T14:30:00Z"})],
        tags=[Tag.CUSTOMERS],
    )
    def get(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response({"error": "Customer profile not found."}, status=status.HTTP_404_NOT_FOUND)
        if not hasattr(profile, "kyc"):
            return Response({"error": "No KYC submitted yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(CustomerKYCSerializer(profile.kyc).data)


# ================================================================ 5. STAFF
class StaffLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    @extend_schema(
        summary="Staff login",
        description="For `super_admin` and `support_admin` only. Every attempt is written to `StaffLoginEvent`.",
        auth=[],
        request=LoginSerializer,
        responses={200: LoginResponse, 400: validation_error("email", "This field is required."),
                   401: error("Invalid credentials or not a staff admin.", "Invalid email or password."),
                   403: error("Account inactive.", "Account is inactive.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "tokens": SAMPLE_TOKENS, "role": "super_admin", "email": "admin@theeinsurance.com", "first_name": "Hilda"})],
        tags=[Tag.STAFF],
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        meta = _client_meta(request)
        staff = Staff.objects.filter(email=email).first()
        user = authenticate(request, username=email, password=serializer.validated_data["password"])

        def log(ok, reason=""):
            if staff:
                StaffLoginEvent.objects.create(staff=staff, successful=ok, failure_reason=reason, **meta)

        if not user or user.role not in ("super_admin", "support_admin"):
            log(False, "Invalid credentials or insufficient role.")
            return Response({"error": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            log(False, "Account inactive.")
            return Response({"error": "Account is inactive."}, status=status.HTTP_403_FORBIDDEN)

        log(True)
        return Response({"tokens": generate_tokens(user), "role": user.role,
                         "email": user.email, "first_name": user.first_name})


class StaffAccountView(APIView):
    permission_classes = [IsSuperAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(summary="List staff accounts", responses={200: StaffSerializer(many=True)}, tags=[Tag.STAFF])
    def get(self, request):
        staff = Staff.objects.select_related("created_by").order_by("-date_joined")
        return Response(StaffSerializer(staff, many=True).data)

    @extend_schema(
        summary="Create staff account (super admin only)",
        description="`role`: `super_admin` | `support_admin` | `service_account`.",
        request=StaffCreateSerializer,
        responses={201: StaffSerializer, 400: validation_error("role", '"x" is not a valid choice.'),
                   403: error("Caller is not a Staff record / not super admin.", "Only a Super Admin can create a staff account.")},
        examples=[OpenApiExample("Request", request_only=True, value={
            "email": "support@theeinsurance.com", "first_name": "Sade", "last_name": "Ojo",
            "role": "support_admin", "password": "S3curePassw0rd!"})],
        tags=[Tag.STAFF],
    )
    def post(self, request):
        try:
            created_by = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response({"error": "Your account is not recognized as a Staff record."}, status=status.HTTP_403_FORBIDDEN)
        serializer = StaffCreateSerializer(data=request.data, context={"created_by": created_by})
        serializer.is_valid(raise_exception=True)
        try:
            record = serializer.save()
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=status.HTTP_403_FORBIDDEN)
        return Response(StaffSerializer(record).data, status=status.HTTP_201_CREATED)


class StaffAccountDeactivateView(APIView):
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Deactivate staff account",
        parameters=[OpenApiParameter("staff_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        request=None,
        responses={200: MessageSerializer, 400: error("Self-deactivation.", "You cannot deactivate your own account."),
                   403: error("Not a super admin.", "Only a Super Admin can deactivate a staff account."),
                   404: error("Unknown staff id.", "Staff account not found.")},
        tags=[Tag.STAFF],
    )
    def post(self, request, staff_id):
        try:
            by_user = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response({"error": "Your account is not recognized as a Staff record."}, status=status.HTTP_403_FORBIDDEN)
        try:
            target = Staff.objects.get(pk=staff_id)
        except Staff.DoesNotExist:
            return Response({"error": "Staff account not found."}, status=status.HTTP_404_NOT_FOUND)
        if target.pk == by_user.pk:
            return Response({"error": "You cannot deactivate your own account."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target.deactivate(by=by_user)
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=status.HTTP_403_FORBIDDEN)
        return Response({"message": f"{target.email} has been deactivated."})


KYC_LIST_ITEM = {"partner_id": "3f6c1f4e-8a58-4b6e-9a53-2d6a7f0d9c11", "partner_name": "QuickCover Ltd",
                 "partner_type": "distributor", "rc_number": "RC1234567", "status": "pending",
                 "submitted_at": "2026-10-06T09:12:44Z"}


class StaffPartnerKYCReviewView(APIView):
    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List partner KYC submissions / get one",
        description="Without `partner_id`: list filtered by `?status=` (default `pending`). With `partner_id`: single submission.",
        parameters=[
            OpenApiParameter("partner_id", OpenApiTypes.UUID, OpenApiParameter.PATH, required=False),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=["pending", "approved", "rejected"]),
        ],
        responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="List or single submission.",
                                        examples=[OpenApiExample("List", value={"kyc_submissions": [KYC_LIST_ITEM]}),
                                                  OpenApiExample("Single", value={"kyc_submission": {**KYC_LIST_ITEM, "review_note": ""}})]),
                   404: error("No KYC for this partner.", "KYC submission not found for this partner.")},
        tags=[Tag.STAFF],
    )
    def get(self, request, partner_id=None):
        if partner_id:
            try:
                k = PartnerKYC.objects.select_related("partner").get(partner__id=partner_id)
            except PartnerKYC.DoesNotExist:
                return Response({"error": "KYC submission not found for this partner."}, status=status.HTTP_404_NOT_FOUND)
            return Response({"kyc_submission": {
                "partner_id": str(k.partner.id), "partner_name": k.partner.name, "partner_type": k.partner.partner_type,
                "rc_number": k.rc_number, "status": k.status, "submitted_at": k.submitted_at,
                "review_note": k.review_note}})
        status_filter = request.query_params.get("status", "pending")
        kyc_list = PartnerKYC.objects.filter(status=status_filter).select_related("partner")
        return Response({"kyc_submissions": [{
            "partner_id": str(k.partner.id), "partner_name": k.partner.name, "partner_type": k.partner.partner_type,
            "rc_number": k.rc_number, "status": k.status, "submitted_at": k.submitted_at} for k in kyc_list]})

    @extend_schema(
        summary="Approve or reject partner KYC",
        description=(
            "**approve** → partner becomes active (X-Partner-Key goes live) and a Resend email is sent. "
            "**reject** → `note` is required and is emailed to the partner, who may resubmit. "
            "Already-reviewed submissions return 409."
        ),
        parameters=[OpenApiParameter("partner_id", OpenApiTypes.UUID, OpenApiParameter.PATH, required=True)],
        request=inline_serializer("KYCReviewRequest", {"action": s.ChoiceField(["approve", "reject"]),
                                                       "note": s.CharField(required=False)}),
        responses={200: KYCReviewResponse, 400: error("Bad action, or note missing on reject.", "A note is required when rejecting."),
                   404: error("KYC not found.", "KYC not found."),
                   409: error("Already reviewed.", "KYC has already been approved.")},
        examples=[OpenApiExample("Approve", request_only=True, value={"action": "approve"}),
                  OpenApiExample("Reject", request_only=True, value={"action": "reject", "note": "CAC certificate is expired."}),
                  OpenApiExample("Approved", response_only=True, status_codes=["200"],
                                 value={"message": "Partner KYC approved.", "partner": "QuickCover Ltd", "is_active": True})],
        tags=[Tag.STAFF],
    )
    def patch(self, request, partner_id):
        kyc = PartnerKYC.objects.filter(partner__id=partner_id).first()
        if not kyc:
            return Response({"error": "KYC not found."}, status=status.HTTP_404_NOT_FOUND)
        action = request.data.get("action")
        if action not in ("approve", "reject"):
            return Response({"error": "Action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            kyc = review_partner_kyc(kyc.pk, approve=action == "approve",
                                    note=request.data.get("note", ""), reviewer_email=request.user.email)
        except KYCReviewError as e:
            return Response({"error": str(e)}, status=e.status_code)
        return Response({"message": f"Partner KYC {kyc.status}.", "partner": kyc.partner.name,
                        "is_active": kyc.partner.is_active})

class StaffCustomerKYCReviewView(APIView):
    """NEW: nothing could approve customer KYC before, so no customer could ever subscribe."""
    permission_classes = [IsStaffMember]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List customer KYC submissions",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=["pending", "approved", "rejected"])],
        responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Submissions.", examples=[OpenApiExample("List", value={
            "kyc_submissions": [{"id": "9b1d6c0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f", "customer_email": "chidi@example.com",
                                 "partner_name": "QuickCover Ltd", "id_type": "nin", "status": "pending",
                                 "submitted_at": "2026-10-06T10:00:00Z"}]})])},
        tags=[Tag.STAFF],
    )
    def get(self, request):
        qs = (CustomerKYC.objects.filter(status=request.query_params.get("status", "pending"))
              .select_related("customer__user", "customer__partner"))
        return Response({"kyc_submissions": [{
            "id": str(k.id), "customer_email": k.customer.user.email, "partner_name": k.customer.partner.name,
            "id_type": k.id_type, "status": k.status, "submitted_at": k.submitted_at} for k in qs]})

    @extend_schema(
        summary="Approve or reject customer KYC",
        parameters=[OpenApiParameter("kyc_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        request=inline_serializer("CustomerKYCReviewRequest", {"action": s.ChoiceField(["approve", "reject"]),
                                                               "note": s.CharField(required=False)}),
        responses={200: MessageSerializer, 400: error("Bad action / note missing.", "A note is required when rejecting."),
                   404: error("Not found.", "KYC not found."), 409: error("Already reviewed.", "KYC has already been approved.")},
        examples=[OpenApiExample("Reject", request_only=True, value={"action": "reject", "note": "ID photo is blurry."}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={"message": "Customer KYC approved."})],
        tags=[Tag.STAFF],
    )
    def patch(self, request, kyc_id):
        if not CustomerKYC.objects.filter(id=kyc_id).exists():
            return Response({"error": "KYC not found."}, status=status.HTTP_404_NOT_FOUND)
        action = request.data.get("action")
        if action not in ("approve", "reject"):
            return Response({"error": "Action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            kyc = review_customer_kyc(kyc_id, approve=action == "approve", note=request.data.get("note", ""))
        except KYCReviewError as e:
            return Response({"error": str(e)}, status=e.status_code)

        return Response({"message": f"Customer KYC {kyc.status}."})

# ---- Service accounts
class StaffServiceAccountCreateView(APIView):
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Create service account (M2M)",
        description="For automation such as n8n. `client_secret` is returned **once** and cannot be recovered.",
        request=inline_serializer("ServiceAccountCreateRequest", {"name": s.CharField()}),
        responses={201: ServiceAccountCreated, 400: error("Name missing.", "A 'name' is required, e.g. 'n8n automation'.", key="detail"),
                   409: error("Duplicate name.", "A service account named 'n8n automation' already exists.", key="detail")},
        examples=[OpenApiExample("Request", request_only=True, value={"name": "n8n automation"}),
                  OpenApiExample("Created", response_only=True, status_codes=["201"], value={
                      "client_id": "Xk2m9QvT7nB4cR1sW6yU3hJ8aL5dF0gE", "client_secret": "r4N8v1Zq6Pw3Tj9Ls2Kc7Hb5Xa0Mf-d",
                      "name": "n8n automation", "warning": "This secret will not be shown again. Store it securely."})],
        tags=[Tag.STAFF],
    )
    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "A 'name' is required, e.g. 'n8n automation'."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            created_by = Staff.objects.get(pk=request.user.pk)
        except Staff.DoesNotExist:
            return Response({"detail": "Your account is not recognized as a Staff record."}, status=status.HTTP_403_FORBIDDEN)

        email = f"svc-{name.lower().replace(' ', '-')}@internal.theeinsurance.local"
        if CustomUser.objects.filter(email=email).exists():
            return Response({"detail": f"A service account named '{name}' already exists."}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            staff = Staff.objects.create_staff(created_by=created_by, email=email, first_name=name,
                                               last_name="Service Account", role="service_account", password=None)
            cred = ServiceAccountCredential(staff=staff, name=name)
            raw_secret = secrets.token_urlsafe(32)
            cred.set_secret(raw_secret)
            cred.save()
        return Response({"client_id": cred.client_id, "client_secret": raw_secret, "name": cred.name,
                         "warning": "This secret will not be shown again. Store it securely."},
                        status=status.HTTP_201_CREATED)


class StaffServiceAccountListView(APIView):
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="List service accounts",
        responses={200: inline_serializer("ServiceAccountItem", {
            "client_id": s.CharField(), "name": s.CharField(), "is_active": s.BooleanField(),
            "created_at": s.DateTimeField(), "last_used_at": s.DateTimeField(allow_null=True), "email": s.EmailField()}, many=True)},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=[{
            "client_id": "Xk2m9QvT7nB4cR1sW6yU3hJ8aL5dF0gE", "name": "n8n automation", "is_active": True,
            "created_at": "2026-10-01T08:00:00Z", "last_used_at": "2026-10-06T07:45:10Z",
            "email": "svc-n8n-automation@internal.theeinsurance.local"}])],
        tags=[Tag.STAFF],
    )
    def get(self, request):
        creds = ServiceAccountCredential.objects.select_related("staff").order_by("-created_at")
        return Response([{"client_id": c.client_id, "name": c.name, "is_active": c.is_active,
                          "created_at": c.created_at, "last_used_at": c.last_used_at, "email": c.staff.email}
                         for c in creds])


class StaffServiceAccountRevokeView(APIView):
    permission_classes = [IsSuperAdmin]

    @extend_schema(
        summary="Revoke service account",
        parameters=[OpenApiParameter("client_id", OpenApiTypes.STR, OpenApiParameter.PATH)],
        request=None,
        responses={200: DetailSerializer, 404: error("Unknown client_id.", "Service account not found.", key="detail")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"detail": "Service account 'n8n automation' revoked."})],
        tags=[Tag.STAFF],
    )
    def post(self, request, client_id):
        try:
            cred = ServiceAccountCredential.objects.select_related("staff").get(client_id=client_id)
        except ServiceAccountCredential.DoesNotExist:
            return Response({"detail": "Service account not found."}, status=status.HTTP_404_NOT_FOUND)
        cred.is_active = False
        cred.save(update_fields=["is_active"])
        cred.staff.is_active = False
        cred.staff.save(update_fields=["is_active"])
        return Response({"detail": f"Service account '{cred.name}' revoked."})


class ServiceAccountTokenView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [IPRateThrottle]
    SERVICE_ACCOUNT_TOKEN_LIFETIME = timedelta(days=10)

    @extend_schema(
        summary="Service account token exchange (client credentials)",
        auth=[],
        request=inline_serializer("ServiceTokenRequest", {"client_id": s.CharField(), "client_secret": s.CharField()}),
        responses={200: inline_serializer("ServiceTokenResponse", {"access": s.CharField(), "refresh": s.CharField(), "expires_in": s.IntegerField()}),
                   400: error("Missing credentials.", "client_id and client_secret are required."),
                   401: error("Bad credentials or disabled account.", "Invalid credentials.")},
        examples=[OpenApiExample("Request", request_only=True, value={"client_id": "Xk2m9QvT7nB4cR1sW6yU3hJ8aL5dF0gE", "client_secret": "r4N8v1Zq6Pw3Tj9Ls2Kc7Hb5Xa0Mf-d"}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={**SAMPLE_TOKENS, "expires_in": 864000})],
        tags=[Tag.STAFF],
    )
    def post(self, request):
        client_id, client_secret = request.data.get("client_id"), request.data.get("client_secret")
        if not client_id or not client_secret:
            return Response({"error": "client_id and client_secret are required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            cred = ServiceAccountCredential.objects.select_related("staff").get(client_id=client_id, is_active=True)
        except ServiceAccountCredential.DoesNotExist:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)
        if not cred.check_secret(client_secret) or not cred.staff.is_active:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        cred.last_used_at = timezone.now()
        cred.save(update_fields=["last_used_at"])

        lifetime = self.SERVICE_ACCOUNT_TOKEN_LIFETIME
        refresh = jwt_tokens.RefreshToken.for_user(cred.staff)
        refresh["role"], refresh["service_account"] = cred.staff.role, True
        refresh.set_exp(lifetime=lifetime)
        access = refresh.access_token
        access["role"], access["service_account"] = cred.staff.role, True
        access.set_exp(lifetime=lifetime)
        return Response({"access": str(access), "refresh": str(refresh), "expires_in": int(lifetime.total_seconds())})


# ================================================================ 10. ACCESS GRANT
class StaffDistributorAccessView(APIView):
    permission_classes = [IsHumanStaff]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List access grants",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=["pending", "approved", "rejected", "revoked"])],
        responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Grants.", examples=[OpenApiExample("OK", value={
            "access_grants": [{"id": "5e0d3c7a-2b1f-4a9e-8c6d-7f1e2d3c4b5a", "distributor": "QuickCover Ltd",
                               "provider": "Sunrise Assurance Plc", "scope": "plan", "plan": "Motor Comprehensive",
                               "status": "pending", "requested_at": "2026-10-06T09:00:00Z"}]})])},
        tags=[Tag.ACCESS_GRANT],
    )
    def get(self, request):
        from plans.models import DistributorAccessGrant
        grants = (DistributorAccessGrant.objects.filter(status=request.query_params.get("status", "pending"))
                  .select_related("distributor", "provider", "plan"))
        return Response({"access_grants": [{
            "id": str(g.id), "distributor": g.distributor.name, "provider": g.provider.name, "scope": g.scope,
            "plan": g.plan.name if g.plan_id else None, "status": g.status, "requested_at": g.requested_at} for g in grants]})

    @extend_schema(
        summary="Revoke all approved access for a distributor↔provider pair",
        request=inline_serializer("RevokeAccessRequest", {"distributor_id": s.UUIDField(), "provider_id": s.UUIDField()}),
        responses={200: MessageSerializer, 404: error("Nothing to revoke.", "No approved access found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"message": "Access revoked."})],
        tags=[Tag.ACCESS_GRANT],
    )
    def delete(self, request):
        from plans.models import DistributorAccessGrant
        grants = DistributorAccessGrant.objects.filter(
            distributor_id=request.data.get("distributor_id"), provider_id=request.data.get("provider_id"), status="approved")
        if not grants.exists():
            return Response({"error": "No approved access found."}, status=404)
        for g in grants:
            DistributorAccessGrant.objects.revoke(g, by=request.user)
        return Response({"message": "Access revoked."})


class StaffDistributorGrantReviewView(APIView):
    permission_classes = [IsHumanStaff]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Staff kill-switch: revoke an access grant",
        description="Approving/rejecting is the **provider's** decision (`PATCH /partner/access-requests/{id}/`). "
                    "Staff may only revoke an **approved** grant (e.g. fraud or compliance).",
        parameters=[OpenApiParameter("grant_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        request=inline_serializer("StaffRevokeRequest", {"action": s.ChoiceField(["revoke"]), "note": s.CharField(required=False)}),
        responses={200: inline_serializer("StaffGrantRevoked", {"message": s.CharField(), "id": s.UUIDField()}),
                   400: error("Bad action or grant not approved.", "Staff can only revoke grants."),
                   404: error("Not found.", "Grant not found.")},
        examples=[OpenApiExample("Request", request_only=True, value={"action": "revoke", "note": "Distributor suspended for compliance."}),
                  OpenApiExample("OK", response_only=True, status_codes=["200"], value={"message": "Grant revoked.", "id": "5e0d3c7a-2b1f-4a9e-8c6d-7f1e2d3c4b5a"})],
        tags=[Tag.ACCESS_GRANT],
    )
    def patch(self, request, grant_id):
        from plans.models import DistributorAccessGrant
        if request.data.get("action") != "revoke":
            return Response({"error": "Staff can only revoke grants."}, status=400)
        grant = DistributorAccessGrant.objects.filter(id=grant_id).first()
        if not grant:
            return Response({"error": "Grant not found."}, status=404)
        try:
            DistributorAccessGrant.objects.revoke(grant, by=request.user, note=request.data.get("note", ""))
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=400)
        return Response({"message": f"Grant {grant.status}.", "id": str(grant.id)})
