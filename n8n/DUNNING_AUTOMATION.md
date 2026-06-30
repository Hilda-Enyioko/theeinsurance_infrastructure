## n8n Automation — Integration Guide

**Base URL:** `https://theeinsurance-staging.onrender.com`

### 1. Authentication

Get a service-account access token before calling anything else.

```
POST /api/v1/auth/service-account/token/
Content-Type: application/json

{
  "client_id": "<client_id>",
  "client_secret": "<client_secret>"
}
```

**Response:**
```json
{
  "access": "<jwt>",
  "refresh": "<jwt>",
  "expires_in": 300
}
```

Use `access` as a Bearer token on every request below:
```
Authorization: Bearer <access>
```

⚠️ **Tokens are short-lived (currently 5 min default).** Re-fetch when expired, or use `refresh` against `/api/v1/auth/token/refresh/`. We'll extend the service-account token lifetime soon — flagging in case retries start hitting 401s mid-flow.

**Credentials** (send these to the engineer separately/securely, not in this doc):
- `client_id`: `<fill in>`
- `client_secret`: `<fill in>`

### 2. Register your webhook callback URLs

One-time setup (re-run any time you change endpoints):

```
POST /api/v1/webhooks/register/
Authorization: Bearer <access>
Content-Type: application/json

{
  "webhooks": [
    {"event": "payment.successful", "url": "https://<your-n8n-host>/webhook/payment-success"},
    {"event": "charge.failed", "url": "https://<your-n8n-host>/webhook/charge-failed"}
  ]
}
```

We dispatch to whatever URL is registered for each event — no redeploy needed on our end if you change n8n endpoints.

### 3. `charge.failed` webhook — payload we send you

Fired on each failed renewal attempt (we call this; you don't call it).

```json
{
  "event": "charge.failed",
  "transaction_ref": "TII-XXXXXXXX",
  "subscription_id": "<uuid>",
  "customer_email": "customer@example.com",
  "amount": "5000.00",
  "currency": "NGN",
  "failure_reason": "declined" | "timeout" | "auth_failure" | "...",
  "retry_endpoint": "POST /api/v1/payments/nomba/renewal/charge/",
  "retry_payload": { "subscription_id": "<uuid>" }
}
```

### 4. Dunning retry flow (day 1 → 3 → 7)

On each scheduled retry, call:

```
POST /api/v1/payments/nomba/renewal/charge/
Authorization: Bearer <access>
Content-Type: application/json

{
  "subscription_id": "<uuid>"
}
```

- **Success** → we fire `payment.successful` to your registered URL; subscription is reactivated on our end. No further action needed from you for this subscription.
- **Failure** → we fire `charge.failed` again (same contract as above) so your dunning schedule can continue or escalate.

### 5. After day 7 — final failure

If the day-7 retry still fails (or you've exhausted your retry policy), tell us so we can cancel the subscription and notify the partner:

```
POST /api/v1/payments/dunning/final-failure/
Authorization: Bearer <access>
Content-Type: application/json

{
  "subscription_id": "<uuid>",
  "transaction_ref": "<last failed transaction_ref>",
  "reason": "dunning_exhausted"
}
```

This call is **idempotent** — safe to retry if your request times out; calling it twice for the same subscription won't double-cancel or double-notify.

**Response:**
```json
{
  "message": "Subscription cancelled and partner notified.",
  "subscription_id": "<uuid>"
}
```

### Quick reference table

| Step | Method | Endpoint |
|---|---|---|
| Get token | `POST` | `/api/v1/auth/service-account/token/` |
| Register callback URLs | `POST` | `/api/v1/webhooks/register/` |
| Retry a failed renewal | `POST` | `/api/v1/payments/nomba/renewal/charge/` |
| Report dunning exhausted | `POST` | `/api/v1/payments/dunning/final-failure/` |

### Notes / open items

- All endpoints require the `Authorization: Bearer <access>` header except the token endpoint itself.
- `transaction_ref` in `charge.failed` always refers to the **most recent failed attempt** — use it for your audit trail, not as a stable identifier across retries (each retry creates a new transaction with a new ref).
- Let us know your actual webhook receiver URLs before go-live so we can confirm `/webhooks/register/` round-trips correctly in staging.
