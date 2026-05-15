from django.contrib import admin
from .models import Partner, Webhook, WebhookEvent

# Register your models here.
@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "partner_type", "is_active", "commission_rate", "created_at")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)

@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("partner", "url", "is_active", "created_at")
    search_fields = ("partner__name", "url")
    list_filter = ("is_active",)

@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("webhook", "event", "created_at")
    search_fields = ("webhook__partner__name", "event")