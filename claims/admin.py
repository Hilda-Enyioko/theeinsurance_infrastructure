from django.contrib import admin
from .models import Claim, ClaimDocument

# Register your models here.
@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = [
        "claim_reference", "customer", "provider",
        "claim_type", "status", "claimed_amount",
        "approved_amount", "submitted_at"
    ]
    readonly_fields = [
        "id", "claim_reference", "submitted_at", "updated_at"
    ]
    list_filter = ["status", "claim_type"]
    search_fields = ["claim_reference", "customer__user__email"]

@admin.register(ClaimDocument)
class ClaimDocumentAdmin(admin.ModelAdmin):
    list_display = ["claim", "document_type", "uploaded_at"]
    readonly_fields = ["id", "uploaded_at"]