from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.audit.models import record_audit_event

from .models import Role, User, allow_staff_write

ASSIGNABLE_ROLES = (Role.SECRETARY, Role.EMPLOYEE)


def require_owner(user):
    if not user.is_authenticated or user.role != Role.OWNER or not user.is_superuser:
        raise PermissionDenied("Only the owner can manage staff accounts.")


@transaction.atomic
def create_staff_account(*, actor, username, first_name, last_name, email="", role, password, is_active=True):
    require_owner(actor)
    if role not in ASSIGNABLE_ROLES:
        raise ValidationError({"role": "Staff accounts can only be assigned Secretary or Employee roles."})
    user = User(
        username=username.strip(), first_name=first_name.strip(), last_name=last_name.strip(),
        email=email.strip(), role=role, is_active=is_active, must_change_password=True,
    )
    validate_password(password, user=user)
    user.set_password(password)
    with allow_staff_write():
        user.save()
    record_audit_event(
        action="STAFF_ACCOUNT_CREATED", actor=actor, object_type="user", object_reference=user.pk,
        details={"username": user.username, "role": user.role},
    )
    return user


@transaction.atomic
def update_staff_account(*, actor, target, first_name=None, last_name=None, email=None, role=None, is_active=None):
    require_owner(actor)
    target_id = getattr(target, "pk", target)
    # Lock active owner rows in a stable order so concurrent demotions cannot
    # both observe themselves as the last active owner.
    active_owner_ids = list(
        User.objects.select_for_update().filter(role=Role.OWNER, is_active=True)
        .order_by("pk").values_list("pk", flat=True)
    )
    user = User.objects.select_for_update().get(pk=target_id)
    old_role = user.role
    if role is not None:
        if role not in (*ASSIGNABLE_ROLES, Role.OWNER):
            raise ValidationError({"role": "Choose a valid staff role."})
        if role == Role.OWNER and user.role != Role.OWNER:
            raise ValidationError({"role": "A staff account cannot be promoted to Owner."})
    changes = {}
    for field, value in (
        ("first_name", first_name), ("last_name", last_name), ("email", email),
        ("role", role), ("is_active", is_active),
    ):
        if value is not None and getattr(user, field) != value:
            changes[field] = value
    loses_active_owner = (
        user.pk in active_owner_ids
        and (changes.get("role", user.role) != Role.OWNER or changes.get("is_active", user.is_active) is False)
    )
    if loses_active_owner and len(active_owner_ids) <= 1:
        raise ValidationError("The last active owner account cannot be deactivated or demoted.")
    if not changes:
        return user
    for field, value in changes.items():
        setattr(user, field, value)
    with allow_staff_write():
        user.save()
    if "role" in changes:
        record_audit_event(
            action="STAFF_ROLE_CHANGED", actor=actor, object_type="user", object_reference=user.pk,
            details={"username": user.username, "old_role": old_role, "new_role": user.role},
        )
    if "is_active" in changes:
        record_audit_event(
            action="STAFF_ACCOUNT_ACTIVATED" if user.is_active else "STAFF_ACCOUNT_DEACTIVATED",
            actor=actor, object_type="user", object_reference=user.pk,
            details={"username": user.username},
        )
    profile_fields = set(changes) - {"role", "is_active"}
    if profile_fields:
        record_audit_event(
            action="STAFF_PROFILE_UPDATED", actor=actor, object_type="user", object_reference=user.pk,
            details={"username": user.username, "changed_fields": sorted(profile_fields)},
        )
    return user


@transaction.atomic
def reset_staff_password(*, actor, target, password):
    require_owner(actor)
    target_id = getattr(target, "pk", target)
    user = User.objects.select_for_update().get(pk=target_id)
    if user.pk == actor.pk:
        raise ValidationError("Use your personal password change page to change your own password.")
    validate_password(password, user=user)
    user.set_password(password)
    user.must_change_password = True
    with allow_staff_write():
        user.save(update_fields=("password", "must_change_password", "is_staff", "is_superuser"))
    record_audit_event(
        action="STAFF_PASSWORD_RESET", actor=actor, object_type="user", object_reference=user.pk,
        details={"username": user.username, "must_change_password": True},
    )
    return user


@transaction.atomic
def change_own_password(*, user, password):
    validate_password(password, user=user)
    user.set_password(password)
    user.must_change_password = False
    with allow_staff_write():
        user.save(update_fields=("password", "must_change_password", "is_staff", "is_superuser"))
    record_audit_event(
        action="STAFF_PASSWORD_CHANGED", actor=user, object_type="user", object_reference=user.pk,
        details={"username": user.username},
    )
    return user
