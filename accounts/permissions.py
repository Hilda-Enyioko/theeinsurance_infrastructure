from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied
from .models import CustomerKYC


class IsStaffMember(BasePermission):
    """
    Super Admin or Support Admin 
    any platform staff role
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in ("super_admin", "support_admin")
        )

class IsSuperAdmin(BasePermission):
    """
    TheeInsurance staff only.
    """
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "super_admin"
        )


class IsPartnerAdmin(BasePermission):
    """
    Any partner_admin regardless of partner type (provider or distributor).
    """
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "partner_admin"
        )


class IsPartnerAdminOfOwnOrg(BasePermission):
    def has_permission(self, request, view):
        profile = getattr(request.user, "partner_admin_profile", None)
        return bool(profile and profile.role == "partner_admin")

    def has_object_permission(self, request, view, obj):
        # obj is the PartnerAdmin being created/modified — must be same org
        return obj.partner_id == request.user.partner_admin_profile.partner_id


class IsCustomer(BasePermission):
    """
    End users (customers) only.
    """
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "customer"
        )


class IsKYCVerifiedCustomer(IsCustomer):
    """Customers may browse plans without KYC, but can only purchase with APPROVED KYC."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        kyc = CustomerKYC.objects.filter(
            customer__user=request.user, customer__partner=getattr(request, "partner", None)
        ).first()
        status_ = kyc.status if kyc else "not_submitted"
        if status_ != "approved":
            raise PermissionDenied({
                "error": "Complete KYC verification before purchasing a plan.",
                "code": "kyc_required",
                "kyc_status": status_,
            })
        return True


class IsProviderAdmin(BasePermission):
    """
    partner_admin users whose partner is of type 'provider'.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role != "partner_admin":
            return False
        partner = getattr(request.user, 'partner_admin_profile', None)
        if not partner:
            return False
        return partner.partner.partner_type == "provider"


class IsDistributorAdmin(BasePermission):
    """
    partner_admin users whose partner is of type 'distributor'.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role != "partner_admin":
            return False
        partner = getattr(request.user, 'partner_admin_profile', None)
        if not partner:
            return False
        return partner.partner.partner_type == "distributor"


class IsProviderOrSuperAdmin(BasePermission):
    """
    Allows access to both provider admins and TheeInsurance super admins.
    Useful for shared review endpoints.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role == "super_admin":
            return True
        if request.user.role != "partner_admin":
            return False
        partner = getattr(request.user, 'partner_admin_profile', None)
        if not partner:
            return False
        return partner.partner.partner_type == "provider"


class IsServiceAccount(BasePermission):
    """
    For internal service-to-service endpoints (e.g. n8n → Django).
    Requires authentication — n8n must call with a valid JWT
    belonging to a dedicated service account user (role='service').
    """
class IsServiceAccount(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) == "service_account"
        )


class IsHumanStaff(BasePermission):
    """super_admin or support_admin, never a service account."""
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role in ("super_admin", "support_admin"))
