# TheeInsurance API

A headless, API-first insurance distribution platform — "Stripe for insurance" for Nigeria. Companies integrate TheeInsurance into their products via API and offer insurance plans to their customers without building the insurance layer themselves.

Built for the **DevCareer x Nomba Hack4FUTO** hackathon by team **Meridian**.

## What it does

TheeInsurance lets a partner (a provider, distributor, or platform) plug insurance distribution into their existing product. The platform handles multi-tenant partner onboarding, KYC, plan management, subscription lifecycle, payments, and automated renewal/dunning — exposed entirely through a REST API, so partners never need to build insurance infrastructure themselves.

Core capabilities:

- **Multi-tenant partner architecture** — Provider Admins, Distributor Admins, and End Users scoped via `X-Partner-Key` headers, with role-based access control enforced across every app.
- **KYC and onboarding** — partner and customer KYC submission, document upload, and staff review/approval flows.
- **Plans API** — filterable, searchable, paginated insurance plan catalog.
- **Payments** :
  - **Nomba** — checkout with card tokenization for recurring charges, OAuth2 token lifecycle management, webhook verification (HMAC-SHA256 composite signature), and distributor payout splits via Nomba Transfers.
- **Subscription lifecycle** — activation, renewal, and automated dunning on failed recurring charges, orchestrated via n8n.
- **Service accounts** — staff-provisioned client-credential auth for non-human callers (n8n automation, schedulers) — no human login required for backend automation.

## Nomba Integration

TheeInsurance integrates Nomba for end-to-end recurring premium collection and distributor payouts.

### Core Payment Flow
- **Checkout** — customer pays premium via Nomba-hosted checkout, with internal `Transaction` record created before redirect.
- **Tokenised cards** — card details tokenised on successful checkout and stored for recurring charges, gated behind explicit customer consent (`auto_charge_enabled`).
- **Auto-charge on renewal** — stored token used to automatically charge the customer's card at each subscription renewal cycle.
- **Dunning** — failed renewal charges trigger a retry sequence at day 1, 3, and 7, orchestrated via n8n; subscription lapses to `grace_period` → `lapsed` if all retries are exhausted.
- **Transfers** — successful premium collection automatically splits and pays out to the provider and distributor via Nomba Transfers.

### Technical Execution
- **Proration engine** — mid-cycle plan changes calculate a credit/debit adjustment rather than charging the full new premium.
- **Subscription pause/resume** — pausing a subscription skips the next scheduled Nomba charge without cancelling the underlying token or policy.

### Security & Reliability
- **Idempotency keys** on every outbound Nomba API call, preventing duplicate charges/transfers on retry or network failure.
- **Signature verification** on all inbound Nomba webhooks (HMAC-SHA256 composite-string signing, per Nomba's spec — not a raw-body signature like Interswitch).
- Consent is checked and enforced server-side before any card token is persisted — no tokenisation without explicit `auto_charge_enabled`.

### Product UX & Clarity
- Full Swagger/OpenAPI docs (via drf-spectacular) with example requests and responses for every Nomba-related endpoint.
- Mock distributor portal demonstrating the partner and distributor dashboard experience, for the hackathon demo flow.

### n8n Automation
- **Dunning workflow** — the single n8n-owned automation for this submission: listens for failed-charge webhooks, schedules day 1/3/7 retries, and fires the lapse/exhaustion event back to TheeInsurance when retries are exhausted.

## Tech Stack

- Django + Django REST Framework
- SimpleJWT authentication (interactive users) + client-credentials token exchange (service accounts)
- PostgreSQL via [Neon](https://neon.com) (staging/production), SQLite (local development)
- [Cloudinary](https://cloudinary.com) for document storage — public delivery for non-sensitive assets, private signed delivery for KYC documents
- Payment gateways: Interswitch Quickteller Pay, Nomba
- Automation: [n8n](https://n8n.io) (notifications, dunning, payout orchestration)
- Deployed on [Render](https://render.com)

## Project Structure

```
theeinsurance-api/
├── accounts/        # Users, partner admins, customer profiles, KYC, service accounts
├── core/             # Partner model, shared utilities, Nomba auth service
├── plans/            # Insurance plan catalog, distributor-provider access
├── subscriptions/     # Policy subscriptions, documents, Nomba token storage
├── payments/         # Transaction processing, gateway webhooks, callback logs
├── claims/            # Claims handling
└── webhooks/          # Outbound webhook dispatch to partners and n8n
```

## Local Setup

```bash
git clone <repo-url>
cd theeinsurance-api
python -m venv venv
source venv/bin/activate      # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as reference.

Then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Deployment

Staging is deployed on Render at every push to `staging`. Migrations run against Neon's **direct** (non-pooled) connection string as part of `build.sh` — PgBouncer's transaction-mode pooling breaks schema migrations, so this distinction matters. Runtime traffic uses the pooled connection.

## Service Accounts

Backend automation (n8n) authenticates via staff-provisioned service account credentials rather than a human login. A `super_admin` creates a credential through `POST /staff/service-accounts/`, which returns a `client_id` and `client_secret` **shown once**. The service account then exchanges these for a JWT via `POST /auth/service-account/token/`. Credentials can be revoked (not deleted, for audit purposes) via `POST /staff/service-accounts/<client_id>/revoke/`.

## API Docs

Schema is generated with `drf-spectacular`. Once running locally or on staging, see `/api/schema/swagger-ui/` (or your configured docs path) for the full interactive reference.

## Project Status

Active hackathon build for DevCareer x Nomba. Core partner infrastructure, payments (Nomba), and subscription lifecycle are built; dunning automation, automated payout split, and full n8n handoff complete.

## Team — Meridian

- **Hilda Enyioko** — Full-stack development (backend architecture, payments integration, API design)
- **Chinedu Adindu** — AI/Automation engineering (n8n workflows: notifications, dunning, plan recommendation, provider analytics)



PLEASE READ FILE FOR FULL WALKTHROUGH ON SIGNUP AND APPLICATION USE:
https://docs.google.com/document/d/1Copu9wlzUoL5IMtOeG-bQ1v8rJGlQbNtgvMdDALjZXI/edit?usp=drivesdk