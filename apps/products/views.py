from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.models import Role
from apps.accounts.permissions import role_required
from apps.audit.models import record_audit_event

from .forms import MovementFilterForm, ProductForm, StockAdjustmentForm, StockReceiveForm
from .models import Brand, Category, InventoryMovement, InventoryUnit, MovementType, Product, UnitStatus
from .services import record_stock_movement


SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)


def _is_manager(user):
    return user.role in MANAGER_ROLES


@login_required
@role_required(*SHOP_ROLES)
def inventory_overview(request):
    active_products = Product.objects.filter(is_active=True)
    context = {
        "page_title": "Inventory",
        "active_products_count": active_products.count(),
        "available_units": active_products.aggregate(total=Sum("stock_quantity"))["total"] or 0,
        "low_stock_count": active_products.filter(stock_quantity__lte=F("minimum_stock_level")).count(),
        "out_of_stock_count": active_products.filter(stock_quantity=0).count(),
        "low_stock_products": active_products.filter(
            stock_quantity__lte=F("minimum_stock_level"),
        ).order_by("stock_quantity", "name")[:8],
        "recent_movements": InventoryMovement.objects.select_related("product", "responsible_user")[:8] if _is_manager(request.user) else (),
        "is_manager": _is_manager(request.user),
    }
    return render(request, "products/inventory.html", context)


@login_required
@role_required(*SHOP_ROLES)
def product_list(request):
    products = Product.objects.select_related("category", "brand").all()
    query = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "")
    brand_id = request.GET.get("brand", "")
    status = request.GET.get("status", "")
    if query:
        products = products.filter(Q(name__icontains=query) | Q(sku__icontains=query) | Q(model__icontains=query))
    if category_id.isdigit():
        products = products.filter(category_id=category_id)
    if brand_id.isdigit():
        products = products.filter(brand_id=brand_id)
    if status == "active":
        products = products.filter(is_active=True)
    elif status == "inactive":
        products = products.filter(is_active=False)
    page = Paginator(products, 20).get_page(request.GET.get("page"))
    return render(request, "products/list.html", {
        "page_title": "Products", "products": page, "query": query,
        "categories": Category.objects.filter(is_active=True), "brands": Brand.objects.filter(is_active=True),
        "selected_category": category_id, "selected_brand": brand_id, "selected_status": status,
        "is_manager": _is_manager(request.user),
    })


@login_required
@role_required(*SHOP_ROLES)
def product_detail(request, product_id):
    product = get_object_or_404(Product.objects.select_related("category", "brand"), pk=product_id)
    movements = product.movements.select_related("responsible_user").prefetch_related("units")[:15] if _is_manager(request.user) else ()
    units = product.units.filter(status=UnitStatus.AVAILABLE) if _is_manager(request.user) and product.track_individual_units else InventoryUnit.objects.none()
    return render(request, "products/detail.html", {
        "page_title": product.name, "product": product, "movements": movements,
        "available_units": units, "is_manager": _is_manager(request.user),
    })


@login_required
@role_required(*MANAGER_ROLES)
def product_create(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = form.save()
            record_audit_event(
                action="PRODUCT_CREATED", actor=request.user, object_type="product", object_reference=product.pk,
                details={"sku": product.sku, "name": product.name},
            )
        messages.success(request, f"Product {product.name} registered. Receive stock separately when it arrives.")
        return redirect("products:detail", product_id=product.pk)
    return render(request, "products/form.html", {"page_title": "Register product", "title": "Register product", "form": form})


@login_required
@role_required(*MANAGER_ROLES)
def product_edit(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    form = ProductForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        changed_fields = sorted(form.changed_data)
        with transaction.atomic():
            product = form.save()
            if changed_fields:
                record_audit_event(
                    action="PRODUCT_UPDATED", actor=request.user, object_type="product", object_reference=product.pk,
                    details={"changed_fields": changed_fields, "sku": product.sku},
                )
        messages.success(request, f"Product {product.name} updated.")
        return redirect("products:detail", product_id=product.pk)
    return render(request, "products/form.html", {"page_title": "Edit product", "title": "Edit product", "form": form, "product": product})


@login_required
@role_required(*MANAGER_ROLES)
def stock_receive(request):
    form = StockReceiveForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                movement = record_stock_movement(
                    product=data["product"], quantity=data["quantity"], movement_type=MovementType.STOCK_RECEIVED,
                    user=request.user, reason=data["reason"], reference=data["reference"],
                    unit_identifiers=data["unit_identifiers"], unit_condition=data["unit_condition"],
                )
                record_audit_event(
                    action="INVENTORY_STOCK_RECEIVED", actor=request.user, object_type="inventory_movement",
                    object_reference=movement.pk,
                    details={"product_sku": movement.product.sku, "quantity": movement.quantity, "reference": movement.reference},
                )
            messages.success(request, f"Received {movement.quantity} unit(s) of {movement.product.name}.")
            return redirect("products:history")
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "products/movement_form.html", {"page_title": "Receive stock", "title": "Receive stock", "form": form, "submit_label": "Record stock received"})


@login_required
@role_required(*MANAGER_ROLES)
def stock_adjust(request):
    form = StockAdjustmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                movement = record_stock_movement(
                    product=data["product"], quantity=data["quantity"], movement_type=MovementType.APPROVED_ADJUSTMENT,
                    user=request.user, reason=data["reason"], reference=data["reference"],
                    unit_identifiers=data["unit_identifiers"], unit_condition=data["unit_condition"],
                )
                record_audit_event(
                    action="INVENTORY_ADJUSTMENT_RECORDED", actor=request.user, object_type="inventory_movement",
                    object_reference=movement.pk,
                    details={"product_sku": movement.product.sku, "quantity": movement.quantity, "reference": movement.reference, "reason": movement.reason},
                )
            messages.success(request, "Stock adjustment recorded in the movement history.")
            return redirect("products:history")
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "products/movement_form.html", {"page_title": "Adjust stock", "title": "Record stock adjustment", "form": form, "submit_label": "Record adjustment", "is_adjustment": True})


@login_required
@role_required(*MANAGER_ROLES)
def movement_history(request):
    form = MovementFilterForm(request.GET or None)
    movements = InventoryMovement.objects.select_related("product", "responsible_user")
    if form.is_valid():
        data = form.cleaned_data
        if data.get("product"):
            movements = movements.filter(product=data["product"])
        if data.get("movement_type"):
            movements = movements.filter(movement_type=data["movement_type"])
        if data.get("date_from"):
            movements = movements.filter(occurred_at__date__gte=data["date_from"])
        if data.get("date_to"):
            movements = movements.filter(occurred_at__date__lte=data["date_to"])
    page = Paginator(movements, 25).get_page(request.GET.get("page"))
    return render(request, "products/history.html", {"page_title": "Inventory history", "form": form, "movements": page, "is_manager": _is_manager(request.user)})
