from django.urls import path
from .views import (
    PartnerOnboardingView,
    PartnerKYCView,
    PartnerMeView,
    PartnerAPIKeyRetrieveView,
    PartnerAPIKeyRegenerateView,
    CustomerRegisterView,
    CustomerKYCView,
    LoginView,
    TokenRefreshView,
    StaffLoginView,
    StaffPartnerKYCReviewView,
    StaffDistributorAccessView,
    StaffServiceAccountCreateView,
    StaffServiceAccountListView,
    StaffServiceAccountRevokeView,
    ServiceAccountTokenView,
)

urlpatterns = [
    # partner
    path("partner/onboard/", PartnerOnboardingView.as_view(), name="partner-onboard"),
    path("partner/kyc/", PartnerKYCView.as_view(), name="partner-kyc"),
    path('partner/me/', PartnerMeView.as_view(), name='partner-me'),
    path("partner/api-key/retrieve/", PartnerAPIKeyRetrieveView.as_view(), name="partner-api-key-retrieve"),
    path("partner/api-key/regenerate/", PartnerAPIKeyRegenerateView.as_view(), name="partner-api-key-regenerate"),

    # customer
    path("auth/register/", CustomerRegisterView.as_view(), name="customer-register"),
    path("auth/kyc/", CustomerKYCView.as_view(), name="customer-kyc"),

    # shared
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    
    # theeinsurance staff
    path("staff/auth/login/", StaffLoginView.as_view(), name="staff-login"),
    path("staff/kyc/partners/", StaffPartnerKYCReviewView.as_view(), name="staff-partner-kyc-list"),
    path("staff/kyc/partners/<uuid:partner_id>/", StaffPartnerKYCReviewView.as_view(), name="staff-partner-kyc-review"),
    path("staff/distributor-access/", StaffDistributorAccessView.as_view(), name="staff-distributor-access"),
    path("staff/service-accounts/", StaffServiceAccountListView.as_view(), name="staff-service-account-list"),
    path("staff/service-accounts/create/", StaffServiceAccountCreateView.as_view(), name="staff-service-account-create"),
    path("staff/service-accounts/<str:client_id>/revoke/", StaffServiceAccountRevokeView.as_view(), name="staff-service-account-revoke"),
    
    # service accounts
    path("auth/service-account/token/", ServiceAccountTokenView.as_view(), name="service-account-token"),
]