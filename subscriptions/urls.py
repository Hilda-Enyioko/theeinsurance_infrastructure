from django.urls import path
from .views import (
    CustomerSubscriptionListView, CustomerSubscriptionCreateView, CustomerSubscriptionDetailView,
    SubscriptionDocumentUploadView, SubscriptionPayView, SubscriptionRenewView,
    PartnerSubscriptionListView, StaffSubscriptionListView, StaffSubscriptionDetailView,
)

urlpatterns = [
    # customer
    path("subscriptions/", CustomerSubscriptionListView.as_view(), name="subscription-list"),
    path("subscriptions/create/", CustomerSubscriptionCreateView.as_view(), name="subscription-create"),
    path("subscriptions/<uuid:subscription_id>/", CustomerSubscriptionDetailView.as_view(), name="subscription-detail"),
    path("subscriptions/<uuid:subscription_id>/documents/", SubscriptionDocumentUploadView.as_view(), name="subscription-documents"),
    path("subscriptions/<uuid:subscription_id>/pay/", SubscriptionPayView.as_view(), name="subscription-pay"),
    path("subscriptions/<uuid:subscription_id>/renew/", SubscriptionRenewView.as_view(), name="subscription-renew"),

    # partner admin (provider + distributor)
    path("partner/subscriptions/", PartnerSubscriptionListView.as_view(), name="partner-subscription-list"),

    # staff
    path("staff/subscriptions/", StaffSubscriptionListView.as_view(), name="staff-subscription-list"),
    path("staff/subscriptions/<uuid:subscription_id>/", StaffSubscriptionDetailView.as_view(), name="staff-subscription-detail"),
]
