from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


_profile_mutation_allowed = ContextVar("shop_profile_mutation_allowed", default=False)


@contextmanager
def allow_profile_mutation():
    token = _profile_mutation_allowed.set(True)
    try:
        yield
    finally:
        _profile_mutation_allowed.reset(token)


class ShopProfileQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Shop settings must be changed through the settings workflow.")

    def delete(self):
        raise ValidationError("The shop profile cannot be deleted.")


class ShopProfile(models.Model):
    """The single active business profile and global in-app alert preferences."""

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    shop_name = models.CharField(max_length=200, blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    business_email = models.EmailField(max_length=254, blank=True)
    physical_address = models.TextField(blank=True)
    notify_low_stock = models.BooleanField(default=True)
    notify_pending_credit = models.BooleanField(default=True)
    notify_pending_momo = models.BooleanField(default=True)
    return_window_days = models.PositiveSmallIntegerField(default=7, validators=[MinValueValidator(1)])
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shop_profile_updates",
    )

    objects = ShopProfileQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(singleton_key=1), name="shop_settings_singleton_key_is_one"),
        ]
        verbose_name = "shop profile and notification preferences"
        verbose_name_plural = "shop profile and notification preferences"

    def __str__(self):
        return self.shop_name or "Shop profile (not configured)"

    @classmethod
    def get_solo(cls):
        # A missing row is returned in memory so a GET request remains read-only.
        return cls.objects.filter(singleton_key=1).first() or cls(singleton_key=1)

    def save(self, *args, **kwargs):
        if self.pk and not _profile_mutation_allowed.get():
            raise ValidationError("Shop settings must be changed through the audited settings workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The shop profile cannot be deleted.")
