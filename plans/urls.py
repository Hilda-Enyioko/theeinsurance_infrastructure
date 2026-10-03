from django.urls import path
from .views import (
    InsuranceCategoryListView,
    InsurancePlanListView,
    InsurancePlanDetailView,
    ProviderPlanListCreateView,
    ProviderPlanDetailView,
    ProviderAccessRequestListView,
    DistributorAccessGrantView,
    DistributorAccessGrantWithdrawView,
    PlanRecommendationView,
    PlanContextView,
)

urlpatterns = [
    # customer / distributor facing
    path("categories/", InsuranceCategoryListView.as_view(), name="category-list"),
    path("plans/", InsurancePlanListView.as_view(), name="plan-list"),
    path("plans/<uuid:plan_id>/", InsurancePlanDetailView.as_view(), name="plan-detail"),
    path("plans/recommend/", PlanRecommendationView.as_view(), name="plan-recommend"),
    path("plans/context/", PlanContextView.as_view(), name="plan-context"),

    # provider team
    path("partner/plans/", ProviderPlanListCreateView.as_view(), name="provider-plan-list-create"),
    path("partner/plans/<uuid:plan_id>/", ProviderPlanDetailView.as_view(), name="provider-plan-detail"),
    path("partner/access-requests/", ProviderAccessRequestListView.as_view(), name="provider-access-requests"),

    # distributor team
    path("partner/access-grants/", DistributorAccessGrantView.as_view(), name="distributor-access-grants"),
    path("partner/access-grants/<uuid:grant_id>/withdraw/", DistributorAccessGrantWithdrawView.as_view(), name="distributor-access-grant-withdraw"),
]
