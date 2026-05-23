from django.urls import path
from .views import (
    InsuranceCategoryListView,
    InsurancePlanListView,
    InsurancePlanDetailView,
    ProviderPlanListCreateView,
    ProviderPlanDetailView,
    DistributorProviderAccessView,
)

urlpatterns = [
    # customer facing
    path("categories/", InsuranceCategoryListView.as_view(), name="category-list"),
    path("plans/", InsurancePlanListView.as_view(), name="plan-list"),
    path("plans/<uuid:plan_id>/", InsurancePlanDetailView.as_view(), name="plan-detail"),

    # provider admin
    path("partner/plans/", ProviderPlanListCreateView.as_view(), name="provider-plan-list-create"),
    path("partner/plans/<uuid:plan_id>/", ProviderPlanDetailView.as_view(), name="provider-plan-detail"),

    # distributor
    path("partner/providers/", DistributorProviderAccessView.as_view(), name="distributor-provider-access"),
]