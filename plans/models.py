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

    # BR-002: every plan has an explicit visibility state, since
    # DistributorAccessGrant below depends on it directly.
    VISIBILITY_CHOICES = [
        ("private", "Private"),
        ("public", "Public (Request Required)"),
        ("shared", "Shared"),
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


# ---Distributor Access Grant---
# Replaces DistributorProviderAccess. Handles both provider-level and
# plan-level access under one approval workflow (BR-001, BR-002, BR-003,
# BR-020), instead of two parallel models with duplicated approve/reject
# logic.

class DistributorAccessGrantManager(models.Manager):
    def request_access(self, *, distributor: Partner, provider: Partner, scope: str, plan: "InsurancePlan | None" = None):
        if distributor.partner_type != "distributor":
            raise ValidationError("distributor must be a Partner of type 'distributor'.")
        if provider.partner_type != "provider":
            raise ValidationError("provider must be a Partner of type 'provider'.")
        if scope == "plan" and plan is None:
            raise ValidationError("plan is required for a plan-level access request.")
        if scope == "provider" and plan is not None:
            raise ValidationError("plan must not be set for a provider-level access request.")
        if plan is not None and plan.provider_id != provider.id:
            raise ValidationError("plan does not belong to the specified provider.")
        if plan is not None and plan.visibility == "private":
            raise ValidationError("Cannot request access to a Private plan.")

        lookup = dict(distributor=distributor, provider=provider, scope=scope, plan=plan)
        existing = self.filter(**lookup).first()
        if existing:
            if existing.status == "approved":
                return existing  # already granted, no-op
            existing.status = "pending"
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            return existing

        return self.create(**lookup, status="pending")

    def grant_directly(self, *, distributor: Partner, provider: Partner, scope: str, plan=None, by):
        """Provider proactively shares access (BR-002 'Shared') without the
        distributor requesting first — skips the pending state."""
        grant = self.request_access(distributor=distributor, provider=provider, scope=scope, plan=plan)
        return self.approve(grant, by=by)

    def approve(self, grant: "DistributorAccessGrant", *, by):
        if getattr(by, "role", None) not in ("super_admin", "support_admin"):
            raise ValidationError("Only platform staff can approve distributor access requests.")
        grant.status = "approved"
        grant.reviewed_by = by
        grant.reviewed_at = timezone.now()
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        return grant

    def reject(self, grant: "DistributorAccessGrant", *, by, note: str = ""):
        if getattr(by, "role", None) not in ("super_admin", "support_admin"):
            raise ValidationError("Only platform staff can reject distributor access requests.")
        grant.status = "rejected"
        grant.reviewed_by = by
        grant.reviewed_at = timezone.now()
        grant.review_note = note
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
        return grant

    def revoke(self, grant: "DistributorAccessGrant", *, by):
        if getattr(by, "role", None) not in ("super_admin", "support_admin"):
            raise ValidationError("Only platform staff can revoke distributor access.")
        grant.status = "revoked"
        grant.reviewed_by = by
        grant.reviewed_at = timezone.now()
        grant.save(update_fields=["status", "reviewed_by", "reviewed_at"])
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
        if self.plan_id and self.plan.visibility == "private":
            raise ValidationError("Cannot grant distributor access to a Private plan.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        target = self.plan.name if self.plan_id else f"{self.provider.name} (all plans)"
        return f"{self.distributor.name} → {target} [{self.status}]"
