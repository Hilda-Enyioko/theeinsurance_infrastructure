from rest_framework import serializers
from .models import InsuranceCategory, InsurancePlan, DistributorProviderAccess
from core.models import Partner

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
            "is_active", "granted_at",
        ]
        read_only_fields = ["id", "distributor", "granted_at"]
