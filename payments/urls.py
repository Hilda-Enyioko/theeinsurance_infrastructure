"""
Payments URL Configuration.

Endpoints:
  POST /payments/initiate/   — initiate a payment, get Interswitch payment URL
  GET  /payments/callback/   — user lands here after Interswitch redirect
  POST /payments/webhook/    — Interswitch server-to-server notification
"""

from django.urls import path

from .views import (
  InitiatePaymentView,
  PaymentCallbackView,
  InterswitchWebhookView,
  NombaCheckoutView,
  # NombaWebhookView,
  # NombaRenewalChargeView,
  # DunningFinalFailureView,
)

app_name = 'payments'

urlpatterns = [
  # Interswitch Payment Endpoints
  path('initiate/', InitiatePaymentView.as_view(), name='initiate'),
  path('callback/', PaymentCallbackView.as_view(), name='callback'),
  path('interswitch/webhook/', InterswitchWebhookView.as_view(), name='webhook'),
  
  # Nomba Payment Endpoints
  path('nomba/checkout/', NombaCheckoutView.as_view(), name='nomba-checkout'),
]