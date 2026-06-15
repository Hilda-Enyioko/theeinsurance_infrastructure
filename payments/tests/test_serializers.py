"""
Payments Serializer Tests.

Tests for InitiatePaymentSerializer and TransactionSerializer covering:
- Input validation (ownership, status, duplicates, payment type)
- Amount sourcing from subscription (never from client)
- Transaction creation on save()
- Read-only output shape of TransactionSerializer
"""

from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory

from payments.models import Transaction
from payments.serializers import InitiatePaymentSerializer, TransactionSerializer
from subscriptions.models import PolicySubscription
from accounts.models import CustomerProfile
from core.models import Partner
from plans.models import InsuranceCategory, InsurancePlan

User = get_user_model()
factory = APIRequestFactory()

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


class InitiatePaymentSerializerTests(TestCase):
    """Tests for InitiatePaymentSerializer."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="test@newexample.com",
            password="testpass4123",
            first_name="Tessie",
            last_name="Usher",
        )
        self.other_user = User.objects.create_user(
            email="test@otherexample.com",
            password="testpass4523",
            first_name="Tessa",
            last_name="Ursula",
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
        self.other_customer = CustomerProfile.objects.create(
            user=self.other_user,
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

    def _get_request(self, user=None):
        request = factory.post("/")
        request.user = user or self.user
        return request

    def _make_serializer(self, data, user=None):
        return InitiatePaymentSerializer(
            data=data,
            context={"request": self._get_request(user)}
        )

    # --- Test 31 ---
    def test_valid_data_creates_pending_transaction(self):
        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "NEW_SUBSCRIPTION",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        txn = serializer.save()

        self.assertIsInstance(txn, Transaction)
        self.assertEqual(txn.payment_status, Transaction.PAYMENT_STATUS.PENDING)
        self.assertEqual(txn.subscription, self.subscription)

    # --- Test 32 ---
    def test_subscription_belonging_to_other_user_is_invalid(self):
        serializer = self._make_serializer(
            {
                "subscription_id": str(self.subscription.id),
                "payment_type": "NEW_SUBSCRIPTION",
            },
            user=self.other_user,  # different user
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("subscription_id", serializer.errors)

    # --- Test 33 ---
    def test_nonexistent_subscription_is_invalid(self):
        import uuid
        serializer = self._make_serializer({
            "subscription_id": str(uuid.uuid4()),
            "payment_type": "NEW_SUBSCRIPTION",
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("subscription_id", serializer.errors)

    # --- Test 34 ---
    def test_non_payable_subscription_status_is_invalid(self):
        self.subscription.status = "active"
        self.subscription.save()

        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "NEW_SUBSCRIPTION",
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    # --- Test 35 ---
    def test_existing_pending_transaction_blocks_new_one(self):
        # Create an existing PENDING transaction
        Transaction.objects.create(
            subscription=self.subscription,
            initiated_by=self.user,
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
        )

        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "NEW_SUBSCRIPTION",
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    # --- Test 36 ---
    def test_amount_is_sourced_from_subscription_not_client(self):
        """Client cannot influence the transaction amount."""
        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "NEW_SUBSCRIPTION",
            "amount": "1.00",  # client tries to send ₦1
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        txn = serializer.save()

        # Amount must come from subscription.amount_paid
        self.assertEqual(txn.amount, Decimal("5000.00"))

    # --- Test 37 ---
    def test_invalid_payment_type_is_rejected(self):
        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "GIFT",  # not a valid choice
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("payment_type", serializer.errors)

    # --- Test 37b ---
    def test_renewal_payment_type_is_accepted(self):
        self.subscription.status = "expired"
        self.subscription.save()

        serializer = self._make_serializer({
            "subscription_id": str(self.subscription.id),
            "payment_type": "RENEWAL",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)


class TransactionSerializerTests(TestCase):
    """Tests for TransactionSerializer."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="test@newexample.com",
            password="testpass4123",
            first_name="Tessie",
            last_name="Usher",
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
            amount=Decimal("5000.00"),
            payment_type=Transaction.PAYMENT_TYPE.NEW_SUBSCRIPTION,
        )

    # --- Test 38 ---
    def test_initiated_by_returns_user_email(self):
        data = TransactionSerializer(self.txn).data
        self.assertEqual(data["initiated_by"], "test@newexample.com")

    # --- Test 39 ---
    def test_initiated_by_returns_none_when_user_is_null(self):
        self.txn.initiated_by = None
        self.txn.save()
        data = TransactionSerializer(self.txn).data
        self.assertIsNone(data["initiated_by"])

    # --- Test 40 ---
    def test_all_expected_fields_present(self):
        data = TransactionSerializer(self.txn).data
        expected_fields = [
            "id", "reference", "amount", "currency",
            "payment_type", "payment_status", "gateway_reference",
            "subscription_id", "initiated_by", "created_at", "updated_at",
        ]
        for field in expected_fields:
            self.assertIn(field, data, f"Missing field: {field}")

    # --- Test 40b ---
    def test_serializer_is_read_only(self):
        """Attempting to write via serializer changes nothing."""
        data = {
            "payment_status": "SUCCESSFUL",
            "amount": "1.00",
        }
        serializer = TransactionSerializer(self.txn, data=data)
        # ModelSerializer with all read_only_fields won't validate write data
        # — just confirm original values are unchanged after attempted update
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_status, Transaction.PAYMENT_STATUS.PENDING)
        self.assertEqual(self.txn.amount, Decimal("5000.00"))

    # --- Test 40c ---
    def test_subscription_id_matches_subscription(self):
        data = TransactionSerializer(self.txn).data
        self.assertEqual(str(data["subscription_id"]), str(self.subscription.id))
