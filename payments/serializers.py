"""
Payments Serializers Module.

Two serializers covering the payment lifecycle:
  2. TransactionSerializer      — read-only representation of Transaction state
"""

import logging

from rest_framework import serializers

from subscriptions.models import PolicySubscription

from .models import Transaction

logger = logging.getLogger(__name__)


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
