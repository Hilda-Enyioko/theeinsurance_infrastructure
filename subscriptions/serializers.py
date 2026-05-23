from rest_framework import serializers
from .models import PolicySubscription
from plans.models import InsurancePlan
from accounts.models import CustomUser

class PolicySubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    provider_name = serializers.CharField(source='provider.name', read_only=True)
    distributor_name = serializers.CharField(source='distributor.name', read_only=True, default=None)
    customer_email = serializers.CharField(source='customer.user.email', read_only=True)
    
    class Meta:
        model = PolicySubscription
        fields = [
            "id", "customer", "customer_email",
            "plan", "plan_name",
            "provider", "provider_name",
            "distributor", "distributor_name",
            "start_date", "end_date", "status",
            "amount_paid", "provider_payout",
            "distributor_commission", "platform_fee",
            "payment_reference", "payment_verified",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "customer", "provider", "distributor",
            "status", "amount_paid", "provider_payout",
            "distributor_commission", "platform_fee",
            "payment_reference", "payment_verified",
            "created_at", "updated_at",
        ]

class PolicySubscriptionCreateSerializer(serializers.ModelSerializer):
    plan_id = serializers.UUIDField(write_only=True)
    start_date = serializers.DateField()

    def validate_plan_id(self, value):
        try:
            plan = InsurancePlan.objects.get(id=value, is_active=True)
        except InsurancePlan.DoesNotExist:
            raise serializers.ValidationError("Plan not found or inactive.")
        return value
    
    def validate(self, attrs):
        # attach the plan object for use in views
        attrs['plan'] = InsurancePlan.objects.get(id=attrs['plan_id'])
        return attrs