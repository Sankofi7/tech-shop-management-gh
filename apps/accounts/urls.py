from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import staff_views
from .password_views import RequiredPasswordChangeForm

app_name = "accounts"

urlpatterns = [
    path("staff/", staff_views.staff_directory, name="staff_list"),
    path("staff/new/", staff_views.staff_create, name="staff_create"),
    path("staff/<int:staff_id>/", staff_views.staff_detail, name="staff_detail"),
    path("staff/<int:staff_id>/edit/", staff_views.staff_edit, name="staff_edit"),
    path("staff/<int:staff_id>/status/", staff_views.staff_set_active, name="staff_status"),
    path("staff/<int:staff_id>/password-reset/", staff_views.staff_password_reset, name="staff_password_reset"),
    path(
        "password/change/",
        auth_views.PasswordChangeView.as_view(
            form_class=RequiredPasswordChangeForm,
            template_name="accounts/password_change.html",
            success_url=reverse_lazy("accounts:password_change_done"),
        ),
        name="password_change_required",
    ),
    path("password/changed/", auth_views.PasswordChangeDoneView.as_view(template_name="accounts/password_change_done.html"), name="password_change_done"),
]
