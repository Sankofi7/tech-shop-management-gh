from decimal import Decimal
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.models import Role
from apps.accounts.permissions import role_required
from apps.sales.models import Payment, PaymentStatus, Sale, SaleStatus, SaleType
from apps.sales.services import record_payment

from .forms import CustomerForm, RepaymentForm
from .models import Customer
from .services import MANAGER_ROLES, SHOP_ROLES, create_customer, credit_totals, update_customer


def _visible_sales(user, customer):
    sales = customer.sales.select_related("created_by", "decided_by").prefetch_related("items__product", "payments")
    if user.role == Role.EMPLOYEE:
        sales = sales.filter(created_by=user)
    return sales


@login_required
@role_required(*SHOP_ROLES)
def customer_list(request):
    customers = Customer.objects.all()
    if request.user.role == Role.EMPLOYEE:
        customers = customers.filter(is_active=True)
    query = request.GET.get("q", "").strip()
    if query:
        phone_query = re.sub(r"\D", "", query)
        customers = customers.filter(
            Q(full_name__icontains=query)
            | Q(phone_number__icontains=query)
            | (Q(normalized_phone__icontains=phone_query) if phone_query else Q(pk__isnull=True))
        )
    return render(request, "customers/list.html", {"customers": customers.distinct(), "query": query, "is_manager": request.user.role in MANAGER_ROLES})


@login_required
@role_required(*SHOP_ROLES)
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            data = {key: form.cleaned_data[key] for key in ("full_name", "phone_number", "email", "address")}
            customer = create_customer(user=request.user, **data)
            messages.success(request, f"Customer {customer.full_name} created.")
            return redirect("customers:detail", customer_id=customer.pk)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "customers/form.html", {"form": form, "title": "Add customer"})


@login_required
@role_required(*MANAGER_ROLES)
def customer_edit(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    form = CustomerForm(request.POST or None, customer=customer, initial={
        "full_name": customer.full_name, "phone_number": customer.phone_number,
        "email": customer.email, "address": customer.address, "is_active": customer.is_active,
    })
    if request.method == "POST" and form.is_valid():
        try:
            update_customer(customer=customer, user=request.user, **form.cleaned_data)
            messages.success(request, "Customer record updated.")
            return redirect("customers:detail", customer_id=customer.pk)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "customers/form.html", {"form": form, "customer": customer, "title": "Edit customer"})


@login_required
@role_required(*SHOP_ROLES)
def customer_detail(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    sales = _visible_sales(request.user, customer)
    credit_sales, total_sales, total_paid, debt = credit_totals(sales)
    payments = Payment.objects.filter(sale__in=credit_sales).select_related("sale", "recorded_by", "decided_by")
    return render(request, "customers/detail.html", {
        "customer": customer,
        "sales": sales,
        "credit_sales": credit_sales,
        "payments": payments,
        "pending_payments": payments.filter(status=PaymentStatus.PENDING_CONFIRMATION),
        "total_credit_sales": total_sales,
        "total_paid": total_paid,
        "outstanding_debt": debt,
        "is_manager": request.user.role in MANAGER_ROLES,
    })


@login_required
@role_required(*SHOP_ROLES)
def customer_repayment_new(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id, is_active=True)
    sales = customer.sales.filter(sale_type=SaleType.CREDIT, status=SaleStatus.APPROVED)
    if request.user.role == Role.EMPLOYEE:
        sales = sales.filter(created_by=request.user)
    sales = sales.annotate(
        reserved_amount=Coalesce(
            Sum("payments__amount", filter=~Q(payments__status=PaymentStatus.REJECTED)),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
    ).filter(reserved_amount__lt=F("total"))
    form = RepaymentForm(request.POST or None, sales=sales)
    if request.method == "POST" and form.is_valid():
        sale = form.cleaned_data["sale"]
        if sale.customer_id != customer.pk:
            raise Http404
        try:
            payment = record_payment(sale=sale, user=request.user, **{
                key: value for key, value in form.cleaned_data.items() if key != "sale"
            })
            messages.success(request, f"Repayment {payment.payment_reference} recorded.")
            return redirect("customers:repayment_receipt", payment_id=payment.pk)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "customers/repayment_form.html", {"customer": customer, "form": form})


@login_required
@role_required(*SHOP_ROLES)
def repayment_receipt(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("sale__customer", "sale__created_by", "recorded_by", "decided_by"), pk=payment_id)
    if payment.sale.customer_id is None or payment.sale.sale_type != SaleType.CREDIT:
        raise Http404
    if request.user.role == Role.EMPLOYEE and payment.sale.created_by_id != request.user.pk:
        raise Http404
    return render(request, "customers/repayment_receipt.html", {"payment": payment, "sale": payment.sale, "customer": payment.sale.customer})


@login_required
@role_required(*SHOP_ROLES)
def debt_list(request, kind="outstanding"):
    if kind not in ("outstanding", "paid", "pending-momo"):
        raise Http404
    if kind == "pending-momo":
        payments = Payment.objects.filter(
            method="MTN_MOMO", status=PaymentStatus.PENDING_CONFIRMATION,
            sale__sale_type=SaleType.CREDIT, sale__status=SaleStatus.APPROVED, sale__customer__isnull=False,
        ).select_related("sale__customer", "sale", "recorded_by")
        if request.user.role == Role.EMPLOYEE:
            payments = payments.filter(sale__created_by=request.user)
        return render(request, "customers/debt_list.html", {"kind": kind, "payments": payments})

    customers = Customer.objects.order_by("full_name")
    rows = []
    for customer in customers:
        visible_sales = _visible_sales(request.user, customer).filter(sale_type=SaleType.CREDIT)
        credit_sales, total_sales, total_paid, balance = credit_totals(visible_sales)
        if not credit_sales.exists():
            continue
        if (kind == "outstanding" and balance > 0) or (kind == "paid" and balance == 0):
            rows.append({"customer": customer, "total_sales": total_sales, "total_paid": total_paid, "balance": balance})
    return render(request, "customers/debt_list.html", {"kind": kind, "rows": rows})
