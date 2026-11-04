import re
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


_customer_mutation_allowed = ContextVar("customer_mutation_allowed", default=False)


@contextmanager
def allow_customer_mutation():
    token = _customer_mutation_allowed.set(True)
    try:
        yield
    finally:
        _customer_mutation_allowed.reset(token)


def normalise_phone(value):
    value = (value or "").strip()
    if not value or not re.fullmatch(r"\+?[0-9().\-\s]+", value):
        raise ValidationError({"phone_number": "Enter a valid phone number using digits and common phone punctuation."})
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        raise ValidationError({"phone_number": "Phone number must contain between 7 and 15 digits."})
    # Treat common Ghana local and international formats as the same number.
    if digits.startswith("0") and len(digits) == 10:
        digits = "233" + digits[1:]
    return digits


class CustomerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if not _customer_mutation_allowed.get():
            raise ValidationError("Customer records must be changed through the customer service.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Customer history cannot be deleted.")


class Customer(models.Model):
    full_name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=32)
    normalized_phone = models.CharField(max_length=15, unique=True, editable=False)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customers_created")
    is_active = models.BooleanField(default=True)

    objects = CustomerQuerySet.as_manager()

    class Meta:
        ordering = ("full_name", "pk")

    def __str__(self):
        return f"{self.full_name} ({self.phone_number})"

    def clean(self):
        super().clean()
        self.full_name = self.full_name.strip()
        if not self.full_name:
            raise ValidationError({"full_name": "Enter the customer's full name."})
        self.normalized_phone = normalise_phone(self.phone_number)

    def save(self, *args, **kwargs):
        if not _customer_mutation_allowed.get():
            raise ValidationError("Customer records must be created or changed through the customer service.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Customer records cannot be deleted; deactivate them instead.")

    @property
    def approved_credit_sales(self):
        from apps.sales.models import SaleStatus, SaleType

        return self.sales.filter(sale_type=SaleType.CREDIT, status=SaleStatus.APPROVED)

    @property
    def total_approved_credit_sales(self):
        from django.db.models import Sum

        return self.approved_credit_sales.aggregate(total=Sum("total"))["total"] or Decimal("0.00")

    @property
    def total_confirmed_credit_payments(self):
        from apps.sales.models import Payment, PaymentStatus
        from django.db.models import Sum

        return Payment.objects.filter(
            sale__in=self.approved_credit_sales, status=PaymentStatus.CONFIRMED
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    @property
    def outstanding_debt(self):
        return self.total_approved_credit_sales - self.total_confirmed_credit_payments
