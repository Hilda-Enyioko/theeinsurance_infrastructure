from drf_spectacular.utils import OpenApiExample, OpenApiResponse, inline_serializer
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers as s


class Tag:
    PARTNERS = "1. Partners"
    PROVIDERS = "2. Providers"
    DISTRIBUTORS = "3. Distributors"
    CUSTOMERS = "4. Customers"
    STAFF = "5. Staff"
    PLANS = "6. Plans"
    SUBSCRIPTIONS = "7. Subscriptions"
    CLAIMS = "8. Claims"
    WEBHOOKS = "9. Webhooks"
    ACCESS_GRANT = "10. Access Grant"
    SERVICE = "11. Service (n8n)"


ErrorSerializer = inline_serializer("Error", {"error": s.CharField()})
DetailSerializer = inline_serializer("Detail", {"detail": s.CharField()})
MessageSerializer = inline_serializer("Message", {"message": s.CharField()})
TokenPairSerializer = inline_serializer(
    "TokenPair", {"refresh": s.CharField(), "access": s.CharField()}
)

SAMPLE_REFRESH = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoicmVmcmVzaCIs..."
SAMPLE_ACCESS = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiw..."
SAMPLE_TOKENS = {"refresh": SAMPLE_REFRESH, "access": SAMPLE_ACCESS}

PARTNER_KEY_AUTH = [{"BearerAuth": [], "PartnerKey": []}]
SERVICE_AUTH = [{"BearerAuth": []}]


def error(description, example, key="error"):
    """Standard {"error": "..."} / {"detail": "..."} response doc."""
    return OpenApiResponse(
        response=ErrorSerializer if key == "error" else DetailSerializer,
        description=description,
        examples=[OpenApiExample("Example", value={key: example})],
    )


def validation_error(field="email", message="Enter a valid email address."):
    return OpenApiResponse(
        response=OpenApiTypes.OBJECT,
        description="Validation failed. Keys are field names, values are lists of messages.",
        examples=[OpenApiExample("Validation error", value={field: [message]})],
    )
