from decimal import Decimal

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from subscriptions.models import PolicySubscription
from .models import Claim, ClaimDocument, ClaimPayment


class ClaimDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClaimDocument
        fields = ["id", "document_type", "file", "uploaded_at"]
        read_only_fields = fields


class ClaimPaymentSerializer(serializers.ModelSerializer):
    """Provider/staff view. The full account number is never exposed here."""
    account_number_masked = serializers.SerializerMethodField()

    class Meta:
        model = ClaimPayment
        fields = ["reference", "amount", "status", "failure_reason", "receipt_url",
                  "account_name", "account_number_masked", "created_at", "completed_at"]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_account_number_masked(self, obj):
        return f"******{obj.account_number[-4:]}" if obj.account_number else ""


class ClaimPaymentCustomerSerializer(ClaimPaymentSerializer):
    """Gateway failure text is internal, so customers don't get it."""
    class Meta(ClaimPaymentSerializer.Meta):
        fields = [f for f in ClaimPaymentSerializer.Meta.fields if f != "failure_reason"]
        read_only_fields = fields


class ClaimSerializer(serializers.ModelSerializer):
    """Full view for providers and TheeInsurance staff (includes `ai_result`)."""
    customer_email = serializers.EmailField(source="customer.user.email", read_only=True)
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    plan_name = serializers.CharField(source="subscription.plan.name", read_only=True)
    settlement_on_file = serializers.BooleanField(
        source="customer.has_settlement", read_only=True,
        help_text="Whether the customer has a settlement account. A claim cannot be approved without one.")
    documents = ClaimDocumentSerializer(many=True, read_only=True)
    payments = ClaimPaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Claim
        fields = [
            "id", "claim_reference", "subscription", "plan_name", "customer", "customer_email",
            "provider", "provider_name", "claim_type", "incident_date", "incident_description",
            "claimed_amount", "approved_amount", "status", "forwarded_at",
            "theeinsurance_review_note", "provider_review_note", "settlement_on_file",
            "documents", "payments", "ai_result", "parties_notified_at", "submitted_at", "updated_at",
        ]
        read_only_fields = fields


class ClaimCustomerSerializer(ClaimSerializer):
    """
    Customer view: no `ai_result` (fraud signals stay internal).
    Review notes ARE shown, so reviewers should write them for the customer.
    """
    payments = ClaimPaymentCustomerSerializer(many=True, read_only=True)

    class Meta(ClaimSerializer.Meta):
        fields = [f for f in ClaimSerializer.Meta.fields if f not in ("ai_result", "parties_notified_at")]
        read_only_fields = fields


class ClaimCreateSerializer(serializers.Serializer):
    """Requires `context={"profile": CustomerProfile}` so a customer can only claim on their own policies."""
    subscription_id = serializers.UUIDField()
    claim_type = serializers.ChoiceField(choices=Claim.CLAIM_TYPE_CHOICES)
    incident_date = serializers.DateField()
    incident_description = serializers.CharField()
    claimed_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))

    MOTOR = ("motor_accident", "motor_theft")
    TRAVEL = ("travel_medical", "travel_baggage", "travel_cancellation")

    def validate_incident_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Incident date cannot be in the future.")
        return value

    def validate(self, attrs):
        sub = (PolicySubscription.objects.select_related("plan__category", "provider")
               .filter(id=attrs["subscription_id"], customer=self.context["profile"], status="active").first())
        if sub is None:
            raise serializers.ValidationError({"subscription_id": "Active subscription not found."})

        category, claim_type = sub.plan.category.name, attrs["claim_type"]
        if category == "motor" and claim_type not in self.MOTOR:
            raise serializers.ValidationError({"claim_type": "This claim type is not valid for motor insurance."})
        if category == "travel" and claim_type not in self.TRAVEL:
            raise serializers.ValidationError({"claim_type": "This claim type is not valid for travel insurance."})

        # assumes start_date / end_date are DateFields; adjust if they are datetimes
        if not (sub.start_date <= attrs["incident_date"] <= sub.end_date):
            raise serializers.ValidationError(
                {"incident_date": "Incident date is outside the policy coverage period."})

        attrs["subscription"] = sub
        return attrs


class ClaimReviewSerializer(serializers.Serializer):
    """
    Used by TheeInsurance staff and provider admins. The note is shown to the customer,
    and is mandatory when rejecting or asking for more information.
    """
    status = serializers.ChoiceField(choices=[c[0] for c in Claim.STATUS_CHOICES])
    review_note = serializers.CharField(required=False, allow_blank=True, default="")
    approved_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False,
                                               min_value=Decimal("0.01"))

    def validate(self, attrs):
        st = attrs["status"]
        if st == "approved" and attrs.get("approved_amount") is None:
            raise serializers.ValidationError(
                {"approved_amount": "Approved amount is required when approving a claim."})
        if st in ("rejected", "more_info_required") and not attrs.get("review_note", "").strip():
            raise serializers.ValidationError(
                {"review_note": "A note is required when rejecting a claim or requesting more information."})
        return attrs
