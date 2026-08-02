import threading
from datetime import date, timedelta
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase

from accounts.models import CustomUser, CustomerProfile
from core.models import Partner
from payments.models import Transaction
from payments.services import PaymentError, charge_policy_renewal
from plans.models import InsuranceCategory, InsurancePlan
from subscriptions.models import NombaTokenStore, PolicySubscription


class TestRenewalDoubleChargeRace(TransactionTestCase):
    """
    Reproduces the TOCTOU race condition in charge_policy_renewal().
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="customer@example.com",
            password="testpass123",
            first_name="Test",
            last_name="Customer",
            role="customer",
        )

        self.provider = Partner.objects.create(
            name="Test Insurance Co",
            partner_type="provider",
            is_active=True,
        )

        self.customer = CustomerProfile.objects.create(
            user=self.user,
            partner=self.provider,
            phone_number="08000000000",
            gender="other",
            address="123 Test Street, Lagos",
        )

        self.category = InsuranceCategory.objects.create(
            name="health",
            description="Health insurance coverage",
            is_active=True,
        )

        self.plan = InsurancePlan.objects.create(
            provider=self.provider,
            category=self.category,
            name="Comprehensive Health Plan",
            coverage_level="comprehensive",
            coverage_amount=1000000.00,
            premium=5000.00,
            duration_months=12,
            description="Full health coverage plan",
            visibility="public",
            is_active=True,
        )

        self.sub = PolicySubscription.objects.create(
            customer=self.customer,
            plan=self.plan,
            provider=self.provider,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=365),
            status="active",
            amount_paid=5000.00,
            auto_charge_enabled=True,
        )

        NombaTokenStore.objects.create(
            policy=self.sub,
            token_key="tok_test_123",
            customer_consented_to_auto_charge=True,
        )

    def test_concurrent_renewal_calls_should_only_charge_once(self):
        results = []
        barrier = threading.Barrier(2)

        real_filter = Transaction.objects.filter

        def synchronized_filter(*args, **kwargs):
            qs = real_filter(*args, **kwargs)

            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass

            return qs

        def fake_nomba_response(*args, **kwargs):
            class Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "code": "00",
                        "data": {
                            "status": True,
                        },
                    }

            return Resp()

        def call_renewal():
            try:
                outcome = charge_policy_renewal(str(self.sub.id))
                results.append(("success", outcome))

            except PaymentError as e:
                results.append(("blocked", str(e)))

            except Exception as e:
                results.append(("error", str(e)))

            finally:
                # Close every DB connection opened in this thread
                connections.close_all()

        with patch(
            "payments.services.Transaction.objects.filter",
            side_effect=synchronized_filter,
        ), patch(
            "payments.services.get_nomba_token",
            return_value="fake-token",
        ), patch(
            "payments.services.requests.post",
            side_effect=fake_nomba_response,
        ):

            t1 = threading.Thread(target=call_renewal)
            t2 = threading.Thread(target=call_renewal)

            t1.start()
            t2.start()

            t1.join(timeout=10)
            t2.join(timeout=10)

        # Close any remaining connections in the main thread
        connections.close_all()

        successes = [r for r in results if r[0] == "success"]
        blocked = [r for r in results if r[0] == "blocked"]
        errors = [r for r in results if r[0] == "error"]

        assert not errors, (
            f"Unexpected system errors during race execution: {errors}"
        )

        assert len(successes) == 1, (
            f"Expected exactly 1 successful renewal charge, got {len(successes)}. "
            f"Blocked: {len(blocked)}. Race condition allowed duplicate billing."
        )

        assert len(blocked) == 1

        renewal_txn_count = Transaction.objects.filter(
            subscription=self.sub,
            payment_type=Transaction.PAYMENT_TYPE.RENEWAL,
        ).count()

        assert renewal_txn_count == 1, (
            f"Expected 1 renewal Transaction record in database, "
            f"found {renewal_txn_count}."
        )
