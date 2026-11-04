from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.accounts.models import Role
from apps.audit.models import record_audit_event

from apps.sales.models import Payment, PaymentStatus, SaleStatus, SaleType

from .models import Customer, allow_customer_mutation, normalise_phone


SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)


def credit_totals(sales):
    """Return approved credit sales, total value, confirmed payments, and balance."""
    credit_sales = sales.filter(sale_type=SaleType.CREDIT, status=SaleStatus.APPROVED)
    total_sales = credit_sales.aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    total_paid = Payment.objects.filter(
        sale__in=credit_sales, status=PaymentStatus.CONFIRMED
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    return credit_sales, total_sales, total_paid, total_sales - total_paid


def _require_shop_user(user):
    if not user.is_authenticated or user.role not in SHOP_ROLES:
        raise PermissionDenied("Sign in with an authorized shop account.")


@transaction.atomic
def create_customer(*, user, full_name, phone_number, email="", address=""):
    _require_shop_user(user)
    phone_key = normalise_phone(phone_number)
    if Customer.objects.filter(normalized_phone=phone_key).exists():
        raise ValidationError({"phone_number": "A customer with this phone number already exists."})
    customer = Customer(
        full_name=full_name,
        phone_number=phone_number,
        normalized_phone=phone_key,
        email=email,
        address=address,
        created_by=user,
    )
    try:
        with transaction.atomic():
            with allow_customer_mutation():
                customer.save()
    except IntegrityError as exc:
        raise ValidationError({"phone_number": "A customer with this phone number already exists."}) from exc
    record_audit_event(
        action="CUSTOMER_CREATED", actor=user, object_type="customer", object_reference=customer.pk,
        details={"full_name": customer.full_name, "phone_number": customer.phone_number},
    )
    return customer


@transaction.atomic
def update_customer(*, customer, user, full_name=None, phone_number=None, email=None, address=None, is_active=None):
    _require_shop_user(user)
    if user.role not in MANAGER_ROLES:
        raise PermissionDenied("Only the owner or secretary can edit customer records.")
    customer_id = getattr(customer, "pk", customer)
    locked = Customer.objects.select_for_update().get(pk=customer_id)
    changes = {}
    for field, value in (("full_name", full_name), ("phone_number", phone_number), ("email", email), ("address", address)):
        if value is not None and getattr(locked, field) != value:
            changes[field] = value
    if is_active is not None and locked.is_active != is_active:
        changes["is_active"] = is_active
    for field, value in changes.items():
        setattr(locked, field, value)
    with allow_customer_mutation():
        locked.save()
    if changes:
        action = "CUSTOMER_DEACTIVATED" if changes.get("is_active") is False else (
            "CUSTOMER_ACTIVATED" if changes.get("is_active") is True else "CUSTOMER_UPDATED"
        )
        details = {
            "changed_fields": sorted(changes),
            "new_values": {field: getattr(locked, field) for field in changes},
        }
        record_audit_event(
            action=action, actor=user, object_type="customer", object_reference=locked.pk, details=details,
        )
        other_changes = set(changes) - {"is_active"}
        if other_changes and action in ("CUSTOMER_DEACTIVATED", "CUSTOMER_ACTIVATED"):
            record_audit_event(
                action="CUSTOMER_UPDATED", actor=user, object_type="customer", object_reference=locked.pk,
                details={"changed_fields": sorted(other_changes)},
            )
    return locked
