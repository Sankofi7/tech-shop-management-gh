from functools import wraps

from django.core.exceptions import PermissionDenied


def has_any_role(user, *roles):
    return user.is_authenticated and getattr(user, "role", None) in roles


def role_required(*roles):
    """Enforce role access in a view on the server, independent of its UI."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not has_any_role(request.user, *roles):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator
