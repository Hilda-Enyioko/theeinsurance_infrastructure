import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from core.models import Partner


# ---Insurance Category---

class InsuranceCategory(models.Model):
    CATEGORY_CHOICES = [
        ("health", "Health"),
        ("motor", "Motor"),
        ("property", "Property"),
        ("life", "Life"),
        ("travel", "Travel"),
        ("business", "Business"),
        ("agriculture", "Agriculture"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50, choices=CATEGORY_CHOICES, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Insurance Categories"

    def __str__(self):
        return self.get_name_display()


# ---Insurance Plan---

class InsurancePlan(models.Model):
    COVERAGE_LEVELS = [
        ("third_party", "Third Party Only"),
        ("tp_fire_theft", "Third Party, Fire & Theft"),
        ("comprehensive", "Comprehensive"),
        ("standard", "Standard"),  # travel only
    ]

    VISIBILITY_CHOICES = [
        ("private", "Private (only my own customers)"),
        ("public", "Public (distributors can request access)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="plans",
        limit_choices_to={"partner_type": "provider"},
    )
    category = models.ForeignKey(
        InsuranceCategory,
        on_delete=models.CASCADE,
        related_name="plans",
        db_index=True,
    )
    name = models.CharField(max_length=500)
    coverage_level = models.CharField(
        max_length=20, choices=COVERAGE_LEVELS, db_index=True
    )
    coverage_amount = models.DecimalField(max_digits=20, decimal_places=2)
    premium = models.DecimalField(max_digits=20, decimal_places=2, db_index=True)
    duration_months = models.IntegerField()
    description = models.TextField()
    visibility = models.CharField(
        max_length=20, choices=VISIBILITY_CHOICES, default="private", db_index=True
    )
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        if self.provider_id and self.provider.partner_type != "provider":
            raise ValidationError(
                f"provider must have partner_type='provider', got '{self.provider.partner_type}'."
            )

    def save(self, *args, **kwargs):
        self.full_clean(exclude=[f.name for f in self._meta.fields if f.name != "provider"])
        super().save(*args, **kwargs)

    def is_accessible_to(self, distributor: Partner) -> bool:
        """BR-001/BR-002: a Private plan is never distributor-accessible,
        regardless of any grant. Public/Shared plans are accessible via an
        approved plan-level grant, or an approved provider-level grant
        covering this plan's provider."""
        if self.visibility == "private":
            return False
        return DistributorAccessGrant.objects.filter(
            distributor=distributor, provider=self.provider, status="approved"
        ).filter(
            models.Q(scope="plan", plan=self) | models.Q(scope="provider", plan__isnull=True)
        ).exists()

    def __str__(self):
        return f"{self.name} - {self.provider.name}"


# Distributor Access Grant

class DistributorAccessGrantManager(models.Manager):
    STAFF_ROLES = ("super_admin", "support_admin")

    @staticmethod
    def _can_review(grant, by):
        """The grant's PROVIDER (full partner_admin) decides. Staff may only revoke (kill-switch)."""
        profile = getattr(by, "partner_admin_profile", None)
        return bool(profile and profile.partner_id == grant.provider_id and profile.role == "partner_admin")

    def _require(self, grant, by, *, allow_staff=False):
        is_staff = getattr(by, "role", None) in self.STAFF_ROLES
        if not (self._can_review(grant, by) or (allow_staff and is_staff)):
            raise ValidationError("Only the provider that owns this plan can review access requests.")

    def request_access(self, *, distributor, provider, scope, plan=None):
        if distributor.partner_type != "distributor":
            raise ValidationError("distributor must be a Partner of type 'distributor'.")
        if provider.partner_type != "provider" or not provider.is_active:
            raise ValidationError("Provider is not available.")
        if scope == "plan" and plan is None:
            raise ValidationError("plan is required for a plan-level access request.")
        if scope == "provider" and plan is not None:
            raise ValidationError("plan must not be set for a provider-level access request.")
        if plan is not None:
            if plan.provider_id != provider.id:
                raise ValidationError("plan does not belong to the specified provider.")
            if plan.visibility != "public" or not plan.is_active:
                raise ValidationError("Access can only be requested for active, public plans.")

        lookup = dict(distributor=distributor, provider=provider, scope=scope, plan=plan)
        existing = self.filter(**lookup).first()
        if existing:
            if existing.status in ("approved", "pending"):
                return existing                       # no-op
            existing.status, existing.reviewed_by, existing.reviewed_at, existing.review_note = "pending", None, None, ""
            existing.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
            return existing
        return self.create(**lookup, status="pending")

    def approve(self, grant, *, by):
        self._require(grant, by)
        if grant.status != "pending":
            raise ValidationError(f"Only a pending request can be approved (current status: {grant.status}).")
        grant.status, grant.reviewed_by, grant.reviewed_at = "approved", by, timezone.now()
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        return grant

    def reject(self, grant, *, by, note=""):
        self._require(grant, by)
        if grant.status != "pending":
            raise ValidationError(f"Only a pending request can be rejected (current status: {grant.status}).")
        if not note.strip():
            raise ValidationError("A note is required when rejecting.")
        grant.status, grant.reviewed_by, grant.reviewed_at, grant.review_note = "rejected", by, timezone.now(), note.strip()
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
        return grant

    def revoke(self, grant, *, by, note=""):
        self._require(grant, by, allow_staff=True)    # provider OR staff kill-switch
        if grant.status != "approved":
            raise ValidationError(f"Only an approved grant can be revoked (current status: {grant.status}).")
        grant.status, grant.reviewed_by, grant.reviewed_at = "revoked", by, timezone.now()
        if note:
            grant.review_note = note
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
        return grant

    def withdraw(self, grant):  # unchanged
        if grant.status != "pending":
            raise ValidationError(f"Only a pending request can be withdrawn (current status: {grant.status}).")
        grant.status = "withdrawn"
        grant.save(update_fields=["status"])
        return grant


class DistributorAccessGrant(models.Model):
    SCOPE_CHOICES = [
        ("provider", "Provider-Level"),
        ("plan", "Plan-Level"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("revoked", "Revoked"),      
        ("withdrawn", "Withdrawn"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    distributor = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="access_grants",
        limit_choices_to={"partner_type": "distributor"},
    )
    provider = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="granted_distributor_access",
        limit_choices_to={"partner_type": "provider"},
    )
    plan = models.ForeignKey(
        InsurancePlan,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="access_grants",
        help_text="Null for provider-level grants; set for plan-level grants.",
    )
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "accounts.CustomUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    objects = DistributorAccessGrantManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["distributor", "provider"],
                condition=models.Q(scope="provider"),
                name="unique_provider_level_grant",
            ),
            models.UniqueConstraint(
                fields=["distributor", "plan"],
                condition=models.Q(scope="plan"),
                name="unique_plan_level_grant",
            ),
        ]

    def clean(self):
        super().clean()
        if self.scope == "plan" and self.plan_id is None:
            raise ValidationError("plan is required when scope='plan'.")
        if self.scope == "provider" and self.plan_id is not None:
            raise ValidationError("plan must be blank when scope='provider'.")
        if self.plan_id and self.plan.provider_id != self.provider_id:
            raise ValidationError("plan must belong to the specified provider.")
        # Only on creation. Otherwise, once a provider flips a plan to private you could never
        # revoke/withdraw the old grant, because save() runs full_clean().
        if self._state.adding and self.plan_id and self.plan.visibility == "private":
            raise ValidationError("Cannot grant distributor access to a Private plan.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        target = self.plan.name if self.plan_id else f"{self.provider.name} (all plans)"
        return f"{self.distributor.name} → {target} [{self.status}]"
