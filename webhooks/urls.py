from django.urls import path
from .views import ServiceWebhookRegisterView

urlpatterns = [
    path("service/register/", ServiceWebhookRegisterView.as_view(), name="service-webhook-register"),
]
