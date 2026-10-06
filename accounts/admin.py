from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

from .models import CustomerKYC, PartnerKYC
from .services import KYCReviewError, review_customer_kyc, review_partner_kyc

REVIEWER_ROLES = ("super_admin", "support_admin")


def _is_reviewer(user):
    return user.is_active and getattr(user, "role", None) in REVIEWER_ROLES


def _file_link(f):
    if not f:
        return "-"
    try:
        return format_html('<a href="{}" target="_blank" rel="noopener">View document</a>', f.url)
    except Exception:           # private storage without a URL
        return f.name


class ReviewerOnlyMixin:
    """Role-based access instead of per-model Django permissions: only super_admin / support_admin."""
    def has_module_permission(self, request): return _is_reviewer(request.user)
    def has_view_permission(self, request, obj=None): return _is_reviewer(request.user)
    def has_change_permission(self, request, obj=None): return _is_reviewer(request.user)
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False


class ReviewForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") == "rejected" and not (cleaned.get("review_note") or "").strip():
            self.add_error("review_note", "A note is required when rejecting (it is emailed to the applicant).")
        return cleaned


class BaseKYCAdmin(ReviewerOnlyMixin, admin.ModelAdmin):
    form = ReviewForm
    list_filter = ("status",)
    ordering = ("-submitted_at",)
    date_hierarchy = "submitted_at"
    review_service = None

    def get_readonly_fields(self, request, obj=None):
        ro = list(self.readonly_fields)
        if obj and obj.status != "pending":          # decisions are final until the applicant resubmits
            ro += ["status", "review_note"]
        return ro

    def _review(self, request, obj):
        try:
            self.review_service(obj.pk, approve=obj.status == "approved",
                                note=obj.review_note, reviewer_email=request.user.email)
        except KYCReviewError as e:
            self.message_user(request, str(e), messages.ERROR)

    def save_model(self, request, obj, form, change):
        if change and "status" in form.changed_data and obj.status in ("approved", "rejected"):
            self._review(request, obj)               # does ALL side effects; never save directly
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description="Approve selected (pending only)")
    def approve_selected(self, request, queryset):
        done = 0
        for obj in queryset.filter(status="pending"):
            try:
                self.review_service(obj.pk, approve=True, note="", reviewer_email=request.user.email)
                done += 1
            except KYCReviewError as e:
                self.message_user(request, f"{obj}: {e}", messages.ERROR)
        self.message_user(request, f"{done} submission(s) approved. To reject, open the record and add a note.")
    actions = ["approve_selected"]


@admin.register(PartnerKYC)
class PartnerKYCAdmin(BaseKYCAdmin):
    review_service = staticmethod(review_partner_kyc)
    list_display = ("partner", "partner_type", "rc_number", "status", "submitted_at", "reviewed_by")
    list_filter = ("status", "partner__partner_type")
    search_fields = ("partner__name", "rc_number", "tax_identification_number")
    list_select_related = ("partner",)
    fields = ("partner", "partner_type", "rc_number", "tax_identification_number", "naicom_licence_number",
              "cac_link", "naicom_link", "address_link", "submitted_at",
              "status", "review_note", "reviewed_by", "reviewed_at")
    readonly_fields = ("partner", "partner_type", "rc_number", "tax_identification_number",
                       "naicom_licence_number", "cac_link", "naicom_link", "address_link",
                       "submitted_at", "reviewed_by", "reviewed_at")

    @admin.display(description="Type")
    def partner_type(self, o): return o.partner.get_partner_type_display()
    @admin.display(description="CAC certificate")
    def cac_link(self, o): return _file_link(o.cac_certificate)
    @admin.display(description="NAICOM licence")
    def naicom_link(self, o): return _file_link(o.naicom_licence_doc)
    @admin.display(description="Proof of address")
    def address_link(self, o): return _file_link(o.proof_of_address)


@admin.register(CustomerKYC)
class CustomerKYCAdmin(BaseKYCAdmin):
    review_service = staticmethod(review_customer_kyc)
    list_display = ("customer_email", "partner_name", "id_type", "masked_id", "status", "submitted_at")
    list_filter = ("status", "id_type")
    search_fields = ("customer__user__email", "customer__partner__name")
    list_select_related = ("customer__user", "customer__partner")
    fields = ("customer_email", "partner_name", "id_type", "id_number", "id_link", "selfie_link",
              "submitted_at", "status", "review_note", "reviewed_at")
    readonly_fields = ("customer_email", "partner_name", "id_type", "id_number", "id_link",
                       "selfie_link", "submitted_at", "reviewed_at")

    @admin.display(description="Customer")
    def customer_email(self, o): return o.customer.user.email
    @admin.display(description="Partner")
    def partner_name(self, o): return o.customer.partner.name
    @admin.display(description="ID number")
    def masked_id(self, o): return f"****{o.id_number[-4:]}"
    @admin.display(description="ID document")
    def id_link(self, o): return _file_link(o.id_document)
    @admin.display(description="Selfie")
    def selfie_link(self, o): return _file_link(o.selfie)
