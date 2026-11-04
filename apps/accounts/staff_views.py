from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.audit.models import AuditEvent

from .models import Role, User
from .permissions import role_required
from .services import create_staff_account, reset_staff_password, update_staff_account
from .staff_forms import StaffCreateForm, StaffEditForm, StaffPasswordResetForm


@login_required
@role_required(Role.OWNER)
def staff_directory(request):
    staff = User.objects.all()
    query = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    status = request.GET.get("status", "")
    if query:
        staff = staff.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query)
            | Q(username__icontains=query) | Q(email__icontains=query)
        )
    if role in Role.values:
        staff = staff.filter(role=role)
    if status == "active":
        staff = staff.filter(is_active=True)
    elif status == "inactive":
        staff = staff.filter(is_active=False)
    page = Paginator(staff.order_by("role", "first_name", "last_name", "username"), 25).get_page(request.GET.get("page"))
    return render(request, "accounts/staff_list.html", {
        "page_title": "Staff Management", "staff_members": page, "query": query,
        "selected_role": role, "selected_status": status, "role_choices": Role.choices,
    })


@login_required
@role_required(Role.OWNER)
def staff_create(request):
    form = StaffCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            staff = create_staff_account(
                actor=request.user, username=data["username"], first_name=data["first_name"],
                last_name=data["last_name"], email=data["email"], role=data["role"],
                password=data["temporary_password"], is_active=data["is_active"],
            )
            messages.success(request, f"{staff.get_role_display()} account {staff.username} created. The user must change the temporary password at first sign-in.")
            return redirect("accounts:staff_detail", staff_id=staff.pk)
        except ValidationError as exc:
            for field, errors in getattr(exc, "error_dict", {}).items():
                form.add_error(field if field in form.fields else None, errors)
            if not getattr(exc, "error_dict", None):
                form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "accounts/staff_form.html", {
        "page_title": "Add staff member", "title": "Create staff account", "form": form,
    })


@login_required
@role_required(Role.OWNER)
def staff_detail(request, staff_id):
    staff = get_object_or_404(User, pk=staff_id)
    events = AuditEvent.objects.filter(
        Q(actor=staff) | Q(object_type="user", object_reference=str(staff.pk))
    ).select_related("actor").order_by("-occurred_at").distinct()
    page = Paginator(events, 25).get_page(request.GET.get("page"))
    return render(request, "accounts/staff_detail.html", {
        "page_title": "Staff profile", "staff": staff, "events": page,
    })


@login_required
@role_required(Role.OWNER)
def staff_edit(request, staff_id):
    staff = get_object_or_404(User, pk=staff_id)
    initial = {"first_name": staff.first_name, "last_name": staff.last_name, "email": staff.email}
    if staff.role != Role.OWNER:
        initial["role"] = staff.role
    form = StaffEditForm(request.POST or None, staff=staff, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            update_staff_account(
                actor=request.user, target=staff,
                first_name=data["first_name"], last_name=data["last_name"], email=data["email"],
                role=data.get("role"),
            )
            messages.success(request, "Staff account details updated.")
            return redirect("accounts:staff_detail", staff_id=staff.pk)
        except ValidationError as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "accounts/staff_form.html", {
        "page_title": "Edit staff account", "title": f"Edit {staff.username}",
        "form": form, "staff": staff,
    })


@login_required
@role_required(Role.OWNER)
@require_POST
def staff_set_active(request, staff_id):
    staff = get_object_or_404(User, pk=staff_id)
    requested = request.POST.get("active")
    if requested not in ("true", "false"):
        raise PermissionDenied("Choose a valid account status action.")
    try:
        update_staff_account(actor=request.user, target=staff, is_active=requested == "true")
    except ValidationError as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        label = "activated" if requested == "true" else "deactivated"
        messages.success(request, f"Account {staff.username} {label}.")
    return redirect("accounts:staff_detail", staff_id=staff.pk)


@login_required
@role_required(Role.OWNER)
def staff_password_reset(request, staff_id):
    staff = get_object_or_404(User, pk=staff_id)
    if staff.pk == request.user.pk:
        messages.info(request, "Use your personal password change page to change your own password.")
        return redirect("accounts:staff_detail", staff_id=staff.pk)
    form = StaffPasswordResetForm(request.POST or None, staff=staff)
    if request.method == "POST" and form.is_valid():
        try:
            reset_staff_password(actor=request.user, target=staff, password=form.cleaned_data["temporary_password"])
        except ValidationError as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, "Temporary password reset. The staff member must change it at next sign-in.")
            return redirect("accounts:staff_detail", staff_id=staff.pk)
    return render(request, "accounts/staff_form.html", {
        "page_title": "Reset temporary password", "title": f"Reset password for {staff.username}",
        "form": form, "staff": staff, "is_password_reset": True,
    })
