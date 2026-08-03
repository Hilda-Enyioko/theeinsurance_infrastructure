from rest_framework import serializers
from .models import Claim, ClaimDocument, REQUIRED_CLAIM_DOCUMENTS
from subscriptions.models import PolicySubscription

class ClaimDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClaimDocument
        fields = ["id", "document_type", "file", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]

class ClaimSerializer(serializers.ModelSerializer):
    customer_email = serializers.EmailField(
        source="customer.user.email", 
        read_only=True
    )
    provider_name = serializers.CharField(
        source="provider.name", read_only=True
    )
    plan_name = serializers.CharField(
        source="subscription.plan.name", read_only=True
    )
    documents = ClaimDocumentSerializer(many=True, read_only=True)
    
    class Meta:
        model = Claim
        fields = [
            "id", "claim_reference",
            "subscription", "plan_name",
            "customer", "customer_email",
            "provider", "provider_name",
            "claim_type", "incident_date",
            "incident_description", "claimed_amount",
            "approved_amount", "status",
            "theeinsurance_review_note",
            "provider_review_note",
            "documents",
            "submitted_at", "updated_at",
        ]

        read_only_fields = [
            "id", "claim_reference", "customer", "provider",
            "approved_amount", "status",
            "theeinsurance_review_note", "provider_review_note",
            "submitted_at", "updated_at",
        ]


class ClaimCreateSerializer(serializers.Serializer):
    subscription_id = serializers.UUIDField()
    claim_type = serializers.ChoiceField(choices=Claim.CLAIM_TYPE_CHOICES)
    incident_date = serializers.DateField()
    incident_description = serializers.CharField()
    claimed_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    
    def validate_subscription_id(self, value):
        try:
            subscription = PolicySubscription.objects.get(
                id=value, status="active"
            )
        except PolicySubscription.DoesNotExist:
            raise serializers.ValidationError(
                "Active subscription not found."
            )
        return value
    
    def validate(self, attrs):
        subscription = PolicySubscription.objects.get(
            id=attrs["subscription_id"]
        )
        claim_type = attrs["claim_type"]
        category = subscription.plan.category.name

        # validate claim type matches plan category
        motor_claims = ["motor_accident", "motor_theft"]
        travel_claims = ["travel_medical", "travel_baggage", "travel_cancellation"]

        if category == "motor" and claim_type not in motor_claims:
            raise serializers.ValidationError({
                "claim_type": "This claim type is not valid for motor insurance."
            })

        if category == "travel" and claim_type not in travel_claims:
            raise serializers.ValidationError({
                "claim_type": "This claim type is not valid for travel insurance."
            })

        attrs["subscription"] = subscription
        return attrs
    

class ClaimReviewSerializer(serializers.Serializer):
    """
    Used by TheeInsurance staff and provider admins to review claims.
    """
    status = serializers.ChoiceField(choices=Claim.STATUS_CHOICES)
    review_note = serializers.CharField(required=False, allow_blank=True)
    approved_amount = serializers.DecimalField(
        max_digits=20, decimal_places=2, required=False
    )

    def validate(self, attrs):
        status = attrs.get("status")
        approved_amount = attrs.get("approved_amount")

        if status == "approved" and not approved_amount:
            raise serializers.ValidationError({
                "approved_amount": "Approved amount is required when approving a claim."
            })
        return attrs
