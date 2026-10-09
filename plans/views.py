"""Plans, provider plan management, distributor marketplace and access grants."""

import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer,
)
from rest_framework import serializers as s
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import (
    IsCustomerOfPartner, IsDistributorAdmin, IsProviderAdmin, IsServiceAccount,
)
from accounts.utils import get_partner_from_user, is_full_partner_admin
from core.docs import MessageSerializer, Tag, error, validation_error
from core.models import Partner
from core.throttles import PartnerRateThrottle

from .models import DistributorAccessGrant, InsuranceCategory, InsurancePlan
from .selectors import (
    best_access, distributor_access_index, marketplace_plans, plans_visible_to_partner,
)
from .serializers import (
    DistributorAccessGrantSerializer, DistributorAccessRequestSerializer,
    InsuranceCategorySerializer, InsurancePlanCreateSerializer, InsurancePlanSerializer,
    MarketplacePlanSerializer,
)

# ------------------------------------------------------------------ doc fixtures
CATEGORY_ID = "0b6f1d52-7c1a-4f0e-9d3a-5e2c8b7a1f10"
PROVIDER_ID = "a7c2e9d4-5b3f-4e1a-9d8c-6b5a4f3e2d1c"
PLAN_ID = "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
GRANT_ID = "5e0d3c7a-2b1f-4a9e-8c6d-7f1e2d3c4b5a"

PLAN_EXAMPLE = {
    "id": PLAN_ID, "name": "Motor Comprehensive Plus", "provider": PROVIDER_ID,
    "provider_name": "Sunrise Assurance Plc", "category": CATEGORY_ID, "category_name": "Motor",
    "coverage_level": "comprehensive", "coverage_amount": "5000000.00", "premium": "85000.00",
    "duration_months": 12, "description": "Full cover incl. theft, fire, third party and flood damage.",
    "visibility": "public", "visibility_display": "Public (distributors can request access)",
    "is_active": True, "created_at": "2026-10-01T09:00:00Z", "updated_at": "2026-10-03T14:20:00Z",
}
GRANT_EXAMPLE = {
    "id": GRANT_ID, "distributor": "3f6c1f4e-8a58-4b6e-9a53-2d6a7f0d9c11", "distributor_name": "QuickCover Ltd",
    "provider": PROVIDER_ID, "provider_name": "Sunrise Assurance Plc", "plan": PLAN_ID,
    "plan_name": "Motor Comprehensive Plus", "scope": "plan", "status": "pending",
    "requested_at": "2026-10-06T09:00:00Z", "reviewed_by_email": None, "reviewed_at": None, "review_note": "",
}

PlanListResponse = inline_serializer("PlanListResponse", {"plans": InsurancePlanSerializer(many=True)})
MarketplacePlanListResponse = inline_serializer("MarketplacePlanListResponse", {"plans": MarketplacePlanSerializer(many=True)})
GrantListResponse = inline_serializer("AccessGrantListResponse", {"access_grants": DistributorAccessGrantSerializer(many=True)})

ERR_KEY = error("Missing, invalid or inactive `X-Partner-Key`.", "Invalid or inactive partner key.")
ERR_AUTH = error("Not authenticated.", "Authentication credentials were not provided.", key="detail")

PLAN_FILTERS = [
    OpenApiParameter("category", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=[c[0] for c in InsuranceCategory.CATEGORY_CHOICES], description="Category name."),
    OpenApiParameter("coverage_level", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=[c[0] for c in InsurancePlan.COVERAGE_LEVELS]),
    OpenApiParameter("min_premium", OpenApiTypes.DECIMAL, OpenApiParameter.QUERY, description="Inclusive, in NGN."),
    OpenApiParameter("max_premium", OpenApiTypes.DECIMAL, OpenApiParameter.QUERY, description="Inclusive, in NGN."),
    OpenApiParameter("search", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Matches plan name or description."),
    OpenApiParameter("sort_by", OpenApiTypes.STR, OpenApiParameter.QUERY, default="-created_at",
                     enum=["premium", "-premium", "created_at", "-created_at", "name"]),
]
ALLOWED_SORTS = {"premium", "-premium", "created_at", "-created_at", "name"}


def _decimal(params, key):
    raw = params.get(key)
    if raw in (None, ""):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise s.ValidationError({key: ["Must be a number."]})


def apply_plan_filters(plans, params):
    if v := params.get("category"):
        plans = plans.filter(category__name=v)
    if v := params.get("coverage_level"):
        plans = plans.filter(coverage_level=v)
    if (v := _decimal(params, "min_premium")) is not None:
        plans = plans.filter(premium__gte=v)
    if (v := _decimal(params, "max_premium")) is not None:
        plans = plans.filter(premium__lte=v)
    if v := params.get("search"):
        plans = plans.filter(Q(name__icontains=v) | Q(description__icontains=v))
    if v := params.get("provider_id"):
        try:
            plans = plans.filter(provider_id=uuid.UUID(v))
        except ValueError:
            raise s.ValidationError({"provider_id": ["Must be a valid UUID."]})
    sort_by = params.get("sort_by", "-created_at")
    return plans.order_by(sort_by if sort_by in ALLOWED_SORTS else "-created_at")


# ================================================================ 6. PLANS: storefront (customers)
class InsuranceCategoryListView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List active insurance categories",
        description="Reference data for filters and for `category` when creating a plan. Requires `X-Partner-Key`.",
        auth=[{"PartnerKey": []}],
        responses={200: inline_serializer("CategoryListResponse", {"categories": InsuranceCategorySerializer(many=True)}), 401: ERR_KEY},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"categories": [
            {"id": CATEGORY_ID, "name": "motor", "description": "Vehicle insurance", "is_active": True, "created_at": "2026-09-01T00:00:00Z"}]})],
        tags=[Tag.PLANS],
    )
    def get(self, request):
        return Response({"categories": InsuranceCategorySerializer(InsuranceCategory.objects.filter(is_active=True), many=True).data})


class InsurancePlanListView(APIView):
    permission_classes = [IsCustomerOfPartner]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Browse plans (customer storefront)",
        description=(
            "Returns the plans for the partner identified by `X-Partner-Key`, and only for customers who registered with that partner.\n\n"
            "- **Provider storefront:** all of that provider's active plans (public and private).\n"
            "- **Distributor storefront:** active **public** plans from providers the distributor has an **approved** "
            "access grant for (provider-level grants include plans the provider creates in future; plan-level grants cover one plan).\n\n"
            "Browsing needs no KYC. Subscribing does."
        ),
        auth=[{"BearerAuth": [], "PartnerKey": []}],
        parameters=PLAN_FILTERS,
        responses={200: PlanListResponse, 400: validation_error("min_premium", "Must be a number."),
                   401: ERR_KEY, 403: error("Caller is not a customer of this partner.", "You are not registered with this partner.", key="detail")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"plans": [PLAN_EXAMPLE]})],
        tags=[Tag.PLANS],
    )
    def get(self, request):
        plans = plans_visible_to_partner(request.partner).select_related("provider", "category")
        plans = apply_plan_filters(plans, request.query_params)
        return Response({"plans": InsurancePlanSerializer(plans, many=True).data})


class InsurancePlanDetailView(APIView):
    permission_classes = [IsCustomerOfPartner]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        operation_id="plans_retrieve_by_id",
        summary="Get one plan (customer storefront)",
        description="Same visibility rules as the list. A plan outside the caller's storefront returns 404 (never 403) so IDs can't be probed.",
        auth=[{"BearerAuth": [], "PartnerKey": []}],
        responses={200: InsurancePlanSerializer, 401: ERR_KEY, 403: error("Not a customer of this partner.", "You are not registered with this partner.", key="detail"),
                   404: error("Not available to this storefront.", "Plan not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=PLAN_EXAMPLE)],
        tags=[Tag.PLANS],
    )
    def get(self, request, plan_id):
        plan = plans_visible_to_partner(request.partner).select_related("provider", "category").filter(id=plan_id).first()
        if plan is None:
            return Response({"error": "Plan not found."}, status=404)
        return Response(InsurancePlanSerializer(plan).data)


# ================================================================ 2. PROVIDERS: manage own plans
class ProviderPlanListCreateView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="List my plans",
        description="Providers only ever see **their own** plans (active and inactive, public and private). Any provider team member may call this.",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=["active", "inactive"])],
        responses={200: PlanListResponse, 401: ERR_AUTH},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"plans": [PLAN_EXAMPLE]})],
        tags=[Tag.PROVIDERS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        plans = InsurancePlan.objects.filter(provider=partner).select_related("provider", "category")
        if request.query_params.get("status") == "active":
            plans = plans.filter(is_active=True)
        elif request.query_params.get("status") == "inactive":
            plans = plans.filter(is_active=False)
        return Response({"plans": InsurancePlanSerializer(plans, many=True).data})

    @extend_schema(
        summary="Create a plan",
        description=(
            "Partner admin only, and the provider must be KYC-verified.\n\n"
            "`visibility`: **private** = only your own customers/platform can see it; **public** = distributors can find it in the marketplace and request access.\n"
            "`is_active` defaults to `true`. `category` is a category UUID from `GET /categories/`.\n\n"
            "Rules: travel plans are forced to `coverage_level=standard`; motor plans must use `third_party`, `tp_fire_theft` or `comprehensive`."
        ),
        request=InsurancePlanCreateSerializer,
        responses={201: InsurancePlanSerializer,
                   400: validation_error("coverage_level", "Motor plans must specify third_party, tp_fire_theft, or comprehensive."),
                   403: error("Not a full partner_admin, or provider not verified.", "Only a partner_admin can create plans.")},
        examples=[OpenApiExample("Request", request_only=True, value={
            "name": "Motor Comprehensive Plus", "category": CATEGORY_ID, "coverage_level": "comprehensive",
            "coverage_amount": "5000000.00", "premium": "85000.00", "duration_months": 12,
            "description": "Full cover incl. theft, fire, third party and flood damage.", "visibility": "public"}),
            OpenApiExample("Created", response_only=True, status_codes=["201"], value=PLAN_EXAMPLE)],
        tags=[Tag.PROVIDERS],
    )
    def post(self, request):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can create plans."}, status=403)
        partner = get_partner_from_user(request.user)
        if not partner.is_active:
            return Response({"error": "Your account must be KYC-verified before you can create plans."}, status=403)
        serializer = InsurancePlanCreateSerializer(data=request.data, context={"provider": partner})
        serializer.is_valid(raise_exception=True)
        return Response(InsurancePlanSerializer(serializer.save()).data, status=201)


class ProviderPlanDetailView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    def get_object(self, plan_id, partner):
        return InsurancePlan.objects.select_related("provider", "category").filter(id=plan_id, provider=partner).first()

    @extend_schema(
        operation_id="partner_plans_retrieve_by_id", summary="Get one of my plans",
        responses={200: InsurancePlanSerializer, 404: error("Not found or not yours.", "Plan not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value=PLAN_EXAMPLE)],
        tags=[Tag.PROVIDERS],
    )
    def get(self, request, plan_id):
        plan = self.get_object(plan_id, get_partner_from_user(request.user))
        if not plan:
            return Response({"error": "Plan not found."}, status=404)
        return Response(InsurancePlanSerializer(plan).data)

    @extend_schema(
        summary="Update a plan (partial)",
        description=(
            "Partner admin only. Send only the fields to change.\n\n"
            "**Changing `visibility` to `private`** immediately hides the plan from every distributor, even those with a grant "
            "(the grant is kept and works again if you set it back to `public`). Existing subscriptions are not affected."
        ),
        request=InsurancePlanCreateSerializer,
        responses={200: InsurancePlanSerializer, 400: validation_error("premium", "A valid number is required."),
                   403: error("Not a full partner_admin.", "Only a partner_admin can edit plans."), 404: error("Not found.", "Plan not found.")},
        examples=[OpenApiExample("Make private", request_only=True, value={"visibility": "private"}),
                  OpenApiExample("Reprice", request_only=True, value={"premium": "90000.00"})],
        tags=[Tag.PROVIDERS],
    )
    def patch(self, request, plan_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can edit plans."}, status=403)
        partner = get_partner_from_user(request.user)
        plan = self.get_object(plan_id, partner)
        if not plan:
            return Response({"error": "Plan not found."}, status=404)
        serializer = InsurancePlanCreateSerializer(plan, data=request.data, partial=True, context={"provider": partner})
        serializer.is_valid(raise_exception=True)
        return Response(InsurancePlanSerializer(serializer.save()).data)

    @extend_schema(
        summary="Deactivate a plan (soft delete)", request=None,
        responses={200: MessageSerializer, 403: error("Not a full partner_admin.", "Only a partner_admin can deactivate plans."),
                   404: error("Not found.", "Plan not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"message": "Plan deactivated successfully."})],
        tags=[Tag.PROVIDERS],
    )
    def delete(self, request, plan_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can deactivate plans."}, status=403)
        plan = self.get_object(plan_id, get_partner_from_user(request.user))
        if not plan:
            return Response({"error": "Plan not found."}, status=404)
        plan.is_active = False
        plan.save()
        return Response({"message": "Plan deactivated successfully."})


# ================================================================ 2 + 10. PROVIDER reviews distributor access
class ProviderAccessRequestListView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Inbox: distributor access requests to my plans",
        description="All requests addressed to the logged-in provider. Filter with `?status=pending` for the review queue.",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY,
                                     enum=[c[0] for c in DistributorAccessGrant.STATUS_CHOICES])],
        responses={200: inline_serializer("AccessRequestListResponse", {"access_requests": DistributorAccessGrantSerializer(many=True)})},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"access_requests": [GRANT_EXAMPLE]})],
        tags=[Tag.PROVIDERS, Tag.ACCESS_GRANT],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        grants = (DistributorAccessGrant.objects.filter(provider=partner)
                  .select_related("distributor", "provider", "plan", "reviewed_by").order_by("-requested_at"))
        if f := request.query_params.get("status"):
            grants = grants.filter(status=f)
        return Response({"access_requests": DistributorAccessGrantSerializer(grants, many=True).data})


class ProviderAccessRequestReviewView(APIView):
    permission_classes = [IsProviderAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Approve, reject or revoke a distributor's access",
        description=(
            "**The provider decides**, not TheeInsurance. Partner admin only; the request must be addressed to your organization.\n\n"
            "- `approve`: pending → approved. Provider-level grants cover all your current **and future** public plans; plan-level grants cover one plan.\n"
            "- `reject`: pending → rejected. `note` is required.\n"
            "- `revoke`: approved → revoked. The distributor immediately loses the plan(s).\n\n"
            "A rejected/revoked distributor may request again."
        ),
        parameters=[OpenApiParameter("grant_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        request=inline_serializer("ProviderGrantReviewRequest", {"action": s.ChoiceField(["approve", "reject", "revoke"]), "note": s.CharField(required=False)}),
        responses={200: DistributorAccessGrantSerializer, 400: error("Bad action, missing note or invalid transition.", "Only a pending request can be approved (current status: approved)."),
                   403: error("Not a full partner_admin.", "Only a partner_admin can review access requests."),
                   404: error("Not addressed to your organization.", "Access request not found.")},
        examples=[OpenApiExample("Approve", request_only=True, value={"action": "approve"}),
                  OpenApiExample("Reject", request_only=True, value={"action": "reject", "note": "We only onboard distributors licensed for motor."}),
                  OpenApiExample("Approved", response_only=True, status_codes=["200"], value={**GRANT_EXAMPLE, "status": "approved", "reviewed_by_email": "tunde@sunriseassurance.ng", "reviewed_at": "2026-10-06T11:30:00Z"})],
        tags=[Tag.PROVIDERS, Tag.ACCESS_GRANT],
    )
    def patch(self, request, grant_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can review access requests."}, status=403)
        partner = get_partner_from_user(request.user)
        grant = DistributorAccessGrant.objects.select_related("distributor", "provider", "plan").filter(id=grant_id, provider=partner).first()
        if not grant:
            return Response({"error": "Access request not found."}, status=404)
        action = request.data.get("action")
        mgr = DistributorAccessGrant.objects
        try:
            if action == "approve":
                mgr.approve(grant, by=request.user)
            elif action == "reject":
                mgr.reject(grant, by=request.user, note=request.data.get("note", ""))
            elif action == "revoke":
                mgr.revoke(grant, by=request.user, note=request.data.get("note", ""))
            else:
                return Response({"error": "action must be approve, reject or revoke."}, status=400)
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=400)
        return Response(DistributorAccessGrantSerializer(grant).data)


# ================================================================ 3. DISTRIBUTORS: marketplace
class DistributorMarketplaceProvidersView(APIView):
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Marketplace: providers with public plans",
        description="Active providers that have at least one active **public** plan. `access` is *your* provider-level grant status "
                    "(`none | pending | approved | rejected | revoked | withdrawn`). Use it to decide whether to request provider-level access.",
        responses={200: inline_serializer("MarketplaceProvidersResponse", {"providers": inline_serializer("MarketplaceProvider", {
            "id": s.UUIDField(), "name": s.CharField(), "slug": s.CharField(), "public_plan_count": s.IntegerField(),
            "access": inline_serializer("MarketplaceAccess", {"status": s.CharField(), "via": s.CharField(allow_null=True)})}, many=True)})},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"providers": [
            {"id": PROVIDER_ID, "name": "Sunrise Assurance Plc", "slug": "sunrise-assurance-plc", "public_plan_count": 4, "access": {"status": "none", "via": None}}]})],
        tags=[Tag.DISTRIBUTORS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        idx = distributor_access_index(partner)
        providers = (Partner.objects.filter(partner_type="provider", is_active=True)
                     .annotate(public_plan_count=Count("plans", filter=Q(plans__is_active=True, plans__visibility="public")))
                     .filter(public_plan_count__gt=0).order_by("name"))
        return Response({"providers": [{
            "id": str(p.id), "name": p.name, "slug": p.slug, "public_plan_count": p.public_plan_count,
            "access": {"status": idx["provider"].get(p.id, "none"), "via": "provider" if p.id in idx["provider"] else None},
        } for p in providers]})


class DistributorMarketplacePlansView(APIView):
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Marketplace: browse all public plans",
        description="Every active public plan from every active provider (private plans never appear). Each plan carries `access`: your effective access "
                    "(`status` + whether it comes `via` a plan-level or provider-level grant). Only plans with `access.status = approved` appear in your customers' storefront.",
        parameters=PLAN_FILTERS + [OpenApiParameter("provider_id", OpenApiTypes.UUID, OpenApiParameter.QUERY, description="Only this provider's plans.")],
        responses={200: MarketplacePlanListResponse, 400: validation_error("provider_id", "Must be a valid UUID.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"plans": [{**PLAN_EXAMPLE, "access": {"status": "approved", "via": "provider"}}]})],
        tags=[Tag.DISTRIBUTORS],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        plans = apply_plan_filters(marketplace_plans().select_related("provider", "category"), request.query_params)
        ser = MarketplacePlanSerializer(plans, many=True, context={"access_index": distributor_access_index(partner)})
        return Response({"plans": ser.data})


# ================================================================ 3 + 10. DISTRIBUTOR access requests
class DistributorAccessGrantView(APIView):
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="My access requests and grants",
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, enum=[c[0] for c in DistributorAccessGrant.STATUS_CHOICES])],
        responses={200: GrantListResponse},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"access_grants": [GRANT_EXAMPLE]})],
        tags=[Tag.DISTRIBUTORS, Tag.ACCESS_GRANT],
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        grants = (DistributorAccessGrant.objects.filter(distributor=partner)
                  .select_related("distributor", "provider", "plan", "reviewed_by").order_by("-requested_at"))
        if f := request.query_params.get("status"):
            grants = grants.filter(status=f)
        return Response({"access_grants": DistributorAccessGrantSerializer(grants, many=True).data})

    @extend_schema(
        summary="Request access to a provider or a single plan",
        description=(
            "Partner admin only. The **provider** reviews the request.\n\n"
            "- `scope=provider`: access to **all** the provider's public plans, including ones created later. Omit `plan_id`.\n"
            "- `scope=plan`: access to that **one** public plan only. `plan_id` is required.\n\n"
            "Only active public plans of active providers can be requested. Re-requesting after a rejection/revocation/withdrawal "
            "reopens the same request as `pending`; requesting something already pending/approved returns it unchanged."
        ),
        request=DistributorAccessRequestSerializer,
        responses={201: DistributorAccessGrantSerializer, 400: validation_error("plan_id", "plan_id is required when scope='plan'."),
                   403: error("Not a full partner_admin.", "Only a partner_admin can request provider access."),
                   404: error("Provider or plan not found.", "Plan not found for this provider.")},
        examples=[OpenApiExample("Provider-level", request_only=True, value={"provider_id": PROVIDER_ID, "scope": "provider"}),
                  OpenApiExample("Plan-level", request_only=True, value={"provider_id": PROVIDER_ID, "scope": "plan", "plan_id": PLAN_ID}),
                  OpenApiExample("Created", response_only=True, status_codes=["201"], value=GRANT_EXAMPLE)],
        tags=[Tag.DISTRIBUTORS, Tag.ACCESS_GRANT],
    )
    def post(self, request):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can request provider access."}, status=403)
        partner = get_partner_from_user(request.user)
        serializer = DistributorAccessRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        provider = Partner.objects.filter(id=data["provider_id"], partner_type="provider", is_active=True).first()
        if not provider:
            return Response({"error": "Provider not found."}, status=404)
        plan = None
        if data["scope"] == "plan":
            plan = InsurancePlan.objects.filter(id=data["plan_id"], provider=provider).first()
            if not plan:
                return Response({"error": "Plan not found for this provider."}, status=404)
        try:
            grant = DistributorAccessGrant.objects.request_access(distributor=partner, provider=provider, scope=data["scope"], plan=plan)
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=400)
        return Response(DistributorAccessGrantSerializer(grant).data, status=201)


class DistributorAccessGrantWithdrawView(APIView):
    permission_classes = [IsDistributorAdmin]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="Withdraw a pending request",
        description="Only `pending` requests. To drop an **approved** grant, ask the provider to revoke it.",
        parameters=[OpenApiParameter("grant_id", OpenApiTypes.UUID, OpenApiParameter.PATH)], request=None,
        responses={200: MessageSerializer, 400: error("Not pending.", "Only a pending request can be withdrawn (current status: approved)."),
                   403: error("Not a full partner_admin.", "Only a partner_admin can withdraw a request."), 404: error("Not yours / not found.", "Access request not found.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"message": "Access request withdrawn."})],
        tags=[Tag.DISTRIBUTORS, Tag.ACCESS_GRANT],
    )
    def post(self, request, grant_id):
        if not is_full_partner_admin(request.user):
            return Response({"error": "Only a partner_admin can withdraw a request."}, status=403)
        grant = DistributorAccessGrant.objects.filter(id=grant_id, distributor=get_partner_from_user(request.user)).first()
        if not grant:
            return Response({"error": "Access request not found."}, status=404)
        try:
            DistributorAccessGrant.objects.withdraw(grant)
        except ValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=400)
        return Response({"message": "Access request withdrawn."})


# ================================================================ 6. PLANS: AI / automation (service accounts)
class PlanRecommendationView(APIView):
    permission_classes = [IsServiceAccount]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="AI engine: recommended plans",
        description="Service-account only. Uses the storefront of the partner in `X-Partner-Key`, ordered by premium (cheapest first).",
        auth=[{"BearerAuth": [], "PartnerKey": []}],
        parameters=[OpenApiParameter("category", OpenApiTypes.STR, OpenApiParameter.QUERY),
                    OpenApiParameter("budget", OpenApiTypes.DECIMAL, OpenApiParameter.QUERY, description="Max premium (NGN)."),
                    OpenApiParameter("coverage_level", OpenApiTypes.STR, OpenApiParameter.QUERY)],
        responses={200: inline_serializer("RecommendationResponse", {"recommended_plans": InsurancePlanSerializer(many=True), "filters_applied": s.DictField()}),
                   400: validation_error("budget", "Must be a number.")},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={
            "recommended_plans": [PLAN_EXAMPLE], "filters_applied": {"category": "motor", "budget": "100000", "coverage_level": None}})],
        tags=[Tag.PLANS],
    )
    def get(self, request):
        p = request.query_params
        plans = plans_visible_to_partner(request.partner).select_related("provider", "category")
        if p.get("category"):
            plans = plans.filter(category__name=p["category"])
        if p.get("coverage_level"):
            plans = plans.filter(coverage_level=p["coverage_level"])
        if (budget := _decimal(p, "budget")) is not None:
            plans = plans.filter(premium__lte=budget)
        return Response({"recommended_plans": InsurancePlanSerializer(plans.order_by("premium"), many=True).data,
                         "filters_applied": {"category": p.get("category"), "budget": p.get("budget"), "coverage_level": p.get("coverage_level")}})


class PlanContextView(APIView):
    permission_classes = [IsServiceAccount]
    throttle_classes = [PartnerRateThrottle]

    @extend_schema(
        summary="AI engine: compact plan context for prompts",
        auth=[{"BearerAuth": [], "PartnerKey": []}],
        responses={200: inline_serializer("PlanContextResponse", {"partner": s.CharField(), "total_plans": s.IntegerField(), "plans": s.ListField(child=s.DictField())})},
        examples=[OpenApiExample("OK", response_only=True, status_codes=["200"], value={"partner": "QuickCover Ltd", "total_plans": 1, "plans": [{
            "id": PLAN_ID, "name": "Motor Comprehensive Plus", "provider": "Sunrise Assurance Plc", "category": "Motor", "coverage_level": "Comprehensive",
            "coverage_amount": "5000000.00", "premium": "85000.00", "duration_months": 12, "description": "Full cover incl. theft, fire, third party and flood damage."}]})],
        tags=[Tag.PLANS],
    )
    def get(self, request):
        plans = plans_visible_to_partner(request.partner).select_related("provider", "category")
        context = [{"id": str(p.id), "name": p.name, "provider": p.provider.name, "category": p.category.get_name_display(),
                    "coverage_level": p.get_coverage_level_display(), "coverage_amount": str(p.coverage_amount),
                    "premium": str(p.premium), "duration_months": p.duration_months, "description": p.description} for p in plans]
        return Response({"partner": request.partner.name, "total_plans": len(context), "plans": context})
