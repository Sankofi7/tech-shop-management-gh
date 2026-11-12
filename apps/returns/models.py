import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.sales.models import PaymentMethod, SaleItem


_return_write_allowed = ContextVar("return_write_allowed", default=False)


@contextmanager
def allow_return_write():
    token = _return_write_allowed.set(True)
    try:
        yield
    finally:
        _return_write_allowed.reset(token)


def new_return_reference():
    return f"RET-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def new_refund_reference():
    return f"RFD-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


class ReturnKind(models.TextChoices):
    RETURN = "RETURN", "Return"
    EXCHANGE = "EXCHANGE", "Exchange"


class ReturnStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ReturnCondition(models.TextChoices):
    NEW = "NEW", "New"
    GOOD = "GOOD", "Good"
    FAIR = "FAIR", "Fair"
    DAMAGED = "DAMAGED", "Damaged"
    DEFECTIVE = "DEFECTIVE", "Defective"
    INCOMPLETE = "INCOMPLETE", "Incomplete"


class ReturnDisposition(models.TextChoices):
    RESTOCK = "RESTOCK", "Resellable; return to available stock"
    QUARANTINE = "QUARANTINE", "Quarantine; not available for sale"
    REPAIR = "REPAIR", "Needs repair; not available for sale"
    DISCARD = "DISCARD", "Discard; not available for sale"


class ImmutableWorkflowQuerySet(models.QuerySet):
    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Return and refund records must be created through their workflow services.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Return and refund records must be changed through their workflow services.")

    def update(self, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Return and refund records must be changed through their workflow services.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Return and refund history cannot be deleted.")

    def _raw_delete(self, using):
        raise ValidationError("Return and refund history cannot be deleted through ORM shortcuts.")


class ReturnCase(models.Model):
    reference = models.CharField(max_length=32, unique=True, default=new_return_reference, editable=False)
    sale = models.ForeignKey("sales.Sale", on_delete=models.PROTECT, related_name="return_cases")
    kind = models.CharField(max_length=8, choices=ReturnKind.choices, default=ReturnKind.RETURN)
    status = models.CharField(max_length=20, choices=ReturnStatus.choices, default=ReturnStatus.PENDING_APPROVAL)
    reason = models.TextField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="return_cases_requested"
    )
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="return_cases_decided", null=True, blank=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    debt_credit_applied = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    refund_credit_available = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    objects = ImmutableWorkflowQuerySet.as_manager()

    class Meta:
        ordering = ("-requested_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=ReturnKind.values), name="returns_case_kind_valid"),
            models.CheckConstraint(condition=Q(debt_credit_applied__gte=0), name="returns_case_debt_credit_nonnegative"),
            models.CheckConstraint(condition=Q(refund_credit_available__gte=0), name="returns_case_refund_credit_nonnegative"),
            models.CheckConstraint(
                condition=(
                    Q(status=ReturnStatus.PENDING_APPROVAL, decided_by__isnull=True, decided_at__isnull=True, decision_reason="")
                    | Q(status=ReturnStatus.APPROVED, decided_by__isnull=False, decided_at__isnull=False, decision_reason="")
                    | Q(status=ReturnStatus.REJECTED, decided_by__isnull=False, decided_at__isnull=False, decision_reason__gt="")
                ),
                name="returns_case_decision_state_valid",
            ),
        ]

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Return cases can only be changed through the return workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Return history cannot be deleted.")

    @property
    def total_value(self):
        return sum((item.line_value for item in self.items.all()), Decimal("0.00"))


class ReturnItem(models.Model):
    return_case = models.ForeignKey(ReturnCase, on_delete=models.PROTECT, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT, related_name="return_items")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, editable=False)
    condition = models.CharField(max_length=12, choices=ReturnCondition.choices, blank=True)
    disposition = models.CharField(max_length=12, choices=ReturnDisposition.choices, blank=True)

    objects = ImmutableWorkflowQuerySet.as_manager()

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="returns_item_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="returns_item_price_nonnegative"),
        ]

    def __str__(self):
        return f"{self.return_case.reference}: {self.sale_item.product.sku} x {self.quantity}"

    @property
    def line_value(self):
        return self.unit_price * self.quantity

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Return items can only be changed through the return workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Return items cannot be deleted.")


class ReturnItemUnit(models.Model):
    return_item = models.ForeignKey(ReturnItem, on_delete=models.PROTECT, related_name="returned_units")
    sale_item_unit = models.ForeignKey("sales.SaleItemUnit", on_delete=models.PROTECT, related_name="return_links")

    objects = ImmutableWorkflowQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("return_item", "sale_item_unit"), name="returns_item_unit_pair_uniq"),
        ]

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Returned units must be linked through the return workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Returned-unit history cannot be deleted.")


class RefundRequestStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    COMPLETED = "COMPLETED", "Fully disbursed"


class RefundRequest(models.Model):
    reference = models.CharField(max_length=32, unique=True, default=new_refund_reference, editable=False)
    return_case = models.ForeignKey(ReturnCase, on_delete=models.PROTECT, related_name="refund_requests")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=12, choices=PaymentMethod.choices)
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=RefundRequestStatus.choices, default=RefundRequestStatus.PENDING_APPROVAL)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refund_requests_created"
    )
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refund_requests_decided", null=True, blank=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    objects = ImmutableWorkflowQuerySet.as_manager()

    class Meta:
        ordering = ("-requested_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="returns_refund_request_amount_positive"),
            models.CheckConstraint(condition=Q(method__in=PaymentMethod.values), name="returns_refund_method_valid"),
            models.CheckConstraint(
                condition=(
                    Q(status=RefundRequestStatus.PENDING_APPROVAL, decided_by__isnull=True, decided_at__isnull=True, rejection_reason="")
                    | Q(status__in=[RefundRequestStatus.APPROVED, RefundRequestStatus.COMPLETED], decided_by__isnull=False, decided_at__isnull=False, rejection_reason="")
                    | Q(status=RefundRequestStatus.REJECTED, decided_by__isnull=False, decided_at__isnull=False, rejection_reason__gt="")
                ),
                name="returns_refund_request_decision_state_valid",
            ),
        ]

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Refund requests can only be changed through the refund workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Refund history cannot be deleted.")

    @property
    def confirmed_disbursed_amount(self):
        return self.disbursements.filter(status=RefundDisbursementStatus.CONFIRMED).aggregate(
            total=models.Sum("amount")
        )["total"] or Decimal("0.00")

    @property
    def pending_disbursement_amount(self):
        return self.disbursements.filter(status=RefundDisbursementStatus.PENDING_CONFIRMATION).aggregate(
            total=models.Sum("amount")
        )["total"] or Decimal("0.00")


class RefundDisbursementStatus(models.TextChoices):
    CONFIRMED = "CONFIRMED", "Disbursed"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION", "Pending MTN confirmation"
    REJECTED = "REJECTED", "Rejected"


class RefundDisbursement(models.Model):
    refund_request = models.ForeignKey(RefundRequest, on_delete=models.PROTECT, related_name="disbursements")
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=12, choices=PaymentMethod.choices)
    status = models.CharField(max_length=24, choices=RefundDisbursementStatus.choices)
    momo_reference = models.CharField(max_length=120, blank=True)
    confirmation_reference = models.CharField(max_length=120, blank=True)
    rejection_reason = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refund_disbursements_recorded"
    )
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refund_disbursements_decided", null=True, blank=True
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    objects = ImmutableWorkflowQuerySet.as_manager()

    class Meta:
        ordering = ("recorded_at", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="returns_disbursement_amount_positive"),
            models.CheckConstraint(condition=Q(method__in=PaymentMethod.values), name="returns_disbursement_method_valid"),
            models.UniqueConstraint(fields=("momo_reference",), condition=~Q(momo_reference=""), name="returns_refund_momo_ref_uniq"),
            models.UniqueConstraint(fields=("confirmation_reference",), condition=~Q(confirmation_reference=""), name="returns_refund_confirmation_ref_uniq"),
            models.CheckConstraint(
                condition=(
                    Q(method=PaymentMethod.CASH, status=RefundDisbursementStatus.CONFIRMED, momo_reference="", confirmation_reference="", rejection_reason="", decided_by__isnull=False, decided_at__isnull=False)
                    | Q(method=PaymentMethod.MTN_MOMO, status=RefundDisbursementStatus.PENDING_CONFIRMATION, momo_reference__gt="", confirmation_reference="", rejection_reason="", decided_by__isnull=True, decided_at__isnull=True)
                    | Q(method=PaymentMethod.MTN_MOMO, status=RefundDisbursementStatus.CONFIRMED, momo_reference__gt="", confirmation_reference__gt="", rejection_reason="", decided_by__isnull=False, decided_at__isnull=False)
                    | Q(method=PaymentMethod.MTN_MOMO, status=RefundDisbursementStatus.REJECTED, momo_reference__gt="", confirmation_reference="", rejection_reason__gt="", decided_by__isnull=False, decided_at__isnull=False)
                ),
                name="returns_disbursement_decision_state_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Refund disbursements can only be changed through the refund workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Refund disbursement history cannot be deleted.")


class ExchangeLink(models.Model):
    return_case = models.OneToOneField(ReturnCase, on_delete=models.PROTECT, related_name="exchange_link")
    replacement_sale = models.OneToOneField("sales.Sale", on_delete=models.PROTECT, related_name="exchange_origin")
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exchanges_linked")
    linked_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = ImmutableWorkflowQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not _return_write_allowed.get():
            raise ValidationError("Exchange links must be created through the exchange workflow.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Exchange history cannot be deleted.")
