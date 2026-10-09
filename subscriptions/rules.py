from django.utils import timezone

CANCELLABLE_STATUSES = ("pending_document", "pending_payment", "active", "grace_period")
RENEWABLE_STATUSES = ("active", "grace_period", "expired", "lapsed")
RENEW_WINDOW_DAYS = 30


def can_cancel(sub) -> bool:
    return sub.status in CANCELLABLE_STATUSES


def can_renew(sub) -> bool:
    if sub.status not in RENEWABLE_STATUSES:
        return False
    if sub.status == "active":
        return (sub.end_date - timezone.localdate()).days <= RENEW_WINDOW_DAYS
    return True
