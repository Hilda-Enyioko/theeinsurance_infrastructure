from drf_spectacular.utils import OpenApiExample, inline_serializer
from rest_framework import serializers as s
from core.docs import TokenPairSerializer, SAMPLE_TOKENS

PARTNER_ID = "3f6c1f4e-8a58-4b6e-9a53-2d6a7f0d9c11"

OnboardResponse = inline_serializer("PartnerOnboardResponse", {
    "message": s.CharField(), "partner_id": s.UUIDField(), "partner_type": s.CharField(),
    "is_active": s.BooleanField(),
    "api_key": s.CharField(help_text="X-Partner-Key. Shown ONCE. Inactive until KYC is approved."),
    "tokens": TokenPairSerializer,
    "next_step": s.CharField(),
})

LoginResponse = inline_serializer("LoginResponse", {
    "tokens": TokenPairSerializer, "role": s.CharField(), "email": s.EmailField(),
    "first_name": s.CharField(),
    "kyc_status": s.CharField(required=False, help_text="Customers only: not_submitted | pending | approved | rejected"),
})

ApiKeyResponse = inline_serializer("ApiKeyResponse", {"api_key": s.CharField(), "warning": s.CharField()})

KYCReviewResponse = inline_serializer("KYCReviewResponse", {
    "message": s.CharField(), "partner": s.CharField(), "is_active": s.BooleanField()})

ServiceAccountCreated = inline_serializer("ServiceAccountCreated", {
    "client_id": s.CharField(), "client_secret": s.CharField(), "name": s.CharField(), "warning": s.CharField()})

ONBOARD_REQ_EXAMPLES = [
    OpenApiExample("Distributor", request_only=True, value={
        "name": "QuickCover Ltd", "partner_type": "distributor", "commission_rate": "12.50",
        "first_name": "Ada", "last_name": "Okafor", "email": "ada@quickcover.ng",
        "phone_number": "+2348012345678", "address": "12 Allen Ave, Ikeja, Lagos",
        "password": "S3curePassw0rd!"}),
    OpenApiExample("Provider (no commission_rate)", request_only=True, value={
        "name": "Sunrise Assurance Plc", "partner_type": "provider",
        "first_name": "Tunde", "last_name": "Bello", "email": "tunde@sunriseassurance.ng",
        "phone_number": "+2348098765432", "address": "5 Marina, Lagos Island", "password": "S3curePassw0rd!"}),
]

ONBOARD_201 = OpenApiExample("Created", response_only=True, status_codes=["201"], value={
    "message": "Partner account created. Submit KYC to activate your X-Partner-Key.",
    "partner_id": PARTNER_ID, "partner_type": "distributor", "is_active": False,
    "api_key": "q7Zk3mV1Qy0n8sLw2xT5aB9cD4eF6gH-uJ1kL3mN5oP",
    "tokens": SAMPLE_TOKENS, "next_step": "POST /api/partner/kyc/"})

PROFILE_EXAMPLE = {
    "id": PARTNER_ID, "name": "QuickCover Ltd", "slug": "quickcover-ltd", "partner_type": "distributor",
    "is_active": True, "phone_number": "+2348012345678", "address": "12 Allen Ave, Ikeja, Lagos",
    "website": "https://quickcover.ng", "commission_rate": "12.50",
    "settlement": {"account_name": "QuickCover Ltd", "account_number": "0123456789", "bank_code": "058"},
    "kyc_status": "approved"}

LOGIN_EXAMPLES = [
    OpenApiExample("Partner admin", response_only=True, status_codes=["200"], value={
        "tokens": SAMPLE_TOKENS, "role": "partner_admin", "email": "ada@quickcover.ng", "first_name": "Ada"}),
    OpenApiExample("Customer", response_only=True, status_codes=["200"], value={
        "tokens": SAMPLE_TOKENS, "role": "customer", "email": "chidi@example.com",
        "first_name": "Chidi", "kyc_status": "pending"}),
]
