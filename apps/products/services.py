from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.accounts.models import Role

from .models import (
    InventoryMovement,
    InventoryMovementUnit,
    InventoryUnit,
    MovementType,
    Product,
    UnitCondition,
    UnitStatus,
    allow_movement_creation,
)


AUTHORIZED_STOCK_ROLES = (Role.OWNER, Role.SECRETARY)
POSITIVE_MOVEMENTS = (MovementType.STOCK_RECEIVED, MovementType.CUSTOMER_RETURN)
NEGATIVE_MOVEMENTS = (MovementType.STOCK_SOLD, MovementType.SUPPLIER_RETURN)
_employee_sale_stock_allowed = ContextVar("employee_sale_stock_allowed", default=False)


@contextmanager
def employee_sale_stock_context():
    """Narrowly allow an employee's stock decrement while recording a sale."""
    token = _employee_sale_stock_allowed.set(True)
    try:
        yield
    finally:
        _employee_sale_stock_allowed.reset(token)


@transaction.atomic
def record_stock_movement(
    *, product, quantity, movement_type, user, reason, reference="", unit_identifiers=(),
    unit_condition=UnitCondition.NEW,
):
    """Apply one authorized stock change and append its immutable history row.

    Tracked products require one unique identifier for every unit entering or
    leaving stock. ``quantity`` is signed; approved adjustments may go either way.
    """
    manager_authorized = user.is_authenticated and user.role in AUTHORIZED_STOCK_ROLES
    employee_sale_authorized = (
        user.is_authenticated
        and user.role == Role.EMPLOYEE
        and _employee_sale_stock_allowed.get()
        and movement_type == MovementType.STOCK_SOLD
    )
    if not (manager_authorized or employee_sale_authorized):
        raise PermissionDenied("Your role cannot make inventory movements.")
    if movement_type not in MovementType.values:
        raise ValidationError({"movement_type": "Choose a valid inventory movement type."})
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity == 0:
        raise ValidationError({"quantity": "Quantity must be a non-zero whole number."})
    if movement_type in POSITIVE_MOVEMENTS and quantity < 1:
        raise ValidationError({"quantity": "This movement type must add stock."})
    if movement_type in NEGATIVE_MOVEMENTS and quantity > -1:
        raise ValidationError({"quantity": "This movement type must remove stock."})
    if not reason or not reason.strip():
        raise ValidationError({"reason": "Provide a reason or reference for the movement."})

    identifiers = [value.strip() for value in unit_identifiers if value and value.strip()]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError({"unit_identifiers": "Identifiers in one movement must be unique."})

    locked_product = Product.objects.select_for_update().get(pk=getattr(product, "pk", product))
    if locked_product.track_individual_units:
        if len(identifiers) != abs(quantity):
            raise ValidationError({"unit_identifiers": "Provide one serial number or IMEI for each unit moved."})
        if unit_condition not in UnitCondition.values:
            raise ValidationError({"unit_condition": "Choose a valid unit condition."})
    elif identifiers:
        raise ValidationError({"unit_identifiers": "This product does not track individual units."})

    previous_stock = locked_product.stock_quantity
    resulting_stock = previous_stock + quantity
    if resulting_stock < 0:
        raise ValidationError({"quantity": "This movement would make stock negative."})

    units = []
    if locked_product.track_individual_units:
        if movement_type in (MovementType.STOCK_RECEIVED,):
            if InventoryUnit.objects.filter(product=locked_product, identifier__in=identifiers).exists():
                raise ValidationError({"unit_identifiers": "A serial number or IMEI is already registered for this product."})
            units = [
                InventoryUnit(product=locked_product, identifier=identifier, condition=unit_condition, status=UnitStatus.AVAILABLE)
                for identifier in identifiers
            ]
            InventoryUnit.objects.bulk_create(units)
        elif movement_type == MovementType.CUSTOMER_RETURN:
            units = list(InventoryUnit.objects.select_for_update().filter(
                product=locked_product, identifier__in=identifiers, status=UnitStatus.SOLD
            ))
            _require_all_units_found(units, identifiers)
            InventoryUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(
                status=UnitStatus.AVAILABLE, condition=unit_condition
            )
        elif movement_type == MovementType.STOCK_SOLD:
            units = list(InventoryUnit.objects.select_for_update().filter(
                product=locked_product, identifier__in=identifiers, status=UnitStatus.AVAILABLE
            ))
            _require_all_units_found(units, identifiers)
            InventoryUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(status=UnitStatus.SOLD)
        elif movement_type == MovementType.SUPPLIER_RETURN:
            units = list(InventoryUnit.objects.select_for_update().filter(
                product=locked_product, identifier__in=identifiers, status=UnitStatus.AVAILABLE
            ))
            _require_all_units_found(units, identifiers)
            InventoryUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(status=UnitStatus.SUPPLIER_RETURNED)
        else:  # approved stock adjustment
            if quantity > 0:
                if InventoryUnit.objects.filter(product=locked_product, identifier__in=identifiers).exists():
                    raise ValidationError({"unit_identifiers": "A serial number or IMEI is already registered for this product."})
                units = [
                    InventoryUnit(product=locked_product, identifier=identifier, condition=unit_condition, status=UnitStatus.AVAILABLE)
                    for identifier in identifiers
                ]
                InventoryUnit.objects.bulk_create(units)
            else:
                units = list(InventoryUnit.objects.select_for_update().filter(
                    product=locked_product, identifier__in=identifiers, status=UnitStatus.AVAILABLE
                ))
                _require_all_units_found(units, identifiers)
                InventoryUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(status=UnitStatus.ADJUSTED_OUT)

        if movement_type in (MovementType.STOCK_RECEIVED, MovementType.APPROVED_ADJUSTMENT) and quantity > 0:
            units = list(InventoryUnit.objects.filter(product=locked_product, identifier__in=identifiers))

    with allow_movement_creation():
        movement = InventoryMovement.objects.create(
            product=locked_product,
            quantity=quantity,
            movement_type=movement_type,
            responsible_user=user,
            reason=reason.strip(),
            reference=reference.strip(),
            previous_stock=previous_stock,
            resulting_stock=resulting_stock,
        )
    if units:
        InventoryMovementUnit.objects.bulk_create([
            InventoryMovementUnit(movement=movement, unit=unit) for unit in units
        ])
    Product.objects.filter(pk=locked_product.pk)._apply_movement_delta(quantity)
    return movement


@transaction.atomic
def quarantine_customer_return_units(*, product, unit_identifiers, user, unit_condition):
    """Mark returned serialized units as non-sellable without inflating sellable stock."""
    if not user.is_authenticated or user.role not in AUTHORIZED_STOCK_ROLES:
        raise PermissionDenied("Only the owner or secretary can disposition returned inventory.")
    locked_product = Product.objects.select_for_update().get(pk=getattr(product, "pk", product))
    if not locked_product.track_individual_units:
        raise ValidationError({"product": "Only individually tracked products have quarantined unit records."})
    identifiers = [value.strip() for value in unit_identifiers if value and value.strip()]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ValidationError({"unit_identifiers": "Provide unique identifiers for every returned unit."})
    if unit_condition not in UnitCondition.values:
        raise ValidationError({"unit_condition": "Choose a valid unit condition."})
    units = list(InventoryUnit.objects.select_for_update().filter(
        product=locked_product, identifier__in=identifiers, status=UnitStatus.SOLD
    ))
    _require_all_units_found(units, identifiers)
    InventoryUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(
        status=UnitStatus.QUARANTINED, condition=unit_condition
    )
    return units


def _require_all_units_found(units, identifiers):
    if len(units) != len(identifiers):
        raise ValidationError({"unit_identifiers": "Each identifier must belong to this product and be in the required availability state."})
