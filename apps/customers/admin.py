from django.contrib import admin

from apps.accounts.models import Role

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone_number", "email", "is_active", "created_at", "created_by")
    list_filter = ("is_active", "created_at")
    search_fields = ("full_name", "phone_number", "normalized_phone", "email")
    readonly_fields = tuple(field.name for field in Customer._meta.fields)

    def has_module_permission(self, request):
        return request.user.is_authenticated and request.user.role in (Role.OWNER, Role.SECRETARY)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
