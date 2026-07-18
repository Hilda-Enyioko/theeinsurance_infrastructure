from django.contrib import admin
from .models import InsuranceCategory, InsurancePlan, DistributorAccessGrant


@admin.register(InsuranceCategory)
class InsuranceCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at"]
    list_filter = ["is_active"]
    readonly_fields = ["id", "created_at"]


@admin.register(InsurancePlan)
class InsurancePlanAdmin(admin.ModelAdmin):
    list_display = [
        "name", "provider", "category", "coverage_level",
        "premium", "visibility", "is_active", "created_at",
    ]
    list_filter = ["visibility", "is_active", "category", "coverage_level"]
    search_fields = ["name", "provider__name", "description"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["-created_at"]


@admin.register(DistributorAccessGrant)
class DistributorAccessGrantAdmin(admin.ModelAdmin):
    list_display = [
        "distributor", "provider", "scope", "plan", "status", "requested_at",
    ]
    list_filter = ["status", "scope"]
    search_fields = ["distributor__name", "provider__name", "plan__name"]
    readonly_fields = [
        "id", "requested_at", "reviewed_by", "reviewed_at",
    ]
    ordering = ["-requested_at"]
