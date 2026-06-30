from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
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
    """
    GET/POST /webhooks/register/

    Lets a service account (n8n) register or update the callback URLs it
    wants theeinsurance to call for payment lifecycle events.
    n8n can update these itself without a redeploy.

    POST body:
        {
          "webhooks": [
            {"event": "payment.successful", "url": "https://n8n.internal/webhook/payment-success"},
            {"event": "charge.failed", "url": "https://n8n.internal/webhook/charge-failed"}
          ]
        }
    """
    permission_classes = [IsServiceAccount]

    def get(self, request):
        endpoints = ServiceWebhookEndpoint.objects.filter(service_account=request.user)
        return Response({
            "webhooks": [
                {"event": e.event, "url": e.url, "is_active": e.is_active}
                for e in endpoints
            ]
        })

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
