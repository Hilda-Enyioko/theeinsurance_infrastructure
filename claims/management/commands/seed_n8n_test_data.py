import uuid
from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser, CustomerProfile
from core.models import Partner
from plans.models import InsurancePlan
from subscriptions.models import PolicySubscription
from claims.models import Claim, ClaimDocument, REQUIRED_CLAIM_DOCUMENTS


class Command(BaseCommand):
    help = "Seed an unreviewed, un-forwarded claim submission for n8n workflow testing."

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding raw submitted claim for n8n workflow tests..."))

        provider_id = uuid.UUID("4363d1f2-89ef-453a-9122-6ab1c923c3de")
        plan_id = uuid.UUID("5e2e9e16-863e-4df8-bab8-e5373c7c1b9a")
        customer_email = "customer@test.com"

        with transaction.atomic():
            # 1. Verify provider & plan
            provider = Partner.objects.filter(id=provider_id, partner_type="provider").first()
            if not provider:
                # Fallback to provider_id without partner_type filter if seed differed
                provider = Partner.objects.filter(id=provider_id).first()
                if not provider:
                    self.stderr.write(self.style.ERROR(f"Provider {provider_id} not found. Ensure base seed ran."))
                    return

            plan = InsurancePlan.objects.filter(id=plan_id).first()
            if not plan:
                self.stderr.write(self.style.ERROR(f"Plan {plan_id} not found. Ensure base seed ran."))
                return

            # 2. Customer user & profile
            user, _ = CustomUser.objects.get_or_create(
                email=customer_email,
                defaults={
                    "first_name": "Tolu",
                    "last_name": "Adeyemi",
                    "role": "customer",
                },
            )
            if not user.has_usable_password():
                user.set_password("Customer@2026")
                user.save()

            customer_profile, _ = CustomerProfile.objects.get_or_create(
                user=user,
                partner=provider,
                defaults={
                    "phone_number": "08012345678",
                    "date_of_birth": "1995-06-15",
                    "gender": "female",
                    "address": "12 Broad Street, Lagos Island, Lagos",
                    "settlement_account_name": "Tolu Adeyemi",
                    "settlement_bank_account": "0123456789",
                    "settlement_bank_code": "058",
                },
            )

            if not customer_profile.settlement_account_name:
                customer_profile.settlement_account_name = "Tolu Adeyemi"
                customer_profile.settlement_bank_account = "0123456789"
                customer_profile.settlement_bank_code = "058"
                customer_profile.save()

            # 3. Active policy subscription
            now = timezone.now()
            subscription, _ = PolicySubscription.objects.get_or_create(
                customer=customer_profile,
                plan=plan,
                provider=provider,
                defaults={
                    "status": "active",
                    "start_date": now - timedelta(days=30),
                    "end_date": now + timedelta(days=335),
                    "amount_paid": Decimal("85000.00"),
                },
            )

            # 4. Create fresh, raw Claim (Unchecked & Not Forwarded)
            # Use "submitted" (or "ai_check_pending" if n8n polls specifically for ai_check_pending)
            claim = Claim.objects.create(
                subscription=subscription,
                customer=customer_profile,
                provider=provider,
                claim_type="motor_accident",
                status="submitted",
                incident_date=(now - timedelta(days=2)).date(),
                incident_description="Front bumper damage from a minor collision along Lekki Expressway.",
                claimed_amount=Decimal("175000.00"),
                approved_amount=None,
                forwarded_at=None,
                ai_result=None,
                parties_notified_at=None,
                theeinsurance_review_note="",
                reviewed_by_theeinsurance=None,
                provider_review_note="",
                reviewed_by_provider=None,
            )

            # 5. Attach mandatory documents using direct .file.name assignment (bypasses Cloudinary API)
            required_docs = REQUIRED_CLAIM_DOCUMENTS.get("motor_accident", [])
            for doc_type in required_docs:
                doc = ClaimDocument.objects.create(
                    claim=claim,
                    document_type=doc_type,
                )
                doc.file.name = f"claims/documents/{claim.claim_reference}_{doc_type}.pdf"
                doc.save(update_fields=["file"])

            self.stdout.write(self.style.SUCCESS(
                f"\n--- Fresh Test Claim Created for n8n ---"
                f"\nClaim ID:         {claim.id}"
                f"\nClaim Ref:        {claim.claim_reference}"
                f"\nStatus:           {claim.status}"
                f"\nForwarded At:     {claim.forwarded_at} (None -> provider cannot see yet)"
                f"\nClaimed Amount:   ₦{claim.claimed_amount}"
                f"\nApproved Amount:  {claim.approved_amount}"
                f"\nDocuments:        {claim.documents.count()}/{len(required_docs)} attached"
                f"\nCustomer:         {customer_profile.user.email}"
                f"\nProvider:         {provider.name if hasattr(provider, 'name') else provider.id}"
                f"\n----------------------------------------\n"
            ))
