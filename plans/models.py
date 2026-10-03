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


# ---Distributor Access---

class DistributorProviderAccess(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    is_active = models.BooleanField(default=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

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
        return f"{self.distributor.name} → {self.provider.name} ({self.status})"
