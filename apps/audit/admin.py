from django.contrib import admin

from apps.accounts.models import Role

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "action", "actor", "object_type", "object_reference")
    list_filter = ("action", "object_type", "occurred_at")
    search_fields = ("object_reference", "actor__username")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)

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
