from django.urls import path
from .views import (
    CustomerSubscriptionListView,
    CustomerSubscriptionCreateView,
    CustomerSubscriptionDetailView,
    PaymentVerificationView,
    PartnerSubscriptionListView,
)

urlpatterns = [
    # customer
    path("subscriptions/", CustomerSubscriptionListView.as_view(), name="subscription-list"),
    path("subscriptions/create/", CustomerSubscriptionCreateView.as_view(), name="subscription-create"),
    path("subscriptions/<uuid:subscription_id>/", CustomerSubscriptionDetailView.as_view(), name="subscription-detail"),
    path("subscriptions/<uuid:subscription_id>/verify-payment/", PaymentVerificationView.as_view(), name="payment-verify"),

    # partner admin
    path("partner/subscriptions/", PartnerSubscriptionListView.as_view(), name="partner-subscription-list"),
]