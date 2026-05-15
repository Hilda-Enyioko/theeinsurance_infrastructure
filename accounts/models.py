import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from core.models import Partner


# User Manager

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "super_admin")
        return self.create_user(email, password, **extra_fields)


# Custom User

class CustomUser(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = [
        ("super_admin", "Super Admin"),
        ("partner_admin", "Partner Admin"),
        ("customer", "Customer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.email} ({self.role})"


# Partner Admin Profile

class PartnerAdmin(models.Model):
    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("manager", "Manager"),
        ("viewer", "Viewer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        CustomUser, on_delete=models.CASCADE, related_name="partner_admin_profile"
    )
    partner = models.ForeignKey(
        Partner, on_delete=models.CASCADE, related_name="admins"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="owner")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} — {self.partner.name} ({self.role})"


# Customer Profile 

class CustomerProfile(models.Model):
    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        CustomUser, on_delete=models.CASCADE, related_name="customer_profile"
    )
    partner = models.ForeignKey(
        Partner, on_delete=models.CASCADE, related_name="customers"
    )
    phone_number = models.CharField(max_length=20, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} — {self.partner.name}"


# Customer KYC─

class CustomerKYC(models.Model):
    ID_TYPE_CHOICES = [
        ("nin", "National Identity Number"),
        ("bvn", "Bank Verification Number"),
        ("passport", "International Passport"),
        ("drivers_licence", "Driver's Licence"),
        ("voters_card", "Voter's Card"),
    ]

    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.OneToOneField(
        CustomerProfile, on_delete=models.CASCADE, related_name="kyc"
    )
    id_type = models.CharField(max_length=20, choices=ID_TYPE_CHOICES)
    id_number = models.CharField(max_length=50)
    id_document = models.FileField(upload_to="kyc/customers/id/")
    selfie = models.FileField(upload_to="kyc/customers/selfie/", blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    review_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.customer.user.email} — {self.id_type} ({self.status})"


# Partner KYC

class PartnerKYC(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.OneToOneField(
        Partner, on_delete=models.CASCADE, related_name="kyc"
    )

    # Business identity
    rc_number = models.CharField(max_length=20, unique=True)
    naicom_licence_number = models.CharField(max_length=50, blank=True)
    tax_identification_number = models.CharField(max_length=20, unique=True)

    # Documents
    cac_certificate = models.FileField(upload_to="kyc/partners/cac/")
    naicom_licence_doc = models.FileField(upload_to="kyc/partners/naicom/", blank=True)
    proof_of_address = models.FileField(upload_to="kyc/partners/address/")

    # Review
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    reviewed_by = models.CharField(max_length=255, blank=True)
    review_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.partner.name} — {self.status}"