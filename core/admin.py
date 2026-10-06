from django.contrib import admin
from .models import DistributorProfile, Partner, ProviderProfile, Webhook, WebhookEvent


@admin.register(Partner)
class PartnerModelAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "partner_type", "is_active", "kyc_status", "created_at")
    list_filter = ("is_active", "partner_type")
    search_fields = ("name", "slug")
    readonly_fields = ("id", "api_key_hash", "created_at")

    def has_add_permission(self, request):   # partners must come through /partner/onboard/ (key + profile + admin user)
        return False

    @admin.display(description="KYC")
    def kyc_status(self, obj):
        return obj.kyc.status if hasattr(obj, "kyc") else "not submitted"


@admin.register(DistributorProfile)
class DistributorProfileAdmin(admin.ModelAdmin):
    list_display = ["partner", "commission_rate", "created_at"]
    readonly_fields = ["id", "created_at"]       # commission_rate is staff-editable here (partners can't change it)


@admin.register(ProviderProfile)
class ProviderProfileAdmin(admin.ModelAdmin):
    list_display = ["partner", "naicom_licence_number", "created_at"]
    readonly_fields = ["id", "created_at"]


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("partner", "url", "is_active", "created_at")
    search_fields = ("partner__name", "url")
    list_filter = ("is_active",)


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("webhook", "event", "created_at")
    search_fields = ("webhook__partner__name", "event")
