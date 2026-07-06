from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from .models import InsuranceCategory, InsurancePlan, DistributorProviderAccess
from core.models import Partner

ACCESS_STATUS_CHOICES = ["not_requested", "pending", "approved", "rejected"]

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
    
    class Meta:
        model = InsurancePlan
        fields = [
            "id", "name", "provider", "provider_name",
            "category", "category_name", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

class InsurancePlanCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = InsurancePlan
        fields = [
            "name", "category", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description", "is_active",
        ]
    
    def validate_category(self, value):
        if not value.is_active:
            raise serializers.ValidationError("This category is not currently active.")
        return value
    
    def create(self, validated_data):
        validated_data.pop("is_active", None)
        provider = self.context["provider"]
        return InsurancePlan.objects.create(provider=provider, **validated_data)

    def validate(self, attrs):
        category = attrs.get("category") or getattr(self.instance, "category", None)
        coverage_level = attrs.get("coverage_level")

        if category.name == "travel":
            attrs["coverage_level"] = "standard"

        if category.name == "motor":
            motor_levels = ["third_party", "tp_fire_theft", "comprehensive"]
            if coverage_level not in motor_levels:
                raise serializers.ValidationError({
                    "coverage_level": "Motor plans must specify third_party, third_party_fire_theft, or comprehensive."
                })

        return attrs
    
# Distributor Provider Access Serializer
class DistributorProviderAccessSerializer(serializers.ModelSerializer):
    distributor_name = serializers.CharField(source="distributor.name", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = DistributorProviderAccess
        fields = [
            "id", "distributor", "distributor_name",
            "provider", "provider_name",
            "status", "is_active", "granted_at", "reviewed_at",
        ]
        read_only_fields = ["id", "distributor", "status", "granted_at", "reviewed_at"]

class ProviderBrowseSerializer(serializers.ModelSerializer):
    """
    Read-only view of a provider, for the
    distributor 'browse providers' list.
    """
    access_status = serializers.SerializerMethodField()

    class Meta:
        model = Partner
        fields = ["id", "name", "slug", "access_status"]

    @extend_schema_field(
        serializers.ChoiceField(choices=ACCESS_STATUS_CHOICES)
    )
    def get_access_status(self, obj):
        distributor = self.context["distributor"]
        access = DistributorProviderAccess.objects.filter(
            distributor=distributor, provider=obj
        ).first()
        if not access:
            return "not_requested"
        return access.status


class DistributorPlanBrowseSerializer(InsurancePlanSerializer):
    """
    Plans list for distributors, annotated with 
    this distributor's access status to the plan's 
    provider.
    """
    access_status = serializers.SerializerMethodField()

    class Meta(InsurancePlanSerializer.Meta):
        fields = InsurancePlanSerializer.Meta.fields + ["access_status"]

    @extend_schema_field(
        serializers.ChoiceField(choices=ACCESS_STATUS_CHOICES)
    )
    def get_access_status(self, obj):
        distributor = self.context["distributor"]
        access = DistributorProviderAccess.objects.filter(
            distributor=distributor, provider=obj.provider
        ).first()
        return access.status if access else "not_requested"


class DistributorAccessRequestSerializer(serializers.Serializer):
    provider = serializers.PrimaryKeyRelatedField(
        queryset=Partner.objects.filter(partner_type="provider", is_active=True)
    )

    def validate_provider(self, value):
        distributor = self.context["distributor"]
        existing = DistributorProviderAccess.objects.filter(
            distributor=distributor, provider=value
        ).first()
        if existing and existing.status == "approved":
            raise serializers.ValidationError("Access already approved for this provider.")
        if existing and existing.status == "pending":
            raise serializers.ValidationError("A request for this provider is already pending.")
        return value

    def create(self, validated_data):
        distributor = self.context["distributor"]
        provider = validated_data["provider"]
        access, _ = DistributorProviderAccess.objects.update_or_create(
            distributor=distributor,
            provider=provider,
            defaults={"status": "pending", "is_active": False},
        )
        return access
