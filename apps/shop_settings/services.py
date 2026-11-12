from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.accounts.models import Role
from apps.audit.models import record_audit_event

from .models import ShopProfile, allow_profile_mutation


def require_owner(user):
    if not user.is_authenticated or user.role != Role.OWNER or not user.is_superuser:
        raise PermissionDenied("Only the owner can change shop settings.")


@transaction.atomic
def update_shop_profile(*, actor, cleaned_data):
    require_owner(actor)
    profile, created = ShopProfile.objects.get_or_create(singleton_key=1)
    profile = ShopProfile.objects.select_for_update().get(pk=profile.pk)
    editable_fields = (
        "shop_name", "contact_phone", "business_email", "physical_address",
        "notify_low_stock", "notify_pending_credit", "notify_pending_momo",
        "return_window_days",
    )
    changed_fields = [field for field in editable_fields if getattr(profile, field) != cleaned_data[field]]
    if not created and not changed_fields:
        return profile, False
    for field in editable_fields:
        setattr(profile, field, cleaned_data[field])
    profile.updated_by = actor
    with allow_profile_mutation():
        profile.save()
    record_audit_event(
        action="SHOP_SETTINGS_CREATED" if created else "SHOP_SETTINGS_UPDATED",
        actor=actor,
        object_type="shop_profile",
        object_reference=profile.singleton_key,
        details={"affected_settings": changed_fields if not created else editable_fields},
    )
    return profile, True
