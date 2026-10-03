from rest_framework import serializers
from .models import InsuranceCategory, InsurancePlan, DistributorAccessGrant


# Insurance Category Serializer
class InsuranceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = InsuranceCategory
        fields = ["id", "name", "description", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


# Insurance Plan Serializer
class InsurancePlanSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.get_name_display", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    visibility_display = serializers.CharField(source="get_visibility_display", read_only=True)

    class Meta:
        model = InsurancePlan
        fields = [
            "id", "name", "provider", "provider_name",
            "category", "category_name", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description", "visibility", "visibility_display",
            "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class InsurancePlanCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = InsurancePlan
        fields = [
            "name", "category", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description", "visibility", "is_active",
        ]

    def validate_category(self, value):
        if not value.is_active:
            raise serializers.ValidationError("This category is not currently active.")
        return value

    def validate(self, attrs):
        category = attrs.get("category") or getattr(self.instance, "category", None)
        coverage_level = attrs.get("coverage_level") or getattr(self.instance, "coverage_level", None)

        if category.name == "travel":
            attrs["coverage_level"] = "standard"
        elif category.name == "motor":
            # Fixed: these keys must match InsurancePlan.COVERAGE_LEVELS exactly —
            # the previous list used "third_party_fire_theft", which never
            # matched the model's actual "tp_fire_theft" key, so this
            # validation silently never fired.
            motor_levels = ["third_party", "tp_fire_theft", "comprehensive"]
            if coverage_level not in motor_levels:
                raise serializers.ValidationError({
                    "coverage_level": "Motor plans must specify third_party, tp_fire_theft, or comprehensive."
                })

        return attrs

    def create(self, validated_data):
        provider = self.context["provider"]
        return InsurancePlan.objects.create(provider=provider, **validated_data)


# Distributor Access Grant Serializer
# Replaces DistributorProviderAccessSerializer. Represents a single grant
# in any state (pending/approved/rejected/revoked), covering both
# provider-level and plan-level scope.
class DistributorAccessGrantSerializer(serializers.ModelSerializer):
    distributor_name = serializers.CharField(source="distributor.name", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True, default=None)
    reviewed_by_email = serializers.SerializerMethodField()

    class Meta:
        model = DistributorAccessGrant
        fields = [
            "id", "distributor", "distributor_name",
            "provider", "provider_name",
            "plan", "plan_name", "scope", "status",
            "requested_at", "reviewed_by_email", "reviewed_at", "review_note",
        ]
        read_only_fields = fields

    def get_reviewed_by_email(self, obj):
        return obj.reviewed_by.email if obj.reviewed_by_id else None


class DistributorAccessRequestSerializer(serializers.Serializer):
    """
    Distributor-initiated request — 4.4 (Provider-Level / Plan-Level
    Access). The distributor is always taken from request context, never
    from the request body, so a distributor can't request access on
    another org's behalf.
    """
    provider_id = serializers.UUIDField()
    scope = serializers.ChoiceField(choices=DistributorAccessGrant.SCOPE_CHOICES)
    plan_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["scope"] == "plan" and not attrs.get("plan_id"):
            raise serializers.ValidationError(
                {"plan_id": "plan_id is required when scope='plan'."}
            )
        if attrs["scope"] == "provider" and attrs.get("plan_id"):
            raise serializers.ValidationError(
                {"plan_id": "plan_id must not be set when scope='provider'."}
            )
        return attrs
