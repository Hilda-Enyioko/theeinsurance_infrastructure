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
    fields = [
            "id", "name", "provider", "provider_name",
            "category", "category_name", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description", "is_active", "created_at", "updated_at",
        ]
    read_only_fields = ["id", "provider", "created_at", "updated_at"]

class InsurancePlanCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = InsurancePlan
        fields = [
            "name", "category", "coverage_level",
            "coverage_amount", "premium", "duration_months",
            "description",
        ]
    
    def validate_category(self, value):
        if not value.is_active:
            raise serializers.ValidationError("This category is not currently active.")
        return value
    
    def create(self, validated_data):
        provider = self.context["provider"]
        return InsurancePlan.objects.create(provider=provider, **validated_data)
    
# Distributor Provider Access Serializer
class DistributorProviderAccessSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)

class Meta:
        model = DistributorProviderAccess
        fields = [
            "id", "distributor", "distributor_name",
            "provider", "provider_name",
            "is_active", "granted_at",
        ]
        read_only_fields = ["id", "distributor", "granted_at"]
