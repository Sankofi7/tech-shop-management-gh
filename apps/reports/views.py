import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.permissions import role_required
from apps.audit.models import AuditEvent
from apps.customers.ledger import customer_credit_ledger
from apps.customers.models import Customer
from apps.customers.services import MANAGER_ROLES, SHOP_ROLES, credit_totals
from apps.products.models import InventoryMovement, MovementType, Product
from apps.sales.models import Payment, PaymentMethod, PaymentStatus, Sale, SaleStatus, SaleType

from .forms import ReportDateForm


ZERO = Decimal("0.00")


def _settled_credit_sales(credit_sales):
    """Return approved credit sales whose shared-ledger balance is settled."""
    sales = list(credit_sales.select_related("customer", "created_by").order_by("-created_at"))
    balances_by_customer = {
        customer_id: customer_credit_ledger(Customer.objects.get(pk=customer_id))[0]
        for customer_id in {sale.customer_id for sale in sales if sale.customer_id}
    }
    return [
        sale for sale in sales
        if sale.customer_id and balances_by_customer[sale.customer_id].get(sale.pk, ZERO) <= ZERO
    ]


def _sales_scope(user):
    sales = Sale.objects.filter(status=SaleStatus.APPROVED)
    if user.role == Role.EMPLOYEE:
        sales = sales.filter(created_by=user)
    return sales


def _with_period(queryset, field, first_day, last_day):
    return queryset.filter(**{f"{field}__date__range": (first_day, last_day)})


def _period_form(request):
    if request.GET:
        form = ReportDateForm(request.GET)
    else:
        form = ReportDateForm(initial={"period": "last7"})
    if form.is_valid():
        start, end = form.get_range()
    else:
        today = timezone.localdate()
        start, end = today - timedelta(days=6), today
    return form, start, end


def _csv_url(request):
    params = request.GET.copy()
    params["format"] = "csv"
    return f"?{params.urlencode()}"


def _csv_download(filename, headers, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        safe_row = []
        for value in row:
            original = value
            value = "" if value is None else str(value)
            prefix = value.lstrip(" \t\r\n")[:1] if isinstance(original, str) else ""
            if prefix in ("=", "+", "-", "@"):
                value = "'" + value
            safe_row.append(value)
        writer.writerow(safe_row)
    return response


def _chart_rows(queryset, *, date_field, sum_field, group_fields=()):
    values = [*group_fields, "report_day"]
    grouped = (
        queryset.annotate(report_day=TruncDate(date_field, tzinfo=timezone.get_current_timezone()))
        .values(*values).annotate(total=Sum(sum_field)).order_by("report_day", *group_fields)
    )
    return list(grouped)


def _daily_series(queryset, date_field, sum_field, start, end):
    grouped = {
        row["report_day"]: row["total"] or ZERO
        for row in _chart_rows(queryset, date_field=date_field, sum_field=sum_field)
    }
    points = []
    cursor = start
    while cursor <= end:
        amount = Decimal(grouped.get(cursor) or ZERO).quantize(Decimal("0.01"))
        points.append({"date": cursor.isoformat(), "label": cursor.strftime("%b %-d"), "amount": str(amount)})
        cursor += timedelta(days=1)
    maximum = max((Decimal(point["amount"]) for point in points), default=ZERO)
    for point in points:
        point["height"] = max(3, round(100 * Decimal(point["amount"]) / maximum)) if maximum else 3
    return points


@login_required
@role_required(*SHOP_ROLES)
def reports_home(request):
    user = request.user
    is_manager = user.role in MANAGER_ROLES
    sales = _sales_scope(user)
    today = timezone.localdate()
    sales_today = sales.filter(created_at__date=today)
    paid_today = Payment.objects.filter(
        sale__in=sales, status=PaymentStatus.CONFIRMED, decided_at__date=today,
    )
    context = {
        "page_title": "Reports & analytics", "is_manager": is_manager,
        "sales_today_total": sales_today.aggregate(total=Sum("total"))["total"] or ZERO,
        "sales_today_count": sales_today.count(),
        "received_today_total": paid_today.aggregate(total=Sum("amount"))["total"] or ZERO,
        "recent_activity": AuditEvent.objects.filter(actor=user).select_related("actor")[:6] if not is_manager else AuditEvent.objects.select_related("actor")[:6],
    }
    context["recent_activity"] = list(context["recent_activity"])
    for event in context["recent_activity"]:
        event.action_label = event.action.replace("_", " ").title()
    if is_manager:
        _, _, _, debt = credit_totals(Sale.objects.all())
        context["outstanding_debt"] = debt
    return render(request, "reports/home.html", context)


@login_required
@role_required(*SHOP_ROLES)
def sales_report(request):
    form, start, end = _period_form(request)
    sales = _with_period(_sales_scope(request.user), "created_at", start, end).select_related("created_by")
    totals = sales.aggregate(total=Sum("total"), count=Count("pk"))
    daily_data = _daily_series(sales, "created_at", "total", start, end)
    payments = Payment.objects.filter(
        sale__in=_sales_scope(request.user), status=PaymentStatus.CONFIRMED,
    )
    payments = _with_period(payments, "decided_at", start, end)
    method_rows = list(payments.values("method").annotate(total=Sum("amount")).order_by("method"))
    method_names = dict(PaymentMethod.choices)
    for row in method_rows:
        row["label"] = method_names.get(row["method"], row["method"])
    employee_rows = list(sales.values("created_by_id", "created_by__username").annotate(
        count=Count("pk"), total=Sum("total")
    ).order_by("-total", "created_by__username")) if request.user.role in MANAGER_ROLES else []
    pending_sales = _with_period(Sale.objects.filter(
        created_by=request.user, sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL,
    ), "created_at", start, end) if request.user.role == Role.EMPLOYEE else Sale.objects.none()
    pending_totals = pending_sales.aggregate(total=Sum("total"), count=Count("pk"))
    maximum_method = max((row["total"] or ZERO for row in method_rows), default=ZERO)
    for row in method_rows:
        row["percent"] = round(100 * row["total"] / maximum_method) if maximum_method else 0
    maximum_employee = max((row["total"] or ZERO for row in employee_rows), default=ZERO)
    for row in employee_rows:
        row["percent"] = round(100 * row["total"] / maximum_employee) if maximum_employee else 0
    if request.GET.get("format") == "csv":
        rows = sales.order_by("created_at").values_list("reference", "created_at", "created_by__username", "sale_type", "total")
        return _csv_download("sales-report.csv", ("Sale reference", "Sale date", "Employee", "Sale type", "Approved sale value GHS"), rows)
    return render(request, "reports/sales.html", {
        "page_title": "Sales report", "form": form, "start": start, "end": end,
        "sales": sales.order_by("-created_at")[:50], "sales_count": totals["count"],
        "sales_total": totals["total"] or ZERO, "daily_data": daily_data,
        "method_rows": method_rows, "employee_rows": employee_rows,
        "chart_width": max(560, len(daily_data) * 38),
        "pending_count": pending_totals["count"], "pending_total": pending_totals["total"] or ZERO,
        "is_manager": request.user.role in MANAGER_ROLES, "csv_url": _csv_url(request),
    })


@login_required
@role_required(*SHOP_ROLES)
def payments_report(request):
    form, start, end = _period_form(request)
    approved_sales = _sales_scope(request.user)
    scoped_payments = Payment.objects.filter(sale__in=approved_sales)
    period_payments = scoped_payments.filter(
        Q(status=PaymentStatus.CONFIRMED, decided_at__date__range=(start, end))
        | Q(status=PaymentStatus.PENDING_CONFIRMATION, recorded_at__date__range=(start, end))
    )
    confirmed = period_payments.filter(status=PaymentStatus.CONFIRMED)
    pending = period_payments.filter(status=PaymentStatus.PENDING_CONFIRMATION, method=PaymentMethod.MTN_MOMO)
    method_totals = {row["method"]: row["total"] for row in confirmed.values("method").annotate(total=Sum("amount"))}
    daily_data = _daily_series(confirmed, "decided_at", "amount", start, end)
    method_chart = [
        {"label": PaymentMethod.CASH.label, "amount": str(method_totals.get(PaymentMethod.CASH, ZERO)), "key": "cash"},
        {"label": PaymentMethod.MTN_MOMO.label, "amount": str(method_totals.get(PaymentMethod.MTN_MOMO, ZERO)), "key": "momo"},
    ]
    method_max = max((Decimal(item["amount"]) for item in method_chart), default=ZERO)
    for item in method_chart:
        item["percent"] = round(100 * Decimal(item["amount"]) / method_max) if method_max else 0
    if request.GET.get("format") == "csv":
        if request.user.role in MANAGER_ROLES:
            rows = period_payments.exclude(status=PaymentStatus.REJECTED).order_by("recorded_at").values_list(
                "payment_reference", "recorded_at", "decided_at", "sale__reference", "method", "amount", "status", "momo_reference", "confirmation_reference"
            )
            headers = ("Payment reference", "Recorded at", "Confirmed at", "Sale reference", "Method", "Amount GHS", "Status", "MoMo transaction reference", "Confirmation reference")
        else:
            rows = period_payments.exclude(status=PaymentStatus.REJECTED).order_by("recorded_at").values_list(
                "recorded_at", "decided_at", "sale__reference", "method", "amount", "status"
            )
            headers = ("Recorded at", "Confirmed at", "Sale reference", "Method", "Amount GHS", "Status")
        return _csv_download("payment-report.csv", headers, rows)
    return render(request, "reports/payments.html", {
        "page_title": "Payment report", "form": form, "start": start, "end": end,
        "cash_total": method_totals.get(PaymentMethod.CASH, ZERO),
        "momo_total": method_totals.get(PaymentMethod.MTN_MOMO, ZERO),
        "confirmed_total": confirmed.aggregate(total=Sum("amount"))["total"] or ZERO,
        "pending_total": pending.aggregate(total=Sum("amount"))["total"] or ZERO,
        "pending_count": pending.count(), "payments": period_payments.exclude(status=PaymentStatus.REJECTED).select_related("sale", "recorded_by").order_by("-recorded_at")[:100],
        "daily_data": daily_data, "method_chart": method_chart,
        "chart_width": max(560, len(daily_data) * 38),
        "is_manager": request.user.role in MANAGER_ROLES, "csv_url": _csv_url(request),
    })


@login_required
@role_required(*MANAGER_ROLES)
def credit_report(request):
    form, start, end = _period_form(request)
    all_credit_sales = Sale.objects.filter(sale_type=SaleType.CREDIT)
    credit_sales, credit_total, paid_total, outstanding = credit_totals(all_credit_sales)
    period_credit_sales = _with_period(credit_sales, "created_at", start, end)
    period_credit_total = period_credit_sales.aggregate(total=Sum("total"))["total"] or ZERO
    period_repayments = _with_period(Payment.objects.filter(
        sale__in=credit_sales, status=PaymentStatus.CONFIRMED,
    ), "decided_at", start, end)
    period_paid = period_repayments.aggregate(total=Sum("amount"))["total"] or ZERO
    pending_sales = _with_period(Sale.objects.filter(
        sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL,
    ), "created_at", start, end)
    customers = []
    for customer in Customer.objects.order_by("full_name"):
        balance = customer.outstanding_debt
        if balance > 0:
            customers.append({"customer": customer, "balance": balance})
    paid_sales = _settled_credit_sales(credit_sales)
    if request.GET.get("format") == "csv":
        rows = ((row["customer"].full_name, row["customer"].phone_number, row["balance"]) for row in customers)
        return _csv_download("outstanding-debt.csv", ("Customer", "Phone", "Outstanding balance GHS"), rows)
    return render(request, "reports/credit.html", {
        "page_title": "Credit & debt report", "form": form, "start": start, "end": end,
        "credit_total": credit_total, "paid_total": paid_total, "outstanding": outstanding,
        "period_credit_total": period_credit_total, "period_paid": period_paid,
        "pending_count": pending_sales.count(), "pending_total": pending_sales.aggregate(total=Sum("total"))["total"] or ZERO,
        "customers": customers[:100], "paid_sales": paid_sales[:50],
        "is_manager": True, "csv_url": _csv_url(request),
    })


@login_required
@role_required(*MANAGER_ROLES)
def inventory_report(request):
    form, start, end = _period_form(request)
    active_products = Product.objects.filter(is_active=True)
    movements = _with_period(InventoryMovement.objects.select_related("product", "responsible_user"), "occurred_at", start, end)
    received = movements.filter(movement_type=MovementType.STOCK_RECEIVED, quantity__gt=0).aggregate(total=Sum("quantity"))["total"] or 0
    deducted = movements.filter(quantity__lt=0).aggregate(total=Sum("quantity"))["total"] or 0
    daily = (
        movements.annotate(report_day=TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()))
        .values("report_day")
        .annotate(
            added=Sum("quantity", filter=Q(quantity__gt=0)),
            deducted=Sum(-F("quantity"), filter=Q(quantity__lt=0)),
        )
    )
    daily_map = {row["report_day"]: row for row in daily}
    daily_data = []
    cursor = start
    while cursor <= end:
        daily_row = daily_map.get(cursor, {})
        received_day = daily_row.get("added") or 0
        deducted_day = daily_row.get("deducted") or 0
        daily_data.append({"date": cursor.isoformat(), "label": cursor.strftime("%b %-d"), "received": received_day, "deducted": deducted_day})
        cursor += timedelta(days=1)
    movement_max = max((max(point["received"], point["deducted"]) for point in daily_data), default=0)
    for point in daily_data:
        point["received_height"] = max(3, round(100 * point["received"] / movement_max)) if movement_max else 3
        point["deducted_height"] = max(3, round(100 * point["deducted"] / movement_max)) if movement_max else 3
    low_stock = active_products.filter(stock_quantity__lte=F("minimum_stock_level"))
    if request.GET.get("format") == "csv":
        rows = movements.order_by("occurred_at").values_list(
            "occurred_at", "product__name", "product__sku", "movement_type", "previous_stock", "quantity", "resulting_stock", "responsible_user__username", "reason", "reference"
        )
        return _csv_download("inventory-movements.csv", ("Date", "Product", "SKU", "Movement", "Previous", "Change", "Result", "User", "Reason", "Reference"), rows)
    return render(request, "reports/inventory.html", {
        "page_title": "Inventory report", "form": form, "start": start, "end": end,
        "active_count": active_products.count(), "available_units": active_products.aggregate(total=Sum("stock_quantity"))["total"] or 0,
        "low_stock_count": low_stock.count(), "out_of_stock_count": active_products.filter(stock_quantity=0).count(),
        "received": received, "deducted": abs(deducted), "low_stock_products": low_stock.order_by("stock_quantity", "name")[:100],
        "movements": movements.order_by("-occurred_at")[:100], "daily_data": daily_data,
        "chart_width": max(560, len(daily_data) * 38), "csv_url": _csv_url(request),
    })


@login_required
@role_required(*MANAGER_ROLES)
def employee_report(request):
    form, start, end = _period_form(request)
    sales = _with_period(_sales_scope_for_all(), "created_at", start, end)
    pending = _with_period(Sale.objects.filter(
        sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL,
    ), "created_at", start, end)
    employees = User.objects.filter(role=Role.EMPLOYEE)
    approved_rows = {row["created_by_id"]: row for row in sales.values("created_by_id").annotate(count=Count("pk"), total=Sum("total"))}
    pending_rows = {row["created_by_id"]: row for row in pending.values("created_by_id").annotate(count=Count("pk"), total=Sum("total"))}
    employee_data = []
    for employee in employees.order_by("first_name", "last_name", "username"):
        approved = approved_rows.get(employee.pk, {})
        waiting = pending_rows.get(employee.pk, {})
        employee_data.append({
            "employee": employee, "sales_count": approved.get("count", 0),
            "sales_total": approved.get("total") or ZERO,
            "pending_count": waiting.get("count", 0), "pending_total": waiting.get("total") or ZERO,
        })
    max_employee_value = max((row["sales_total"] for row in employee_data), default=ZERO)
    for row in employee_data:
        row["percent"] = round(100 * row["sales_total"] / max_employee_value) if max_employee_value else 0
    activity = _with_period(AuditEvent.objects.select_related("actor"), "occurred_at", start, end)
    activity = activity.filter(actor__role=Role.EMPLOYEE).order_by("-occurred_at")
    activity = list(activity[:100])
    for event in activity:
        event.action_label = event.action.replace("_", " ").title()
    if request.GET.get("format") == "csv":
        return _csv_download("employee-performance.csv", ("Employee", "Approved sales count", "Approved sales value GHS", "Pending credit count", "Pending credit value GHS"), (
            (row["employee"].get_full_name() or row["employee"].username, row["sales_count"], row["sales_total"], row["pending_count"], row["pending_total"]) for row in employee_data
        ))
    return render(request, "reports/employees.html", {
        "page_title": "Employee performance", "form": form, "start": start, "end": end,
        "employee_data": employee_data, "activity": activity, "csv_url": _csv_url(request),
    })


def _sales_scope_for_all():
    return Sale.objects.filter(status=SaleStatus.APPROVED)
