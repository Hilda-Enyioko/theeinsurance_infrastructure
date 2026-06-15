# mypy: ignore-errors
"""
Payments Serializers Module.

Two serializers covering the payment lifecycle:
  1. InitiatePaymentSerializer  — validates initiation request, creates Transaction
  2. TransactionSerializer      — read-only representation of Transaction state
"""

import logging

from rest_framework import serializers

from subscriptions.models import PolicySubscription

from .models import Transaction

logger = logging.getLogger(__name__)


class InitiatePaymentSerializer(serializers.Serializer):
    """
    Validates a payment initiation request and creates a PENDING Transaction.

    Ensures:
      - The subscription exists and belongs to the requesting user
      - The subscription is in a payable state (PENDING or EXPIRED)
      - No duplicate PENDING transaction already exists for this subscription
      - Amount is pulled from the subscription's plan (not trusted from client)
    """

    subscription_id = serializers.UUIDField()
    payment_type = serializers.ChoiceField(choices=Transaction.PAYMENT_TYPE.choices)

    def validate_subscription_id(self, value):
        """
        Confirm the subscription exists and belongs to the requesting user.
        """
        request = self.context['request']

        try:
            subscription = PolicySubscription.objects.get(
                id=value,
                customer__user=request.user
            )
        
        except PolicySubscription.DoesNotExist:
            raise serializers.ValidationError(
                "Subscription not found or does not belong to you."
            )

        self._subscription = subscription
        return value

    def validate(self, attrs):
        """
        Cross-field validation:
          1. Subscription must be in a payable state
          2. No active PENDING transaction for this subscription
        """
        
        subscription = self._subscription

        payable_statuses = ['pending_payment', 'expired']

        if subscription.status not in payable_statuses:
            raise serializers.ValidationError(
                f"Subscription is not in a payable state"
                f"(current: {subscription.status})."
        )

        already_pending = Transaction.objects.filter(
            subscription=subscription,
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
        ).exists()

        if already_pending:
            raise serializers.ValidationError(
                "A pending transaction already exists for this subscription."
            )

        return attrs

    def save(self) -> Transaction:
        """
        Create and return a PENDING Transaction.
        Amount is sourced from the plan attached to the subscription —
        never from the client request.
        """
        request = self.context['request']
        subscription = self._subscription

        transaction = Transaction.objects.create(
            subscription=subscription,
            initiated_by=request.user,
            amount=subscription.amount_paid,
            payment_type=self.validated_data['payment_type'],
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
        )

        logger.info(
            "Transaction %s created for subscription %s by user %s",
            transaction.reference,
            subscription.id,
            request.user.id,
        )
        return transaction


class TransactionSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for returning Transaction state to the frontend.
    Used in the callback view and any transaction detail/list endpoints.
    """

    initiated_by = serializers.SerializerMethodField()
    subscription_id = serializers.UUIDField(source='subscription.id', read_only=True)

    class Meta:
        model = Transaction
        fields = [
            'id',
            'reference',
            'amount',
            'currency',
            'payment_type',
            'payment_status',
            'gateway_reference',
            'subscription_id',
            'initiated_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_initiated_by(self, obj) -> str | None:
        """Return the email of the user who initiated the transaction."""
        if obj.initiated_by:
            return getattr(obj.initiated_by, 'email', None)
        return None
