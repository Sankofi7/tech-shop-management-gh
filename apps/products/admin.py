from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied

from apps.accounts.models import Role

from .models import (
    Brand,
    Category,
    InventoryMovement,
    InventoryUnit,
    MovementType,
    Product,
    UnitCondition,
)
from .services import record_stock_movement


def _can_manage_catalog(user):
    return user.is_authenticated and user.role in (Role.OWNER, Role.SECRETARY)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)

    def has_module_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_view_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_add_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_change_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_delete_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)

    def has_module_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_view_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_add_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_change_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_delete_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "category", "brand", "stock_quantity", "minimum_stock_level", "is_low_stock", "is_active")
    list_filter = ("category", "brand", "track_individual_units", "is_active")
    search_fields = ("name", "sku", "model", "brand__name")
    readonly_fields = ("stock_quantity", "created_at", "updated_at")
    fieldsets = (
        ("Product", {"fields": ("name", "category", "brand", "model", "description", "sku")}),
        ("Pricing (GHS)", {"fields": ("purchase_price", "selling_price")}),
        ("Inventory", {"fields": ("stock_quantity", "minimum_stock_level", "track_individual_units", "is_active")}),
        ("Record dates", {"fields": ("created_at", "updated_at")}),
    )

    def has_module_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_view_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_add_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_change_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_delete_permission(self, request, obj=None):
        return False


class InventoryMovementForm(forms.ModelForm):
    unit_identifiers = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="For tracked products, enter one serial number or IMEI per line.",
    )
    unit_condition = forms.ChoiceField(choices=UnitCondition.choices, required=False)

    class Meta:
        model = InventoryMovement
        fields = ("product", "quantity", "movement_type", "reason", "reference")

    def clean_unit_identifiers(self):
        return [line.strip() for line in self.cleaned_data["unit_identifiers"].splitlines() if line.strip()]


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    form = InventoryMovementForm
    list_display = ("occurred_at", "product", "movement_type", "quantity", "previous_stock", "resulting_stock", "responsible_user", "reference")
    list_filter = ("movement_type", "occurred_at")
    search_fields = ("product__name", "product__sku", "reference", "reason", "responsible_user__username")
    fields = (
        "product", "quantity", "movement_type", "reason", "reference",
        "unit_identifiers", "unit_condition", "previous_stock", "resulting_stock",
        "responsible_user", "occurred_at",
    )
    readonly_fields = ("previous_stock", "resulting_stock", "responsible_user", "occurred_at")

    def has_module_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_view_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_add_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            raise PermissionDenied("Inventory movement history cannot be changed.")
        movement = record_stock_movement(
            product=form.cleaned_data["product"],
            quantity=form.cleaned_data["quantity"],
            movement_type=form.cleaned_data["movement_type"],
            user=request.user,
            reason=form.cleaned_data["reason"],
            reference=form.cleaned_data["reference"],
            unit_identifiers=form.cleaned_data["unit_identifiers"],
            unit_condition=form.cleaned_data["unit_condition"] or UnitCondition.NEW,
        )
        obj.pk = movement.pk
        obj._state.adding = False


@admin.register(InventoryUnit)
class InventoryUnitAdmin(admin.ModelAdmin):
    list_display = ("product", "identifier", "condition", "status", "created_at")
    list_filter = ("condition", "status", "product__category")
    search_fields = ("identifier", "product__name", "product__sku")
    readonly_fields = ("product", "identifier", "condition", "status", "created_at")

    def has_module_permission(self, request):
        return _can_manage_catalog(request.user)

    def has_view_permission(self, request, obj=None):
        return _can_manage_catalog(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
