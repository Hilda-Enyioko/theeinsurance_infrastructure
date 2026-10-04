from django.urls import path
from .views import (
    PartnerOnboardingView, PartnerKYCView, PartnerMeView,
    PartnerAPIKeyRegenerateView, CustomerRegisterView, CustomerKYCView,
    LoginView, TokenRefreshView, StaffLoginView, StaffAccountView,
    StaffAccountDeactivateView, StaffPartnerKYCReviewView,
    StaffDistributorAccessView, StaffDistributorGrantReviewView,
    StaffServiceAccountCreateView, StaffServiceAccountListView,
    StaffServiceAccountRevokeView, ServiceAccountTokenView,
)

urlpatterns = [
    path("partner/onboard/", PartnerOnboardingView.as_view(), name="partner-onboard"),
    path("partner/kyc/", PartnerKYCView.as_view(), name="partner-kyc"),
    path("partner/me/", PartnerMeView.as_view(), name="partner-me"),
    path("partner/api-key/regenerate/", PartnerAPIKeyRegenerateView.as_view(), name="partner-api-key-regenerate"),

    path("auth/register/", CustomerRegisterView.as_view(), name="customer-register"),
    path("auth/kyc/", CustomerKYCView.as_view(), name="customer-kyc"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),

    path("staff/auth/login/", StaffLoginView.as_view(), name="staff-login"),
    path("staff/accounts/", StaffAccountView.as_view(), name="staff-account-list-create"),
    path("staff/accounts/<uuid:staff_id>/deactivate/", StaffAccountDeactivateView.as_view(), name="staff-account-deactivate"),
    path("staff/kyc/partners/", StaffPartnerKYCReviewView.as_view(), name="staff-partner-kyc-list"),
    path("staff/kyc/partners/<uuid:partner_id>/", StaffPartnerKYCReviewView.as_view(), name="staff-partner-kyc-review"),
    path("staff/distributor-access/", StaffDistributorAccessView.as_view(), name="staff-distributor-access"),
    path("staff/distributor-access/<uuid:grant_id>/", StaffDistributorGrantReviewView.as_view(), name="staff-distributor-grant-review"),
    path("staff/service-accounts/", StaffServiceAccountListView.as_view(), name="staff-service-account-list"),
    path("staff/service-accounts/create/", StaffServiceAccountCreateView.as_view(), name="staff-service-account-create"),
    path("staff/service-accounts/<str:client_id>/revoke/", StaffServiceAccountRevokeView.as_view(), name="staff-service-account-revoke"),

    path("auth/service-account/token/", ServiceAccountTokenView.as_view(), name="service-account-token"),
]
