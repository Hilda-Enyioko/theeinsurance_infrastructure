from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.contrib.auth import authenticate
from django.utils import timezone
from accounts.permissions import IsSuperAdmin
from .models import PartnerKYC, CustomerKYC
from core.models import Partner
from webhooks.views import dispatch_webhook

from .serializers import (
    CustomerRegistrationSerializer,
    PartnerOnboardingSerializer,
    LoginSerializer,
    CustomerKYCSerializer,
    PartnerKYCSerializer,
)
from .models import CustomerProfile, PartnerKYC, CustomerKYC
from core.models import Partner


# Helper functions
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


def generate_tokens(user, partner_id=None):
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
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PartnerOnboardingSerializer(data=request.data)
        if serializer.is_valid():
            partner, user = serializer.save()
            return Response({
                "message": "Partner account created. Submit KYC to activate your account.",
                "partner_id": str(partner.id),
                "api_key": partner.api_key,    # shown once only
            }, status=201)
        return Response(serializer.errors, status=400)


# Partner KYC
class PartnerKYCView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        if hasattr(partner, 'kyc'):
            return Response({"error": "KYC already submitted."}, status=400)

        serializer = PartnerKYCSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(partner=partner)
            return Response({
                "message": "KYC submitted. Your account will be reviewed shortly."
            }, status=201)
        
        dispatch_webhook(
            partner,
            "kyc.submitted",
            {
                "partner_id": str(partner.id),
                "partner_name": partner.name,
                "partner_type": partner.partner_type,
            }
        )

        
        return Response(serializer.errors, status=400)

    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        if not hasattr(partner, 'kyc'):
            return Response({"error": "No KYC submitted yet."}, status=404)

        serializer = PartnerKYCSerializer(partner.kyc)
        return Response(serializer.data)


# Customer Registration 

class CustomerRegisterView(APIView):
    permission_classes = [AllowAny]

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
    permission_classes = [IsAuthenticated]

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
            return Response({
                "message": "KYC submitted successfully."
            }, status=201)
        return Response(serializer.errors, status=400)

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
    permission_classes = [AllowAny]

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


# Token Refresh
class TokenRefreshView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token is required."}, status=400)

        try:
            refresh = RefreshToken(refresh_token)
            return Response({
                "access": str(refresh.access_token)
            })
        except TokenError as e:
            return Response({"error": str(e)}, status=401)

# TheeInsurance Staff Only Login View
class StaffLoginView(APIView):
    permission_classes = [AllowAny]
    
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
            return Response({'error': 'Invalid email or password'}, status=401)
        
        if not user.is_active:
            return Response({'error': 'Account is inactive'}, status=403)
        
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
    permission_classes = [IsSuperAdmin]
    
    def get(self, request):
        """List all pending partner KYC submissions."""
        status_filter = request.query_params.set("status", "pending")
        kyc_list = PartnerKYC.objects.filter(status=status_filter)
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
        """Approve or reject a partner KYC"""
        try:
            kyc = PartnerKYC.objects.get(partner__id=partner_id)
        except PartnerKYC.DoesNotExist:
            return Response(
                {'error': 'KYC not found'},
                status=404
            )
        
        action = request.data.get('action')
        note = request.data.get('note', '')
        
        if action not in ['approve', 'reject']:
            return Response(
                {'error': "Action must be approve or reject."},
                status=400
            )
        
        kyc.status = 'approved' if action == 'approve' else 'rejected'
        kyc.review_note = note
        kyc.reviewed_by = request.user.email
        kyc.reviewed_at = timezone.now()
        
        # activate partner on approval
        if action == 'approve':
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


class StaffDistributorAccessView(APIView):
    """Grant or revoke distributor access to a provider."""
    permission_classes = [IsSuperAdmin]

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