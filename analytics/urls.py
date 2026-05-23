from django.urls import path
from .views import (
    ProviderSummaryView,
    ProviderSubscriptionsTrendsView,
    DistributorSummaryView,
    DistributorSubscriptionsTrendsView
)

urlpatterns = [
    # provider
    path("analytics/provider/summary/", ProviderSummaryView.as_view(), name="provider-summary"),
    path("analytics/provider/subscriptions-trends/", ProviderSubscriptionsTrendsView.as_view(), name="provider-subscriptions-trends"),

    # distributor
    path("analytics/distributor/summary/", DistributorSummaryView.as_view(), name="distributor-summary"),
    path("analytics/distributor/subscriptions-trends/", DistributorSubscriptionsTrendsView.as_view(), name="distributor-subscriptions-trends"),
]