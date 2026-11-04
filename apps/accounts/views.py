from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.notifications import dashboard_notifications
from apps.accounts.permissions import role_required
from apps.audit.models import AuditEvent
from apps.customers.models import Customer
from apps.products.models import Product
from apps.sales.models import Payment, PaymentStatus, Sale, SaleType


SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)
ZERO_MONEY = Decimal("0.00")


def _local_day_range(first_day, after_last_day):
    """Build timezone-aware local-midnight bounds for indexed timestamp filters."""
    current_tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first_day, time.min), current_tz)
    end = timezone.make_aware(datetime.combine(after_last_day, time.min), current_tz)
    return start, end


def _sales_scope(user):
    sales = Sale.objects.approved()
    if user.role == Role.EMPLOYEE:
        sales = sales.filter(created_by=user)
    return sales


@login_required
@role_required(*SHOP_ROLES)
def dashboard(request):
    user = request.user
    is_manager = user.role in MANAGER_ROLES
    today = timezone.localdate()
    today_start, tomorrow_start = _local_day_range(today, today + timedelta(days=1))
    chart_start_day = today - timedelta(days=6)
    chart_start, chart_end = _local_day_range(chart_start_day, today + timedelta(days=1))

    sales_scope = _sales_scope(user)
    todays_sales = sales_scope.filter(created_at__gte=today_start, created_at__lt=tomorrow_start)
    today_totals = todays_sales.aggregate(total=Sum("total"), count=Count("pk"))
    today_total = today_totals["total"] if today_totals["total"] is not None else ZERO_MONEY
    today_count = today_totals["count"]

    credit_sales = sales_scope.filter(sale_type=SaleType.CREDIT)
    credit_totals = credit_sales.aggregate(total=Sum("total"))["total"]
    confirmed_payments = Payment.objects.filter(
        sale__in=credit_sales, status=PaymentStatus.CONFIRMED
    ).aggregate(total=Sum("amount"))["total"]
    outstanding_debt = (credit_totals or ZERO_MONEY) - (confirmed_payments or ZERO_MONEY)

    daily_totals = {
        row["sale_day"]: row["total"]
        for row in sales_scope.filter(created_at__gte=chart_start, created_at__lt=chart_end)
        .annotate(sale_day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values("sale_day")
        .annotate(total=Sum("total"))
    }
    chart_data = []
    for offset in range(7):
        day = chart_start_day + timedelta(days=offset)
        chart_data.append({
            "date": day.isoformat(),
            "label": day.strftime("%a"),
            "amount": str(daily_totals.get(day, ZERO_MONEY)),
        })

    stock_query = Product.objects.filter(
        is_active=True, stock_quantity__lte=F("minimum_stock_level")
    )
    low_stock_count = stock_query.count()
    low_stock_products = stock_query.only(
        "name", "sku", "stock_quantity", "minimum_stock_level"
    ).order_by("stock_quantity", "name")[:6]

    activity = AuditEvent.objects.select_related("actor").only(
        "action", "occurred_at", "actor__username", "actor__first_name", "actor__last_name"
    )
    if not is_manager:
        activity = activity.filter(actor=user)
    recent_activity = list(activity[:6])
    for event in recent_activity:
        event.action_label = event.action.replace("_", " ").capitalize()

    active_customer_count = Customer.objects.filter(is_active=True).count()
    notifications = dashboard_notifications(request)

    hour = timezone.localtime().hour
    greeting = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    return render(request, "dashboard.html", {
        "page_title": "Dashboard",
        "greeting": greeting,
        "recent_activity": recent_activity,
        "low_stock_products": low_stock_products,
        "low_stock_count": low_stock_count,
        "active_customer_count": active_customer_count,
        "today_sales_total": today_total,
        "today_sales_count": today_count,
        "outstanding_debt": outstanding_debt,
        "chart_data": chart_data,
        "chart_has_sales": any(Decimal(point["amount"]) > 0 for point in chart_data),
        "is_manager": is_manager,
        **notifications,
    })
