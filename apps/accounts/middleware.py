from django.shortcuts import redirect
from django.urls import reverse


class PasswordChangeRequiredMiddleware:
    """Keep temporary-password accounts inside the password-change flow."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.must_change_password:
            allowed_paths = {
                reverse("accounts:password_change_required"),
                reverse("logout"),
            }
            if request.path not in allowed_paths and not request.path.startswith("/static/"):
                return redirect("accounts:password_change_required")
        return self.get_response(request)
