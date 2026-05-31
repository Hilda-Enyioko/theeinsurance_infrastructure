from django.contrib import admin
from .models import PolicySubscription, SubscriptionDocument


@admin.register(PolicySubscription)
class PolicySubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "customer", "plan", "provider", "distributor",
        "status", "amount_paid", "payment_verified", "created_at"
    ]
    readonly_fields = [
        "id", "created_at", "updated_at",
        "provider_payout", "distributor_commission", "platform_fee"
    ]
    search_fields = ["customer__user__email", "plan__name", "payment_reference"]
    list_filter = ["status", "payment_verified"]
    ordering = ["-created_at"]

@admin.register(SubscriptionDocument)
class SubscriptionDocumentAdmin(admin.ModelAdmin):
    list_display = ["subscription", "document_type", "uploaded_at"]
    readonly_fields = ["id", "uploaded_at"]