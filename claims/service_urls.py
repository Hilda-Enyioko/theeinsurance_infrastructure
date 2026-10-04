from django.urls import path
from .service_views import (
    ServiceClaimAiResultView, ServiceClaimReceiptView, ServiceClaimNotifiedView,
)

urlpatterns = [
    path("<uuid:claim_id>/ai-result/", ServiceClaimAiResultView.as_view(), name="service-claim-ai-result"),
    path("<uuid:claim_id>/receipt/", ServiceClaimReceiptView.as_view(), name="service-claim-receipt"),
    path("<uuid:claim_id>/notified/", ServiceClaimNotifiedView.as_view(), name="service-claim-notified"),
]