import uuid
from django.db import models
from accounts.models import CustomerProfile
from plans.models import InsurancePlan
from core.models import Partner
from core.storage import KYCDocumentStorage

#---Policy Subscription---
class PolicySubscription(models.Model):
    STATUS_CHOICES = [
        ("active", "Active"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
        ("failed", "Failed"),
        ("grace_period", "Grace Period"),
        ("lapsed", "Lapsed"),
        ("pending_payment", "Pending Payment"),
        ("pending_document", "Pending Document"),
    ]

    id: models.UUIDField = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,editable=False
    )

    customer: models.ForeignKey = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        db_index=True,
    )

    plan: models.ForeignKey = models.ForeignKey(
        InsurancePlan,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        db_index=True,
    )

    # both partners tracked on every transaction
    provider: models.ForeignKey = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="provider_subscriptions",
        limit_choices_to={"partner_type": "provider"},
    )
    
    distributor: models.ForeignKey = models.ForeignKey(
        Partner,
        on_delete=models.SET_NULL,
        related_name="distributor_subscriptions",
        limit_choices_to={"partner_type": "distributor"},
        null=True, 
        blank=True,
    )

    # policy period
    start_date: models.DateField = models.DateField(db_index=True)
    end_date: models.DateField = models.DateField(db_index=True)
    status: models.CharField = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default="pending_document",
        db_index=True
    )

    # financials
    amount_paid: models.DecimalField = models.DecimalField(max_digits=20, decimal_places=2)
    provider_payout: models.DecimalField = models.DecimalField(max_digits=20, decimal_places=2, default=0.00)
    distributor_commission: models.DecimalField = models.DecimalField(max_digits=20, decimal_places=2, default=0.00)
    platform_fee: models.DecimalField = models.DecimalField(max_digits=20, decimal_places=2, default=0.00)

    # payment reference from payment gateway
    payment_reference: models.CharField = models.CharField(max_length=255, unique=True, null=True, blank=True)
    payment_verified: models.BooleanField = models.BooleanField(default=False)

    auto_charge_enabled = models.BooleanField(default=False)
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.customer.user.email} — {self.plan.name} ({self.status})"


REQUIRED_DOCUMENTS = {
    "motor": {
        "third_party": [
            "vehicle_license",
            "proof_of_ownership",
            "vehicle_registration",
        ],
        "third_party_fire_theft": [
            "vehicle_license",
            "proof_of_ownership",
            "vehicle_registration",
            "theft_declaration",
        ],
        "comprehensive": [
            "vehicle_license",
            "proof_of_ownership",
            "vehicle_registration",
            "inspection_report",
            "vehicle_photos",
        ],
    },
    "travel": {
        "standard": [
            "passport",
            "flight_tickets",
            "travel_itinerary",
        ],
    },
}

# Stores plan-specific documents per subscription
class SubscriptionDocument(models.Model):
    DOCUMENT_TYPE_CHOICES = [
        # motor documents
        ("vehicle_license", "Vehicle License"),
        ("proof_of_ownership", "Proof of Ownership"),
        ("vehicle_registration", "Vehicle Registration"),
        ("inspection_report", "Inspection Report"),
        ("vehicle_photos", "Vehicle Photographs"),
        ("theft_declaration", "Theft Declaration"),
        # travel documents
        ("passport", "International Passport"),
        ("flight_tickets", "Flight Tickets"),
        ("travel_itinerary", "Travel Itinerary"),
        ("insurance_certificate", "Insurance Certificate"),
        # shared
        ("other", "Other"),
    ]

    id: models.UUIDField = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription: models.ForeignKey = models.ForeignKey(
        PolicySubscription,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    document_type: models.CharField = models.CharField(max_length=50, choices=DOCUMENT_TYPE_CHOICES)
    file: models.FileField = models.FileField(
        upload_to="subscriptions/documents/",
        storage=KYCDocumentStorage(),
    )
    uploaded_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["subscription", "document_type"]

    def __str__(self):
        return f"{self.subscription} — {self.document_type}"


class NombaTokenStore(models.Model):
    """
    Stores the tokenized card key returned by Nomba after a successful
    checkout payment. Only created when the customer explicitly consented
    to automated subscription charges.
    """
    
    policy = models.OneToOneField(
        'PolicySubscription',
        on_delete=models.CASCADE,
        related_name='nomba_token'
    )
    token_key = models.CharField(max_length=255)
    card_type = models.CharField(max_length=50, blank=True)
    card_pan  = models.CharField(max_length=50, blank=True)

    customer_consented_to_auto_charge = models.BooleanField(default=False)
    consent_recorded_at = models.DateTimeField(null=True, blank=True)

    nomba_order_reference = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Nomba Token Store"

    def __str__(self):
        return f"Token for Policy {self.policy_id} — {self.card_pan or 'no pan'}"
