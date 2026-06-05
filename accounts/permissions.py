from rest_framework.permissions import BasePermission

class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "super_admin"
        )


class IsPartnerAdmin(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "partner_admin"
        )


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "customer"
        )


class IsProviderAdmin(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role != "partner_admin":
            return False
        partner = getattr(
            request.user, 'partner_admin_profile', None
        )
        if not partner:
            return False
        return partner.partner.partner_type == "provider"


class IsDistributorAdmin(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.role != "partner_admin":
            return False
        partner = getattr(
            request.user, 'partner_admin_profile', None
        )
        if not partner:
            return False
        return partner.partner.partner_type == "distributor"