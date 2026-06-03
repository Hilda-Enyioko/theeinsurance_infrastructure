import uuid
import secrets
from django.db import models
from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile, PartnerAdmin
from core.models import Partner

def generate_claim_reference():
    return f"CLM-{secrets.token_hex(8).upper()}"

class Claim(models.Model):
    CLAIM_TYPE_CHOICES = [
        ("motor_accident", "Motor Accident"),
        ("motor_theft", "Motor Theft"),
        ("travel_medical", "Travel Medical Emergency"),
        ("travel_baggage", "Travel Lost/Damaged Baggage"),
        ("travel_cancellation", "Travel Flight Cancellation"), 
    ]
    
    STATUS_CHOICES = [
        ("submitted", "Submitted"),
        ("under_review", "Under Review"),
        ("more_info_required", "More Information Required"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claim_reference = models.CharField(
        max_length=20, unique=True, editable=False
    )
    subscription = models.ForeignKey(
        PolicySubscription, 
        on_delete=models.CASCADE, 
        related_name="claims"
    )
    customer = models.ForeignKey(
        CustomerProfile,
        on_delete=models.CASCADE,
        related_name="claims"
    )
    provider = models.ForeignKey(
        Partner,
        on_delete=models.CASCADE,
        related_name="claims",
        limit_choices_to={'partner_type': 'provider'}
    )
    claim_type = models.CharField(max_length=50, choices=CLAIM_TYPE_CHOICES)
    incident_date = models.DateField()
    incident_description = models.TextField()
    
    claimed_amount = models.DecimalField(max_digits=10, decimal_places=2)
    approved_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="submitted")
    
    # TheeInsurance Internal Review
    theeinsurance_review_note = models.TextField(blank=True)
    reviewed_by_theeinsurance = models.ForeignKey(
        "accounts.CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="theeinsurance_reviewed_claims",
        limit_choices_to={"role": "super_admin"},
    )
    
    # Provider Review
    provider_review_note = models.TextField(blank=True)
    reviewed_by_provider = models.ForeignKey(
        PartnerAdmin,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_reviewed_claims",
    )
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def save(self, *args, **kwargs):
        if not self.claim_reference:
            self.claim_reference = generate_claim_reference()
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.claim_reference} - {self.customer.user.email} ({self.status()})"


class ClaimDocument(models.Model):
    DOCUMENT_TYPE_CHOICES = [
                # motor
        ("claim_form", "Completed Claim Form"),
        ("police_report", "Police Report"),
        ("drivers_licence", "Driver's Licence"),
        ("damage_photos", "Damage Photographs"),
        ("repair_estimate", "Repair Estimate from Garage"),
        ("vehicle_particulars", "Vehicle Particulars"),
        # travel
        ("medical_report", "Medical Report"),
        ("hospital_bills", "Hospital Bills and Receipts"),
        ("property_irregularity_report", "Property Irregularity Report"),
        ("flight_documents", "Flight Tickets and Boarding Pass"),
        ("travel_insurance_certificate", "Travel Insurance Certificate"),
        # shared
        ("other", "Other Supporting Document"),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claim = models.ForeignKey(
        Claim, 
        on_delete=models.CASCADE, 
        related_name="documents"
    )
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPE_CHOICES)
    file = models.FileField(upload_to="claims/documents/")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.claim.claim_reference} - {self.document_type}"


# Define required documents dictionary for each claim type
REQUIRED_CLAIM_DOCUMENTS = {
    "motor_accident": [
        "claim_form",
        "police_report",
        "drivers_licence",
        "damage_photos",
        "repair_estimate",
        "vehicle_particulars",
    ],
    "motor_theft": [
        "claim_form",
        "police_report",
        "drivers_licence",
        "vehicle_particulars",
    ],
    "travel_medical": [
        "claim_form",
        "medical_report",
        "hospital_bills",
        "travel_insurance_certificate",
    ],
    "travel_baggage": [
        "claim_form",
        "property_irregularity_report",
        "flight_documents",
        "travel_insurance_certificate",
    ],
    "travel_cancellation": [
        "claim_form",
        "flight_documents",
        "travel_insurance_certificate",
    ],
}