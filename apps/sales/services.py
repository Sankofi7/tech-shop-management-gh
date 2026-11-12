import uuid
from contextlib import contextmanager
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import record_audit_event
from apps.customers.models import Customer
from apps.products.models import InventoryUnit, MovementType, Product, UnitStatus
from apps.products.services import employee_sale_stock_context, record_stock_movement

from .models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    Sale,
    SaleItem,
    SaleItemUnit,
    SaleStatus,
    SaleType,
    new_payment_reference,
    _payment_mutation_allowed,
    _sale_item_creation_allowed,
    _sale_mutation_allowed,
)


MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)
CENT = Decimal("0.01")
MIN_REASON_LENGTH = 5


@contextmanager
def _allow_sale_mutations():
    token = _sale_mutation_allowed.set(True)
    try:
        yield
    finally:
        _sale_mutation_allowed.reset(token)


@contextmanager
def _allow_payment_mutations():
    token = _payment_mutation_allowed.set(True)
    try:
        yield
    finally:
        _payment_mutation_allowed.reset(token)


@contextmanager
def _allow_sale_item_creation():
    token = _sale_item_creation_allowed.set(True)
    try:
        yield
    finally:
        _sale_item_creation_allowed.reset(token)


def _require_signed_in(user):
    if not user.is_authenticated or user.role not in (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE):
        raise PermissionDenied("Sign in with an authorized shop account to record a sale.")


def _manager(user):
    return user.role in MANAGER_ROLES


def _amount(value):
    if isinstance(value, float):
        raise ValidationError({"amount": "Use a decimal payment amount, not a floating-point value."})
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValidationError({"amount": "Enter a valid payment amount."}) from exc
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValidationError({"amount": "Payment must be positive and use no more than two decimal places."})
    return amount


def _reason(value, *, action):
    reason = (value or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError({"reason": f"Enter a meaningful reason of at least {MIN_REASON_LENGTH} characters to {action}."})
    return reason


def _normalise_lines(lines):
    normalised = []
    for line in lines:
        product_id = getattr(line.get("product"), "pk", line.get("product"))
        quantity = line.get("quantity")
        raw_quantity = str(quantity).strip()
        if isinstance(quantity, bool) or not raw_quantity.isdigit():
            raise ValidationError({"items": "Each item needs a whole-number quantity."})
        quantity = int(raw_quantity)
        if quantity < 1:
            raise ValidationError({"items": "Each item quantity must be at least one."})
        identifiers = line.get("unit_identifiers", ())
        if isinstance(identifiers, str):
            identifiers = identifiers.splitlines()
        identifiers = [value.strip() for value in identifiers if value and value.strip()]
        normalised.append({"product_id": product_id, "quantity": quantity, "unit_identifiers": identifiers})
    if not normalised:
        raise ValidationError({"items": "Add at least one product to the sale."})
    return normalised


@transaction.atomic
def create_sale(*, user, sale_type, lines, initial_payment=None, customer=None, submission_key=None):
    _require_signed_in(user)
    if submission_key:
        try:
            submission_key = uuid.UUID(str(submission_key))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"submission_key": "Invalid checkout submission key."}) from exc
        existing_sale = Sale.objects.filter(submission_key=submission_key).first()
        if existing_sale:
            if existing_sale.created_by_id != user.pk:
                raise PermissionDenied("This checkout submission belongs to a different user.")
            return existing_sale
    if sale_type not in SaleType.values:
        raise ValidationError({"sale_type": "Choose cash or credit."})
    if sale_type == SaleType.CREDIT and customer is None:
        raise ValidationError({"customer": "A customer is required for every credit sale."})
    if customer is not None:
        customer_id = getattr(customer, "pk", customer)
        customer = Customer.objects.select_for_update().get(pk=customer_id)
        if not customer.is_active:
            raise ValidationError({"customer": "Choose an active customer."})
    lines = _normalise_lines(lines)
    product_ids = {line["product_id"] for line in lines}
    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk")
    }
    if len(products) != len(product_ids):
        raise ValidationError({"items": "One or more selected products no longer exist."})

    quantities = {}
    seen_units = set()
    line_totals = []
    for line in lines:
        product = products[line["product_id"]]
        if not product.is_active:
            raise ValidationError({"items": f"{product.name} is inactive and cannot be sold."})
        quantities[product.pk] = quantities.get(product.pk, 0) + line["quantity"]
        if product.track_individual_units:
            if len(line["unit_identifiers"]) != line["quantity"]:
                raise ValidationError({"items": f"Select one available serial number or IMEI for each {product.name} unit."})
            for identifier in line["unit_identifiers"]:
                key = (product.pk, identifier)
                if key in seen_units:
                    raise ValidationError({"items": "A serialized unit cannot appear more than once in a sale."})
                seen_units.add(key)
            available = set(InventoryUnit.objects.filter(
                product=product, identifier__in=line["unit_identifiers"], status=UnitStatus.AVAILABLE
            ).values_list("identifier", flat=True))
            if available != set(line["unit_identifiers"]):
                raise ValidationError({"items": f"A selected {product.name} serial number is unavailable."})
        elif line["unit_identifiers"]:
            raise ValidationError({"items": f"{product.name} does not use individual serial tracking."})
        line_totals.append(product.selling_price * line["quantity"])

    for product_id, quantity in quantities.items():
        if products[product_id].stock_quantity < quantity:
            raise ValidationError({"items": f"Insufficient stock for {products[product_id].name}."})

    total = sum(line_totals, Decimal("0.00")).quantize(CENT)
    if total <= 0:
        raise ValidationError({"items": "A sale must have a positive total."})
    is_employee_credit = user.role == Role.EMPLOYEE and sale_type == SaleType.CREDIT
    if is_employee_credit and initial_payment is not None:
        raise ValidationError({"initial_payment": "Payment can be recorded after this credit sale is approved."})
    if sale_type == SaleType.CASH:
        if not initial_payment:
            raise ValidationError({"initial_payment": "A cash sale must record payment for the full total."})
        if _amount(initial_payment.get("amount")) != total:
            raise ValidationError({"initial_payment": "A cash sale must be paid in full at creation."})

    with _allow_sale_mutations():
        sale = Sale.objects.create(
            submission_key=submission_key,
            created_by=user,
            sale_type=sale_type,
            status=SaleStatus.PENDING_APPROVAL if is_employee_credit else SaleStatus.APPROVED,
            subtotal=total,
            total=total,
            customer=customer,
            customer_name_snapshot=customer.full_name if customer else "",
            customer_phone_snapshot=customer.phone_number if customer else "",
            decided_by=user if (sale_type == SaleType.CREDIT and not is_employee_credit) else None,
            decided_at=timezone.now() if (sale_type == SaleType.CREDIT and not is_employee_credit) else None,
        )

    created_items = []
    for line in lines:
        product = products[line["product_id"]]
        item_total = (product.selling_price * line["quantity"]).quantize(CENT)
        # New sale lines are append-only and can only be created during a sale.
        with _allow_sale_item_creation():
            item = SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=line["quantity"],
                unit_price=product.selling_price,
                line_total=item_total,
            )
        if line["unit_identifiers"]:
            units = list(InventoryUnit.objects.filter(
                product=product, identifier__in=line["unit_identifiers"]
            ))
            SaleItemUnit.objects.bulk_create([SaleItemUnit(sale_item=item, unit=unit) for unit in units])
        created_items.append((item, line))

    if sale.status == SaleStatus.APPROVED:
        _apply_sale_inventory(sale, created_items, actor=user)

    record_audit_event(
        action="SALE_CREATED",
        actor=user,
        object_type="sale",
        object_reference=sale.reference,
        details={
            "sale_type": sale.sale_type, "status": sale.status, "total_ghs": str(total),
            "customer_id": sale.customer_id,
        },
    )
    if initial_payment:
        record_payment(
            sale=sale,
            user=user,
            amount=initial_payment["amount"],
            method=initial_payment["method"],
            momo_reference=initial_payment.get("momo_reference", ""),
        )
    return sale


def _apply_sale_inventory(sale, items_and_lines, *, actor):
    employee_creator = actor.role == Role.EMPLOYEE
    for item, line in items_and_lines:
        stock_args = {
            "product": item.product_id,
            "quantity": -item.quantity,
            "movement_type": MovementType.STOCK_SOLD,
            "user": actor,
            "reason": f"Sale {sale.reference}",
            "reference": sale.reference,
            "unit_identifiers": line.get("unit_identifiers", ()),
        }
        if employee_creator:
            with employee_sale_stock_context():
                record_stock_movement(**stock_args)
        else:
            record_stock_movement(**stock_args)


@transaction.atomic
def approve_sale(*, sale, user):
    _require_manager(user)
    sale_id = getattr(sale, "pk", sale)
    sale_stub = Sale.objects.only("customer_id").get(pk=sale_id)
    if sale_stub.customer_id:
        Customer.objects.select_for_update().get(pk=sale_stub.customer_id)
    locked_sale = Sale.objects.select_for_update().get(pk=sale_id)
    if locked_sale.status != SaleStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending sales can be approved.")
    if locked_sale.created_by_id == user.pk:
        raise PermissionDenied("You cannot approve a credit sale you created.")
    items = list(locked_sale.items.select_related("product").order_by("product_id", "pk"))
    product_ids = sorted({item.product_id for item in items})
    locked_products = list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
    product_map = {product.pk: product for product in locked_products}
    by_product = {}
    for item in items:
        by_product.setdefault(item.product_id, []).append(item)
    for product_id, product_items in by_product.items():
        if not product_map[product_id].is_active:
            raise ValidationError({"sale": f"{product_map[product_id].name} is now inactive and cannot be sold."})
        required = sum(item.quantity for item in product_items)
        if product_map[product_id].stock_quantity < required:
            raise ValidationError({"sale": f"Not enough {product_map[product_id].name} stock remains to approve this sale."})
    for item in items:
        unit_identifiers = list(item.units.values_list("identifier", flat=True))
        record_stock_movement(
            product=item.product_id,
            quantity=-item.quantity,
            movement_type=MovementType.STOCK_SOLD,
            user=user,
            reason=f"Approved sale {locked_sale.reference}",
            reference=locked_sale.reference,
            unit_identifiers=unit_identifiers,
        )
    with _allow_sale_mutations():
        Sale.objects.filter(pk=locked_sale.pk).update(
            status=SaleStatus.APPROVED, decided_by=user, decided_at=timezone.now()
        )
    record_audit_event(
        action="SALE_APPROVED", actor=user, object_type="sale", object_reference=locked_sale.reference,
        details={"created_by_id": locked_sale.created_by_id, "total_ghs": str(locked_sale.total)},
    )
    locked_sale.status = SaleStatus.APPROVED
    locked_sale.decided_by = user
    return locked_sale


@transaction.atomic
def reject_sale(*, sale, user, reason):
    _require_manager(user)
    reason = _reason(reason, action="reject a sale")
    sale_id = getattr(sale, "pk", sale)
    locked_sale = Sale.objects.select_for_update().get(pk=sale_id)
    if locked_sale.status != SaleStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending sales can be rejected.")
    if locked_sale.created_by_id == user.pk:
        raise PermissionDenied("You cannot reject a credit sale you created.")
    with _allow_sale_mutations():
        Sale.objects.filter(pk=locked_sale.pk).update(
            status=SaleStatus.REJECTED,
            decided_by=user,
            decided_at=timezone.now(),
            rejection_reason=reason,
        )
    record_audit_event(
        action="SALE_REJECTED", actor=user, object_type="sale", object_reference=locked_sale.reference,
        details={"reason": reason, "created_by_id": locked_sale.created_by_id},
    )
    locked_sale.status = SaleStatus.REJECTED
    locked_sale.rejection_reason = reason
    locked_sale.decided_by = user
    return locked_sale


@transaction.atomic
def record_payment(*, sale, user, amount, method, momo_reference="", idempotency_key=None):
    _require_signed_in(user)
    sale_id = getattr(sale, "pk", sale)
    sale_stub = Sale.objects.only("customer_id").get(pk=sale_id)
    if sale_stub.customer_id:
        Customer.objects.select_for_update().get(pk=sale_stub.customer_id)
    locked_sale = Sale.objects.select_for_update().get(pk=sale_id)
    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if (
                existing.sale_id == locked_sale.pk
                and existing.amount == _amount(amount)
                and existing.method == method
                and existing.momo_reference == (momo_reference or "").strip()
            ):
                return existing
            raise ValidationError("This repayment submission reference was already used for a different payment.")
    if locked_sale.status != SaleStatus.APPROVED:
        raise ValidationError("Payments can be recorded only for approved sales.")
    if user.role == Role.EMPLOYEE and locked_sale.created_by_id != user.pk:
        raise PermissionDenied("Employees may record payments only for their own sales.")
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Choose cash or MTN Mobile Money."})
    amount = _amount(amount)
    momo_reference = (momo_reference or "").strip()
    if method == PaymentMethod.MTN_MOMO and not momo_reference:
        raise ValidationError({"momo_reference": "Enter the MTN transaction reference."})
    if method == PaymentMethod.CASH and momo_reference:
        raise ValidationError({"momo_reference": "Cash payments do not use an MTN reference."})

    current_balance = _sale_collectible_balance(locked_sale)
    pending_reserved = locked_sale.payments.filter(status=PaymentStatus.PENDING_CONFIRMATION).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")
    if pending_reserved + amount > current_balance:
        raise ValidationError({"amount": "Payment would exceed the current return-adjusted outstanding balance after pending confirmations."})
    if locked_sale.sale_type == SaleType.CREDIT and locked_sale.customer_id:
        from apps.customers.ledger import customer_credit_balance

        customer_pending = _pending_customer_credit_payments(locked_sale.customer)
        if customer_pending + amount > customer_credit_balance(locked_sale.customer):
            raise ValidationError({"amount": "Pending MoMo confirmations already reserve this customer's remaining approved credit debt."})

    now = timezone.now()
    status = PaymentStatus.CONFIRMED if method == PaymentMethod.CASH else PaymentStatus.PENDING_CONFIRMATION
    try:
        with transaction.atomic():
            with _allow_payment_mutations():
                payment = Payment.objects.create(
                    payment_reference=new_payment_reference(),
                    idempotency_key=str(idempotency_key) if idempotency_key else None,
                    customer_name_snapshot=locked_sale.customer_name_snapshot,
                    customer_phone_snapshot=locked_sale.customer_phone_snapshot,
                    sale=locked_sale,
                    amount=amount,
                    method=method,
                    status=status,
                    momo_reference=momo_reference,
                    recorded_by=user,
                    decided_by=user if status == PaymentStatus.CONFIRMED else None,
                    decided_at=now if status == PaymentStatus.CONFIRMED else None,
                )
    except IntegrityError as exc:
        raise ValidationError({"momo_reference": "This MTN transaction reference or repayment submission has already been used."}) from exc
    record_audit_event(
        action="PAYMENT_RECORDED", actor=user, object_type="payment", object_reference=payment.payment_reference,
        details={"sale_reference": locked_sale.reference, "method": method, "amount_ghs": str(amount), "status": status},
    )
    if locked_sale.sale_type == SaleType.CREDIT and locked_sale.customer_id:
        record_audit_event(
            action="CUSTOMER_REPAYMENT_ALLOCATED", actor=user, object_type="payment",
            object_reference=payment.payment_reference,
            details={
                "customer_id": locked_sale.customer_id,
                "sale_reference": locked_sale.reference,
                "amount_ghs": str(amount),
                "status": status,
            },
        )
    return payment


@transaction.atomic
def confirm_momo_payment(*, payment, user, confirmation_reference):
    _require_manager(user)
    payment_id = getattr(payment, "pk", payment)
    payment_stub = Payment.objects.select_related("sale").only("sale_id", "sale__customer_id").get(pk=payment_id)
    if payment_stub.sale.customer_id:
        Customer.objects.select_for_update().get(pk=payment_stub.sale.customer_id)
    locked_sale = Sale.objects.select_for_update().get(pk=payment_stub.sale_id)
    locked_payment = Payment.objects.select_for_update().get(pk=payment_id)
    if locked_payment.method != PaymentMethod.MTN_MOMO or locked_payment.status != PaymentStatus.PENDING_CONFIRMATION:
        raise ValidationError("Only pending MTN Mobile Money payments can be confirmed.")
    confirmation_reference = (confirmation_reference or "").strip()
    if not confirmation_reference:
        raise ValidationError({"confirmation_reference": "Record a transaction confirmation reference."})
    if locked_sale.status != SaleStatus.APPROVED:
        raise ValidationError("Payments can be confirmed only for approved sales.")
    current_balance = _sale_collectible_balance(locked_sale)
    other_pending = Payment.objects.filter(
        sale=locked_sale, status=PaymentStatus.PENDING_CONFIRMATION,
    ).exclude(pk=locked_payment.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if locked_payment.amount + other_pending > current_balance:
        raise ValidationError("The return-adjusted balance changed. Do not confirm this MoMo payment; ask a manager to review or reject it.")
    if locked_sale.sale_type == SaleType.CREDIT and locked_sale.customer_id:
        from apps.customers.ledger import customer_credit_balance

        customer_pending = _pending_customer_credit_payments(
            locked_sale.customer, exclude_payment_id=locked_payment.pk,
        )
        if locked_payment.amount + customer_pending > customer_credit_balance(locked_sale.customer):
            raise ValidationError("The customer's return-adjusted debt no longer supports this MoMo payment. Ask a manager to review or reject it.")
    now = timezone.now()
    with _allow_payment_mutations():
        Payment.objects.filter(pk=locked_payment.pk).update(
            status=PaymentStatus.CONFIRMED,
            decided_by=user,
            decided_at=now,
            confirmation_reference=confirmation_reference,
        )
    record_audit_event(
        action="PAYMENT_MOMO_CONFIRMED", actor=user, object_type="payment", object_reference=str(locked_payment.pk),
        details={"sale_reference": locked_payment.sale.reference, "confirmation_reference": confirmation_reference},
    )
    locked_payment.status = PaymentStatus.CONFIRMED
    locked_payment.decided_by = user
    locked_payment.decided_at = now
    locked_payment.confirmation_reference = confirmation_reference
    return locked_payment


@transaction.atomic
def reject_momo_payment(*, payment, user, reason):
    _require_manager(user)
    reason = _reason(reason, action="reject a payment confirmation")
    payment_id = getattr(payment, "pk", payment)
    payment_stub = Payment.objects.select_related("sale").only("sale_id", "sale__customer_id").get(pk=payment_id)
    if payment_stub.sale.customer_id:
        Customer.objects.select_for_update().get(pk=payment_stub.sale.customer_id)
    Sale.objects.select_for_update().get(pk=payment_stub.sale_id)
    locked_payment = Payment.objects.select_for_update().get(pk=payment_id)
    if locked_payment.method != PaymentMethod.MTN_MOMO or locked_payment.status != PaymentStatus.PENDING_CONFIRMATION:
        raise ValidationError("Only pending MTN Mobile Money payments can be rejected.")
    now = timezone.now()
    with _allow_payment_mutations():
        Payment.objects.filter(pk=locked_payment.pk).update(
            status=PaymentStatus.REJECTED, decided_by=user, decided_at=now, decision_reason=reason
        )
    record_audit_event(
        action="PAYMENT_MOMO_REJECTED", actor=user, object_type="payment", object_reference=str(locked_payment.pk),
        details={"sale_reference": locked_payment.sale.reference, "reason": reason},
    )
    locked_payment.status = PaymentStatus.REJECTED
    locked_payment.decided_by = user
    locked_payment.decided_at = now
    locked_payment.decision_reason = reason
    return locked_payment


def _require_manager(user):
    _require_signed_in(user)
    if not _manager(user):
        raise PermissionDenied("Only the owner or secretary can approve or reject this action.")


def _lock_customer_credit_ledger(sale):
    if sale.customer_id:
        Customer.objects.select_for_update().get(pk=sale.customer_id)


def _sale_collectible_balance(sale):
    from apps.customers.ledger import sale_outstanding_balance

    return sale_outstanding_balance(sale)


def _pending_customer_credit_payments(customer, *, exclude_payment_id=None):
    pending = Payment.objects.filter(
        sale__customer=customer, sale__sale_type=SaleType.CREDIT,
        sale__status=SaleStatus.APPROVED, status=PaymentStatus.PENDING_CONFIRMATION,
    )
    if exclude_payment_id is not None:
        pending = pending.exclude(pk=exclude_payment_id)
    return pending.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
