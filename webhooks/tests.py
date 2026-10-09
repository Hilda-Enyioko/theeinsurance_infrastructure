import time
from django.test import SimpleTestCase, TestCase

from core.models import Partner
from webhooks.events import E
from webhooks.models import OutboundEvent
from webhooks.services import emit
from webhooks.signing import sign, verify


class SigningTests(SimpleTestCase):
    def test_roundtrip(self):
        ts = int(time.time())
        sig = sign("s", ts, b'{"a":1}')
        self.assertTrue(verify("s", ts, b'{"a":1}', sig))

    def test_tampered_body_rejected(self):
        ts = int(time.time())
        sig = sign("s", ts, b'{"a":1}')
        self.assertFalse(verify("s", ts, b'{"a":2}', sig))

    def test_replay_rejected(self):
        old = int(time.time()) - 1000
        sig = sign("s", old, b"x")
        self.assertFalse(verify("s", old, b"x", sig))


class EmitTests(TestCase):
    def test_emit_is_idempotent(self):
        p = Partner.objects.create(name="P", slug="p", partner_type="provider")
        data = {"claim_id": "1", "claim_reference": "CLM-X", "claim_type": "motor_accident",
                "status": "submitted", "claimed_amount": "1.00", "customer": {}}
        a = emit(E.CLAIM_SUBMITTED, partner=p, aggregate_id="c1", data=data)
        b = emit(E.CLAIM_SUBMITTED, partner=p, aggregate_id="c1", data=data)
        self.assertEqual(a.id, b.id)
        self.assertEqual(OutboundEvent.objects.count(), 1)

    def test_missing_required_key_fails_loudly(self):
        p = Partner.objects.create(name="P2", slug="p2", partner_type="provider")
        with self.assertRaises(ValueError):
            emit(E.CLAIM_SUBMITTED, partner=p, aggregate_id="c2", data={"claim_id": "1"})
