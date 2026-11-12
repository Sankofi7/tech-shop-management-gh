from datetime import timedelta
from decimal import Decimal
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import record_audit_event
from apps.customers.models import Customer
from apps.customers.ledger import customer_credit_ledger
from apps.products.models import InventoryUnit, MovementType, Product, UnitCondition, UnitStatus
from apps.products.services import quarantine_customer_return_units, record_stock_movement
from apps.sales.models import (
    Payment, PaymentMethod, PaymentStatus, Sale, SaleItem, SaleItemUnit, SaleStatus, SaleType,
)
from apps.shop_settings.models import ShopProfile

from .models import (
    ExchangeLink, RefundDisbursement, RefundDisbursementStatus, RefundRequest,
    RefundRequestStatus, ReturnCase, ReturnCondition, ReturnDisposition,
    ReturnItem, ReturnItemUnit, ReturnKind, ReturnStatus, allow_return_write,
)


MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)
SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
ACTIVE_RETURN_STATUSES = (ReturnStatus.PENDING_APPROVAL, ReturnStatus.APPROVED)
ACTIVE_REFUND_STATUSES = (
    RefundRequestStatus.PENDING_APPROVAL, RefundRequestStatus.APPROVED, RefundRequestStatus.COMPLETED,
)
CENT = Decimal("0.01")
RESTOCKABLE_CONDITIONS = (ReturnCondition.NEW, ReturnCondition.GOOD, ReturnCondition.FAIR)
MIN_REASON_LENGTH = 5


def _require_shop_user(user):
    if not user.is_authenticated or user.role not in SHOP_ROLES:
        raise PermissionDenied("Sign in with an authorized shop account.")


def _require_manager(user):
    _require_shop_user(user)
    if user.role not in MANAGER_ROLES:
        raise PermissionDenied("Only the owner or secretary may approve or disburse returns and refunds.")


def _check_sale_visibility(user, sale):
    if user.role == Role.EMPLOYEE and sale.created_by_id != user.pk:
        raise PermissionDenied("Employees may handle return requests only for their own sales.")


def _lock_customer_ledger(sale):
    """Serialize all customer-linked ledger and refund-capacity decisions."""
    if sale.customer_id:
        Customer.objects.select_for_update().get(pk=sale.customer_id)


def _reason(value, *, field="reason"):
    cleaned = (value or "").strip()
    if len(cleaned) < MIN_REASON_LENGTH:
        raise ValidationError({field: f"Enter a meaningful reason of at least {MIN_REASON_LENGTH} characters."})
    return cleaned


def _money(value):
    if isinstance(value, float):
        raise ValidationError({"amount": "Use a decimal amount, not a floating-point value."})
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValidationError({"amount": "Enter a valid amount."}) from exc
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValidationError({"amount": "Amount must be positive and use no more than two decimal places."})
    return amount


def _sale_return_deadline(sale):
    profile = ShopProfile.get_solo()
    sale_date = timezone.localtime(sale.created_at).date()
    return sale_date + timedelta(days=profile.return_window_days)


def _normalise_lines(lines):
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValidationError({"items": "Select at least one item from the original sale."})
    result = []
    seen_identifiers = set()
    for line in lines:
        raw_sale_item = line.get("sale_item")
        sale_item_id = getattr(raw_sale_item, "pk", raw_sale_item)
        try:
            sale_item_id = int(sale_item_id)
            raw_quantity = line.get("quantity")
            quantity = int(str(raw_quantity).strip())
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError({"items": "Each return line needs a valid sale item and whole-number quantity."}) from exc
        if isinstance(raw_quantity, bool) or quantity < 1 or str(raw_quantity).strip() not in (str(quantity), f"+{quantity}"):
            raise ValidationError({"items": "Each return quantity must be a positive whole number."})
        identifiers = line.get("unit_identifiers", ()) or ()
        if isinstance(identifiers, str):
            identifiers = identifiers.splitlines()
        identifiers = [str(value).strip() for value in identifiers if str(value).strip()]
        if len(identifiers) != len(set(identifiers)) or seen_identifiers.intersection(identifiers):
            raise ValidationError({"unit_identifiers": "A serial number or IMEI may appear only once in a return request."})
        seen_identifiers.update(identifiers)
        result.append({"sale_item_id": sale_item_id, "quantity": quantity, "unit_identifiers": identifiers})
    return result


def _reserved_return_quantity(sale_item):
    return ReturnItem.objects.filter(
        sale_item=sale_item, return_case__status__in=ACTIVE_RETURN_STATUSES,
    ).aggregate(total=Sum("quantity"))["total"] or 0


@transaction.atomic
def create_return_request(*, sale, user, reason, items, kind=ReturnKind.RETURN):
    _require_shop_user(user)
    reason = _reason(reason)
    if kind not in ReturnKind.values:
        raise ValidationError({"kind": "Choose return or exchange."})
    sale_id = getattr(sale, "pk", sale)
    sale_stub = Sale.objects.only("customer_id").get(pk=sale_id)
    if sale_stub.customer_id:
        Customer.objects.select_for_update().get(pk=sale_stub.customer_id)
    locked_sale = Sale.objects.select_for_update().get(pk=sale_id)
    _check_sale_visibility(user, locked_sale)
    if locked_sale.status != SaleStatus.APPROVED:
        raise ValidationError({"sale": "Only approved sales may be returned."})
    if timezone.localdate() > _sale_return_deadline(locked_sale):
        raise ValidationError({"sale": "The return window for this sale has expired."})

    lines = _normalise_lines(items)
    item_ids = sorted({line["sale_item_id"] for line in lines})
    requested_by_item = {}
    for line in lines:
        requested_by_item[line["sale_item_id"]] = requested_by_item.get(line["sale_item_id"], 0) + line["quantity"]
    sale_items = {
        item.pk: item for item in SaleItem.objects.select_for_update().select_related("product").filter(
            sale_id=locked_sale.pk, pk__in=item_ids
        ).order_by("pk")
    }
    if len(sale_items) != len(item_ids):
        raise ValidationError({"items": "Every return item must belong to the selected original sale."})

    for item_id, quantity in requested_by_item.items():
        item = sale_items[item_id]
        if _reserved_return_quantity(item) + quantity > item.quantity:
            raise ValidationError({"items": f"Return quantity exceeds the remaining purchased quantity for {item.product.name}."})

    unit_links_by_line = {}
    for line_index, line in enumerate(lines):
        item = sale_items[line["sale_item_id"]]
        product = item.product
        identifiers = line["unit_identifiers"]
        if product.track_individual_units:
            if len(identifiers) != line["quantity"]:
                raise ValidationError({"unit_identifiers": f"Provide one serial number or IMEI for each returned {product.name} unit."})
            unit_links = list(SaleItemUnit.objects.select_for_update().select_related("unit").filter(
                sale_item=item, unit__identifier__in=identifiers, unit__status=UnitStatus.SOLD,
            ))
            if {link.unit.identifier for link in unit_links} != set(identifiers):
                raise ValidationError({"unit_identifiers": "Each serial number or IMEI must belong to this exact sale item and still be sold."})
            active_unit_ids = set(ReturnItemUnit.objects.filter(
                sale_item_unit__sale_item=item,
                return_item__return_case__status__in=ACTIVE_RETURN_STATUSES,
            ).values_list("sale_item_unit__unit__identifier", flat=True))
            if active_unit_ids.intersection(identifiers):
                raise ValidationError({"unit_identifiers": "A serial number or IMEI is already reserved by another return."})
            unit_links_by_line[line_index] = unit_links
        elif identifiers:
            raise ValidationError({"unit_identifiers": f"{product.name} does not use individual unit tracking."})

    with allow_return_write():
        case = ReturnCase.objects.create(
            sale=locked_sale, kind=kind, reason=reason, requested_by=user,
        )
        for line_index, line in enumerate(lines):
            item = sale_items[line["sale_item_id"]]
            return_item = ReturnItem.objects.create(
                return_case=case, sale_item=item, quantity=line["quantity"], unit_price=item.unit_price,
            )
            for unit_link in unit_links_by_line.get(line_index, ()):
                ReturnItemUnit.objects.create(return_item=return_item, sale_item_unit=unit_link)
    record_audit_event(
        action="RETURN_SUBMITTED", actor=user, object_type="return_case", object_reference=case.reference,
        details={
            "sale_reference": locked_sale.reference,
            "kind": kind,
            "reason": reason,
            "items": [{"sale_item_id": line["sale_item_id"], "quantity": line["quantity"]} for line in lines],
        },
    )
    return case


def _normalise_item_decisions(return_case, decisions):
    try:
        supplied = {int(key): value for key, value in decisions.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError({"items": "Provide a condition and disposition for every returned item."}) from exc
    return_items = list(return_case.items.select_related("sale_item__product").prefetch_related("returned_units__sale_item_unit__unit"))
    expected = {item.pk for item in return_items}
    if set(supplied) != expected:
        raise ValidationError({"items": "Provide exactly one inspection decision for every returned item."})
    normalized = []
    for item in return_items:
        data = supplied[item.pk]
        condition = data.get("condition")
        disposition = data.get("disposition")
        if condition not in ReturnCondition.values or disposition not in ReturnDisposition.values:
            raise ValidationError({"items": "Choose a valid returned-product condition and disposition."})
        if disposition == ReturnDisposition.RESTOCK and condition not in RESTOCKABLE_CONDITIONS:
            raise ValidationError({"items": "Damaged, defective or incomplete products cannot be returned to available stock."})
        normalized.append((item, condition, disposition))
    return normalized


@transaction.atomic
def approve_return_request(*, return_case, user, item_decisions):
    _require_manager(user)
    case_id = getattr(return_case, "pk", return_case)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=case_id)
    _lock_customer_ledger(case_stub.sale)
    locked_sale = Sale.objects.select_for_update().get(pk=case_stub.sale_id)
    case = ReturnCase.objects.select_for_update().get(pk=case_id)
    if case.status != ReturnStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending return requests may be approved.")
    if case.requested_by_id == user.pk:
        raise PermissionDenied("You cannot approve a return request you submitted.")
    decisions = _normalise_item_decisions(case, item_decisions)
    case_value = case.total_value
    if case.kind == ReturnKind.EXCHANGE:
        # An exchange credit is reserved until a replacement credit sale is
        # linked. The live customer ledger then applies it in settlement order.
        # Do not store a misleading normal-return allocation at approval time.
        debt_credit = Decimal("0.00")
        refund_credit = Decimal("0.00")
    elif locked_sale.customer_id:
        debt_credit = min(case_value, locked_sale.customer.outstanding_debt)
        refund_credit = case_value - debt_credit
    else:
        debt_credit = Decimal("0.00")
        refund_credit = case_value
    decided_at = timezone.now()
    for item, condition, disposition in decisions:
        product = item.sale_item.product
        identifiers = [link.sale_item_unit.unit.identifier for link in item.returned_units.all()]
        if disposition == ReturnDisposition.RESTOCK:
            stock_condition = condition
            if stock_condition not in UnitCondition.values:
                raise ValidationError({"items": "This condition cannot be restocked as sellable inventory."})
            movement = record_stock_movement(
                product=product,
                quantity=item.quantity,
                movement_type=MovementType.CUSTOMER_RETURN,
                user=user,
                reason=f"Resellable customer return {case.reference}",
                reference=case.reference,
                unit_identifiers=identifiers,
                unit_condition=stock_condition,
            )
            record_audit_event(
                action="RETURN_INVENTORY_RESTOCKED", actor=user, object_type="inventory_movement",
                object_reference=movement.pk,
                details={"return_reference": case.reference, "product_sku": product.sku, "quantity": item.quantity},
            )
        elif product.track_individual_units:
            stock_condition = condition if condition in UnitCondition.values else UnitCondition.DAMAGED
            quarantine_customer_return_units(
                product=product, unit_identifiers=identifiers, user=user, unit_condition=stock_condition,
            )
        with allow_return_write():
            ReturnItem.objects.filter(pk=item.pk).update(condition=condition, disposition=disposition)
        record_audit_event(
            action="RETURN_ITEM_DISPOSITIONED", actor=user, object_type="return_item",
            object_reference=item.pk,
            details={
                "return_reference": case.reference, "sale_reference": locked_sale.reference,
                "product_sku": product.sku, "quantity": item.quantity,
                "condition": condition, "disposition": disposition,
            },
        )
    with allow_return_write():
        ReturnCase.objects.filter(pk=case.pk).update(
            status=ReturnStatus.APPROVED, decided_by=user, decided_at=decided_at,
            debt_credit_applied=debt_credit, refund_credit_available=refund_credit,
        )
    record_audit_event(
        action="RETURN_APPROVED", actor=user, object_type="return_case", object_reference=case.reference,
        details={
            "sale_reference": locked_sale.reference, "approved_value_ghs": str(case_value),
            "debt_credit_applied_ghs": str(debt_credit), "refund_credit_available_ghs": str(refund_credit),
        },
    )
    case.status = ReturnStatus.APPROVED
    case.decided_by = user
    case.decided_at = decided_at
    case.debt_credit_applied = debt_credit
    case.refund_credit_available = refund_credit
    return case


@transaction.atomic
def reject_return_request(*, return_case, user, reason):
    _require_manager(user)
    reason = _reason(reason, field="decision_reason")
    case_id = getattr(return_case, "pk", return_case)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=case_stub.sale_id)
    case = ReturnCase.objects.select_for_update().get(pk=case_id)
    if case.status != ReturnStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending return requests may be rejected.")
    if case.requested_by_id == user.pk:
        raise PermissionDenied("You cannot reject a return request you submitted.")
    now = timezone.now()
    with allow_return_write():
        ReturnCase.objects.filter(pk=case.pk).update(
            status=ReturnStatus.REJECTED, decided_by=user, decided_at=now, decision_reason=reason,
        )
    record_audit_event(
        action="RETURN_REJECTED", actor=user, object_type="return_case", object_reference=case.reference,
        details={"reason": reason},
    )
    case.status, case.decided_by, case.decided_at, case.decision_reason = ReturnStatus.REJECTED, user, now, reason
    return case


def _approved_return_value(sale):
    items = ReturnItem.objects.filter(return_case__sale=sale, return_case__status=ReturnStatus.APPROVED)
    return sum((price * quantity for price, quantity in items.values_list("unit_price", "quantity")), Decimal("0.00"))


def _confirmed_payment_total(sale):
    return Payment.objects.filter(sale=sale, status=PaymentStatus.CONFIRMED).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _reserved_refund_requests(sale, *, exclude_id=None):
    requests = RefundRequest.objects.filter(return_case__sale=sale, status__in=ACTIVE_REFUND_STATUSES)
    if exclude_id is not None:
        requests = requests.exclude(pk=exclude_id)
    return requests.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _refund_capacity(return_case, *, exclude_request_id=None):
    sale = return_case.sale
    exchange = ExchangeLink.objects.filter(return_case=return_case).select_related("replacement_sale").first()
    if return_case.kind == ReturnKind.EXCHANGE and exchange is None:
        return Decimal("0.00")
    exchange_used = min(return_case.total_value, exchange.replacement_sale.total) if exchange else Decimal("0.00")
    case_remaining = max(return_case.total_value - exchange_used, Decimal("0.00"))

    if sale.customer_id:
        financial_sales = Sale.objects.filter(
            customer_id=sale.customer_id, status=SaleStatus.APPROVED,
        )
    else:
        financial_sale_ids = [sale.pk]
        if exchange:
            financial_sale_ids.append(exchange.replacement_sale_id)
        financial_sales = Sale.objects.filter(pk__in=financial_sale_ids, status=SaleStatus.APPROVED)
    gross_value = financial_sales.aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    approved_returns = ReturnItem.objects.filter(
        (Q(return_case__sale__in=financial_sales)
         | Q(return_case__exchange_link__replacement_sale__in=financial_sales)),
        return_case__status=ReturnStatus.APPROVED,
    )
    returned_value = sum(
        (price * quantity for price, quantity in approved_returns.values_list("unit_price", "quantity")),
        Decimal("0.00"),
    )
    received = Payment.objects.filter(
        sale__in=financial_sales, status=PaymentStatus.CONFIRMED,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    active_requests = RefundRequest.objects.filter(
        (Q(return_case__sale__in=financial_sales)
         | Q(return_case__exchange_link__replacement_sale__in=financial_sales)),
        status__in=ACTIVE_REFUND_STATUSES,
    )
    if exclude_request_id is not None:
        active_requests = active_requests.exclude(pk=exclude_request_id)
    reserved = active_requests.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    remaining_confirmed_overpayment = max(received - max(gross_value - returned_value, Decimal("0.00")) - reserved, Decimal("0.00"))
    return min(case_remaining, remaining_confirmed_overpayment)


@transaction.atomic
def create_refund_request(*, return_case, user, amount, method, reason):
    _require_shop_user(user)
    amount = _money(amount)
    reason = _reason(reason)
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Choose cash or MTN Mobile Money."})
    case_id = getattr(return_case, "pk", return_case)
    stub = ReturnCase.objects.select_related("sale").get(pk=case_id)
    _lock_customer_ledger(stub.sale)
    sale = Sale.objects.select_for_update().get(pk=stub.sale_id)
    _check_sale_visibility(user, sale)
    case = ReturnCase.objects.select_for_update().get(pk=case_id)
    if case.status != ReturnStatus.APPROVED:
        raise ValidationError({"return_case": "A refund can be requested only for an approved return."})
    if amount > _refund_capacity(case):
        raise ValidationError({"amount": "Refund exceeds the remaining value supported by confirmed payments after debt reduction and prior refund requests."})
    with allow_return_write():
        request = RefundRequest.objects.create(
            return_case=case, amount=amount, method=method, reason=reason, requested_by=user,
        )
    record_audit_event(
        action="REFUND_REQUESTED", actor=user, object_type="refund_request", object_reference=request.reference,
        details={"return_reference": case.reference, "sale_reference": sale.reference, "amount_ghs": str(amount), "method": method, "reason": reason},
    )
    return request


@transaction.atomic
def approve_refund_request(*, refund_request, user):
    _require_manager(user)
    request_id = getattr(refund_request, "pk", refund_request)
    stub = RefundRequest.objects.select_related("return_case").get(pk=request_id)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=stub.return_case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=stub.return_case.sale_id)
    ReturnCase.objects.select_for_update().get(pk=stub.return_case_id)
    request = RefundRequest.objects.select_for_update().get(pk=request_id)
    if request.status != RefundRequestStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending refund requests may be approved.")
    if request.requested_by_id == user.pk:
        raise PermissionDenied("You cannot approve a refund request you submitted.")
    if request.return_case.status != ReturnStatus.APPROVED:
        raise ValidationError("The return must remain approved before its refund can be approved.")
    if request.amount > _refund_capacity(request.return_case, exclude_request_id=request.pk):
        raise ValidationError("Confirmed payment and approved return balances no longer support this refund amount.")
    now = timezone.now()
    with allow_return_write():
        RefundRequest.objects.filter(pk=request.pk).update(status=RefundRequestStatus.APPROVED, decided_by=user, decided_at=now)
    record_audit_event(
        action="REFUND_APPROVED", actor=user, object_type="refund_request", object_reference=request.reference,
        details={"return_reference": request.return_case.reference, "amount_ghs": str(request.amount), "method": request.method},
    )
    request.status, request.decided_by, request.decided_at = RefundRequestStatus.APPROVED, user, now
    return request


@transaction.atomic
def reject_refund_request(*, refund_request, user, reason):
    _require_manager(user)
    reason = _reason(reason, field="rejection_reason")
    request_id = getattr(refund_request, "pk", refund_request)
    stub = RefundRequest.objects.select_related("return_case").get(pk=request_id)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=stub.return_case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=stub.return_case.sale_id)
    ReturnCase.objects.select_for_update().get(pk=stub.return_case_id)
    request = RefundRequest.objects.select_for_update().get(pk=request_id)
    if request.status != RefundRequestStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending refund requests may be rejected.")
    if request.requested_by_id == user.pk:
        raise PermissionDenied("You cannot reject a refund request you submitted.")
    now = timezone.now()
    with allow_return_write():
        RefundRequest.objects.filter(pk=request.pk).update(
            status=RefundRequestStatus.REJECTED, decided_by=user, decided_at=now, rejection_reason=reason,
        )
    record_audit_event(
        action="REFUND_REJECTED", actor=user, object_type="refund_request", object_reference=request.reference,
        details={"reason": reason},
    )
    request.status, request.decided_by, request.decided_at, request.rejection_reason = RefundRequestStatus.REJECTED, user, now, reason
    return request


@transaction.atomic
def record_refund_disbursement(*, refund_request, user, amount, method, momo_reference="", idempotency_key=None):
    _require_manager(user)
    amount = _money(amount)
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Choose cash or MTN Mobile Money."})
    momo_reference = (momo_reference or "").strip()
    if method == PaymentMethod.MTN_MOMO and not momo_reference:
        raise ValidationError({"momo_reference": "Enter the MTN refund transaction reference."})
    if method == PaymentMethod.CASH and momo_reference:
        raise ValidationError({"momo_reference": "Cash refunds do not use an MTN reference."})
    request_id = getattr(refund_request, "pk", refund_request)
    stub = RefundRequest.objects.select_related("return_case").get(pk=request_id)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=stub.return_case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=case_stub.sale_id)
    case = ReturnCase.objects.select_for_update().get(pk=case_stub.pk)
    request = RefundRequest.objects.select_for_update().get(pk=request_id)
    if request.status != RefundRequestStatus.APPROVED:
        raise ValidationError("Only approved refund requests can be disbursed.")
    if request.method != method:
        raise ValidationError({"method": "Use the payment method approved for this refund request."})
    if idempotency_key:
        try:
            key = uuid.UUID(str(idempotency_key))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"idempotency_key": "Invalid disbursement submission key."}) from exc
        existing = RefundDisbursement.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.refund_request_id == request.pk and existing.amount == amount and existing.method == method and existing.momo_reference == momo_reference:
                return existing
            raise ValidationError("This refund disbursement submission key was already used.")
    reserved_disbursements = request.disbursements.exclude(status=RefundDisbursementStatus.REJECTED).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")
    current_eligibility = _refund_capacity(case, exclude_request_id=request.pk)
    remaining_approved = max(request.amount - reserved_disbursements, Decimal("0.00"))
    if remaining_approved > current_eligibility:
        raise ValidationError("Refund eligibility changed after approval. Disbursement is paused; a manager must review the request against the current ledger.")
    if reserved_disbursements + amount > request.amount or reserved_disbursements + amount > current_eligibility:
        raise ValidationError({"amount": "Disbursement would exceed the approved amount or its current confirmed-funds eligibility."})
    now = timezone.now()
    status = RefundDisbursementStatus.CONFIRMED if method == PaymentMethod.CASH else RefundDisbursementStatus.PENDING_CONFIRMATION
    try:
        with transaction.atomic():
            with allow_return_write():
                disbursement = RefundDisbursement.objects.create(
                    refund_request=request, idempotency_key=key if idempotency_key else uuid.uuid4(),
                    amount=amount, method=method, status=status, momo_reference=momo_reference,
                    recorded_by=user, decided_by=user if status == RefundDisbursementStatus.CONFIRMED else None,
                    decided_at=now if status == RefundDisbursementStatus.CONFIRMED else None,
                )
    except IntegrityError as exc:
        raise ValidationError({"momo_reference": "This MTN refund reference has already been recorded."}) from exc
    if status == RefundDisbursementStatus.CONFIRMED:
        _complete_refund_if_paid(request, actor=user, now=now)
    record_audit_event(
        action="REFUND_DISBURSEMENT_RECORDED", actor=user, object_type="refund_disbursement",
        object_reference=disbursement.pk,
        details={"refund_reference": request.reference, "amount_ghs": str(amount), "method": method, "status": status, "momo_reference": momo_reference},
    )
    return disbursement


def _complete_refund_if_paid(request, *, actor, now):
    confirmed = request.disbursements.filter(status=RefundDisbursementStatus.CONFIRMED).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    if confirmed >= request.amount:
        with allow_return_write():
            RefundRequest.objects.filter(pk=request.pk, status=RefundRequestStatus.APPROVED).update(
                status=RefundRequestStatus.COMPLETED,
            )


@transaction.atomic
def confirm_refund_momo_disbursement(*, disbursement, user, confirmation_reference):
    _require_manager(user)
    confirmation_reference = (confirmation_reference or "").strip()
    if not confirmation_reference:
        raise ValidationError({"confirmation_reference": "Record a MoMo refund confirmation reference."})
    disbursement_id = getattr(disbursement, "pk", disbursement)
    stub = RefundDisbursement.objects.select_related("refund_request__return_case").get(pk=disbursement_id)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=stub.refund_request.return_case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=case_stub.sale_id)
    case = ReturnCase.objects.select_for_update().get(pk=case_stub.pk)
    request = RefundRequest.objects.select_for_update().get(pk=stub.refund_request_id)
    locked = RefundDisbursement.objects.select_for_update().get(pk=disbursement_id)
    if request.status not in (RefundRequestStatus.APPROVED, RefundRequestStatus.COMPLETED):
        raise ValidationError("The refund request is not approved for disbursement.")
    if locked.method != PaymentMethod.MTN_MOMO or locked.status != RefundDisbursementStatus.PENDING_CONFIRMATION:
        raise ValidationError("Only pending MTN MoMo refund disbursements can be confirmed.")
    if locked.recorded_by_id == user.pk:
        raise PermissionDenied("You cannot confirm a refund disbursement you recorded.")
    prior_disbursements = request.disbursements.exclude(status=RefundDisbursementStatus.REJECTED).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")
    eligible = _refund_capacity(case, exclude_request_id=request.pk)
    remaining_approved = max(request.amount - prior_disbursements, Decimal("0.00"))
    if remaining_approved > eligible or prior_disbursements > eligible:
        raise ValidationError("Refund eligibility changed after approval. Do not confirm this MoMo disbursement; a manager must review it.")
    now = timezone.now()
    try:
        with transaction.atomic():
            with allow_return_write():
                RefundDisbursement.objects.filter(pk=locked.pk).update(
                    status=RefundDisbursementStatus.CONFIRMED, confirmation_reference=confirmation_reference,
                    decided_by=user, decided_at=now,
                )
    except IntegrityError as exc:
        raise ValidationError({"confirmation_reference": "This confirmation reference has already been recorded."}) from exc
    _complete_refund_if_paid(request, actor=user, now=now)
    record_audit_event(
        action="REFUND_MOMO_CONFIRMED", actor=user, object_type="refund_disbursement",
        object_reference=locked.pk,
        details={"refund_reference": request.reference, "confirmation_reference": confirmation_reference},
    )
    locked.status, locked.confirmation_reference, locked.decided_by, locked.decided_at = RefundDisbursementStatus.CONFIRMED, confirmation_reference, user, now
    return locked


@transaction.atomic
def reject_refund_momo_disbursement(*, disbursement, user, reason):
    _require_manager(user)
    reason = _reason(reason, field="rejection_reason")
    disbursement_id = getattr(disbursement, "pk", disbursement)
    stub = RefundDisbursement.objects.select_related("refund_request__return_case").get(pk=disbursement_id)
    case_stub = ReturnCase.objects.select_related("sale").get(pk=stub.refund_request.return_case_id)
    _lock_customer_ledger(case_stub.sale)
    Sale.objects.select_for_update().get(pk=case_stub.sale_id)
    ReturnCase.objects.select_for_update().get(pk=case_stub.pk)
    RefundRequest.objects.select_for_update().get(pk=stub.refund_request_id)
    locked = RefundDisbursement.objects.select_for_update().get(pk=disbursement_id)
    if locked.method != PaymentMethod.MTN_MOMO or locked.status != RefundDisbursementStatus.PENDING_CONFIRMATION:
        raise ValidationError("Only pending MTN MoMo refund disbursements can be rejected.")
    if locked.recorded_by_id == user.pk:
        raise PermissionDenied("You cannot reject a refund disbursement you recorded.")
    now = timezone.now()
    with allow_return_write():
        RefundDisbursement.objects.filter(pk=locked.pk).update(
            status=RefundDisbursementStatus.REJECTED, rejection_reason=reason, decided_by=user, decided_at=now,
        )
    record_audit_event(
        action="REFUND_MOMO_REJECTED", actor=user, object_type="refund_disbursement", object_reference=locked.pk,
        details={"refund_reference": locked.refund_request.reference, "reason": reason},
    )
    locked.status, locked.rejection_reason, locked.decided_by, locked.decided_at = RefundDisbursementStatus.REJECTED, reason, user, now
    return locked


@transaction.atomic
def link_exchange_sale(*, return_case, replacement_sale, user):
    _require_manager(user)
    case_id = getattr(return_case, "pk", return_case)
    stub = ReturnCase.objects.select_related("sale").get(pk=case_id)
    _lock_customer_ledger(stub.sale)
    locked_sales = list(Sale.objects.select_for_update().filter(
        pk__in=[stub.sale_id, getattr(replacement_sale, "pk", replacement_sale)]
    ).order_by("pk"))
    if len(locked_sales) != 2:
        raise ValidationError("The original and replacement sales must both exist.")
    case = ReturnCase.objects.select_for_update().select_related("sale").get(pk=case_id)
    replacement = Sale.objects.select_for_update().get(pk=getattr(replacement_sale, "pk", replacement_sale))
    if case.kind != ReturnKind.EXCHANGE or case.status != ReturnStatus.APPROVED:
        raise ValidationError("Only an approved exchange return can be linked to a replacement sale.")
    if RefundRequest.objects.filter(return_case=case).exists():
        raise ValidationError("An exchange cannot be linked after a refund request has been recorded.")
    if replacement.pk == case.sale_id or replacement.status != SaleStatus.APPROVED:
        raise ValidationError("Choose a different approved replacement sale.")
    if not case.sale.customer_id or replacement.customer_id != case.sale.customer_id:
        raise ValidationError("The replacement sale must belong to the same customer as the original sale.")
    if replacement.sale_type != SaleType.CREDIT:
        raise ValidationError("Create the replacement as an approved credit sale so return credit offsets it and any difference can use the repayment workflow.")
    if replacement.payments.exclude(status=PaymentStatus.REJECTED).exists():
        raise ValidationError("Link the approved replacement credit sale before recording any payment against it.")
    if case.sale.sale_type == SaleType.CASH and _confirmed_payment_total(case.sale) < case.total_value:
        raise ValidationError("An exchange from a cash sale requires confirmed incoming funds at least equal to the returned value.")
    try:
        with transaction.atomic():
            with allow_return_write():
                link = ExchangeLink.objects.create(return_case=case, replacement_sale=replacement, linked_by=user)
    except IntegrityError as exc:
        raise ValidationError("This return or replacement sale is already linked to an exchange.") from exc
    record_audit_event(
        action="EXCHANGE_LINKED", actor=user, object_type="exchange", object_reference=link.pk,
        details={"return_reference": case.reference, "original_sale": case.sale.reference, "replacement_sale": replacement.reference},
    )
    return link
