"""
One-off diagnostic script used during the Bug Smash investigation.
Feeds the actual test failure traceback and the relevant function source
to Gemini for an independent root-cause read, as part of the debugging
narrative for the Clear the Lineup submission.

Run manually:
    python scripts/analyze_race_condition.py
Not part of the application runtime — do not import from views/services.
"""

import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theeinsurance.settings")
django.setup()

import google.genai as genai
from django.conf import settings

# Initialize the modern Client using the new google-genai SDK
client = genai.Client(api_key=settings.GOOGLE_AI_API_KEY)

# Paste your actual before_output.txt failure content here
FAILURE_TRACEBACK = """
python : Using existing test database for alias 'default' ('test_neondb')...
At line:1 char:1
+ python manage.py test payments.tests.test_renewal_race_condition --ke ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (Using existing ...est_neondb')...:String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError
 

test_concurrent_renewal_calls_should_only_charge_once (payments.tests.test_renewal_race_condition.TestRenewalDoubleChargeRace.test_concurrent_renewal_calls_should_only_charge_once) ... FAIL

======================================================================
FAIL: test_concurrent_renewal_calls_should_only_charge_once (payments.tests.test_renewal_race_condition.TestRenewalDoubleChargeRace.test_concurrent_renewal_calls_should_only_charge_once)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\\Users\\ADMIN\\Projects\\theeinsurance_infrastructure\\payments\\tests\\test_renewal_race_condition.py", line 157, 
in test_concurrent_renewal_calls_should_only_charge_once
    assert len(successes) == 1, (
           ^^^^^^^^^^^^^^^^^^^
AssertionError: Expected exactly 1 successful renewal charge, got 2. Blocked: 0. Race condition allowed duplicate billing.

----------------------------------------------------------------------
Ran 1 test in 29.213s

FAILED (failures=1)
Preserving test database for alias 'default' ('test_neondb')...

Found 1 test(s).
Operations to perform:
  Synchronize unmigrated apps: cloudinary, cloudinary_storage, corsheaders, drf_spectacular, drf_spectacular_sidecar, messages, rest_framework, staticfiles
  Apply all migrations: accounts, admin, auth, claims, contenttypes, core, payments, plans, sessions, subscriptions
Synchronizing apps without migrations:
  Creating tables...
    Running deferred SQL...
Running migrations:
  No migrations to apply.
System check identified no issues (0 silenced).
"""

# Paste the original (pre-fix) charge_policy_renewal source here
FUNCTION_SOURCE = """
def charge_policy_renewal(subscription_id: str) -> dict:
    \"\"\"
    Charge a policy renewal using a stored Nomba tokenized card.

    Called by:
      - The renewal scheduler (Django management command or Celery beat)
      - n8n via the service account endpoint on scheduled renewal dates
      - Dunning retry attempts (n8n calls this again on day 1, 3, 7)

    Flow:
      1. Load the PolicySubscription and its NombaTokenStore
      2. Guard against double-charging (idempotency at the DB level)
      3. Create a new RENEWAL Transaction record
      4. POST to Nomba /v1/checkout/tokenized-card-payment
      5. On success -> activate/renew subscription, fire n8n payment.successful
      6. On failure -> mark transaction FAILED, fire n8n charge.failed for dunning

    Returns a dict with outcome details for the caller (n8n or scheduler).
    \"\"\"
    from subscriptions.models import PolicySubscription, NombaTokenStore

    # 1. Load subscription
    try:
        sub = PolicySubscription.objects.select_related(
            'plan', 'customer'
        ).get(id=subscription_id)
    except PolicySubscription.DoesNotExist:
        raise PaymentError(f"Subscription {subscription_id} not found.")

    # 2. Guard: only charge active or grace-period subscriptions
    if sub.status not in ('active', 'grace_period'):
        raise PaymentError(
            f"Subscription {subscription_id} is '{sub.status}'. "
            "Only active or grace_period subscriptions can be renewed."
        )

    # 3. Guard: skip if auto-charge is disabled (customer revoked consent)
    if not sub.auto_charge_enabled:
        raise PaymentError(
            f"Auto-charge is disabled for subscription {subscription_id}. "
            "Skipping renewal charge."
        )

    # 4. Load the stored token
    try:
        token_store = NombaTokenStore.objects.get(policy=sub)
    except NombaTokenStore.DoesNotExist:
        raise PaymentError(
            f"No stored Nomba token for subscription {subscription_id}. "
            "Customer must complete a checkout to enable auto-renewal."
        )

    # 5. Idempotency guard at DB level:
    #    If a PENDING renewal transaction already exists for this subscription,
    #    a previous attempt is still in flight — don't fire a second charge.
    in_flight = Transaction.objects.filter(
        subscription=sub,
        payment_type=Transaction.PAYMENT_TYPE.RENEWAL,
        payment_status=Transaction.PAYMENT_STATUS.PENDING,
        gateway=Transaction.GATEWAY.NOMBA,
    ).exists()

    if in_flight:
        logger.warning(
            "Renewal charge skipped for subscription %s — a PENDING renewal "
            "transaction already exists. Possible duplicate trigger.",
            subscription_id,
        )
        raise PaymentError(
            f"A renewal charge is already in progress for subscription {subscription_id}."
        )

    # 6. Create the renewal Transaction record before calling Nomba
    #    we have an audit trail even if the network call fails.
    with db_transaction.atomic():
        txn = Transaction.objects.create(
            amount=sub.plan.premium,
            currency='NGN',
            initiated_by=sub.customer.user,
            subscription=sub,
            payment_type=Transaction.PAYMENT_TYPE.RENEWAL,
            payment_status=Transaction.PAYMENT_STATUS.PENDING,
            gateway=Transaction.GATEWAY.NOMBA,
        )

    # 7. Build Nomba tokenized charge payload
    charge_payload = {
        "order": {
            "orderReference": txn.reference,
            "customerEmail":  sub.customer.user.email,
            "amount":         float(sub.plan.premium),
            "currency":       "NGN",
            "accountId":      settings.NOMBA_SUB_ACCOUNT_ID,
            "callbackUrl":    settings.NOMBA_CALLBACK_URL,
            "orderMetaData": {
                "subscriptionId": str(sub.id),
                "chargeType":     "AUTO_RENEWAL",
            },
        },
        "tokenKey": token_store.token_key,
    }

    # 8. Call Nomba
    try:
        access_token = get_nomba_token()
        response = requests.post(
            f"{settings.NOMBA_BASE_URL}/checkout/tokenized-card-payment",
            json=charge_payload,
            headers={
                "Authorization":    f"Bearer {access_token}",
                "accountId":        settings.NOMBA_ACCOUNT_ID,
                "Content-Type":     "application/json",
                "X-Idempotency-Key": txn.reference,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

    except NombaAuthError as e:
        logger.error("Nomba auth failed during renewal for txn %s: %s", txn.reference, str(e))
        _mark_renewal_failed(txn, error="auth_failure")
        raise PaymentError("Could not authenticate with payment gateway.")

    except requests.Timeout:
        logger.error("Nomba renewal charge timed out for txn %s", txn.reference)
        _mark_renewal_failed(txn, error="timeout")
        raise PaymentError("Payment gateway timed out.")

    except requests.RequestException as e:
        logger.error("Nomba renewal request failed for txn %s: %s", txn.reference, str(e))
        _mark_renewal_failed(txn, error=str(e))
        raise PaymentError("Could not reach payment gateway.")

    # 9. Handle Nomba response
    response_code = data.get("code")
    nomba_data    = data.get("data", {})
    charge_status = nomba_data.get("status")

    if response_code == "00" and charge_status is True:
        # Success — activate the subscription and notify n8n
        with db_transaction.atomic():
            txn.payment_status    = Transaction.PAYMENT_STATUS.SUCCESSFUL
            txn.gateway_reference = txn.reference   # Nomba doesn't return a separate ref here
            txn.gateway_response  = data
            txn.save(update_fields=[
                "payment_status", "gateway_reference", "gateway_response", "updated_at"
            ])
            _activate_subscription(txn)

        db_transaction.on_commit(lambda: _fire_n8n_payment_webhook(txn))

        logger.info(
            "Auto-renewal successful: subscription=%s txn=%s amount=%s",
            sub.id, txn.reference, sub.plan.premium,
        )
        return {
            "outcome":          "success",
            "transaction_ref":  txn.reference,
            "subscription_id":  str(sub.id),
            "amount":           str(sub.plan.premium),
        }

    else:
        # Nomba returned non-00 or status=False — card declined or gateway error
        logger.warning(
            "Auto-renewal charge failed: subscription=%s txn=%s code=%s message=%s",
            sub.id, txn.reference, response_code, nomba_data.get("message", ""),
        )
        _mark_renewal_failed(txn, error=nomba_data.get("message", "declined"), raw=data)

        return {
            "outcome":          "failed",
            "transaction_ref":  txn.reference,
            "subscription_id":  str(sub.id),
            "failure_reason":   nomba_data.get("message", "declined"),
        }
"""

# Call the API using client.models.generate_content
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=f"""
Given this test failure and the relevant function source, identify the
root cause and suggest a fix:

FAILURE:
{FAILURE_TRACEBACK}

FUNCTION:
{FUNCTION_SOURCE}
""",
)

print(response.text)

# Save the diagnostic report output
with open("gemini_root_cause_analysis.txt", "w", encoding="utf-8") as f:
    f.write(response.text)
