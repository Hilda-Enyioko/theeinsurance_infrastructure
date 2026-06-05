from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.db.models import Q

from .models import InsuranceCategory, InsurancePlan, DistributorProviderAccess
from .serializers import (
    InsuranceCategorySerializer,
    InsurancePlanSerializer,
    InsurancePlanCreateSerializer,
    DistributorProviderAccessSerializer,
)
from core.models import Partner
from accounts.models import CustomerProfile


# Helpers
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Categories
class InsuranceCategoryListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        categories = InsuranceCategory.objects.filter(is_active=True)
        serializer = InsuranceCategorySerializer(categories, many=True)
        return Response({"categories": serializer.data})


# Plans — Customer Facing
class InsurancePlanListView(APIView):
    """
    Public endpoint — customers browse available plans.
    Scoped to the requesting partner:
    - If provider: shows their own plans only
    - If distributor: shows plans from providers they have access to
    """
    permission_classes = [AllowAny]

    def get(self, request):
        partner = request.partner

        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(
                provider=partner, is_active=True
            )
        else:
            # distributor — get accessible providers
            accessible_providers = DistributorProviderAccess.objects.filter(
                distributor=partner, is_active=True
            ).values_list("provider_id", flat=True)

            plans = InsurancePlan.objects.filter(
                provider__in=accessible_providers, is_active=True
            )

        # filtering
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

        # sorting
        sort_by = request.query_params.get("sort_by", "-created_at")
        allowed_sorts = ["premium", "-premium", "created_at", "-created_at", "name"]
        if sort_by in allowed_sorts:
            plans = plans.order_by(sort_by)

        serializer = InsurancePlanSerializer(plans, many=True)
        return Response({"plans": serializer.data})


class InsurancePlanDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, plan_id):
        partner = request.partner

        try:
            plan = InsurancePlan.objects.get(id=plan_id, is_active=True)
        except InsurancePlan.DoesNotExist:
            return Response({"error": "Plan not found."}, status=404)

        # verify this partner can see this plan
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
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response({"error": "Only insurance providers can manage plans."}, status=403)

        plans = InsurancePlan.objects.filter(provider=partner)

        # include inactive plans for provider's own view
        status_filter = request.query_params.get("status")
        if status_filter == "active":
            plans = plans.filter(is_active=True)
        elif status_filter == "inactive":
            plans = plans.filter(is_active=False)

        serializer = InsurancePlanSerializer(plans, many=True)
        return Response({"plans": serializer.data})

    def post(self, request):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response({"error": "Only insurance providers can create plans."}, status=403)

        serializer = InsurancePlanCreateSerializer(
            data=request.data,
            context={"provider": partner}
        )
        if serializer.is_valid():
            plan = serializer.save()
            return Response(
                InsurancePlanSerializer(plan).data,
                status=201
            )
        return Response(serializer.errors, status=400)


class ProviderPlanDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, plan_id, partner):
        try:
            return InsurancePlan.objects.get(id=plan_id, provider=partner)
        except InsurancePlan.DoesNotExist:
            return None

    def get(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response({"error": "Access denied."}, status=403)

        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)

        serializer = InsurancePlanSerializer(plan)
        return Response(serializer.data)

    def patch(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response({"error": "Access denied."}, status=403)

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

    def delete(self, request, plan_id):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "provider":
            return Response({"error": "Access denied."}, status=403)

        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)

        # soft delete — deactivate rather than destroy
        plan.is_active = False
        plan.save()
        return Response({"message": "Plan deactivated successfully."})


# Distributor Provider Access
class DistributorProviderAccessView(APIView):
    """
    Super admin grants distributors access to specific providers.
    Distributors can view their own access list.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner or partner.partner_type != "distributor":
            return Response({"error": "Access restricted to distributors."}, status=403)

        access = DistributorProviderAccess.objects.filter(distributor=partner)
        serializer = DistributorProviderAccessSerializer(access, many=True)
        return Response({"providers": serializer.data})


class PlanRecommendationView(APIView):
    """
    AI Plan Recommendation Engine.
    n8n calls this with customer context, feeds response to AI model.
    Returns filtered and ranked plans based on query params.
    """
    permission_classes = [AllowAny]
    
    def get(self, request):
        partner = request.partner
        category = request.query_params.get('category')
        budget = request.query_params.get('budget')
        coverage_level = request.query_params.get('coverage_level')
        
        # resolve accessible plans for this partner
        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(
                provider=partner, is_active=True
            )
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

        # order by premium ascending — best value first
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
    Conversational Insurance Assistant.
    Provides structured plan data for Claude API to reason over.
    n8n fetches this and injects into Claude system prompt.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        partner = request.partner

        if partner.partner_type == "provider":
            plans = InsurancePlan.objects.filter(
                provider=partner, is_active=True
            )
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