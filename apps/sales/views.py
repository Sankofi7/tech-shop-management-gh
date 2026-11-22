import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import Concat
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Role
from apps.accounts.permissions import role_required
from apps.customers.models import Customer
from apps.customers.services import create_customer
from apps.products.models import Category, InventoryUnit, Product, UnitStatus
from apps.shop_settings.models import ShopProfile

from .forms import MomoConfirmationForm, PaymentForm, RejectionForm, SaleHeaderForm, SaleLineFormSet
from .models import Payment, PaymentMethod, PaymentStatus, Sale, SaleItem, SaleStatus
from .services import approve_sale, confirm_momo_payment, create_sale, record_payment, reject_momo_payment, reject_sale


SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)


def _parse_filter_date(raw_value):
    if not raw_value:
        return None
    try:
        return parse_date(raw_value)
    except ValueError:
        return None


@login_required
@role_required(*SHOP_ROLES)
def sales_history(request):
    sales = Sale.objects.select_related("created_by", "decided_by").prefetch_related("items__product", "payments")
    is_manager = request.user.role in MANAGER_ROLES
    if not is_manager:
        sales = sales.filter(created_by=request.user)

    date_from_raw = request.GET.get("date_from", "").strip()
    date_to_raw = request.GET.get("date_to", "").strip()
    date_from = _parse_filter_date(date_from_raw)
    date_to = _parse_filter_date(date_to_raw)
    errors = []
    if date_from_raw and date_from is None:
        errors.append("Enter a valid start date.")
    if date_to_raw and date_to is None:
        errors.append("Enter a valid end date.")
    reversed_date_range = bool(date_from and date_to and date_to < date_from)
    if reversed_date_range:
        errors.append("End date must be on or after the start date.")
    else:
        if date_from:
            sales = sales.filter(created_at__date__gte=date_from)
        if date_to:
            sales = sales.filter(created_at__date__lte=date_to)
    employee_id = request.GET.get("employee", "")
    if is_manager and employee_id.isdigit():
        sales = sales.filter(created_by_id=int(employee_id))
    product_id = request.GET.get("product", "")
    if product_id.isdigit():
        matching_product = SaleItem.objects.filter(sale_id=OuterRef("pk"), product_id=int(product_id))
        sales = sales.filter(Exists(matching_product))
    search = request.GET.get("search", "").strip()
    if search:
        sales = sales.annotate(
            employee_display_name=Concat("created_by__first_name", Value(" "), "created_by__last_name"),
        )
        matching_search_product = SaleItem.objects.filter(sale_id=OuterRef("pk")).filter(
            Q(product__name__icontains=search) | Q(product__sku__icontains=search)
        )
        sales = sales.filter(
            Q(reference__icontains=search)
            | Q(created_by__username__icontains=search)
            | Q(created_by__first_name__icontains=search)
            | Q(created_by__last_name__icontains=search)
            | Q(employee_display_name__icontains=search)
            | Q(customer_name_snapshot__icontains=search)
            | Q(customer__full_name__icontains=search)
            | Exists(matching_search_product)
        )
    sale_status = request.GET.get("sale_status", "")
    if sale_status in SaleStatus.values:
        sales = sales.filter(status=sale_status)
    payment_method = request.GET.get("payment_method", "")
    if payment_method in PaymentMethod.values:
        sales = sales.filter(Exists(Payment.objects.filter(sale_id=OuterRef("pk"), method=payment_method)))
    payment_status = request.GET.get("payment_status", "")
    if payment_status in PaymentStatus.values:
        matching_payments = Payment.objects.filter(sale_id=OuterRef("pk"), status=payment_status)
        if payment_method in PaymentMethod.values:
            matching_payments = matching_payments.filter(method=payment_method)
        sales = sales.filter(Exists(matching_payments))

    pending_momo = Payment.objects.filter(
        sale_id=OuterRef("pk"), method=PaymentMethod.MTN_MOMO,
        status=PaymentStatus.PENDING_CONFIRMATION,
    )
    confirmed_payment = Payment.objects.filter(sale_id=OuterRef("pk"), status=PaymentStatus.CONFIRMED)
    rejected_payment = Payment.objects.filter(sale_id=OuterRef("pk"), status=PaymentStatus.REJECTED)
    sales = sales.annotate(
        has_pending_momo=Exists(pending_momo),
        has_confirmed_payment=Exists(confirmed_payment),
        has_rejected_payment=Exists(rejected_payment),
    )

    context = {
        "sales": sales,
        "is_manager": is_manager,
        "employees": Sale.objects.values("created_by_id", "created_by__username").distinct().order_by("created_by__username") if is_manager else (),
        "products": Product.objects.filter(is_active=True).order_by("name"),
        "filters": request.GET,
        "errors": errors,
        "sale_statuses": SaleStatus.choices,
        "payment_methods": PaymentMethod.choices,
        "payment_statuses": PaymentStatus.choices,
    }
    return render(request, "sales/history.html", context)


@login_required
@role_required(*SHOP_ROLES)
def create_sale_view(request):
    if request.method == "POST":
        raw_submission_key = request.POST.get("submission_key", "")
        try:
            posted_submission_key = uuid.UUID(raw_submission_key) if raw_submission_key else None
        except (ValueError, TypeError, AttributeError):
            posted_submission_key = None
        if posted_submission_key:
            existing_sale = Sale.objects.filter(submission_key=posted_submission_key).first()
            if existing_sale:
                if existing_sale.created_by_id != request.user.pk:
                    raise PermissionDenied("This checkout submission belongs to a different user.")
                return redirect("sales:receipt", sale_id=existing_sale.pk)
        header = SaleHeaderForm(request.POST)
        lines_formset = SaleLineFormSet(request.POST, prefix="items")
        if header.is_valid() and lines_formset.is_valid():
            submission_key = header.cleaned_data.get("submission_key") or uuid.uuid4()
            existing_sale = Sale.objects.filter(submission_key=submission_key).first()
            if existing_sale:
                if existing_sale.created_by_id != request.user.pk:
                    raise PermissionDenied("This checkout submission belongs to a different user.")
                return redirect("sales:receipt", sale_id=existing_sale.pk)
            lines = [
                {"product": data["product"], "quantity": data["quantity"], "unit_identifiers": data["unit_identifiers"]}
                for data in (form.cleaned_data for form in lines_formset.forms)
                if data.get("product")
            ]
            initial_payment = None
            if header.cleaned_data["payment_amount"] is not None:
                initial_payment = {
                    "amount": header.cleaned_data["payment_amount"],
                    "method": header.cleaned_data["payment_method"],
                    "momo_reference": header.cleaned_data["momo_reference"],
                }
            try:
                with transaction.atomic():
                    customer = header.cleaned_data.get("customer")
                    if header.cleaned_data.get("new_customer_name") and header.cleaned_data.get("new_customer_phone"):
                        customer = create_customer(
                            user=request.user,
                            full_name=header.cleaned_data["new_customer_name"],
                            phone_number=header.cleaned_data["new_customer_phone"],
                            email=header.cleaned_data.get("new_customer_email", ""),
                            address=header.cleaned_data.get("new_customer_address", ""),
                        )
                    sale = create_sale(
                        user=request.user,
                        sale_type=header.cleaned_data["sale_type"],
                        lines=lines,
                        initial_payment=initial_payment,
                        customer=customer,
                        submission_key=submission_key,
                    )
                if sale.status == SaleStatus.PENDING_APPROVAL:
                    messages.warning(request, f"Sale {sale.reference} recorded as PENDING APPROVAL.")
                elif sale.payments.filter(status=PaymentStatus.PENDING_CONFIRMATION).exists():
                    messages.info(request, f"Sale {sale.reference} recorded. MTN payment is PENDING CONFIRMATION.")
                else:
                    messages.success(request, f"Sale {sale.reference} recorded.")
                return redirect("sales:receipt", sale_id=sale.pk)
            except IntegrityError:
                existing_sale = Sale.objects.filter(
                    submission_key=submission_key, created_by=request.user
                ).first() if submission_key else None
                if existing_sale:
                    return redirect("sales:receipt", sale_id=existing_sale.pk)
                raise
            except (ValidationError, PermissionDenied) as exc:
                header.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        header = SaleHeaderForm(initial={"submission_key": uuid.uuid4(), "sale_type": "CASH"})
        lines_formset = SaleLineFormSet(prefix="items")
    cart_initial = _cart_from_posted_lines(lines_formset) if request.method == "POST" else []
    return render(request, "sales/create.html", {
        "header": header,
        "lines_formset": lines_formset,
        "cart_initial": cart_initial,
        "is_manager": request.user.role in MANAGER_ROLES,
        "page_title": "Point of Sale",
        "has_checkout_errors": bool(
            header.errors
            or lines_formset.non_form_errors()
            or any(lines_formset.errors)
        ),
    })


def _cart_from_posted_lines(lines_formset):
    posted = []
    for form in lines_formset.forms:
        product_id = form.data.get(form.add_prefix("product"), "")
        if not product_id.isdigit():
            continue
        quantity = form.data.get(form.add_prefix("quantity"), "1")
        unit_identifiers = form.data.get(form.add_prefix("unit_identifiers"), "")
        posted.append((int(product_id), quantity, [value.strip() for value in unit_identifiers.splitlines() if value.strip()]))
    products = {
        product.pk: product for product in Product.objects.filter(
            pk__in={item[0] for item in posted}, is_active=True
        )
    }
    result = []
    for product_id, raw_quantity, unit_identifiers in posted:
        product = products.get(product_id)
        if product is None:
            continue
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            quantity = 1
        result.append({
            "product_id": product.pk,
            "quantity": quantity,
            "unit_identifiers": unit_identifiers,
        })
    return result


@login_required
@role_required(*SHOP_ROLES)
@require_GET
def product_catalog(request):
    query = request.GET.get("q", "").strip()[:100]
    category_id = request.GET.get("category", "").strip()
    if category_id and not category_id.isdigit():
        return JsonResponse({"error": "Choose a valid category."}, status=400)

    products = Product.objects.filter(is_active=True).select_related("category")
    if query:
        products = products.filter(
            Q(name__icontains=query) | Q(sku__icontains=query) | Q(model__icontains=query)
            | Q(brand__name__icontains=query)
        )
    if category_id:
        products = products.filter(category_id=int(category_id))

    payload = [{
        "id": product.pk,
        "name": product.name,
        "sku": product.sku,
        "model": product.model,
        "category_id": product.category_id,
        "category": product.category.name,
        "selling_price": str(product.selling_price),
        "stock_quantity": product.stock_quantity,
        "minimum_stock_level": product.minimum_stock_level,
        "is_low_stock": product.is_low_stock,
        "track_individual_units": product.track_individual_units,
    } for product in products.order_by("name", "pk")[:250]]
    categories = list(Category.objects.filter(is_active=True).order_by("name").values("id", "name"))
    return JsonResponse({"products": payload, "categories": categories, "query": query})


@login_required
@role_required(*SHOP_ROLES)
@require_GET
def available_product_units(request, product_id):
    product = get_object_or_404(Product, pk=product_id, is_active=True)
    if not product.track_individual_units:
        return JsonResponse({"units": []})
    units = InventoryUnit.objects.filter(
        product=product, status=UnitStatus.AVAILABLE
    ).order_by("identifier").values("id", "identifier")
    return JsonResponse({"product_id": product.pk, "units": list(units)})


@login_required
@role_required(*SHOP_ROLES)
def sale_receipt(request, sale_id):
    sale = get_object_or_404(
        Sale.objects.select_related("created_by", "decided_by").prefetch_related(
            "items__product", "items__units", "payments__recorded_by", "payments__decided_by"
        ), pk=sale_id
    )
    _ensure_sale_visible(request.user, sale)
    profile = ShopProfile.get_solo()
    shop_name = profile.shop_name.strip() or "1ST POINT TECH HUB"
    return render(request, "sales/receipt.html", {
        "sale": sale, "payments": sale.payments.all(), "shop_name": shop_name,
    })


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def approve_sale_view(request, sale_id):
    sale = get_object_or_404(Sale, pk=sale_id)
    try:
        approved = approve_sale(sale=sale, user=request.user)
        messages.success(request, f"Sale {approved.reference} approved.")
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("sales:receipt", sale_id=sale_id)


@login_required
@role_required(*MANAGER_ROLES)
def reject_sale_view(request, sale_id):
    sale = get_object_or_404(Sale, pk=sale_id)
    form = RejectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            rejected = reject_sale(sale=sale, user=request.user, reason=form.cleaned_data["reason"])
            messages.success(request, f"Sale {rejected.reference} rejected and retained in history.")
            return redirect("sales:receipt", sale_id=sale_id)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/decision.html", {"sale": sale, "form": form, "title": "Reject credit sale"})


@login_required
@role_required(*SHOP_ROLES)
def record_payment_view(request, sale_id):
    sale = get_object_or_404(Sale, pk=sale_id)
    _ensure_sale_visible(request.user, sale)
    form = PaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            payment = record_payment(sale=sale, user=request.user, **form.cleaned_data)
            if payment.status == PaymentStatus.PENDING_CONFIRMATION:
                messages.info(request, "MTN payment recorded as pending confirmation; it is not counted as received yet.")
            else:
                messages.success(request, "Cash payment recorded.")
            return redirect("sales:receipt", sale_id=sale_id)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/payment.html", {"sale": sale, "form": form, "title": "Record payment"})


@login_required
@role_required(*MANAGER_ROLES)
def confirm_payment_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("sale"), pk=payment_id)
    form = MomoConfirmationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            confirm_momo_payment(payment=payment, user=request.user, **form.cleaned_data)
            messages.success(request, "MTN payment confirmation recorded.")
            return redirect("sales:receipt", sale_id=payment.sale_id)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/payment_decision.html", {"payment": payment, "form": form, "title": "Confirm MTN payment"})


@login_required
@role_required(*MANAGER_ROLES)
def reject_payment_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("sale"), pk=payment_id)
    form = RejectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reject_momo_payment(payment=payment, user=request.user, reason=form.cleaned_data["reason"])
            messages.success(request, "MTN payment was rejected and remains in payment history.")
            return redirect("sales:receipt", sale_id=payment.sale_id)
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/payment_decision.html", {"payment": payment, "form": form, "title": "Reject MTN payment"})


def _ensure_sale_visible(user, sale):
    if user.role == Role.EMPLOYEE and sale.created_by_id != user.pk:
        raise Http404
    if user.role not in SHOP_ROLES:
        raise PermissionDenied
