"""Authoritative return-adjusted allocation of approved customer credit sales.

Approved return credits are allocated in decision order. A normal return applies
to its original sale first, then to the customer's other approved credit sales
ordered oldest-first by (created_at, primary key). An exchange applies first to
its linked replacement credit sale, then to the original sale, then to the same
oldest-first remainder. Only confirmed payments/refunds and approved returns
affect balances; pending MoMo payments reserve capacity separately in the payment
workflow.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum

from apps.sales.models import Payment, PaymentStatus, SaleStatus, SaleType

ZERO = Decimal("0.00")


def customer_credit_ledger(customer):
    """Return the approved credit sales and their current return-adjusted dues."""
    from apps.returns.models import (
        ExchangeLink, RefundDisbursement, RefundDisbursementStatus, ReturnCase,
        ReturnItem, ReturnStatus,
    )

    sales = list(customer.sales.filter(
        sale_type=SaleType.CREDIT, status=SaleStatus.APPROVED,
    ).order_by("created_at", "pk"))
    if not sales:
        return {}, ZERO, {}

    sale_ids = [sale.pk for sale in sales]
    payments = dict(Payment.objects.filter(
        sale_id__in=sale_ids, status=PaymentStatus.CONFIRMED,
    ).values("sale_id").annotate(total=Sum("amount")).values_list("sale_id", "total"))
    balances = {
        sale.pk: max(sale.total - payments.get(sale.pk, ZERO), ZERO)
        for sale in sales
    }

    approved_cases = ReturnCase.objects.filter(
        Q(sale__customer=customer)
        | Q(exchange_link__replacement_sale__customer=customer),
        status=ReturnStatus.APPROVED,
    )
    items = list(ReturnItem.objects.filter(return_case__in=approved_cases).values(
        "return_case_id", "return_case__sale_id", "return_case__kind", "return_case__requested_at",
        "return_case__decided_at", "unit_price", "quantity",
    ).order_by("return_case__decided_at", "return_case_id", "pk"))
    case_values = defaultdict(lambda: ZERO)
    case_meta = {}
    for row in items:
        case_id = row["return_case_id"]
        case_values[case_id] += row["unit_price"] * row["quantity"]
        case_meta[case_id] = (
            row["return_case__sale_id"], row["return_case__kind"],
            row["return_case__requested_at"], row["return_case__decided_at"],
        )

    exchange_targets = dict(ExchangeLink.objects.filter(
        return_case_id__in=case_values,
        replacement_sale_id__in=sale_ids,
    ).values_list("return_case_id", "replacement_sale_id"))
    confirmed_refunds = dict(RefundDisbursement.objects.filter(
        refund_request__return_case_id__in=case_values,
        status=RefundDisbursementStatus.CONFIRMED,
    ).values("refund_request__return_case_id").annotate(total=Sum("amount")).values_list(
        "refund_request__return_case_id", "total",
    ))
    sales_by_id = {sale.pk: sale for sale in sales}
    credit_allocated_by_case = {}
    for case_id in sorted(case_values, key=lambda pk: (case_meta[pk][3], pk)):
        original_sale_id = case_meta[case_id][0]
        case_kind = case_meta[case_id][1]
        preferred = []
        replacement_id = exchange_targets.get(case_id)
        # Keep an exchange credit reserved until a manager links the approved
        # replacement credit sale. It must not reduce unrelated customer debt.
        if case_kind == "EXCHANGE" and replacement_id is None:
            credit_allocated_by_case[case_id] = ZERO
            continue
        if replacement_id is not None:
            preferred.append(replacement_id)
        preferred.append(original_sale_id)
        preferred.extend(sale.pk for sale in sales if sale.pk not in preferred)

        # A confirmed refund consumes that portion of the return credit. This
        # keeps cash returns from reducing later credit sales after refunding.
        remaining = max(case_values[case_id] - confirmed_refunds.get(case_id, ZERO), ZERO)
        applied = ZERO
        for sale_id in preferred:
            if remaining <= ZERO:
                break
            if sale_id not in balances:
                continue
            available = balances[sale_id]
            take = min(available, remaining)
            balances[sale_id] -= take
            remaining -= take
            applied += take
        credit_allocated_by_case[case_id] = applied

    balances = {sale_id: value.quantize(Decimal("0.01")) for sale_id, value in balances.items()}
    return balances, sum(balances.values(), ZERO), credit_allocated_by_case


def sale_credit_balance(sale):
    """Return the ledger balance attributed to one approved credit sale."""
    if sale.sale_type != SaleType.CREDIT or sale.status != SaleStatus.APPROVED or not sale.customer_id:
        return ZERO
    balances, _, _ = customer_credit_ledger(sale.customer)
    return balances.get(sale.pk, ZERO)


def sale_outstanding_balance(sale):
    """Return the sale-attributed collectible balance after confirmed receipts and returns."""
    if sale.sale_type == SaleType.CREDIT:
        return sale_credit_balance(sale)
    from apps.returns.models import ReturnItem, ReturnStatus

    confirmed = Payment.objects.filter(sale=sale, status=PaymentStatus.CONFIRMED).aggregate(
        total=Sum("amount")
    )["total"] or ZERO
    returned = sum((price * quantity for price, quantity in ReturnItem.objects.filter(
        return_case__sale=sale, return_case__status=ReturnStatus.APPROVED,
    ).values_list("unit_price", "quantity")), ZERO)
    return max(sale.total - confirmed - returned, ZERO)


def customer_credit_balance(customer):
    return customer_credit_ledger(customer)[1]
