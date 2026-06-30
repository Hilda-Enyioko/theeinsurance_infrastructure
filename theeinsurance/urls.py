"""
URL configuration for theeinsurance project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView
)

def health_check(request):
    return JsonResponse({"status": "ok"})

urlpatterns = [
    # Health check
    path("", health_check),
    path("health/", health_check),
    
    # Core Admin
    path('admin/', admin.site.urls),
    
    # Core API Resources (v1)
    path('api/v1/', include('accounts.urls')),
    path('api/v1/', include('plans.urls')),
    path('api/v1/', include('subscriptions.urls')),
    path('api/v1/', include('claims.urls')),
    path('api/v1/payments/', include(('payments.urls', 'payments'), namespace='payments')),
    
    # Partner API Resources (v1)
    path('api/v1/partner/', include(('analytics.urls', 'analytics'), namespace='partner-analytics')),
    # path('api/v1/partner/', include(('webhooks.urls', 'webhooks'), namespace='partner-webhooks')),
    
    # N8N API Resources
    path('api/v1/webhooks/', include(('webhooks.urls', 'webhooks'), namespace='register-n8n-webhooks')),
    
    # OpenAPI Schema & Documentation Engine
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# Serve Media Assets locally during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
