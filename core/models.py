from django.db import models
import uuid
import secrets

# Create your models here.
class Partner(models.Model):
    # Partner Type Added
    PARTNER_TYPE_CHOICES = [
        ("provider", "Insurance Provider"),
        ("distributor", "Distribution Partner"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)   
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    api_key = models.CharField(max_length=64, unique=True, editable=False)
    partner_type = models.CharField(max_length=20, choices=PARTNER_TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    nomba_account_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        default=None,
        unique=True,
        help_text=(
            "Nomba sub-account ID for payout settlement. Leave blank if this "
            "partner doesn't have one — they'll default to Interswitch checkout."
        ),
    )

    def __str__(self):
        return self.name
    
    def save(self, *args, **kwargs):
        if not self.api_key:
            self.api_key = secrets.token_urlsafe(32)  # Generate a secure random API key
        super().save(*args, **kwargs)


# Add Distribution Partner Profile
class DistributorProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.OneToOneField(
        Partner,
        on_delete=models.CASCADE,
        related_name="distributor_profile",
        limit_choices_to={"partner_type": "distributor"},
    )
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.partner.name} — {self.commission_rate}%"

class Webhook(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(
        Partner, on_delete=models.CASCADE, related_name="webhooks"
    )
    url = models.URLField()
    secret = models.CharField(max_length=64, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.partner.name} → {self.url}"

class WebhookEvent(models.Model):
    EVENT_CHOICES = [
        # subscription events
        ("subscription.created", "Subscription Created"),
        ("subscription.cancelled", "Subscription Cancelled"),
        ("subscription.updated", "Subscription Updated"),
        # claim events
        ("claim.submitted", "Claim Submitted"),
        ("claim.status_updated", "Claim Status Updated"),
        # kyc events
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
        unique_together = ["webhook", "event"]  # no duplicate events per webhook

    def __str__(self):
        return f"{self.webhook} → {self.event}"


class ServiceWebhookEndpoint(models.Model):
    """
    Callback URLs registered by internal service accounts (n8n, schedulers)
    for receiving lifecycle events from theeinsurance — distinct from the
    partner-facing Webhook/WebhookEvent models above.
    """
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
