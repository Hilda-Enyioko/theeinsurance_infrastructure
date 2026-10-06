from django.urls import path
from .views import (
    InsuranceCategoryListView, InsurancePlanListView, InsurancePlanDetailView,
    ProviderPlanListCreateView, ProviderPlanDetailView,
    ProviderAccessRequestListView, ProviderAccessRequestReviewView,
    DistributorMarketplaceProvidersView, DistributorMarketplacePlansView,
    DistributorAccessGrantView, DistributorAccessGrantWithdrawView,
    PlanRecommendationView, PlanContextView,
)

urlpatterns = [
    # customer storefront
    path("categories/", InsuranceCategoryListView.as_view(), name="category-list"),
    path("plans/", InsurancePlanListView.as_view(), name="plan-list"),
    path("plans/recommend/", PlanRecommendationView.as_view(), name="plan-recommend"),
    path("plans/context/", PlanContextView.as_view(), name="plan-context"),
    path("plans/<uuid:plan_id>/", InsurancePlanDetailView.as_view(), name="plan-detail"),

    # provider portal
    path("partner/plans/", ProviderPlanListCreateView.as_view(), name="provider-plan-list-create"),
    path("partner/plans/<uuid:plan_id>/", ProviderPlanDetailView.as_view(), name="provider-plan-detail"),
    path("partner/access-requests/", ProviderAccessRequestListView.as_view(), name="provider-access-requests"),
    path("partner/access-requests/<uuid:grant_id>/", ProviderAccessRequestReviewView.as_view(), name="provider-access-request-review"),

    # distributor portal
    path("distributor/marketplace/providers/", DistributorMarketplaceProvidersView.as_view(), name="distributor-marketplace-providers"),
    path("distributor/marketplace/plans/", DistributorMarketplacePlansView.as_view(), name="distributor-marketplace-plans"),
    path("partner/access-grants/", DistributorAccessGrantView.as_view(), name="distributor-access-grants"),
    path("partner/access-grants/<uuid:grant_id>/withdraw/", DistributorAccessGrantWithdrawView.as_view(), name="distributor-access-grant-withdraw"),
]
