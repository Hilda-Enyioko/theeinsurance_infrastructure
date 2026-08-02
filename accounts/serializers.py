"""
Serializers for authentication, user profiles, onboarding, and KYC workflows.
"""

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from core.models import DistributorProfile, Partner, ProviderProfile
from .models import CustomUser, CustomerKYC, CustomerProfile, PartnerAdmin, PartnerKYC, Staff


class CustomerRegistrationSerializer(serializers.ModelSerializer):
    """
    Handles registration details for creating a new customer profile.
    """

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
        """Metadata options for CustomerRegistrationSerializer."""
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
        """
        Verify that the email is unique within the context of the current partner platform.
        """
        if CustomUser.objects.filter(email=value).exists():
            partner = self.context.get("partner")
            user = CustomUser.objects.get(email=value)
            if CustomerProfile.objects.filter(user=user, partner=partner).exists():
                raise serializers.ValidationError(
                    "An account with this email already exists on this platform."
                )
        return value

    def validate(self, attrs):
        """
        Validate that the provided password and confirm_password fields match perfectly.
        """
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"password": "Passwords do not match."}
            )
        return attrs

    def create(self, validated_data):
        """
        Creates a CustomUser instance along with its nested CustomerProfile.
        """
        phone_number = validated_data.pop("phone_number")
        date_of_birth = validated_data.pop("date_of_birth")
        gender = validated_data.pop("gender")
        address = validated_data.pop("address")
        validated_data.pop("confirm_password")

        partner = self.context.get("partner")
        email = validated_data["email"]

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

        CustomerProfile.objects.create(
            user=user,
            partner=partner,
            phone_number=phone_number,
            date_of_birth=date_of_birth,
            gender=gender,
            address=address,
        )

        return user


class PartnerOnboardingSerializer(serializers.Serializer):
    """
    Validates payload structure and sets up an entirely new Partner enterprise 
    alongside its founding Administrator account.
    """

    # Partner details
    partner_name = serializers.CharField(max_length=255)
    partner_slug = serializers.CharField(max_length=100)
    partner_type = serializers.ChoiceField(choices=Partner.PARTNER_TYPE_CHOICES)

    # Distributor-specific properties
    commission_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=0.00
    )

    # Provider-specific properties
    naicom_licence_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    settlement_bank_account = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    settlement_bank_code = serializers.CharField(
        max_length=10, required=False, allow_blank=True, default=""
    )

    # Primary administrative user fields
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate_partner_slug(self, value):
        """Ensures the organization slug is unique."""
        if Partner.objects.filter(slug=value).exists():
            raise serializers.ValidationError(
                "A partner with this slug already exists."
            )
        return value

    def validate_email(self, value):
        """Ensures the registration email has not been taken globally."""
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )
        return value

    def validate(self, attrs):
        """Validates that password confirmation parameters are congruent."""
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"password": "Passwords do not match."}
            )
        return attrs

    def create(self, validated_data):
        """
        Coordinates creation steps across Partner, internal profile types, 
        and administrative User profiles.
        """
        partner_type = validated_data.pop("partner_type")
        commission_rate = validated_data.pop("commission_rate", 0.00)
        naicom_licence_number = validated_data.pop("naicom_licence_number", "")
        settlement_bank_account = validated_data.pop("settlement_bank_account", "")
        settlement_bank_code = validated_data.pop("settlement_bank_code", "")
        validated_data.pop("confirm_password")

        partner = Partner.objects.create(
            name=validated_data.pop("partner_name"),
            slug=validated_data.pop("partner_slug"),
            partner_type=partner_type,
            is_active=False,  # Inactive status until KYC is successfully checked
        )

        if partner_type == "distributor":
            DistributorProfile.objects.create(
                partner=partner,
                commission_rate=commission_rate,
                settlement_bank_account=settlement_bank_account,
                settlement_bank_code=settlement_bank_code,
            )
        else:
            ProviderProfile.objects.create(
                partner=partner,
                naicom_licence_number=naicom_licence_number,
                settlement_bank_account=settlement_bank_account,
                settlement_bank_code=settlement_bank_code,
            )

        user = CustomUser.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            role="partner_admin",
        )

        PartnerAdmin.objects.create(
            user=user,
            partner=partner,
            role="partner_admin",
        )

        return partner


class PartnerTeamInviteSerializer(serializers.Serializer):
    """
    Invites a new team member into the operational branch of the caller's organization.
    """

    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(
        choices=PartnerAdmin.ROLE_CHOICES, default="partner_viewer"
    )

    def validate_email(self, value):
        """Ensures the prospective email target does not belong to an active user."""
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        """Delegates safe creation behavior to the designated manager layer."""
        inviter = self.context["inviter"]
        return PartnerAdmin.objects.invite_team_member(
            inviter=inviter,
            email=validated_data["email"],
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            role=validated_data["role"],
        )


class PartnerTeamMemberSerializer(serializers.ModelSerializer):
    """
    Read-only presentation mapping to simplify retrieval of existing partner accounts.
    """

    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    invited_by_email = serializers.SerializerMethodField()

    class Meta:
        """Metadata configurations for PartnerTeamMemberSerializer."""
        model = PartnerAdmin
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_active",
            "invited_by_email",
            "created_at",
        ]

    def get_invited_by_email(self, obj):
        """Retrieves email metadata string associated with the parent inviter entity."""
        return obj.invited_by.email if obj.invited_by_id else None


class StaffCreateSerializer(serializers.Serializer):
    """
    Validates structural requirements for systemic staff member generation.
    """

    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(choices=Staff.STAFF_ROLES)
    password = serializers.CharField(
        write_only=True, required=False, allow_null=True, validators=[validate_password]
    )

    def validate_email(self, value):
        """Enforces field uniqueness checks across active instances."""
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        """Instructs manager objects to safely construct core security records."""
        created_by = self.context["created_by"]
        return Staff.objects.create_staff(created_by=created_by, **validated_data)


class StaffSerializer(serializers.ModelSerializer):
    """
    Exposes attributes for serialization of staff level entries.
    """

    created_by_email = serializers.SerializerMethodField()

    class Meta:
        """Metadata options for StaffSerializer."""
        model = Staff
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_active",
            "is_two_factor_enabled",
            "created_by_email",
            "deactivated_at",
            "date_joined",
        ]
        read_only_fields = fields

    def get_created_by_email(self, obj):
        """Extracts text information regarding creation ownership boundaries."""
        return obj.created_by.email if obj.created_by_id else None


class LoginSerializer(serializers.Serializer):
    """
    Simple verification target parsing login input sets.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class CustomerKYCSerializer(serializers.ModelSerializer):
    """
    Serializes KYC compliance metrics mapping to customers.
    """

    class Meta:
        """Metadata rules for CustomerKYCSerializer."""
        model = CustomerKYC
        fields = [
            "id",
            "id_type",
            "id_number",
            "id_document",
            "selfie",
            "status",
            "submitted_at",
        ]
        read_only_fields = ["id", "status", "submitted_at"]


class PartnerKYCSerializer(serializers.ModelSerializer):
    """
    Serializes entity-level KYC verification documentation for businesses.
    """

    class Meta:
        """Metadata constraints governing PartnerKYC fields."""
        model = PartnerKYC
        fields = [
            "id",
            "rc_number",
            "naicom_licence_number",
            "tax_identification_number",
            "cac_certificate",
            "naicom_licence_doc",
            "proof_of_address",
            "status",
            "submitted_at",
        ]
        read_only_fields = ["id", "status", "submitted_at"]
