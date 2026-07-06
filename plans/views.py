from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from drf_spectacular.types import OpenApiTypes

from .models import InsuranceCategory, InsurancePlan, DistributorProviderAccess
from .serializers import (
    InsuranceCategorySerializer,
    InsurancePlanSerializer,
    InsurancePlanCreateSerializer,
    DistributorProviderAccessSerializer,
)
from accounts.permissions import IsProviderAdmin, IsDistributorAdmin, IsServiceAccount
from core.throttles import PartnerRateThrottle


# Helpers
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Categories
class InsuranceCategoryListView(APIView):
    """
    Public. Returns all active insurance categories.
    Protected at middleware level via X-Partner-Key.
    """
    permission_classes = [AllowAny]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List active insurance categories",
        responses={200: OpenApiResponse(description="A list of active categories.")},
        tags=["Insurance Categories"]
    )
    def get(self, request):
        categories = InsuranceCategory.objects.filter(is_active=True)
        serializer = InsuranceCategorySerializer(categories, many=True)
        return Response({"categories": serializer.data})


# Plans — Customer / Public Facing
class InsurancePlanListView(APIView):
    """
    Public endpoint — customers browse available plans.
    Scoped to the requesting partner via X-Partner-Key middleware:
    - Provider partner: shows their own active plans only.
    - Distributor partner: shows plans from providers they have access to.
    """
    permission_classes = [AllowAny]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List and filter active insurance plans",
        parameters=[
            OpenApiParameter(name="category", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter by category name"),
            OpenApiParameter(name="coverage_level", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter by coverage level"),
            OpenApiParameter(name="min_premium", type=OpenApiTypes.DECIMAL, location=OpenApiParameter.QUERY, description="Minimum premium price"),
            OpenApiParameter(name="max_premium", type=OpenApiTypes.DECIMAL, location=OpenApiParameter.QUERY, description="Maximum premium price"),
            OpenApiParameter(name="search", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Search term for name or description"),
            OpenApiParameter(name="sort_by", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, default="-created_at", description="Sort fields: premium, -premium, created_at, -created_at, name"),
        ],
        responses={200: OpenApiResponse(description="Filtered list of insurance plans.")},
        tags=["Insurance Plans"]
    )
    def get(self, request):
        partner = request.partner

        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(provider=partner, is_active=True)
        else:
            accessible_providers = DistributorProviderAccess.objects.filter(
                distributor=partner, is_active=True
            ).values_list("provider_id", flat=True)
            plans = InsurancePlan.objects.filter(
                provider__in=accessible_providers, is_active=True
            )

        category = request.query_params.get("category")
        coverage_level = request.query_params.get("coverage_level")
        min_premium = request.query_params.get("min_premium")
        max_premium = request.query_params.get("max_premium")
        search = request.query_params.get("search")

        if category:
            plans = plans.filter(category__name=category)
        if coverage_level:
            plans = plans.filter(coverage_level=coverage_level)
        if min_premium:
            plans = plans.filter(premium__gte=min_premium)
        if max_premium:
            plans = plans.filter(premium__lte=max_premium)
        if search:
            plans = plans.filter(
                Q(name__icontains=search) | Q(description__icontains=search)
            )

        sort_by = request.query_params.get("sort_by", "-created_at")
        allowed_sorts = ["premium", "-premium", "created_at", "-created_at", "name"]
        if sort_by in allowed_sorts:
            plans = plans.order_by(sort_by)

        serializer = InsurancePlanSerializer(plans, many=True)
        return Response({"plans": serializer.data})


class InsurancePlanDetailView(APIView):
    """
    Public endpoint — single plan detail.
    Distributor access check is enforced at queryset level.
    """
    permission_classes = [AllowAny]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="plans_retrieve_by_id",
        summary="Retrieve an active plan detail",
        responses={
            200: InsurancePlanSerializer,
            404: OpenApiResponse(description="Plan not found or access denied.")
        },
        tags=["Insurance Plans"]
    )
    def get(self, request, plan_id):
        partner = request.partner

        try:
            plan = InsurancePlan.objects.get(id=plan_id, is_active=True)
        except InsurancePlan.DoesNotExist:
            return Response({"error": "Plan not found."}, status=404)

        if partner.partner_type == "distributor":
            has_access = DistributorProviderAccess.objects.filter(
                distributor=partner,
                provider=plan.provider,
                is_active=True,
            ).exists()
            if not has_access:
                return Response({"error": "Plan not found."}, status=404)

        serializer = InsurancePlanSerializer(plan)
        return Response(serializer.data)


# Plans — Provider Admin
class ProviderPlanListCreateView(APIView):
    """
    Provider admins manage their own plans.
    IsProviderAdmin enforces: authenticated + partner_admin + provider type.
    No inline role checks needed.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Provider Admin: List managed plans",
        parameters=[
            OpenApiParameter(name="status", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter by status: 'active' or 'inactive'"),
        ],
        responses={200: OpenApiResponse(description="List of insurance plans for the authenticated provider.")}
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        plans = InsurancePlan.objects.filter(provider=partner)

        status_filter = request.query_params.get("status")
        if status_filter == "active":
            plans = plans.filter(is_active=True)
        elif status_filter == "inactive":
            plans = plans.filter(is_active=False)

        serializer = InsurancePlanSerializer(plans, many=True)
        return Response({"plans": serializer.data})

    @extend_schema(
        summary="Provider Admin: Create a new plan",
        request=InsurancePlanCreateSerializer,
        responses={
            201: InsurancePlanSerializer,
            400: OpenApiResponse(description="Validation error data.")
        },
        tags=["Insurance Plans"]
    )
    def post(self, request):
        partner = get_partner_from_user(request.user)
        serializer = InsurancePlanCreateSerializer(
            data=request.data,
            context={"provider": partner}
        )
        if serializer.is_valid():
            plan = serializer.save()
            return Response(InsurancePlanSerializer(plan).data, status=201)
        return Response(serializer.errors, status=400)


class ProviderPlanDetailView(APIView):
    """
    Provider admins retrieve, update, or soft-delete their own plans.
    IsProviderAdmin enforces role at the class level.
    Object ownership is enforced via provider=partner filter in get_object().
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    def get_object(self, plan_id, partner):
        try:
            return InsurancePlan.objects.get(id=plan_id, provider=partner)
        except InsurancePlan.DoesNotExist:
            return None

    @extend_schema(
        operation_id="partner_plans_retrieve_by_id",
        summary="Provider Admin: Retrieve a managed plan details",
        responses={
            200: InsurancePlanSerializer,
            404: OpenApiResponse(description="Plan not found.")
        },
        tags=["Insurance Plans"]
    )
    def get(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)
        return Response(InsurancePlanSerializer(plan).data)

    @extend_schema(
        summary="Provider Admin: Partially update a plan",
        request=InsurancePlanCreateSerializer,
        responses={
            200: InsurancePlanSerializer,
            400: OpenApiResponse(description="Validation error data."),
            404: OpenApiResponse(description="Plan not found.")
        },
        tags=["Insurance Plans"]
    )
    def patch(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)

        serializer = InsurancePlanCreateSerializer(
            plan, data=request.data, partial=True,
            context={"provider": partner}
        )
        if serializer.is_valid():
            plan = serializer.save()
            return Response(InsurancePlanSerializer(plan).data)
        return Response(serializer.errors, status=400)

    @extend_schema(
        summary="Provider Admin: Soft-delete a plan",
        responses={
            200: OpenApiResponse(description="Plan deactivated successfully."),
            404: OpenApiResponse(description="Plan not found.")
        },
        tags=["Insurance Plans"]
    )
    def delete(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)

        # Soft delete — deactivate rather than destroy data.
        plan.is_active = False
        plan.save()
        return Response({"message": "Plan deactivated successfully."})


# Distributor — Provider Access
class DistributorProviderAccessView(APIView):
    """
    Distributors view the list of providers they have been granted access to.
    IsDistributorAdmin enforces: authenticated + partner_admin + distributor type.
    """
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Distributor Admin: View accessible providers",
        responses={200: OpenApiResponse(description="List of allowed provider accesses.")},
        tags=["Insurance Plans: Distributor Access"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        access = DistributorProviderAccess.objects.filter(distributor=partner)
        serializer = DistributorProviderAccessSerializer(access, many=True)
        return Response({"providers": serializer.data})


# AI / Automation — Internal Service Endpoints
class PlanRecommendationView(APIView):
    """
    AI Plan Recommendation Engine.
    Called by n8n — requires a valid JWT from a dedicated service account.
    Returns filtered and ranked plans based on query params for AI to reason over.
    """
    permission_classes = [IsServiceAccount]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="AI Engine: Get plan recommendations",
        parameters=[
            OpenApiParameter(name="category", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Filter recommendations by category"),
            OpenApiParameter(name="budget", type=OpenApiTypes.DECIMAL, location=OpenApiParameter.QUERY, description="Maximum premium budget allowed"),
            OpenApiParameter(name="coverage_level", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, description="Target coverage level"),
        ],
        responses={200: OpenApiResponse(description="Ranked and filtered recommended plans with context information.")},
        tags=["Insurance Plans: AI Recommendations"]
    )
    def get(self, request):
        partner = request.partner
        category = request.query_params.get('category')
        budget = request.query_params.get('budget')
        coverage_level = request.query_params.get('coverage_level')

        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(provider=partner, is_active=True)
        else:
            accessible_providers = DistributorProviderAccess.objects.filter(
                distributor=partner, is_active=True
            ).values_list("provider_id", flat=True)
            plans = InsurancePlan.objects.filter(
                provider__in=accessible_providers, is_active=True
            )

        if category:
            plans = plans.filter(category__name=category)
        if coverage_level:
            plans = plans.filter(coverage_level=coverage_level)
        if budget:
            plans = plans.filter(premium__lte=budget)

        plans = plans.order_by("premium")

        serializer = InsurancePlanSerializer(plans, many=True)
        return Response({
            "recommended_plans": serializer.data,
            "filters_applied": {
                "category": category,
                "budget": budget,
                "coverage_level": coverage_level,
            }
        })


class PlanContextView(APIView):
    """
    Conversational Insurance Assistant context feed.
    Called by n8n — requires a valid JWT from a dedicated service account.
    Provides structured plan data injected into Claude system prompt.
    """
    permission_classes = [IsServiceAccount]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="AI Engine: Fetch structured system prompt context",
        responses={200: OpenApiResponse(description="Flattened, highly compressed plan parameters tailored for LLM consumption.")},
        tags=["Insurance Plans: AI Recommendations"]
    )
    def get(self, request):
        partner = request.partner

        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(provider=partner, is_active=True)
        else:
            accessible_providers = DistributorProviderAccess.objects.filter(
                distributor=partner, is_active=True
            ).values_list("provider_id", flat=True)
            plans = InsurancePlan.objects.filter(
                provider__in=accessible_providers, is_active=True
            )

        context = []
        for plan in plans:
            context.append({
                "id": str(plan.id),
                "name": plan.name,
                "provider": plan.provider.name,
                "category": plan.category.get_name_display(),
                "coverage_level": plan.get_coverage_level_display(),
                "coverage_amount": str(plan.coverage_amount),
                "premium": str(plan.premium),
                "duration_months": plan.duration_months,
                "description": plan.description,
            })

        return Response({
            "partner": partner.name,
            "total_plans": len(context),
            "plans": context,
        })
