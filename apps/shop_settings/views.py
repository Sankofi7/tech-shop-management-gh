from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render

from apps.accounts.models import Role
from apps.accounts.permissions import role_required

from .forms import ShopProfileForm
from .models import ShopProfile
from .services import update_shop_profile


@login_required
@role_required(Role.OWNER, Role.SECRETARY)
def settings_page(request):
    profile = ShopProfile.get_solo()
    read_only = request.user.role == Role.SECRETARY
    if request.method == "POST":
        if read_only:
            raise PermissionDenied("Only the owner can change shop settings.")
        form = ShopProfileForm(request.POST, instance=profile)
        if form.is_valid():
            try:
                _saved_profile, changed = update_shop_profile(actor=request.user, cleaned_data=form.cleaned_data)
            except PermissionDenied:
                raise
            if changed:
                messages.success(request, "Shop settings saved successfully.")
            else:
                messages.info(request, "There were no settings changes to save.")
            return redirect("shop_settings:settings")
    else:
        form = ShopProfileForm(instance=profile, read_only=read_only)
    return render(request, "shop_settings/settings.html", {
        "page_title": "Settings",
        "form": form,
        "read_only": read_only,
        "profile": profile,
    })
