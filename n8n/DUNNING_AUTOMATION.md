# Nomba Dunning & Subscription Retry Workflow

An automated dunning system built for the Nomba Hackathon. It detects failed subscription charges and manages the retry lifecycle end-to-end — from the first failed payment through final cancellation — using **n8n** as the orchestration layer and a **Django** backend for state management and webhooks.

## What It Does

When a subscription charge fails, this workflow automatically:

- Listens for `charge.failed` events from the Nomba Payment API via a Django webhook.
- Retries the charge on a fixed schedule:
  - **Day 1**
  - **Day 3**
  - **Day 7**
- Cancels the subscription automatically if all three retry attempts are exhausted.

This reduces manual follow-up on failed payments and recovers revenue that would otherwise be lost to silent subscription churn.

## Architecture

```text
Nomba API  ── charge.failed ──▶ Django Webhook ──▶ n8n Workflow
                                      │                  │
                                      │                  ├── Day 1 retry
                                      │                  ├── Day 3 retry
                                      │                  ├── Day 7 retry
                                      │                  │
                                      └──── cancel subscription (after 3 failed attempts)
```

### Components

- **n8n Cloud** — Orchestrates the retry schedule, branching logic, and calls to the Nomba renewal charge endpoint.
- **Django (staging backend)** — Receives and validates incoming Nomba webhooks, exposes endpoints that n8n calls, and tracks retry state.
- **Nomba API** — Payment provider; source of `charge.failed` events and target of retry/cancellation requests.

## Repository Structure

```text
nomba-dunning-workflow/
├── README.md                 # Project documentation
├── workflow.json             # Exported n8n workflow (ready to import)
├── docs/
│   └── flow-diagram.svg      # Visual flowchart of the retry logic
└── django/
    ├── views.py              # Webhook receiver + retry/cancellation views
    ├── urls.py               # URL routing for webhook endpoints
    └── webhooks.py           # Helper functions for outbound webhook events
```

## How to Import the Workflow

1. Open **n8n**.
2. Navigate to **Workflows → Import from File**.
3. Select `workflow.json`.
4. Reconnect the required credentials:
   - Nomba API (OAuth2 / short-lived JWT)
   - Django backend base URL
5. Activate the workflow.

## Integration Guide

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
