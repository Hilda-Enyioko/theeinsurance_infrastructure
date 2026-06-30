from rest_framework.permissions import BasePermission

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
