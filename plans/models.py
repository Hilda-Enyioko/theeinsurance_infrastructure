import uuid
from django.db import models
from core.models import Partner

#---Insurance Category---
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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="plans",
        limit_choices_to={"partner_type":"provider"},
        )
    category = models.ForeignKey(
        InsuranceCategory,
        on_delete=models.CASCADE,
        related_name="plans",
        db_index=True,
    )
    name=models.CharField(max_length=500)
    coverage_level = models.CharField(
        max_length=20, choices=COVERAGE_LEVELS, db_index=True
    )
    coverage_amount = models.DecimalField(max_digits=20, decimal_places=2)
    premium = models.DecimalField(max_digits=20, decimal_places=2, db_index=True)
    duration_months = models.IntegerField()
    description = models.TextField()
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.provider.name}"
    

# ---Distributor Access---
class DistributorProviderAccess(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    distributor = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="provider_access",
        limit_choices_to={"partner_type": "distributor"},
    )
    provider = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="distributor_access",
        limit_choices_to={"partner_type": "provider"},
    )
    is_active = models.BooleanField(default=True)
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["distributor", "provider"]

    def __str__(self):
        return f"{self.distributor.name} → {self.provider.name}"