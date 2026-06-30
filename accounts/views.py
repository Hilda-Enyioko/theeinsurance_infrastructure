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
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.contrib.auth import authenticate
from django.utils import timezone

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

    def post(self, request):
        """
        Accepts registration parameters, spins up an inactive Partner profile, 
        and issues an API key for subsequent configuration steps
        """

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

    def post(self, request):
        """
        Allows verified corporate administrators to upload operational documentation 
        for administrative review, or pull down their current submission state.
        """

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

    def get(self, request):
        """
        Allow only authenticated partners retrieve KYC dicuments they own.
        """

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

    def post(self, request):
        """
        Uses tenant context attached to the request (via upstream middleware parsing)
        to bind the newly registered customer account to the parent organization.
        """

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

    def post(self, request):
        """
        Allows registered system consumers to submit required identification metrics.
        """

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

    def get(self, request):
        """
        Inspect Customer's background check records inside the scope of the current partner
        """

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

    def post(self, request):
        """
        Verifies credentials and returns signed authentication payloads appended 
        with role authorizations and multi-tenant scoping claims.
        """

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


# Token Refresh

class TokenRefreshView(APIView):
    """
    IPRateThrottle: token refresh can also be abused for token farming.
    """
    permission_classes = [AllowAny]
    throttle_classes = [IPRateThrottle]

    def post(self, request):
        """
        Validates submitted refresh states to hand out fresh access hashes, 
        shielding downstream views from re-authentication requirements.
        """

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

    def post(self, request):
        """
        Enforces strict structural checks to confirm the user possesses 
        elevated global administrative roles (`super_admin`) before dispatching tokens.
        """
        
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

    def get(self, request, partner_id=None):
        """
        Allows internal staff members to query incoming organizational applications.
        Handles both full lists and singular lookups based on partner_id.
        """
        # Handle individual detail lookup
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

        # Handle List lookup (Fallback when partner_id is None)
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

    def patch(self, request, partner_id):
        """
        Issue state confirmations (approve/reject), 
        record notes, and toggle partner operational states.
        """
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

    def post(self, request):
        """
        Handles creation, enabling of secure access lines 
        between downstream insurance "distributors" and core policy "providers".
        """

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

    def delete(self, request):
        """
        Disabling of secure access lines 
        between downstream insurance "distributors" and core policy "providers"
        """

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

    The raw client_secret is returned ONLY in this response. It is never
    stored in plaintext and cannot be retrieved again — if it's lost, the
    credential must be revoked and a new one issued.
    """
    permission_classes = [IsSuperAdmin]

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

class ServiceAccountTokenView(APIView):
    """
    POST /auth/service-account/token/

    Client-credentials style token exchange for service accounts (n8n,
    schedulers). No user JWT involved — caller authenticates with a
    client_id/client_secret pair issued once, out-of-band, by staff.

    Request body:
        client_id (str)
        client_secret (str)

    Response:
        200: { access, refresh, expires_in }
        401: Invalid credentials
    """
    permission_classes = []
    authentication_classes = []

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
        access = refresh.access_token

        return Response({
            "access": str(access),
            "refresh": str(refresh),
            "expires_in": int(access.lifetime.total_seconds()),
        }, status=200)
