from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.audit.models import record_audit_event
from .models import Role, User
from .services import update_staff_account


class AccountCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name", "role")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [(role, label) for role, label in Role.choices if role != Role.OWNER]


class AccountChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = AccountCreationForm
    form = AccountChangeForm
    list_display = ("username", "email", "first_name", "last_name", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")
    ordering = ("username",)

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        ("Access", {"fields": ("role", "is_active", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "first_name", "last_name", "role", "password1", "password2"),
        }),
    )
    readonly_fields = ("last_login", "date_joined", "must_change_password")

    def _is_owner(self, request):
        return (
            request.user.is_authenticated
            and request.user.role == Role.OWNER
            and request.user.is_superuser
        )

    def has_module_permission(self, request):
        return self._is_owner(request)

    def has_view_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_add_permission(self, request):
        return self._is_owner(request)

    def has_change_permission(self, request, obj=None):
        return self._is_owner(request)

    def has_delete_permission(self, request, obj=None):
        return False

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if not self._is_owner(request):
            raise PermissionDenied("Only the owner can manage staff accounts.")
        if change:
            try:
                update_staff_account(
                    actor=request.user, target=obj.pk, first_name=obj.first_name,
                    last_name=obj.last_name, email=obj.email, role=obj.role,
                    is_active=obj.is_active,
                )
            except ValidationError:
                raise
            obj.refresh_from_db()
            return
        if obj.role == Role.OWNER:
            raise ValidationError("The staff form cannot create another owner.")
        obj.must_change_password = True
        obj.save()
        record_audit_event(
            action="STAFF_ACCOUNT_CREATED", actor=request.user, object_type="user",
            object_reference=obj.pk,
            details={"username": obj.username, "role": obj.role},
        )
