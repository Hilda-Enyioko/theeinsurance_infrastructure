from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.contrib.auth import authenticate

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