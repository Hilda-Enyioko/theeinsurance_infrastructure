from django.db import models


class OutboundEvent(models.Model):
    id = models.CharField(primary_key=True, max_length=64)            # == envelope["id"]
    event_type = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True)   # type:aggregate:discriminator
    envelope = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type} ({self.id})"


class WebhookDelivery(models.Model):
    PENDING, DELIVERED, FAILED = "pending", "delivered", "failed"

    event = models.ForeignKey(OutboundEvent, on_delete=models.CASCADE, related_name="deliveries")
    target = models.CharField(max_length=300)        # "n8n:<endpoint_id>" or "partner:<webhook_id>"
    url = models.URLField(max_length=500)
    status = models.CharField(max_length=10, default=PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("event", "target")
