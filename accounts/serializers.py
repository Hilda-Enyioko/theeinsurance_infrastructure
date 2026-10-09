"""
Serializers for authentication, user profiles, onboarding, and KYC workflows.
"""
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils.text import slugify
from drf_spectacular.utils import extend_schema_field
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


def _unique_slug(name):
    base = slugify(name)[:40] or "partner"
    slug, i = base, 2
    while Partner.objects.filter(slug=slug).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug

class PartnerOnboardingSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    partner_type = serializers.ChoiceField(choices=Partner.PARTNER_TYPE_CHOICES)
    commission_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False,
        help_text="Percentage (0-100). REQUIRED for distributors, NOT ALLOWED for providers.",
    )
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})

    def validate_email(self, value):
        value = value.lower()
        if CustomUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        ptype, rate = attrs["partner_type"], attrs.get("commission_rate")
        if ptype == "distributor" and rate is None:
            raise serializers.ValidationError({"commission_rate": "Required for distributors."})
        if ptype == "provider" and rate is not None:
            raise serializers.ValidationError({"commission_rate": "Not applicable to providers."})
        return attrs

    @transaction.atomic
    def create(self, vd):
        partner = Partner.objects.create(
            name=vd["name"], slug=_unique_slug(vd["name"]), partner_type=vd["partner_type"],
            phone_number=vd.get("phone_number", ""), address=vd.get("address", ""),
            is_active=False,
        )  # Partner.save() sets partner._raw_api_key
        if partner.partner_type == "distributor":
            DistributorProfile.objects.create(partner=partner, commission_rate=vd["commission_rate"])
        else:
            ProviderProfile.objects.create(partner=partner)

        user = CustomUser.objects.create_user(
            email=vd["email"], password=vd["password"], first_name=vd["first_name"],
            last_name=vd["last_name"], role="partner_admin",
        )
        PartnerAdmin.objects.create(user=user, partner=partner, role="partner_admin")
        self.admin_user = user
        return partner


# ---- Partner profile (post-KYC) ---------------------------------------------------
class SettlementSerializer(serializers.Serializer):
    account_name = serializers.CharField(source="settlement_account_name", max_length=255)
    account_number = serializers.RegexField(r"^\d{10}$", source="settlement_bank_account",
                                            error_messages={"invalid": "Must be a 10-digit NUBAN account number."})
    bank_code = serializers.RegexField(r"^\d{3,6}$", source="settlement_bank_code",
                                       error_messages={"invalid": "Must be 3-6 digits."})


def settlement_profile(partner):
    return partner.distributor_profile if partner.partner_type == "distributor" else partner.provider_profile


class PartnerProfileUpdateSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20, required=False)
    address = serializers.CharField(required=False)
    website = serializers.URLField(required=False, allow_blank=True)
    settlement = SettlementSerializer(required=False)

    @transaction.atomic
    def update(self, partner, vd):
        settlement = vd.pop("settlement", None)
        if vd:
            for k, v in vd.items():
                setattr(partner, k, v)
            partner.save(update_fields=list(vd))
        if settlement:
            profile = settlement_profile(partner)
            for k, v in settlement.items():
                setattr(profile, k, v)
            profile.save()
        return partner


class PartnerProfileSerializer(serializers.ModelSerializer):
    commission_rate = serializers.DecimalField(
        source="distributor_profile.commission_rate", max_digits=5, decimal_places=2,
        read_only=True, help_text="Distributors only. Set by TheeInsurance staff.")
    naicom_licence_number = serializers.CharField(
        source="provider_profile.naicom_licence_number", read_only=True, help_text="Providers only.")
    settlement = serializers.SerializerMethodField()
    kyc_status = serializers.SerializerMethodField()

    class Meta:
        model = Partner
        fields = ["id", "name", "slug", "partner_type", "is_active", "phone_number", "address",
                  "website", "commission_rate", "naicom_licence_number", "settlement", "kyc_status"]
        read_only_fields = fields

    @extend_schema_field(SettlementSerializer)
    def get_settlement(self, obj):
        return SettlementSerializer(settlement_profile(obj)).data

    @extend_schema_field(serializers.CharField())
    def get_kyc_status(self, obj):
        return obj.kyc.status if hasattr(obj, "kyc") else "not_submitted"


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


class PartnerMeSerializer(serializers.Serializer):
    partner_type = serializers.CharField(source='partner_admin_profile.partner.partner_type')
    partner_name = serializers.CharField(source='partner_admin_profile.partner.name')
    is_active = serializers.BooleanField(source='partner_admin_profile.partner.is_active')


class PartnerPasswordConfirmSerializer(serializers.Serializer):
    """
    Shared by both key-retrieval and key-regeneration — requires the
    authenticated partner admin to re-enter their password before either
    viewing or rotating the sensitive api_key.
    """
    password = serializers.CharField(write_only=True, required=True)

    def validate_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Incorrect password.")
        return value


class CustomerProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    settlement = serializers.SerializerMethodField()
    settlement_complete = serializers.BooleanField(
        source="has_settlement", read_only=True,
        help_text="True when account name, number and bank code are all on file. Required before a claim can be approved.")

    class Meta:
        model = CustomerProfile
        fields = ["id", "email", "first_name", "last_name", "phone_number", "date_of_birth",
                  "gender", "address", "settlement", "settlement_complete"]
        read_only_fields = fields

    @extend_schema_field(SettlementSerializer)
    def get_settlement(self, obj):
        return SettlementSerializer(obj).data


class CustomerProfileUpdateSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20, required=False)
    address = serializers.CharField(required=False)
    settlement = SettlementSerializer(required=False)

    def validate_settlement(self, value):
        needed = {"settlement_account_name", "settlement_bank_account", "settlement_bank_code"}
        if needed - value.keys():
            raise serializers.ValidationError(
                "account_name, account_number and bank_code must be provided together.")
        return value

    @transaction.atomic
    def update(self, profile, vd):
        settlement = vd.pop("settlement", None) or {}
        for k, v in {**vd, **settlement}.items():
            setattr(profile, k, v)
        profile.save()
        return profile
