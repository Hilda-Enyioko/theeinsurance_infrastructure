import uuid

from django.conf import settings
from django.utils import timezone

from .events import CATALOGUE, SCHEMA_VERSION


def build_envelope(event_type, data, *, partner_id, correlation_id, event_id=None):
    definition = CATALOGUE[event_type]  # KeyError = programmer error
    missing = [k for k in definition.required if k not in data]
    if missing:
        raise ValueError(f"{event_type} payload missing {missing}")
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "version": SCHEMA_VERSION,
        "created_at": timezone.now().isoformat(),
        "environment": settings.APP_ENV,
        "partner_id": str(partner_id),
        "correlation_id": str(correlation_id),
        "data": data,
    }
