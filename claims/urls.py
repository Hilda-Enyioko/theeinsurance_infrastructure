from django.urls import path
from .views import (
    CustomerClaimListCreateView,
    CustomerClaimDetailView,
    ClaimDocumentUploadView,
    ProviderClaimListView,
    ProviderClaimReviewView,
    StaffClaimListView,
    StaffClaimReviewView,
)

urlpatterns = [
    # customer
    path("claims/", CustomerClaimListCreateView.as_view(), name="claim-list-create"),
    path("claims/<uuid:claim_id>/", CustomerClaimDetailView.as_view(), name="claim-detail"),
    path("claims/<uuid:claim_id>/documents/", ClaimDocumentUploadView.as_view(), name="claim-documents"),

    # provider
    path("partner/claims/", ProviderClaimListView.as_view(), name="provider-claim-list"),
    path("partner/claims/<uuid:claim_id>/", ProviderClaimReviewView.as_view(), name="provider-claim-review"),

    # theeinsurance staff
    path("staff/claims/", StaffClaimListView.as_view(), name="staff-claim-list"),
    path("staff/claims/<uuid:claim_id>/", StaffClaimReviewView.as_view(), name="staff-claim-review"),
]