from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


_movement_creation_allowed = ContextVar("movement_creation_allowed", default=False)


@contextmanager
def allow_movement_creation():
    token = _movement_creation_allowed.set(True)
    try:
        yield
    finally:
        _movement_creation_allowed.reset(token)


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Brand(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class ProductQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if "stock_quantity" in kwargs:
            raise ValidationError("Change stock through an inventory movement.")
        return super().update(**kwargs)

    def _apply_movement_delta(self, quantity):
        """Internal stock update used only inside the movement service."""
        return super().update(stock_quantity=F("stock_quantity") + quantity)


class Product(models.Model):
    objects = ProductQuerySet.as_manager()

    name = models.CharField(max_length=200)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    brand = models.ForeignKey(
        Brand, on_delete=models.PROTECT, related_name="products", blank=True, null=True
    )
    model = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=64, unique=True, verbose_name="SKU / product code")
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Purchase price (GHS)")
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Selling price (GHS)")
    stock_quantity = models.PositiveIntegerField(default=0, editable=False)
    minimum_stock_level = models.PositiveIntegerField(default=0, verbose_name="Minimum stock alert level")
    track_individual_units = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.CheckConstraint(condition=Q(purchase_price__gte=0), name="products_purchase_price_nonnegative"),
            models.CheckConstraint(condition=Q(selling_price__gte=0), name="products_selling_price_nonnegative"),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.minimum_stock_level

    def save(self, *args, **kwargs):
        # Stock is changed only by the transactional movement service.
        if self.pk and not self._state.adding:
            old_value = type(self).objects.filter(pk=self.pk).values_list("stock_quantity", flat=True).first()
            if old_value is not None and self.stock_quantity != old_value:
                raise ValidationError({"stock_quantity": "Change stock through an inventory movement."})
        super().save(*args, **kwargs)


class UnitCondition(models.TextChoices):
    NEW = "NEW", "New"
    GOOD = "GOOD", "Good"
    FAIR = "FAIR", "Fair"
    DAMAGED = "DAMAGED", "Damaged"


class UnitStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    SOLD = "SOLD", "Sold"
    SUPPLIER_RETURNED = "SUPPLIER_RETURNED", "Returned to supplier"
    ADJUSTED_OUT = "ADJUSTED_OUT", "Removed by adjustment"


class InventoryUnit(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="units")
    identifier = models.CharField(max_length=100, verbose_name="Serial number / IMEI")
    condition = models.CharField(max_length=12, choices=UnitCondition.choices, default=UnitCondition.NEW)
    status = models.CharField(max_length=20, choices=UnitStatus.choices, default=UnitStatus.AVAILABLE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("product__name", "identifier")
        constraints = [
            models.UniqueConstraint(fields=("product", "identifier"), name="products_unit_identifier_per_product_uniq"),
        ]

    def __str__(self):
        return f"{self.product.sku}: {self.identifier} ({self.get_status_display()})"


class MovementType(models.TextChoices):
    STOCK_RECEIVED = "STOCK_RECEIVED", "Stock received"
    STOCK_SOLD = "STOCK_SOLD", "Stock sold"
    CUSTOMER_RETURN = "CUSTOMER_RETURN", "Customer return"
    SUPPLIER_RETURN = "SUPPLIER_RETURN", "Supplier return"
    APPROVED_ADJUSTMENT = "APPROVED_ADJUSTMENT", "Approved stock adjustment"


class ImmutableMovementQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Inventory movement history cannot be changed.")

    def delete(self):
        raise ValidationError("Inventory movement history cannot be deleted.")


class InventoryMovement(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    quantity = models.IntegerField(help_text="Positive adds stock; negative removes stock.")
    movement_type = models.CharField(max_length=24, choices=MovementType.choices)
    occurred_at = models.DateTimeField(auto_now_add=True)
    responsible_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inventory_movements")
    reason = models.TextField()
    reference = models.CharField(max_length=120, blank=True)
    previous_stock = models.PositiveIntegerField()
    resulting_stock = models.PositiveIntegerField()
    units = models.ManyToManyField(InventoryUnit, through="InventoryMovementUnit", related_name="movements", blank=True)

    objects = ImmutableMovementQuerySet.as_manager()

    class Meta:
        ordering = ("-occurred_at", "-pk")
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name="products_movement_nonzero_quantity"),
            models.CheckConstraint(
                condition=(
                    Q(movement_type__in=[MovementType.STOCK_RECEIVED, MovementType.CUSTOMER_RETURN], quantity__gt=0)
                    | Q(movement_type__in=[MovementType.STOCK_SOLD, MovementType.SUPPLIER_RETURN], quantity__lt=0)
                    | Q(movement_type=MovementType.APPROVED_ADJUSTMENT)
                ),
                name="products_movement_quantity_direction_valid",
            ),
            models.CheckConstraint(
                condition=Q(resulting_stock=F("previous_stock") + F("quantity")),
                name="products_movement_stock_math_valid",
            ),
        ]

    def __str__(self):
        return f"{self.product.sku}: {self.get_movement_type_display()} ({self.quantity:+d})"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Inventory movements are append-only.")
        if not _movement_creation_allowed.get():
            raise ValidationError("Create inventory movements through the authorized stock service.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Inventory movements cannot be deleted.")


class InventoryMovementUnit(models.Model):
    movement = models.ForeignKey(InventoryMovement, on_delete=models.PROTECT)
    unit = models.ForeignKey(InventoryUnit, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("movement", "unit"), name="products_movement_unit_pair_uniq"),
        ]
