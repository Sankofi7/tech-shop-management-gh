from django.contrib.auth.models import AbstractUser, UserManager
from contextlib import contextmanager
from contextvars import ContextVar
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


_staff_write_allowed = ContextVar("staff_write_allowed", default=False)


@contextmanager
def allow_staff_write():
    token = _staff_write_allowed.set(True)
    try:
        yield
    finally:
        _staff_write_allowed.reset(token)


class Role(models.TextChoices):
    OWNER = "OWNER", "Owner"
    SECRETARY = "SECRETARY", "Secretary"
    EMPLOYEE = "EMPLOYEE", "Employee"


class AccountManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields["role"] = Role.OWNER
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    objects = AccountManager()
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.EMPLOYEE)
    must_change_password = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=Role.values),
                name="accounts_user_role_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(role=Role.OWNER, is_staff=True, is_superuser=True)
                    | Q(role=Role.SECRETARY, is_staff=True, is_superuser=False)
                    | Q(role=Role.EMPLOYEE, is_staff=False, is_superuser=False)
                ),
                name="accounts_user_role_flags_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        # Staff/superuser flags follow the assigned role and cannot be used
        # to silently grant an employee elevated Django access.
        if self.pk and not self._state.adding and not _staff_write_allowed.get():
            previous = type(self).objects.filter(pk=self.pk).values("role", "is_active", "must_change_password").first()
            if previous and any(
                previous[field] != getattr(self, field)
                for field in ("role", "is_active", "must_change_password")
            ):
                raise ValidationError("Role, account status, and password setup changes must use the staff management workflow.")
        self.is_staff = self.role in (Role.OWNER, Role.SECRETARY)
        self.is_superuser = self.role == Role.OWNER
        super().save(*args, **kwargs)
