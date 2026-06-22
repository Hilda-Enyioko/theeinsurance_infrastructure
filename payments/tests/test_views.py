"""
Payments View Tests.

Integration tests for all three payment endpoints:
  POST /payments/initiate/
  GET  /payments/callback/
  POST /payments/webhook/

Uses APIClient for full request/response cycle testing.
Interswitch API calls are mocked throughout.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model

from payments.models import Transaction
from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile
from core.models import Partner
from plans.models import InsuranceCategory, InsurancePlan

User = get_user_model()

def make_partner(partner_type="provider", slug=None):
    """Create a Partner for use in tests."""
    import uuid as _uuid
    slug = slug or f"partner-{_uuid.uuid4().hex[:8]}"
    return Partner.objects.create(
        name=f"Test Partner {slug}",
        slug=slug,
        partner_type=partner_type,
    )


def make_plan(provider):
    """Create a minimal InsurancePlan for use in tests."""
    category, _ = InsuranceCategory.objects.get_or_create(name="motor")
    return InsurancePlan.objects.create(
        provider=provider,
        category=category,
        name="Test Motor Plan",
        coverage_level="third_party",
        coverage_amount="500000.00",
        premium="5000.00",
        duration_months=12,
        description="Test plan",
        is_active=True,
    )

def make_subscription(customer, provider, plan, **kwargs):
    """Create a minimal PolicySubscription."""
    defaults = dict(
        customer=customer,
        plan=plan,
        provider=provider,
        start_date="2026-01-01",
        end_date="2027-01-01",
        amount_paid="5000.00",
        status="pending_payment",
    )
    defaults.update(kwargs)
    return PolicySubscription.objects.create(**defaults)


def make_signature(payload_bytes: bytes) -> str:
    return hmac.new(
        key=settings.INTERSWITCH_CLIENT_SECRET.encode(),
        msg=payload_bytes,
        digestmod=hashlib.sha512,
    ).hexdigest()


class InitiatePaymentViewTests(TestCase):
    """Tests for POST /payments/initiate/"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-model-1")
        self.plan = make_plan(self.provider)
        self.customer = CustomerProfile.objects.create(
            user=self.user,
            partner=self.provider,
            phone_number="08012345678",
            date_of_birth="1995-01-01",
            gender="female",
            address="123 Test Street",
        )
        self.subscription = make_subscription(
            customer=self.customer,
            provider=self.provider,
            plan=self.plan,
        )
        self.url = reverse("payments:initiate")
        self.valid_payload = {
            "subscription_id": str(self.subscription.id),
            "payment_type": "NEW_SUBSCRIPTION",
        }

    # --- Test 41 ---
    def test_unauthenticated_request_returns_401(self):
        response = self.client.post(self.url, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # --- Test 42 ---
    @patch("payments.views.initiate_payment")
    def test_valid_request_returns_200_with_payment_url(self, mock_initiate):
        mock_initiate.return_value = {
            "payment_url": "https://pay.interswitchng.com/pay/abc123",
            "reference": "TII-AABBCCDDAABBCCDD",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, self.valid_payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("payment_url", response.data)
        self.assertIn("reference", response.data)

    # --- Test 43 ---
    def test_invalid_subscription_returns_400(self):
        import uuid
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {
            "subscription_id": str(uuid.uuid4()),
            "payment_type": "NEW_SUBSCRIPTION",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Test 44 ---
    @patch("payments.views.initiate_payment")
    def test_gateway_error_returns_502(self, mock_initiate):
        from payments.services import PaymentError
        mock_initiate.side_effect = PaymentError("Gateway timed out.")

        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, self.valid_payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("detail", response.data)


class PaymentCallbackViewTests(TestCase):
    """Tests for GET /payments/callback/"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-model-1")
        self.plan = make_plan(self.provider)
        self.customer = CustomerProfile.objects.create(
            user=self.user,
            partner=self.provider,
            phone_number="08012345678",
            date_of_birth="1995-01-01",
            gender="female",
            address="123 Test Street",
        )
        self.subscription = make_subscription(
            customer=self.customer,
            provider=self.provider,
            plan=self.plan,
        )
        self.txn = Transaction.objects.create(
            subscription=self.subscription,
            initiated_by=self.user,
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )
        self.url = reverse("payments:callback")

    # --- Test 45 ---
    def test_unauthenticated_request_returns_401(self):
        response = self.client.get(self.url, {"ref": self.txn.reference})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # --- Test 46 ---
    def test_missing_ref_param_returns_400(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    # --- Test 47 ---
    def test_unknown_ref_returns_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"ref": "TII-DOESNOTEXIST0000"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --- Test 48 ---
    @patch("payments.views.verify_transaction")
    def test_pending_transaction_triggers_verification_and_updates_status(self, mock_verify):
        mock_verify.return_value = {
            "responseCode": "00",
            "retrievalReferenceNumber": "RRN123456789",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"ref": self.txn.reference})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.SUCCESSFUL)
        mock_verify.assert_called_once_with(self.txn.reference)

    # --- Test 49 ---
    @patch("payments.views.verify_transaction")
    def test_already_resolved_transaction_skips_verification(self, mock_verify):
        self.txn.payment_status = Transaction.PAYMENT_STATUS.SUCCESSFUL
        self.txn.save()

        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"ref": self.txn.reference})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_verify.assert_not_called()

    # --- Test 50 ---
    @patch("payments.views.verify_transaction")
    def test_gateway_verification_failure_returns_current_state(self, mock_verify):
        from payments.services import PaymentError
        mock_verify.side_effect = PaymentError("Gateway timeout")

        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url, {"ref": self.txn.reference})

        # Should not crash — returns current DB state
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment_status"], "PENDING")


class PaymentWebhookViewTests(TestCase):
    """Tests for POST /payments/webhook/"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-model-1")
        self.plan = make_plan(self.provider)
        self.customer = CustomerProfile.objects.create(
            user=self.user,
            partner=self.provider,
            phone_number="08012345678",
            date_of_birth="1995-01-01",
            gender="female",
            address="123 Test Street",
        )
        self.subscription = make_subscription(
            customer=self.customer,
            provider=self.provider,
            plan=self.plan,
        )
        self.txn = Transaction.objects.create(
            subscription=self.subscription,
            initiated_by=self.user,
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )
        self.url = reverse("payments:webhook")

    def _post_webhook(self, payload: dict, signature: str = None):
        raw = json.dumps(payload).encode()
        if signature is None:
            signature = make_signature(raw)
        return self.client.post(
            self.url,
            data=raw,
            content_type="application/json",
            HTTP_X_INTERSWITCH_SIGNATURE=signature,
        )

    # --- Test 51 ---
    def test_missing_signature_header_returns_400(self):
        response = self.client.post(
            self.url,
            data=json.dumps({}),
            content_type="application/json",
            # no HTTP_X_INTERSWITCH_SIGNATURE header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Test 52 ---
    def test_invalid_signature_returns_401(self):
        payload = {
            "transactionReference": self.txn.reference,
            "responseCode": "00",
            "amount": 500000,
            "retrievalReferenceNumber": "RRN123",
        }
        response = self._post_webhook(payload, signature="badsignature")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # --- Test 53 ---
    @patch("payments.views.process_webhook")
    def test_valid_successful_webhook_returns_200(self, mock_process):
        mock_process.return_value = MagicMock()
        payload = {
            "transactionReference": self.txn.reference,
            "responseCode": "00",
            "amount": 500000,
            "retrievalReferenceNumber": "RRN123",
        }
        response = self._post_webhook(payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["detail"], "Received.")

    # --- Test 54 ---
    @patch("payments.views.process_webhook")
    def test_valid_failed_webhook_returns_200(self, mock_process):
        mock_process.return_value = MagicMock()
        payload = {
            "transactionReference": self.txn.reference,
            "responseCode": "Z6",
            "amount": 500000,
            "retrievalReferenceNumber": "RRN123",
        }
        response = self._post_webhook(payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- Test 55 ---
    @patch("payments.views.process_webhook")
    def test_duplicate_webhook_still_returns_200(self, mock_process):
        """Interswitch must not be given a reason to retry."""
        mock_process.return_value = MagicMock()
        payload = {
            "transactionReference": self.txn.reference,
            "responseCode": "00",
            "amount": 500000,
            "retrievalReferenceNumber": "RRN123",
        }
        # First call
        self._post_webhook(payload)
        # Duplicate
        response = self._post_webhook(payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- Test 56 ---
    def test_webhook_endpoint_requires_no_authentication(self):
        """Webhook must be publicly accessible — Interswitch sends no JWT."""
        payload = {
            "transactionReference": "TII-DOESNOTEXIST0000",
            "responseCode": "00",
            "amount": 500000,
            "retrievalReferenceNumber": "RRN123",
        }
        # No force_authenticate — unauthenticated client
        response = self._post_webhook(payload)
        # Should NOT return 401/403 — signature is the auth mechanism
        self.assertNotIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])
