"""
Small shared helpers used by both accounts and plans views. Kept
dependency-light (no imports from plans) so this module can safely be
imported from any app without circular-import risk.
"""


def get_partner_from_user(user):
    """Returns the Partner a user's PartnerAdmin profile belongs to, or
    None if the user has no such profile (e.g. staff, customer)."""
    if hasattr(user, 'partner_admin_profile'):
        return user.partner_admin_profile.partner
    return None


def is_full_partner_admin(user) -> bool:
    """
    True only for the full partner_admin role within a partner org — not
    support_partner_admin or partner_viewer. CustomUser.role stays coarse
    ('partner_admin' for all three), so the granular check always has to
    read PartnerAdmin.role, never request.user.role directly.
    Used to gate write actions: creating/editing plans, inviting or
    deactivating team members, submitting KYC, requesting or withdrawing
    distributor access.
    """
    profile = getattr(user, "partner_admin_profile", None)
    return profile is not None and profile.role == "partner_admin"
