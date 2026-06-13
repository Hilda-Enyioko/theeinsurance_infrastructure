"""
Payments URL Configuration.

Endpoints:
  POST /payments/initiate/   — initiate a payment, get Interswitch payment URL
  GET  /payments/callback/   — user lands here after Interswitch redirect
  POST /payments/webhook/    — Interswitch server-to-server notification
"""

from django.urls import path

from .views import InitiatePaymentView, PaymentCallbackView, PaymentWebhookView

app_name = 'payments'

urlpatterns = [
    path('initiate/', InitiatePaymentView.as_view(), name='initiate'),
    path('callback/', PaymentCallbackView.as_view(), name='callback'),
    path('webhook/', PaymentWebhookView.as_view(), name='webhook'),
]