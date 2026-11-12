import uuid
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


_sale_mutation_allowed = ContextVar("sale_mutation_allowed", default=False)
_payment_mutation_allowed = ContextVar("payment_mutation_allowed", default=False)
_sale_item_creation_allowed = ContextVar("sale_item_creation_allowed", default=False)


class SaleType(models.TextChoices):
    CASH = "CASH", "Cash sale"
    CREDIT = "CREDIT", "Credit sale"


class SaleStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class SaleQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if not _sale_mutation_allowed.get():
            raise ValidationError("Sales must be changed through the sales workflow.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Sales history cannot be deleted.")

    def approved(self):
        return self.filter(status=SaleStatus.APPROVED)


def new_sale_reference():
    return f"TS-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def new_payment_reference():
    return f"PMT-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


class Sale(models.Model):
    reference = models.CharField(max_length=32, unique=True, default=new_sale_reference, editable=False)
    submission_key = models.UUIDField(unique=True, null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_created")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    sale_type = models.CharField(max_length=8, choices=SaleType.choices)
    status = models.CharField(max_length=20, choices=SaleStatus.choices)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_decided", null=True, blank=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="sales", null=True, blank=True
    )
    customer_name_snapshot = models.CharField(max_length=200, blank=True)
    customer_phone_snapshot = models.CharField(max_length=32, blank=True)

    objects = SaleQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=Q(sale_type__in=SaleType.values), name="sales_sale_type_valid"),
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="sales_subtotal_nonnegative"),
            models.CheckConstraint(condition=Q(total__gte=0), name="sales_total_nonnegative"),
            models.CheckConstraint(condition=Q(total=F("subtotal")), name="sales_total_matches_subtotal"),
            models.CheckConstraint(
                condition=(
                    Q(status=SaleStatus.REJECTED, rejection_reason__gt="")
                    | Q(status__in=[SaleStatus.PENDING_APPROVAL, SaleStatus.APPROVED], rejection_reason="")
                ),
                name="sales_rejection_reason_consistent",
            ),
        ]

    def __str__(self):
        return self.reference

    @property
    def amount_paid(self):
        return self.payments.filter(status=PaymentStatus.CONFIRMED).aggregate(total=models.Sum("amount"))["total"] or 0

    @property
    def outstanding_balance(self):
        from apps.customers.ledger import sale_outstanding_balance

        return sale_outstanding_balance(self)

    @property
    def pending_momo_amount(self):
        return self.payments.filter(status=PaymentStatus.PENDING_CONFIRMATION).aggregate(total=models.Sum("amount"))["total"] or 0

    def save(self, *args, **kwargs):
        if not _sale_mutation_allowed.get():
            raise ValidationError("Sales can only be changed through the approval workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Sales history cannot be deleted.")


class ImmutableSaleItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Sale items cannot be changed after recording.")

    def delete(self):
        raise ValidationError("Sale items cannot be deleted.")


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="sale_items")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    units = models.ManyToManyField("products.InventoryUnit", through="SaleItemUnit", related_name="sale_items", blank=True)

    objects = ImmutableSaleItemQuerySet.as_manager()

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sales_item_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="sales_item_price_nonnegative"),
            models.CheckConstraint(condition=Q(line_total=F("unit_price") * F("quantity")), name="sales_item_total_matches_quantity"),
        ]

    def __str__(self):
        return f"{self.product.sku} x {self.quantity}"

    def save(self, *args, **kwargs):
        if self.pk or not _sale_item_creation_allowed.get():
            raise ValidationError("Sale items can only be added through the sale workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Sale items cannot be deleted.")


class SaleItemUnit(models.Model):
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT)
    unit = models.ForeignKey("products.InventoryUnit", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("sale_item", "unit"), name="sales_sale_item_unit_pair_uniq"),
        ]


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    MTN_MOMO = "MTN_MOMO", "MTN Mobile Money"


class PaymentStatus(models.TextChoices):
    CONFIRMED = "CONFIRMED", "Confirmed"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION", "Pending MTN confirmation"
    REJECTED = "REJECTED", "Rejected"


class PaymentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if not _payment_mutation_allowed.get():
            raise ValidationError("Payments must be changed through the payment workflow.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Payment history cannot be deleted.")


class Payment(models.Model):
    payment_reference = models.CharField(max_length=32, unique=True, null=True, blank=True, editable=False)
    idempotency_key = models.CharField(max_length=36, unique=True, null=True, blank=True, editable=False)
    customer_name_snapshot = models.CharField(max_length=200, blank=True)
    customer_phone_snapshot = models.CharField(max_length=32, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=12, choices=PaymentMethod.choices)
    status = models.CharField(max_length=24, choices=PaymentStatus.choices)
    momo_reference = models.CharField(max_length=120, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_recorded")
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_decided", null=True, blank=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    confirmation_reference = models.CharField(max_length=120, blank=True)
    decision_reason = models.TextField(blank=True)

    objects = PaymentQuerySet.as_manager()

    class Meta:
        ordering = ("recorded_at", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="sales_payment_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(method=PaymentMethod.CASH, status=PaymentStatus.CONFIRMED)
                    | Q(method=PaymentMethod.MTN_MOMO)
                ),
                name="sales_cash_payment_always_confirmed",
            ),
            models.CheckConstraint(
                condition=(
                    Q(method=PaymentMethod.CASH, momo_reference="")
                    | Q(method=PaymentMethod.MTN_MOMO, momo_reference__gt="")
                ),
                name="sales_momo_reference_if_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        method=PaymentMethod.CASH,
                        status=PaymentStatus.CONFIRMED,
                        decided_by__isnull=False,
                        decided_at__isnull=False,
                        confirmation_reference="",
                        decision_reason="",
                    )
                    | Q(
                        method=PaymentMethod.MTN_MOMO,
                        status=PaymentStatus.PENDING_CONFIRMATION,
                        decided_by__isnull=True,
                        decided_at__isnull=True,
                        confirmation_reference="",
                        decision_reason="",
                    )
                    | Q(
                        method=PaymentMethod.MTN_MOMO,
                        status=PaymentStatus.CONFIRMED,
                        decided_by__isnull=False,
                        decided_at__isnull=False,
                        confirmation_reference__gt="",
                        decision_reason="",
                    )
                    | Q(
                        method=PaymentMethod.MTN_MOMO,
                        status=PaymentStatus.REJECTED,
                        decided_by__isnull=False,
                        decided_at__isnull=False,
                        decision_reason__gt="",
                    )
                ),
                name="sales_payment_decision_state_consistent",
            ),
            models.UniqueConstraint(
                fields=("momo_reference",), condition=~Q(momo_reference=""), name="sales_momo_reference_unique"
            ),
        ]

    def __str__(self):
        return f"{self.sale.reference}: {self.amount} GHS ({self.get_method_display()})"

    def save(self, *args, **kwargs):
        if not _payment_mutation_allowed.get():
            raise ValidationError("Payments can only be changed through the payment workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Payment history cannot be deleted.")
