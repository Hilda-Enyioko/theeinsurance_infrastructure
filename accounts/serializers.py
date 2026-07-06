from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import CustomUser, CustomerProfile, PartnerAdmin, PartnerKYC, CustomerKYC
from core.models import Partner, DistributorProfile

# Customer registration
class CustomerRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )
    confirm_password = serializers.CharField(write_only=True, required=True)
    phone_number = serializers.CharField(required=True)
    date_of_birth = serializers.DateField(required=True)
    gender = serializers.ChoiceField(
        choices=CustomerProfile.GENDER_CHOICES, required=True
    )
    address = serializers.CharField(required=True)

    class Meta:
        model = CustomUser
        fields = [
            "email",
            "password",
            "confirm_password",
            "first_name",
            "last_name",
            "phone_number",
            "date_of_birth",
            "gender",
            "address",
        ]

    def validate_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            # user exists globally: check if they're already on this partner
            partner = self.context.get("partner")
            user = CustomUser.objects.get(email=value)
            if CustomerProfile.objects.filter(user=user, partner=partner).exists():
                raise serializers.ValidationError(
                    "An account with this email already exists on this platform."
                )
            # Customer exists elsewhere but not here: allowed
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"password": "Passwords do not match."}
            )
        return attrs
    
    def create(self, validated_data):
        phone_number = validated_data.pop("phone_number")
        date_of_birth = validated_data.pop("date_of_birth")
        gender = validated_data.pop("gender")
        address = validated_data.pop("address")
        validated_data.pop("confirm_password")

        partner = self.context.get("partner")
        email = validated_data["email"]

        # reuse existing user if they exist elsewhere
        if CustomUser.objects.filter(email=email).exists():
            user = CustomUser.objects.get(email=email)
        else:
            user = CustomUser.objects.create_user(
                email=email,
                password=validated_data["password"],
                first_name=validated_data["first_name"],
                last_name=validated_data["last_name"],
                role="customer",
            )

        # create a new profile for this partner
        CustomerProfile.objects.create(
            user=user,
            partner=partner,
            phone_number=phone_number,
            date_of_birth=date_of_birth,
            gender=gender,
            address=address,
        )

        return user
    

# Partner Onboarding
class PartnerOnboardingSerializer(serializers.Serializer):
    # partner details
    partner_name = serializers.CharField(max_length=255)
    partner_slug = serializers.CharField(max_length=100)
    partner_type = serializers.ChoiceField(choices=Partner.PARTNER_TYPE_CHOICES)
    commission_rate = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0.00)
    nomba_account_id = serializers.CharField(
        max_length=100, required=False, allow_blank=True, allow_null=True, default=None,
        help_text="Optional. Leave blank if this partner will use Interswitch instead of Nomba."
    )

    # admin user details
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate_partner_slug(self, value):
        if Partner.objects.filter(slug=value).exists():
            raise serializers.ValidationError(
                "A partner with this slug already exists."
            )
        return value
    
    def validate_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )
        return value
    
    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"password": "Passwords do not match."}
            )
        return attrs
    
    def create(self, validated_data):
        partner_type = validated_data.pop("partner_type")
        commission_rate = validated_data.pop("commission_rate", 0.00)
        nomba_account_id = validated_data.pop("nomba_account_id", None)
        validated_data.pop("confirm_password")

        # create partner
        partner = Partner.objects.create(
            name=validated_data.pop("partner_name"),
            slug=validated_data.pop("partner_slug"),
            partner_type=partner_type,
            nomba_account_id=nomba_account_id,
            is_active=False, # inactive until KYC is approved
        )

        # only create distributor profile for distributors
        if partner_type == "distributor":
            DistributorProfile.objects.create(
                partner=partner,
                commission_rate=commission_rate,
            )

        # create admin user
        user = CustomUser.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            role="partner_admin",
        )

        # Link to PartnerAdmin profile
        PartnerAdmin.objects.create(
            user=user,
            partner=partner,
            role="owner",
        )

        return partner

# Login
class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


# Customer KYC
class CustomerKYCSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerKYC
        fields = [
            "id", "id_type", "id_number",
            "id_document", "selfie", "status",
            "submitted_at",
        ]
        read_only_fields = ["id", "status", "submitted_at"]


# Partner KYC
class PartnerKYCSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartnerKYC
        fields = [
            "id", "rc_number", "naicom_licence_number",
            "tax_identification_number", "cac_certificate",
            "naicom_licence_doc", "proof_of_address",
            "status", "submitted_at",
        ]
        read_only_fields = ["id", "status", "submitted_at"]