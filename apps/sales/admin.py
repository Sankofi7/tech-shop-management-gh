from django.contrib import admin

from apps.accounts.models import Role

from .models import Payment, Sale, SaleItem


def _sales_admin_access(user):
    return user.is_authenticated and user.role in (Role.OWNER, Role.SECRETARY)


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    can_delete = False
    readonly_fields = ("product", "quantity", "unit_price", "line_total")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("reference", "created_at", "created_by", "customer", "sale_type", "status", "total", "decided_by")
    list_filter = ("status", "sale_type", "created_at")
    search_fields = ("reference", "created_by__username", "customer__full_name", "customer__phone_number")
    readonly_fields = tuple(field.name for field in Sale._meta.fields)
    inlines = (SaleItemInline,)

    def has_module_permission(self, request):
        return _sales_admin_access(request.user)

    def has_view_permission(self, request, obj=None):
        return _sales_admin_access(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_reference", "sale", "amount", "method", "status", "momo_reference", "recorded_by", "recorded_at", "decided_by")
    list_filter = ("method", "status", "recorded_at")
    search_fields = ("sale__reference", "momo_reference", "confirmation_reference")
    readonly_fields = tuple(field.name for field in Payment._meta.fields)

    def has_module_permission(self, request):
        return _sales_admin_access(request.user)

    def has_view_permission(self, request, obj=None):
        return _sales_admin_access(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
