"""
Payments Service Tests.

Tests for all service functions covering:
- Helper utilities (_kobo, _build_redirect_url)
- Webhook signature verification
- Payment initiation (mocked Interswitch calls)
- Transaction verification (mocked Interswitch calls)
- Webhook processing (full lifecycle)
- Subscription activation side effect
- n8n webhook firing side effect
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase
from django.contrib.auth import get_user_model

from payments.models import CallbackLog, Transaction
from payments.services import (
    PaymentError,
    SignatureVerificationError,
    _build_redirect_url,
    _kobo,
    initiate_payment,
    process_webhook,
    verify_transaction,
    verify_webhook_signature,
    _activate_subscription,
)
from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile
from core.models import Partner
from plans.models import InsurancePlan, InsuranceCategory

User = get_user_model()


def make_partner(partner_type="provider", slug=None):
    import uuid as _uuid
    slug = slug or f"partner-{_uuid.uuid4().hex[:8]}"
    return Partner.objects.create(
        name=f"Test Partner {slug}",
        slug=slug,
        partner_type=partner_type,
    )


def make_plan(provider):
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


def make_customer(user, partner):
    return CustomerProfile.objects.create(
        user=user,
        partner=partner,
        phone_number="08012345678",
        date_of_birth="1995-01-01",
        gender="female",
        address="123 Test Street",
    )


def make_subscription(customer, provider, plan, **kwargs):
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
    """Helper: generate a valid HMAC-SHA512 signature for test payloads."""
    return hmac.new(
        key=settings.INTERSWITCH_CLIENT_SECRET.encode(),
        msg=payload_bytes,
        digestmod=hashlib.sha512,
    ).hexdigest()


def make_webhook_payload(
    reference: str,
    amount_kobo: int = 500000,
    response_code: str = "00",
    gateway_ref: str = "RRN123456789",
) -> dict:
    return {
        "transactionReference": reference,
        "responseCode": response_code,
        "amount": amount_kobo,
        "retrievalReferenceNumber": gateway_ref,
    }


class KoboConversionTests(TestCase):
    """Tests for _kobo() helper."""

    # --- Test 9 ---
    def test_kobo_converts_whole_naira(self):
        self.assertEqual(_kobo(Decimal("5000.00")), 500000)

    def test_kobo_converts_fractional_naira(self):
        self.assertEqual(_kobo(Decimal("100.50")), 10050)

    def test_kobo_converts_one_naira(self):
        self.assertEqual(_kobo(Decimal("1.00")), 100)

    def test_kobo_returns_int(self):
        result = _kobo(Decimal("250.00"))
        self.assertIsInstance(result, int)


class BuildRedirectUrlTests(TestCase):
    """Tests for _build_redirect_url() helper."""

    # --- Test 10 ---
    def test_redirect_url_contains_reference(self):
        txn = MagicMock()
        txn.reference = "TII-AABBCCDDAABBCCDD"
        with self.settings(INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback"):
            url = _build_redirect_url(txn)
        self.assertIn("ref=TII-AABBCCDDAABBCCDD", url)

    def test_redirect_url_base_is_correct(self):
        txn = MagicMock()
        txn.reference = "TII-AABBCCDDAABBCCDD"
        with self.settings(INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback"):
            url = _build_redirect_url(txn)
        self.assertTrue(url.startswith("https://app.theeinsurance.com/payments/callback"))


class VerifyWebhookSignatureTests(TestCase):
    """Tests for verify_webhook_signature()."""

    # --- Test 11 ---
    def test_valid_signature_returns_true(self):
        payload = b'{"transactionReference": "TII-TEST"}'
        with self.settings(INTERSWITCH_CLIENT_SECRET="testsecret"):
            sig = hmac.new(b"testsecret", payload, hashlib.sha512).hexdigest()
            result = verify_webhook_signature(payload, sig)
        self.assertTrue(result)

    # --- Test 12 ---
    def test_tampered_payload_raises_error(self):
        payload = b'{"transactionReference": "TII-TEST"}'
        sig = hmac.new(b"testsecret", payload, hashlib.sha512).hexdigest()
        tampered = b'{"transactionReference": "TII-FAKE"}'
        with self.settings(INTERSWITCH_CLIENT_SECRET="testsecret"):
            with self.assertRaises(SignatureVerificationError):
                verify_webhook_signature(tampered, sig)

    # --- Test 13 ---
    def test_wrong_secret_raises_error(self):
        payload = b'{"transactionReference": "TII-TEST"}'
        sig = hmac.new(b"wrongsecret", payload, hashlib.sha512).hexdigest()
        with self.settings(INTERSWITCH_CLIENT_SECRET="testsecret"):
            with self.assertRaises(SignatureVerificationError):
                verify_webhook_signature(payload, sig)


class InitiatePaymentTests(TestCase):
    """Tests for initiate_payment()."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="hilda@theeinsurance.com",
            password="testpass123",
            first_name="Hilda",
            last_name="Enyioko",
        )
        # Use a MagicMock transaction — no DB subscription needed for these tests
        self.txn = MagicMock(spec=Transaction)
        self.txn.reference = "TII-AABBCCDDAABBCCDD"
        self.txn.amount = Decimal("5000.00")
        self.txn.currency = "NGN"
        self.txn.initiated_by = self.user
        self.txn.subscription_id = "some-uuid"

    # --- Test 14 ---
    @patch("payments.services.requests.post")
    def test_initiate_calls_interswitch_with_correct_payload(self, mock_post):
        mock_post.return_value.json.return_value = {"paymentUrl": "https://pay.interswitchng.com/pay"}
        mock_post.return_value.raise_for_status = MagicMock()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_MERCHANT_CODE="MX12345",
            INTERSWITCH_PAYABLE_CODE="9405967",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback",
        ):
            initiate_payment(self.txn)

        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"]
        self.assertEqual(payload["transactionReference"], "TII-AABBCCDDAABBCCDD")
        self.assertEqual(payload["amount"], 500000)  # kobo
        self.assertEqual(payload["currencyCode"], "566")

    # --- Test 15 ---
    @patch("payments.services.requests.post")
    def test_initiate_returns_payment_url_and_reference(self, mock_post):
        mock_post.return_value.json.return_value = {"paymentUrl": "https://pay.interswitchng.com/pay"}
        mock_post.return_value.raise_for_status = MagicMock()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_MERCHANT_CODE="MX12345",
            INTERSWITCH_PAYABLE_CODE="9405967",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback",
        ):
            result = initiate_payment(self.txn)

        self.assertIn("payment_url", result)
        self.assertIn("reference", result)
        self.assertEqual(result["reference"], "TII-AABBCCDDAABBCCDD")

    # --- Test 16 ---
    @patch("payments.services.requests.post")
    def test_initiate_raises_on_timeout(self, mock_post):
        import requests as req
        mock_post.side_effect = req.Timeout()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_MERCHANT_CODE="MX12345",
            INTERSWITCH_PAYABLE_CODE="9405967",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback",
        ):
            with self.assertRaises(PaymentError) as ctx:
                initiate_payment(self.txn)
        self.assertIn("timed out", str(ctx.exception))

    # --- Test 17 ---
    @patch("payments.services.requests.post")
    def test_initiate_raises_when_no_payment_url_returned(self, mock_post):
        mock_post.return_value.json.return_value = {"message": "ok"}  # no paymentUrl
        mock_post.return_value.raise_for_status = MagicMock()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_MERCHANT_CODE="MX12345",
            INTERSWITCH_PAYABLE_CODE="9405967",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback",
        ):
            with self.assertRaises(PaymentError) as ctx:
                initiate_payment(self.txn)
        self.assertIn("payment URL", str(ctx.exception))

    # --- Test 18 ---
    @patch("payments.services.requests.post")
    def test_initiate_raises_on_non_2xx_response(self, mock_post):
        import requests as req
        mock_post.return_value.raise_for_status.side_effect = req.HTTPError("500 Server Error")

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_MERCHANT_CODE="MX12345",
            INTERSWITCH_PAYABLE_CODE="9405967",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_REDIRECT_URL="https://app.theeinsurance.com/payments/callback",
        ):
            with self.assertRaises(PaymentError):
                initiate_payment(self.txn)


class VerifyTransactionTests(TestCase):
    """Tests for verify_transaction()."""

    # --- Test 19 ---
    @patch("payments.services.requests.get")
    def test_verify_returns_gateway_response(self, mock_get):
        mock_get.return_value.json.return_value = {"responseCode": "00", "amount": 500000}
        mock_get.return_value.raise_for_status = MagicMock()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_CLIENT_SECRET="secret",
        ):
            result = verify_transaction("TII-AABBCCDDAABBCCDD")

        self.assertEqual(result["responseCode"], "00")

    # --- Test 20 ---
    @patch("payments.services.requests.get")
    def test_verify_raises_on_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.Timeout()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_CLIENT_SECRET="secret",
        ):
            with self.assertRaises(PaymentError) as ctx:
                verify_transaction("TII-AABBCCDDAABBCCDD")
        self.assertIn("timed out", str(ctx.exception).lower())

    # --- Test 21 ---
    @patch("payments.services.requests.get")
    def test_verify_raises_on_request_failure(self, mock_get):
        import requests as req
        mock_get.side_effect = req.ConnectionError()

        with self.settings(
            INTERSWITCH_BASE_URL="https://sandbox.interswitchng.com",
            INTERSWITCH_CLIENT_ID="IKIA1234",
            INTERSWITCH_CLIENT_SECRET="secret",
        ):
            with self.assertRaises(PaymentError):
                verify_transaction("TII-AABBCCDDAABBCCDD")


class ProcessWebhookTests(TestCase):
    """Tests for process_webhook() — full lifecycle."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="hilda@theeinsurance.com",
            password="testpass123",
            first_name="Hilda",
            last_name="Enyioko",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-svc-1")
        self.plan = make_plan(self.provider)
        self.customer = make_customer(self.user, self.provider)
        self.subscription = make_subscription(self.customer, self.provider, self.plan)
        self.txn = Transaction.objects.create(
            subscription=self.subscription,
            initiated_by=self.user,
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )

    def _make_raw_and_sig(self, payload: dict) -> tuple:
        raw = json.dumps(payload).encode()
        sig = hmac.new(
            settings.INTERSWITCH_CLIENT_SECRET.encode(),
            raw,
            hashlib.sha512,
        ).hexdigest()
        return raw, sig

    # --- Test 22 ---
    def test_successful_webhook_updates_transaction_and_activates_subscription(self):
        payload = make_webhook_payload(self.txn.reference, amount_kobo=500000)
        raw, sig = self._make_raw_and_sig(payload)

        log = process_webhook(payload, raw, sig)

        self.txn.refresh_from_db()
        self.subscription.refresh_from_db()

        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.SUCCESSFUL)
        self.assertEqual(log.status, CallbackLog.Status.SUCCESS)
        self.assertEqual(self.subscription.status, "active")
        self.assertTrue(self.subscription.payment_verified)

    # --- Test 23 ---
    def test_failed_payment_updates_transaction_does_not_activate(self):
        payload = make_webhook_payload(self.txn.reference, response_code="Z6")
        raw, sig = self._make_raw_and_sig(payload)

        log = process_webhook(payload, raw, sig)

        self.txn.refresh_from_db()
        self.subscription.refresh_from_db()

        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.FAILED)
        self.assertEqual(log.status, CallbackLog.Status.FAILED)
        self.assertNotEqual(self.subscription.status, "active")

    # --- Test 24 ---
    def test_duplicate_webhook_is_flagged_transaction_unchanged(self):
        # First webhook — success
        payload = make_webhook_payload(self.txn.reference)
        raw, sig = self._make_raw_and_sig(payload)
        process_webhook(payload, raw, sig)

        # Second webhook — same ref, different response_code to avoid unique_together clash
        payload2 = make_webhook_payload(self.txn.reference, response_code="01")
        raw2, sig2 = self._make_raw_and_sig(payload2)
        log = process_webhook(payload2, raw2, sig2)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.SUCCESSFUL)
        self.assertTrue(log.is_duplicate)
        self.assertEqual(log.status, CallbackLog.Status.FLAGGED)

    # --- Test 25 ---
    def test_amount_mismatch_is_flagged_transaction_unchanged(self):
        payload = make_webhook_payload(
            self.txn.reference,
            amount_kobo=100,  # ₦1 instead of ₦5000
        )
        raw, sig = self._make_raw_and_sig(payload)
        log = process_webhook(payload, raw, sig)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.PENDING)
        self.assertTrue(log.is_amount_mismatch)
        self.assertEqual(log.status, CallbackLog.Status.FLAGGED)

    # --- Test 26 ---
    def test_unknown_reference_creates_flagged_log_with_no_transaction(self):
        payload = make_webhook_payload("TII-DOESNOTEXIST0000")
        raw, sig = self._make_raw_and_sig(payload)
        log = process_webhook(payload, raw, sig)

        self.assertIsNone(log.transaction)
        self.assertEqual(log.status, CallbackLog.Status.FLAGGED)
        self.assertEqual(log.transaction_reference, "TII-DOESNOTEXIST0000")

    # --- Test 27 ---
    def test_invalid_signature_raises_and_nothing_written(self):
        payload = make_webhook_payload(self.txn.reference)
        raw = json.dumps(payload).encode()
        bad_sig = "invalidsignature"

        initial_log_count = CallbackLog.objects.count()

        with self.assertRaises(SignatureVerificationError):
            process_webhook(payload, raw, bad_sig)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.PENDING)
        self.assertEqual(CallbackLog.objects.count(), initial_log_count)

    # --- Test 28 ---
    @patch("payments.services.requests.post")
    def test_n8n_failure_does_not_affect_transaction_status(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("n8n is down")

        payload = make_webhook_payload(self.txn.reference)
        raw, sig = self._make_raw_and_sig(payload)

        # Should not raise even though n8n fails
        log = process_webhook(payload, raw, sig)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.SUCCESSFUL)
        self.assertEqual(log.status, CallbackLog.Status.SUCCESS)


class ActivateSubscriptionTests(TestCase):
    """Tests for _activate_subscription() side effect."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="hilda@theeinsurance.com",
            password="testpass123",
            first_name="Hilda",
            last_name="Enyioko",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-svc-2")
        self.plan = make_plan(self.provider)
        self.customer = make_customer(self.user, self.provider)
        self.subscription = make_subscription(self.customer, self.provider, self.plan)
        self.txn = Transaction.objects.create(
            subscription=self.subscription,
            initiated_by=self.user,
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
            gateway_reference="RRN123456789",
        )

    # --- Test 29 ---
    def test_activate_sets_correct_fields(self):
        _activate_subscription(self.txn)
        self.subscription.refresh_from_db()

        self.assertEqual(self.subscription.status, "active")
        self.assertTrue(self.subscription.payment_verified)
        self.assertEqual(self.subscription.payment_reference, "RRN123456789")

    # --- Test 30 ---
    def test_activate_failure_is_swallowed_not_raised(self):
        """Exception inside _activate_subscription must not propagate."""
        with patch.object(
            self.subscription.__class__, "save",
            side_effect=Exception("DB error")
        ):
            try:
                _activate_subscription(self.txn)
            except Exception:
                self.fail("_activate_subscription() raised an exception unexpectedly")
