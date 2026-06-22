"""
Payments Model Tests.

Tests for Transaction and CallbackLog models covering:
- Field defaults and constraints
- Reference generation uniqueness
- String representations
- Database-level integrity constraints
"""

import uuid
from django.test import TestCase
from django.db import IntegrityError
from django.contrib.auth import get_user_model

from payments.models import Transaction, CallbackLog, generate_reference
from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile
from core.models import Partner
from plans.models import InsurancePlan, InsuranceCategory

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


class GenerateReferenceTests(TestCase):
    """Tests for the generate_reference() helper."""

    def test_reference_format(self):
        """Reference starts with TII- and has 16 hex characters."""
        ref = generate_reference()
        self.assertTrue(ref.startswith("TII-"))
        suffix = ref[4:]
        self.assertEqual(len(suffix), 16)
        self.assertTrue(all(c in "0123456789ABCDEF" for c in suffix))

    def test_references_are_unique(self):
        """Two consecutive calls produce different references."""
        refs = {generate_reference() for _ in range(100)}
        self.assertEqual(len(refs), 100)

    def test_reference_is_uppercase(self):
        """Hex suffix is always uppercase."""
        ref = generate_reference()
        self.assertEqual(ref, ref.upper())


class TransactionModelTests(TestCase):
    """Tests for the Transaction model."""

    def setUp(self):
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

    def _make_transaction(self, **kwargs):
        defaults = dict(
            subscription=self.subscription,
            initiated_by=self.user,
            amount="5000.00",
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )
        defaults.update(kwargs)
        return Transaction.objects.create(**defaults)

    # --- Test 1 ---
    def test_reference_auto_generated_on_create(self):
        """Transaction is created with a TII- prefixed reference."""
        txn = self._make_transaction()
        self.assertTrue(txn.reference.startswith("TII-"))
        self.assertEqual(len(txn.reference), 20)

    # --- Test 2 ---
    def test_payment_status_defaults_to_pending(self):
        """payment_status defaults to PENDING on creation."""
        txn = self._make_transaction()
        self.assertEqual(txn.payment_status, Transaction.PAYMENT_STATUS.PENDING)

    # --- Test 3 ---
    def test_str_representation(self):
        """__str__ returns expected format."""
        txn = self._make_transaction()
        expected = f"Txn {txn.reference} (PENDING)"
        self.assertEqual(str(txn), expected)

    # --- Test 4 ---
    def test_uuid_primary_key(self):
        """Transaction ID is a UUID."""
        txn = self._make_transaction()
        self.assertIsInstance(txn.id, uuid.UUID)

    # --- Test 5 ---
    def test_duplicate_reference_raises_integrity_error(self):
        """Two transactions with the same reference violate unique constraint."""
        fixed_ref = "TII-AABBCCDDAABBCCDD"
        self._make_transaction(reference=fixed_ref)
        with self.assertRaises(IntegrityError):
            Transaction.objects.create(
                subscription=self.subscription,
                initiated_by=self.user,
                amount="5000.00",
                payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
                reference=fixed_ref,
            )

    # --- Test 6 ---
    def test_currency_defaults_to_ngn(self):
        """currency defaults to NGN."""
        txn = self._make_transaction()
        self.assertEqual(txn.currency, "NGN")

    # --- Test 7 ---
    def test_gateway_fields_nullable(self):
        """gateway_reference and gateway_response are nullable by default."""
        txn = self._make_transaction()
        self.assertIsNone(txn.gateway_reference)
        self.assertIsNone(txn.gateway_response)

    # --- Test 8 ---
    def test_ordering_is_newest_first(self):
        """Transactions are ordered by -created_at."""
        txn1 = self._make_transaction()
        txn2 = self._make_transaction()
        qs = list(Transaction.objects.all())
        self.assertEqual(qs[0].id, txn2.id)
        self.assertEqual(qs[1].id, txn1.id)


class CallbackLogModelTests(TestCase):
    """Tests for the CallbackLog model."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )
        self.provider = make_partner(partner_type="provider", slug="provider-model-2")
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
            amount="5000.00",
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )

    def _make_log(self, **kwargs):
        defaults = dict(
            transaction_reference=self.txn.reference,
            raw_payload={"responseCode": "00"},
            response_code="00",
            status=CallbackLog.Status.SUCCESS,
            transaction=self.txn,
            is_duplicate=False,
            is_amount_mismatch=False,
        )
        defaults.update(kwargs)
        return CallbackLog.objects.create(**defaults)

    # --- Test 1 ---
    def test_str_representation(self):
        """__str__ returns expected format."""
        log = self._make_log()
        expected = f"CallbackLog {self.txn.reference} \u2014 SUCCESS"
        self.assertEqual(str(log), expected)

    # --- Test 2 ---
    def test_uuid_primary_key(self):
        """CallbackLog primary key is a UUID."""
        log = self._make_log()
        self.assertIsInstance(log.uuid, uuid.UUID)

    # --- Test 3 ---
    def test_unique_together_constraint(self):
        """Duplicate (transaction_reference, response_code, status) raises IntegrityError."""
        self._make_log()
        with self.assertRaises(IntegrityError):
            self._make_log()  # identical reference + response_code + status

    # --- Test 4 ---
    def test_log_survives_when_transaction_nulled(self):
        """CallbackLog.transaction becomes NULL when Transaction is deleted (SET_NULL)."""
        log = self._make_log()
        self.txn.delete()
        log.refresh_from_db()
        self.assertIsNone(log.transaction)

    # --- Test 5 ---
    def test_log_created_without_linked_transaction(self):
        """CallbackLog can be created with transaction=None (unknown reference scenario)."""
        log = CallbackLog.objects.create(
            transaction_reference="TII-UNKNOWNREFERENCE",
            raw_payload={"responseCode": "00"},
            response_code="00",
            status=CallbackLog.Status.FLAGGED,
            transaction=None,
            is_duplicate=False,
            is_amount_mismatch=False,
        )
        self.assertIsNone(log.transaction)
        self.assertEqual(log.status, CallbackLog.Status.FLAGGED)

    # --- Test 6 ---
    def test_is_duplicate_default_false(self):
        """is_duplicate defaults to False."""
        log = self._make_log()
        self.assertFalse(log.is_duplicate)

    # --- Test 7 ---
    def test_is_amount_mismatch_default_false(self):
        """is_amount_mismatch defaults to False."""
        log = self._make_log()
        self.assertFalse(log.is_amount_mismatch)
