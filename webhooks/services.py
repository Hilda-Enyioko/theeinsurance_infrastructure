import hmac
import hashlib
import json
import requests
from core.models import Webhook

# ─── Webhook Dispatch ─────────────────────────────────────────────────────────

def dispatch_webhook(partner, event_type, payload):
    webhooks = Webhook.objects.filter(
        partner=partner,
        is_active=True,
        events__event=event_type,
    )

    for webhook in webhooks:
        payload_bytes = json.dumps(payload).encode("utf-8")

        # sign payload with webhook secret
        signature = hmac.new(
            webhook.secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-TheeInsurance-Signature": signature,
            "X-TheeInsurance-Event": event_type,
        }

        try:
            requests.post(
                webhook.url,
                data=payload_bytes,
                headers=headers,
                timeout=10,
            )
        except requests.exceptions.RequestException:
            # silently fail for now — delivery logging comes later
            pass
