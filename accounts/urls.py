from django.urls import path
from .views import (
    PartnerOnboardingView,
    PartnerKYCView,
    CustomerRegisterView,
    CustomerKYCView,
    LoginView,
    TokenRefreshView,
)

urlpatterns = [
    # partner
    path("partner/onboard/", PartnerOnboardingView.as_view(), name="partner-onboard"),
    path("partner/kyc/", PartnerKYCView.as_view(), name="partner-kyc"),

    # customer
    path("auth/register/", CustomerRegisterView.as_view(), name="customer-register"),
    path("auth/kyc/", CustomerKYCView.as_view(), name="customer-kyc"),

    # shared
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
]