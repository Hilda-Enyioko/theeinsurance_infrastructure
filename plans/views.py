from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from accounts.utils import get_partner_from_user, is_full_partner_admin

from .models import InsuranceCategory, InsurancePlan, DistributorAccessGrant
from .serializers import (
    InsuranceCategorySerializer,
    InsurancePlanSerializer,
    InsurancePlanCreateSerializer,
    DistributorAccessGrantSerializer,
    DistributorAccessRequestSerializer,
)
from accounts.permissions import IsProviderAdmin, IsDistributorAdmin, IsServiceAccount
from core.models import Partner
from core.throttles import PartnerRateThrottle


# Helpers

def _accessible_plan_ids_and_providers(distributor):
    """
    A distributor can see a plan if either (a) it
    has an approved plan-level grant, or (b) it has an approved
    provider-level grant for that plan's provider AND the plan isn't
    Private. Returns the provider-id and plan-id sets used to build the
    queryset filter — computed once per request rather than calling
    InsurancePlan.is_accessible_to() per row (which would be N+1).
    """
    approved = DistributorAccessGrant.objects.filter(distributor=distributor, status="approved")
    provider_ids = approved.filter(scope="provider").values_list("provider_id", flat=True)
    plan_ids = approved.filter(scope="plan").values_list("plan_id", flat=True)
    return list(provider_ids), list(plan_ids)


# Categories
class InsuranceCategoryListView(APIView):
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


# Plans — Customer / Distributor Facing
class InsurancePlanListView(APIView):
    """
    Public endpoint, scoped via X-Partner-Key middleware:
    - Provider partner: their own active plans only.
    - Distributor partner: plans they hold an approved grant for
      (provider-level or plan-level), excluding anything Private.
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
            provider_ids, plan_ids = _accessible_plan_ids_and_providers(partner)
            plans = InsurancePlan.objects.filter(is_active=True).exclude(
                visibility="private"
            ).filter(
                Q(provider_id__in=provider_ids) | Q(id__in=plan_ids)
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
            plan = InsurancePlan.objects.select_related("provider").get(id=plan_id, is_active=True)
        except InsurancePlan.DoesNotExist:
            return Response({"error": "Plan not found."}, status=404)

        if partner.partner_type == "distributor" and not plan.is_accessible_to(partner):
            return Response({"error": "Plan not found."}, status=404)

        serializer = InsurancePlanSerializer(plan)
        return Response(serializer.data)


# Plans — Provider Team
class ProviderPlanListCreateView(APIView):
    """
    Any provider team member (partner_admin / support_partner_admin /
    partner_viewer) can list plans. Only partner_admin can create one.
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
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can create plans."}, status=403)

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
        }
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
        }
    )
    def patch(self, request, plan_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can edit plans."}, status=403)

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
        }
    )
    def delete(self, request, plan_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can deactivate plans."}, status=403)

        partner = get_partner_from_user(request.user)
        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)

        plan.is_active = False
        plan.save()
        return Response({"message": "Plan deactivated successfully."})


# Provider — Access Requests Inbox
class ProviderAccessRequestListView(APIView):
    """
    Providers view distributor access requests targeting them. Approval
    itself still happens via accounts.StaffDistributorAccessView (staff
    review is required) — this view is read-only visibility so a provider
    can see who has requested access before staff acts on it, and see the 
    outcome afterward.
    """
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        grants = DistributorAccessGrant.objects.filter(
            provider=partner
        ).select_related("distributor", "plan").order_by("-requested_at")

        status_filter = request.query_params.get("status")
        if status_filter:
            grants = grants.filter(status=status_filter)

        serializer = DistributorAccessGrantSerializer(grants, many=True)
        return Response({"access_requests": serializer.data})


# Distributor — Browse, Request, Track, Withdraw Access
class DistributorAccessGrantView(APIView):
    """
    Distributors request provider-level or plan-level access, list their
    own requests (any status), and withdraw a still-pending request.
    Approval/rejection remain staff-only actions elsewhere.
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
        grants = DistributorAccessGrant.objects.filter(
            distributor=partner
        ).select_related("provider", "plan").order_by("-requested_at")

        status_filter = request.query_params.get("status")
        if status_filter:
            grants = grants.filter(status=status_filter)

        serializer = DistributorAccessGrantSerializer(grants, many=True)
        return Response({"access_grants": serializer.data})

    def post(self, request):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can request provider access."}, status=403)

        partner = get_partner_from_user(request.user)
        serializer = DistributorAccessRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        data = serializer.validated_data
        try:
            provider = Partner.objects.get(id=data["provider_id"], partner_type="provider")
        except Partner.DoesNotExist:
            return Response({"error": "Provider not found."}, status=404)

        plan = None
        if data["scope"] == "plan":
            try:
                plan = InsurancePlan.objects.get(id=data["plan_id"], provider=provider)
            except InsurancePlan.DoesNotExist:
                return Response({"error": "Plan not found for this provider."}, status=404)

        try:
            grant = DistributorAccessGrant.objects.request_access(
                distributor=partner, provider=provider, scope=data["scope"], plan=plan
            )
        except ValidationError as e:
            return Response({"error": str(e)}, status=400)

        return Response(DistributorAccessGrantSerializer(grant).data, status=201)


class DistributorAccessGrantWithdrawView(APIView):
    """
    Withdraws a still-pending request. Approved/rejected/revoked grants
    can't be withdrawn this way — an approved grant must go through staff
    revocation instead, preserving the audit trail. Ownership is enforced
    via the distributor=partner filter in the lookup itself, so a
    distributor can only ever withdraw its own organization's requests.
    """
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    def post(self, request, grant_id):
        if not is_full_partner_admin(request.user):
            return Response(
                {"error": "Only a partner_admin can withdraw a request."}, status=403
            )

        partner = get_partner_from_user(request.user)
        try:
            grant = DistributorAccessGrant.objects.get(id=grant_id, distributor=partner)
        except DistributorAccessGrant.DoesNotExist:
            return Response({"error": "Access request not found."}, status=404)

        try:
            DistributorAccessGrant.objects.withdraw(grant, by=request.user)
        except ValidationError as e:
            return Response({"error": str(e)}, status=400)

        return Response({"message": "Access request withdrawn."})


# AI / Automation — Internal Service Endpoints
class PlanRecommendationView(APIView):
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
            provider_ids, plan_ids = _accessible_plan_ids_and_providers(partner)
            plans = InsurancePlan.objects.filter(is_active=True).exclude(
                visibility="private"
            ).filter(
                Q(provider_id__in=provider_ids) | Q(id__in=plan_ids)
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
            provider_ids, plan_ids = _accessible_plan_ids_and_providers(partner)
            plans = InsurancePlan.objects.filter(is_active=True).exclude(
                visibility="private"
            ).filter(
                Q(provider_id__in=provider_ids) | Q(id__in=plan_ids)
            )

        context = []
        for plan in plans.select_related("provider", "category"):
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
