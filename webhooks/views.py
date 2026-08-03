from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes
from accounts.permissions import IsServiceAccount
from core.models import Webhook, WebhookEvent, ServiceWebhookEndpoint


# Base: resolve partner from authenticated user
def get_partner_from_user(user):
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


# Webhook Registration
class WebhookListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List all webhooks",
        description="Retrieve a list of all webhooks configured for the authenticated partner account.",
        responses={200: OpenApiTypes.OBJECT},
        tags=["Webhooks: Partner"]
    )
    def get(self, request):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        webhooks = Webhook.objects.filter(partner=partner).prefetch_related("events")
        data = [
            {
                "id": str(w.id),
                "url": w.url,
                "is_active": w.is_active,
                "events": [e.event for e in w.events.all()],
                "created_at": w.created_at,
            }
            for w in webhooks
        ]
        return Response({"webhooks": data})

    @extend_schema(
        summary="Register a new webhook",
        description="Create a new webhook endpoint with specific event subscriptions for the authenticated partner.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri", "example": "https://example.com/webhook"},
                    "events": {"type": "array", "items": {"type": "string"}, "example": ["payment.successful"]}
                },
                "required": ["url", "events"]
            },
        },
        responses={
            201: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT
        },
        tags=["Webhooks: Partner"]
    )
    def post(self, request):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        url = request.data.get("url")
        events = request.data.get("events", [])

        if not url:
            return Response({"error": "Webhook URL is required."}, status=400)

        if not events:
            return Response({"error": "At least one event is required."}, status=400)

        # validate events against allowed choices
        valid_events = [choice[0] for choice in WebhookEvent.EVENT_CHOICES]
        invalid = [e for e in events if e not in valid_events]
        if invalid:
            return Response({
                "error": f"Invalid events: {invalid}. Valid options are: {valid_events}"
            }, status=400)

        webhook = Webhook.objects.create(partner=partner, url=url)

        for event in events:
            WebhookEvent.objects.create(webhook=webhook, event=event)

        return Response({
            "message": "Webhook registered successfully.",
            "id": str(webhook.id),
            "url": webhook.url,
            "secret": webhook.secret,    # shown once at creation only
            "events": events,
        }, status=201)


class WebhookDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, webhook_id, partner):
        try:
            return Webhook.objects.get(id=webhook_id, partner=partner)
        except Webhook.DoesNotExist:
            return None

    @extend_schema(
        summary="Retrieve webhook details",
        description="Fetch detailed configuration for a specific webhook by ID.",
        responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT}
    )
    def get(self, request, webhook_id):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        webhook = self.get_object(webhook_id, partner)
        if not webhook:
            return Response({"error": "Webhook not found."}, status=404)

        return Response({
            "id": str(webhook.id),
            "url": webhook.url,
            "is_active": webhook.is_active,
            "events": [e.event for e in webhook.events.all()],
            "created_at": webhook.created_at,
        })

    @extend_schema(
        summary="Partially update a webhook",
        description="Update a webhook's URL, active status, or its subscribed events list.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "is_active": {"type": "boolean"},
                    "events": {"type": "array", "items": {"type": "string"}}
                }
            }
        },
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT}
    )
    def patch(self, request, webhook_id):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        webhook = self.get_object(webhook_id, partner)
        if not webhook:
            return Response({"error": "Webhook not found."}, status=404)

        # update url if provided
        if "url" in request.data:
            webhook.url = request.data["url"]

        # update active status if provided
        if "is_active" in request.data:
            webhook.is_active = request.data["is_active"]

        # update events if provided
        if "events" in request.data:
            valid_events = [choice[0] for choice in WebhookEvent.EVENT_CHOICES]
            invalid = [e for e in request.data["events"] if e not in valid_events]
            if invalid:
                return Response({
                    "error": f"Invalid events: {invalid}. Valid options are: {valid_events}"
                }, status=400)

            webhook.events.all().delete()
            for event in request.data["events"]:
                WebhookEvent.objects.create(webhook=webhook, event=event)

        webhook.save()
        return Response({"message": "Webhook updated successfully."})

    @extend_schema(
        summary="Delete a webhook",
        description="Permanently remove a webhook configuration.",
        responses={204: None, 404: OpenApiTypes.OBJECT}
    )
    def delete(self, request, webhook_id):
        partner = get_partner_from_user(request.user)
        if not partner:
            return Response({"error": "Partner account required."}, status=403)

        webhook = self.get_object(webhook_id, partner)
        if not webhook:
            return Response({"error": "Webhook not found."}, status=404)

        webhook.delete()
        return Response({"message": "Webhook deleted successfully."}, status=204)


class ServiceWebhookRegisterView(APIView):
    permission_classes = [IsServiceAccount]

    @extend_schema(
        summary="List service account webhooks",
        description="Retrieve all webhook configurations specifically registered for the calling service account (e.g., n8n).",
        responses={200: OpenApiTypes.OBJECT},
        tags=["Webhooks: Service Account"]
    )
    def get(self, request):
        endpoints = ServiceWebhookEndpoint.objects.filter(service_account=request.user)
        return Response({
            "webhooks": [
                {"event": e.event, "url": e.url, "is_active": e.is_active}
                for e in endpoints
            ]
        })

    @extend_schema(
        summary="Register or update service webhooks",
        description="Upsert webhook URLs for payment lifecycle events driven by internal service accounts.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "webhooks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "event": {"type": "string", "example": "payment.successful"},
                                "url": {"type": "string", "format": "uri", "example": "https://n8n.internal/webhook/payment-success"}
                            },
                            "required": ["event", "url"]
                        }
                    }
                },
                "required": ["webhooks"]
            }
        },
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
    )
    def post(self, request):
        registrations = request.data.get("webhooks", [])
        if not registrations:
            return Response({"error": "At least one webhook registration is required."}, status=400)

        valid_events = [c[0] for c in ServiceWebhookEndpoint.EVENT_CHOICES]
        result = []

        for reg in registrations:
            event = reg.get("event")
            url = reg.get("url")

            if event not in valid_events:
                return Response(
                    {"error": f"Invalid event '{event}'. Valid options: {valid_events}"},
                    status=400,
                )
            if not url:
                return Response({"error": f"Missing url for event '{event}'."}, status=400)

            obj, _ = ServiceWebhookEndpoint.objects.update_or_create(
                service_account=request.user,
                event=event,
                defaults={"url": url, "is_active": True},
            )
            result.append({"event": obj.event, "url": obj.url})

        return Response({"message": "Webhooks registered.", "webhooks": result}, status=201)