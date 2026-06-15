"""
Payments Admin Module

Registers Transaction and CallbackLog with the Django admin site.
Optimized for financial audit workflows — read-only ledger entries,
filterable by status, searchable by reference.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Transaction, CallbackLog


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """
    Admin view for the Transaction ledger.
    """

    list_display = [
        'reference',
        'amount',
        'currency',
        'payment_status',
        'payment_type',
        'initiated_by',
        'subscription',
        'created_at',
    ]

    list_filter = [
        'payment_status',
        'payment_type',
        'currency',
        'created_at',
    ]

    search_fields = [
        'reference',
        'gateway_reference',
        'initiated_by__email',
        'subscription__id',
    ]

    readonly_fields = [
        'id',
        'reference',
        'amount',
        'currency',
        'payment_type',
        'payment_status',
        'gateway_reference',
        'gateway_response',
        'subscription',
        'initiated_by',
        'created_at',
        'updated_at',
    ]
    
    fieldsets = (
        (_('Transaction Identity'), {
            'fields': ('id', 'reference', 'gateway_reference')
        }),
        (_('Payment Details'), {
            'fields': ('amount', 'currency', 'payment_type', 'payment_status')
        }),
        (_('Relationships'), {
            'fields': ('subscription', 'initiated_by')
        }),
        (_('Gateway Response'), {
            'fields': ('gateway_response',),
            'classes': ('collapse',),
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False  # transactions are created via API only

    def has_delete_permission(self, request, obj=None):
        return False  # financial ledger — never delete


@admin.register(CallbackLog)
class CallbackLogAdmin(admin.ModelAdmin):
    """
    Admin view for immutable webhook callback audit logs.
    Supports fast triage of duplicate, flagged, and mismatched callbacks.
    """

    list_display = [
        'transaction_reference',
        'status',
        'response_code',
        'is_duplicate',
        'is_amount_mismatch',
        'transaction',
        'created_at',
    ]

    list_filter = [
        'status',
        'is_duplicate',
        'is_amount_mismatch',
        'created_at',
    ]

    search_fields = [
        'transaction_reference',
        'response_code',
        'transaction__reference',
    ]

    readonly_fields = [
        'uuid',
        'transaction_reference',
        'raw_payload',
        'response_code',
        'status',
        'transaction',
        'is_duplicate',
        'is_amount_mismatch',
        'created_at',
        'updated_at',
    ]

    fieldsets = (
        (_('Callback Identity'), {
            'fields': ('uuid', 'transaction_reference', 'response_code', 'status')
        }),
        (_('Flags'), {
            'fields': ('is_duplicate', 'is_amount_mismatch')
        }),
        (_('Linked Transaction'), {
            'fields': ('transaction',)
        }),
        (_('Raw Payload'), {
            'fields': ('raw_payload',),
            'classes': ('collapse',),
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
