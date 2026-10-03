# TheeInsurance API

A headless, API-first insurance distribution platform — **"Stripe for insurance"** for Nigeria. Companies integrate TheeInsurance into their products via API and offer insurance plans to their customers without building the insurance layer themselves.

Built for the **DevCareer × Nomba Hack4FUTO Hackathon** by team **Meridian**.

---

# What It Does

TheeInsurance enables providers, distributors, and digital platforms to embed insurance distribution into their existing products through a REST API. The platform manages:

- Multi-tenant partner onboarding
- KYC
- Insurance plan management
- Subscription lifecycle
- Payments
- Automated renewals and dunning

Partners never have to build insurance infrastructure themselves.

## Core Capabilities

- **Multi-tenant partner architecture**
  - Provider Admins
  - Distributor Admins
  - End Users
  - Scoped using `X-Partner-Key` with role-based access control.

- **KYC & Onboarding**
  - Partner onboarding
  - Customer onboarding
  - Document uploads
  - Staff review and approval

- **Plans API**
  - Searchable
  - Filterable
  - Paginated insurance catalog

- **Payments**
  - **Nomba**
    - Hosted checkout
    - Card tokenization
    - OAuth2 token lifecycle
    - Webhook verification
    - Split payouts
  - **Interswitch**
    - Quickteller Pay fallback for partners without a Nomba sub-account

- **Automated Split Payouts**
  - Platform fee
  - Provider payout
  - Distributor commission

- **Subscription Lifecycle**
  - Activation
  - Renewal
  - Automated dunning via n8n

- **Service Accounts**
  - Client Credentials authentication for automation (n8n, schedulers)

---

# Nomba Integration

TheeInsurance integrates Nomba for recurring premium collection and split payouts.

## Core Payment Flow

- Hosted checkout
- Card tokenization
- Automatic recurring charges
- Split payouts
- Automated dunning (Day 1 → Day 3 → Day 7)

---

## Technical Execution

- Subscription pause/resume
- Renewal charging using stored tokens

---

## Security & Reliability

- Idempotency keys
- Webhook signature verification
- Customer consent enforcement
- Split eligibility validation

---

## Product UX

- Swagger / OpenAPI documentation
- Mock distributor portal

---

## n8n Automation

Current automation:

- Failed payment dunning workflow

---

# End-to-End Flow

1. Partner onboarding
2. Nomba sub-account linkage
3. Plan creation
4. Customer subscription
5. Checkout
6. Successful payment
7. Payment webhook
8. n8n notification
9. Renewal due
10. Successful renewal
11. Failed renewal → Dunning
12. Subscription lapses after retries

---

# Architecture

```text
                 Customer
                     │
                     ▼
          Partner Application
                     │
             REST API Requests
                     │
                     ▼
            TheeInsurance API
                     │
 ┌───────────────────┼────────────────────┐
 │                   │                    │
 ▼                   ▼                    ▼
Plans API      Subscription API      Payments API
 │                   │                    │
 │                   │                    │
 ▼                   ▼                    ▼
PostgreSQL      Cloudinary         Nomba API
                                      │
                                      │
                                      ▼
                            Hosted Checkout
                                      │
                                      ▼
                             Payment Webhook
                                      │
                                      ▼
                           TheeInsurance API
                                      │
                                      ▼
                               n8n Workflow
                                      │
                     ┌────────────────┴───────────────┐
                     ▼                                ▼
              Payment Notifications          Dunning Workflow
                                              Day 1 Retry
                                              Day 3 Retry
                                              Day 7 Retry
                                              Cancel Subscription
```

---

# Tech Stack

- Django
- Django REST Framework
- SimpleJWT
- PostgreSQL (Neon)
- SQLite (development)
- Cloudinary
- Nomba
- Interswitch Quickteller Pay
- n8n
- Render

---

# Project Structure

```text
theeinsurance-api/
├── accounts/
├── core/
├── plans/
├── subscriptions/
├── payments/
├── claims/
└── webhooks/
```

---

# Local Setup

```bash
git clone <repo-url>

cd theeinsurance-api

python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r requirements.txt
```

Create a `.env` file from `.env.example`.

Then run:

```bash
python manage.py migrate

python manage.py createsuperuser

python manage.py runserver
```

---

# Deployment

Staging is deployed on **Render**.

Database:

- PostgreSQL (Neon)
- Direct connection for migrations
- Pooled connection for runtime traffic

---

# Service Accounts

Automation authenticates using Client Credentials.

Flow:

1. Create service account
2. Receive `client_id`
3. Receive `client_secret`
4. Exchange credentials for JWT
5. Authenticate n8n

---

# API Documentation

Swagger documentation is generated using **drf-spectacular**.

Available at:

```text
/api/schema/swagger-ui/
```

---

# Project Status

Hackathon project for the **DevCareer × Nomba Hack4FUTO** Hackathon.

Completed:

- Partner infrastructure
- Payments
- Split payouts
- Subscription lifecycle

In Progress:

- Dunning automation
- Full n8n orchestration

---

# Team — Meridian

- **Hilda Enyioko**
  - Backend architecture
  - Payments integration
  - API design

- **Chinedu Adindu**
  - AI & Automation
  - n8n workflows
  - Notifications
  - Dunning
  - AI recommendations
  - Provider analytics
