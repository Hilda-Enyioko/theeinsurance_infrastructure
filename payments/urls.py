from django.urls import path
from .views import PaymentVerifyView, PaystackWebhookView

app_name = "payments"

urlpatterns = [
    path("verify/<str:reference>/", PaymentVerifyView.as_view(), name="verify"),
    path("paystack/webhook/", PaystackWebhookView.as_view(), name="paystack-webhook"),
]
