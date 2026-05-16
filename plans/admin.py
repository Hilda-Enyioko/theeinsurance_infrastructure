from django.contrib import admin
from .models import InsuranceCategory, InsurancePlan, DistributorProviderAccess


@admin.register(InsuranceCategory)
class InsuranceCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at"]
    readonly_fields = ["id", "created_at"]
    list_filter = ["is_active"]


@admin.register(InsurancePlan)
class InsurancePlanAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "category", "coverage_level", "premium", "is_active", "created_at"]
    readonly_fields = ["id", "created_at", "updated_at"]
    search_fields = ["name", "provider__name"]
    list_filter = ["coverage_level", "is_active", "category"]


@admin.register(DistributorProviderAccess)
class DistributorProviderAccessAdmin(admin.ModelAdmin):
    list_display = ["distributor", "provider", "is_active", "granted_at"]
    readonly_fields = ["id", "granted_at"]
    list_filter = ["is_active"]