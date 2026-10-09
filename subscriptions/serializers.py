from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from plans.selectors import plans_visible_to_partner

from .models import PolicySubscription
from .rules import can_cancel, can_renew

BASE_FIELDS = [
    "id", "customer", "customer_email", "plan", "plan_name", "provider_name", "distributor_name",
    "channel", "start_date", "end_date", "status", "amount_paid",
    "payment_reference", "payment_verified", "created_at", "updated_at",
]


class _Base(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    distributor_name = serializers.CharField(source="distributor.name", read_only=True, default=None)
    customer_email = serializers.CharField(source="customer.user.email", read_only=True)
    channel = serializers.SerializerMethodField()

    class Meta:
        model = PolicySubscription
        fields = BASE_FIELDS
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["direct", "distributor"]))
    def get_channel(self, obj):
        """`distributor` when sold through a distributor, otherwise `direct`."""
        return "distributor" if obj.distributor_id else "direct"


class PolicySubscriptionSerializer(_Base):
    """Customer view. No commission/payout figures."""
    can_renew = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta(_Base.Meta):
        fields = BASE_FIELDS + ["can_renew", "can_cancel"]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_can_renew(self, obj):
        return can_renew(obj)

    @extend_schema_field(serializers.BooleanField())
    def get_can_cancel(self, obj):
        return can_cancel(obj)


class ProviderSubscriptionSerializer(_Base):
    class Meta(_Base.Meta):
        fields = BASE_FIELDS + ["provider_payout"]
        read_only_fields = fields


class DistributorSubscriptionSerializer(_Base):
    class Meta(_Base.Meta):
        fields = BASE_FIELDS + ["distributor_commission"]
        read_only_fields = fields


class StaffSubscriptionSerializer(_Base):
    class Meta(_Base.Meta):
        fields = BASE_FIELDS + ["provider", "distributor", "provider_payout",
                                "distributor_commission", "platform_fee"]
        read_only_fields = fields


class PolicySubscriptionCreateSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(help_text="Must be a plan in the storefront of the partner whose X-Partner-Key is sent.")
    start_date = serializers.DateField(help_text="Coverage start, YYYY-MM-DD. Cannot be in the past.")

    def validate_start_date(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError("start_date cannot be in the past.")
        return value

    def validate(self, attrs):
        plan = (plans_visible_to_partner(self.context["partner"])
                .select_related("provider", "category").filter(id=attrs["plan_id"]).first())
        if plan is None:   # same message for "doesn't exist" and "not yours" so IDs can't be probed
            raise serializers.ValidationError({"plan_id": "Plan not found or not available on this platform."})
        attrs["plan"] = plan
        return attrs
