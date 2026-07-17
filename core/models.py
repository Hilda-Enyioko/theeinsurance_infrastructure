"""
Core models layer capturing insurance business partner structures, profile routing, 
and incoming/outgoing webhook orchestration configurations.
"""

import hashlib
import secrets
import uuid
from django.core.exceptions import ValidationError
from django.db import models


class Partner(models.Model):
    """
    Core business entity table identifying and authorizing third-party partners.
    Stores a hashed version of the API key for maximum security.
    """

    PARTNER_TYPE_CHOICES = [
        ("provider", "Insurance Provider"),
        ("distributor", "Distribution Partner"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    
    # Store the secure hash rather than the plaintext key
    api_key_hash = models.CharField(max_length=64, unique=True, editable=False)
    
    partner_type = models.CharField(max_length=20, choices=PARTNER_TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return str(self.name)

    @staticmethod
    def hash_key(raw_key: str) -> str:
        """Generates a predictable SHA-256 string for token validation."""
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        """Custom hook to safely initialize secure hash tokens if not present."""
        if not self.api_key_hash:
            # Generate a temporary raw secret string
            raw_key = f"pk_{secrets.token_urlsafe(32)}"
            self.api_key_hash = self.hash_key(raw_key)
            
            # CRITICAL: Expose raw_key to runtime context ONCE during creation 
            # so views can display it to the user.
            setattr(self, "_raw_api_key", raw_key)
            
        super().save(*args, **kwargs)


class DistributorProfile(models.Model):
    """Isolated pricing, structural parameters, and banking rules for distributors."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.OneToOneField(
        Partner,
        on_delete=models.CASCADE,
        related_name="distributor_profile",
        limit_choices_to={"partner_type": "distributor"},
    )
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    settlement_bank_account = models.CharField(max_length=64, blank=True)
    settlement_bank_code = models.CharField(max_length=10, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        """Ensures integrity rules cannot be bypassed at the programmatic level."""
        super().clean()
        if self.partner and self.partner.partner_type != "distributor":
            raise ValidationError(
                f"Partner must have partner_type='distributor', got '{self.partner.partner_type}'."
            )
    
    def save(self, *args, **kwargs):
        self.full_clean(exclude=[f.name for f in self._meta.fields if f.name != "partner"])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.partner.name} — {self.commission_rate}%"


class ProviderProfile(models.Model):
    """Regulatory registry identity configurations for risk-carrying carriers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.OneToOneField(
        Partner,
        on_delete=models.CASCADE,
        related_name="provider_profile",
        limit_choices_to={"partner_type": "provider"},
    )
    naicom_licence_number = models.CharField(max_length=50, blank=True)
    settlement_bank_account = models.CharField(max_length=64, blank=True)
    settlement_bank_code = models.CharField(max_length=10, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        """Enforces type constraints at execution layer."""
        super().clean()
        if self.partner and self.partner.partner_type != "provider":
            raise ValidationError(
                f"Partner must have partner_type='provider', got '{self.partner.partner_type}'."
            )
    
    def save(self, *args, **kwargs):
        self.full_clean(exclude=[f.name for f in self._meta.fields if f.name != "partner"])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.partner.name} (Provider)"


class Webhook(models.Model):
    """External client consumer endpoints receiving system status transmissions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(
        Partner, on_delete=models.CASCADE, related_name="webhooks"
    )
    url = models.URLField()
    secret_hash = models.CharField(max_length=64, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def hash_secret(raw_secret: str) -> str:
        """Hashes signature targets for validation."""
        return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.secret_hash:
            raw_secret = f"whsec_{secrets.token_urlsafe(32)}"
            self.secret_hash = self.hash_secret(raw_secret)
            setattr(self, "_raw_webhook_secret", raw_secret)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.partner.name} → {self.url}"


class WebhookEvent(models.Model):
    """Event configuration mapping targeting operational partner topics."""

    EVENT_CHOICES = [
        ("subscription.created", "Subscription Created"),
        ("subscription.cancelled", "Subscription Cancelled"),
        ("subscription.updated", "Subscription Updated"),
        ("claim.submitted", "Claim Submitted"),
        ("claim.status_updated", "Claim Status Updated"),
        ("kyc.submitted", "KYC Submitted"),
        ("kyc.approved", "KYC Approved"),
        ("kyc.rejected", "KYC Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook = models.ForeignKey(
        Webhook, on_delete=models.CASCADE, related_name="events"
    )
    event = models.CharField(max_length=50, choices=EVENT_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["webhook", "event"]

    def __str__(self):
        return f"{self.webhook} → {self.event}"


class ServiceWebhookEndpoint(models.Model):
    """Internal M2M subscription pipes running system lifecycle metrics."""

    EVENT_CHOICES = [
        ("payment.successful", "Payment Successful"),
        ("charge.failed", "Charge Failed (Dunning)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_account = models.ForeignKey(
        "accounts.CustomUser",
        on_delete=models.CASCADE,
        related_name="service_webhooks",
        limit_choices_to={"role": "service_account"},
    )
    event = models.CharField(max_length=50, choices=EVENT_CHOICES)
    url = models.URLField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["service_account", "event"]

    def __str__(self):
        return f"{self.service_account.email} → {self.event} → {self.url}"
