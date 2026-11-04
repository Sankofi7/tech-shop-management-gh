from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from apps.accounts.models import Role

from .models import (
    Brand,
    Category,
    InventoryMovement,
    InventoryUnit,
    MovementType,
    Product,
    UnitCondition,
    UnitStatus,
)
from .admin import InventoryMovementAdmin, ProductAdmin
from .services import record_stock_movement


User = get_user_model()


class InventoryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="inventory-owner", email="owner@example.invalid", password="unique-test-password"
        )
        self.employee = User.objects.create_user(
            username="inventory-employee", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.category = Category.objects.create(name="Laptops")
        self.brand = Brand.objects.create(name="Dell")
        self.product = Product.objects.create(
            name="Latitude Laptop",
            category=self.category,
            brand=self.brand,
            model="7450",
            description="Business laptop",
            sku="DELL-LAT-7450",
            purchase_price=Decimal("4800.00"),
            selling_price=Decimal("5600.00"),
            minimum_stock_level=2,
            track_individual_units=True,
        )

    def test_product_registration_and_low_stock_flag(self):
        self.assertEqual(self.product.purchase_price, Decimal("4800.00"))
        self.assertEqual(self.product.selling_price, Decimal("5600.00"))
        self.assertEqual(self.product.stock_quantity, 0)
        self.assertTrue(self.product.is_low_stock)

    def test_stock_received_creates_movement_and_serialized_units(self):
        movement = record_stock_movement(
            product=self.product,
            quantity=3,
            movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner,
            reason="Supplier delivery",
            reference="DELIVERY-001",
            unit_identifiers=("SN-100", "SN-101", "SN-102"),
        )
        self.product.refresh_from_db()
        self.assertEqual((movement.previous_stock, movement.resulting_stock), (0, 3))
        self.assertEqual(self.product.stock_quantity, 3)
        self.assertEqual(movement.units.count(), 3)
        self.assertFalse(self.product.is_low_stock)

    def test_duplicate_serial_is_rejected_within_product(self):
        record_stock_movement(
            product=self.product,
            quantity=1,
            movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner,
            reason="First delivery",
            unit_identifiers=("SN-DUP",),
        )
        with self.assertRaises(ValidationError):
            record_stock_movement(
                product=self.product,
                quantity=1,
                movement_type=MovementType.STOCK_RECEIVED,
                user=self.owner,
                reason="Second delivery",
                unit_identifiers=("SN-DUP",),
            )
        self.assertEqual(InventoryUnit.objects.filter(product=self.product, identifier="SN-DUP").count(), 1)

    def test_sale_marks_serialized_unit_sold(self):
        record_stock_movement(
            product=self.product,
            quantity=1,
            movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner,
            reason="Supplier delivery",
            unit_identifiers=("SN-SOLD",),
        )
        sale_movement = record_stock_movement(
            product=self.product,
            quantity=-1,
            movement_type=MovementType.STOCK_SOLD,
            user=self.owner,
            reason="Sales transaction reference SALE-1",
            unit_identifiers=("SN-SOLD",),
        )
        unit = InventoryUnit.objects.get(product=self.product, identifier="SN-SOLD")
        self.assertEqual(unit.status, UnitStatus.SOLD)
        self.assertEqual(sale_movement.resulting_stock, 0)

    def test_stock_cannot_go_negative_and_failure_is_atomic(self):
        with self.assertRaises(ValidationError):
            record_stock_movement(
                product=self.product,
                quantity=-1,
                movement_type=MovementType.STOCK_SOLD,
                user=self.owner,
                reason="Attempt without stock",
                unit_identifiers=("UNKNOWN",),
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)
        self.assertEqual(InventoryMovement.objects.count(), 0)

    def test_employee_cannot_make_inventory_movement(self):
        with self.assertRaises(PermissionDenied):
            record_stock_movement(
                product=self.product,
                quantity=1,
                movement_type=MovementType.STOCK_RECEIVED,
                user=self.employee,
                reason="Not permitted",
                unit_identifiers=("SN-NO",),
            )

    def test_untracked_accessory_does_not_require_serial_numbers(self):
        accessory = Product.objects.create(
            name="Wireless Mouse", category=self.category, sku="MOUSE-01",
            purchase_price=Decimal("100.00"), selling_price=Decimal("150.00"),
        )
        record_stock_movement(
            product=accessory,
            quantity=5,
            movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner,
            reason="Bulk accessory delivery",
        )
        accessory.refresh_from_db()
        self.assertEqual(accessory.stock_quantity, 5)

    def test_movement_history_is_append_only(self):
        movement = record_stock_movement(
            product=self.product,
            quantity=1,
            movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner,
            reason="Supplier delivery",
            unit_identifiers=("SN-IMMUTABLE",),
        )
        movement.reason = "Replaced reason"
        with self.assertRaises(ValidationError):
            movement.save()
        with self.assertRaises(ValidationError):
            movement.delete()
        with self.assertRaises(ValidationError):
            InventoryMovement.objects.filter(pk=movement.pk).update(reason="Replaced reason")

    def test_users_cannot_bypass_the_movement_service_for_stock_writes(self):
        with self.assertRaises(ValidationError):
            InventoryMovement.objects.create(
                product=self.product,
                quantity=1,
                movement_type=MovementType.STOCK_RECEIVED,
                responsible_user=self.owner,
                reason="Direct creation attempt",
                previous_stock=0,
                resulting_stock=1,
            )
        with self.assertRaises(ValidationError):
            Product.objects.filter(pk=self.product.pk).update(stock_quantity=1)

    def test_catalog_and_stock_admin_reject_employee_but_allow_secretary(self):
        from django.test import RequestFactory

        secretary = User.objects.create_user(
            username="inventory-secretary", password="unique-test-password", role=Role.SECRETARY
        )
        product_admin = ProductAdmin(Product, AdminSite())
        movement_admin = InventoryMovementAdmin(InventoryMovement, AdminSite())
        request = RequestFactory().get("/admin/")
        request.user = self.employee
        self.assertFalse(product_admin.has_change_permission(request))
        self.assertFalse(movement_admin.has_add_permission(request))
        request.user = secretary
        self.assertTrue(product_admin.has_change_permission(request))
        self.assertTrue(movement_admin.has_add_permission(request))

    def test_owner_can_register_product_and_receive_stock_in_admin(self):
        self.client.force_login(self.owner)
        response = self.client.post("/admin/products/product/add/", {
            "name": "PlayStation Console",
            "category": self.category.pk,
            "brand": self.brand.pk,
            "model": "PS5",
            "description": "Console",
            "sku": "SONY-PS5-ADMIN",
            "purchase_price": "4200.00",
            "selling_price": "5000.00",
            "minimum_stock_level": "1",
            "track_individual_units": "on",
            "is_active": "on",
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(sku="SONY-PS5-ADMIN")
        response = self.client.post("/admin/products/inventorymovement/add/", {
            "product": product.pk,
            "quantity": "1",
            "movement_type": MovementType.STOCK_RECEIVED,
            "reason": "Initial supplier delivery",
            "reference": "DELIVERY-ADMIN-1",
            "unit_identifiers": "PS5-SERIAL-001",
            "unit_condition": UnitCondition.NEW,
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 1)
        self.assertTrue(InventoryMovement.objects.filter(product=product, responsible_user=self.owner).exists())


class InventoryFrontendTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="inventory-ui-owner", email="owner-ui@example.invalid", password="unique-test-password"
        )
        self.secretary = User.objects.create_user(
            username="inventory-ui-secretary", password="unique-test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="inventory-ui-employee", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.category = Category.objects.create(name="Phones UI")
        self.brand = Brand.objects.create(name="Samsung UI")
        self.product = Product.objects.create(
            name="Galaxy UI", category=self.category, brand=self.brand, sku="GALAXY-UI-1",
            purchase_price=Decimal("1000.00"), selling_price=Decimal("1200.00"), minimum_stock_level=2,
        )
        self.other_product = Product.objects.create(
            name="Galaxy Other", category=self.category, brand=self.brand, sku="GALAXY-UI-2",
            purchase_price=Decimal("1500.00"), selling_price=Decimal("1800.00"), minimum_stock_level=1,
        )

    def test_product_list_search_filters_prices_and_employee_permissions(self):
        self.client.force_login(self.employee)
        response = self.client.get("/products/", {"q": "UI-1", "category": self.category.pk, "brand": self.brand.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertNotContains(response, self.other_product.name)
        self.assertNotContains(response, "Purchase price")
        self.assertNotContains(response, "GH₵1,000.00")
        self.assertContains(response, "Out of stock")
        self.assertEqual(self.client.get(f"/products/{self.product.pk}/edit/").status_code, 403)
        self.assertEqual(self.client.get("/products/new/").status_code, 403)
        self.assertEqual(self.client.get("/products/inventory/receive/").status_code, 403)
        self.assertEqual(self.client.get("/products/inventory/adjust/").status_code, 403)
        self.assertEqual(self.client.get("/products/inventory/history/").status_code, 403)

    def test_product_create_and_edit_do_not_change_stock_and_are_audited(self):
        from apps.audit.models import AuditEvent

        self.client.force_login(self.secretary)
        response = self.client.post("/products/new/", {
            "name": "Console UI", "category": self.category.pk, "brand": self.brand.pk,
            "model": "Console X", "description": "Test product", "sku": "CONSOLE-UI-1",
            "purchase_price": "3000.00", "selling_price": "3500.00", "minimum_stock_level": "3",
            "track_individual_units": "on", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        created = Product.objects.get(sku="CONSOLE-UI-1")
        self.assertEqual(created.stock_quantity, 0)
        self.assertTrue(AuditEvent.objects.filter(action="PRODUCT_CREATED", actor=self.secretary, object_reference=str(created.pk)).exists())
        response = self.client.post(f"/products/{created.pk}/edit/", {
            "name": "Console UI Updated", "category": self.category.pk, "brand": self.brand.pk,
            "model": "Console X", "description": "Test product", "sku": "CONSOLE-UI-1",
            "purchase_price": "3000.00", "selling_price": "3600.00", "minimum_stock_level": "3",
            "track_individual_units": "on", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        created.refresh_from_db()
        self.assertEqual(created.stock_quantity, 0)
        self.assertEqual(created.selling_price, Decimal("3600.00"))

    def test_overview_uses_real_active_stock_and_separates_low_and_out(self):
        inactive = Product.objects.create(
            name="Inactive UI", category=self.category, sku="INACTIVE-UI",
            purchase_price=Decimal("1.00"), selling_price=Decimal("2.00"), is_active=False,
        )
        record_stock_movement(
            product=self.product, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test stock", reference="UI-RECEIPT",
        )
        self.client.force_login(self.owner)
        response = self.client.get("/products/inventory/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_products_count"], 2)
        self.assertEqual(response.context["available_units"], 1)
        self.assertEqual(response.context["low_stock_count"], 2)
        self.assertEqual(response.context["out_of_stock_count"], 1)
        self.assertNotContains(response, inactive.name)

    def test_stock_receiving_validates_tracked_identifiers_and_uses_inventory_service(self):
        tracked = Product.objects.create(
            name="Tracked UI", category=self.category, sku="TRACKED-UI",
            purchase_price=Decimal("10.00"), selling_price=Decimal("20.00"), track_individual_units=True,
        )
        self.client.force_login(self.secretary)
        bad = self.client.post("/products/inventory/receive/", {
            "product": tracked.pk, "quantity": 2, "reason": "Delivery", "reference": "DEL-UI",
            "unit_identifiers": "ONLY-ONE", "unit_condition": "NEW",
        })
        self.assertEqual(bad.status_code, 200)
        tracked.refresh_from_db()
        self.assertEqual(tracked.stock_quantity, 0)
        good = self.client.post("/products/inventory/receive/", {
            "product": tracked.pk, "quantity": 2, "reason": "Supplier delivery", "reference": "DEL-UI",
            "unit_identifiers": "SERIAL-UI-1\nSERIAL-UI-2", "unit_condition": "NEW",
        })
        self.assertEqual(good.status_code, 302)
        tracked.refresh_from_db()
        self.assertEqual(tracked.stock_quantity, 2)
        self.assertEqual(tracked.units.filter(status="AVAILABLE").count(), 2)
        self.assertTrue(InventoryMovement.objects.filter(product=tracked, movement_type=MovementType.STOCK_RECEIVED).exists())

    def test_stock_adjustment_negative_stock_fails_and_history_is_read_only_filtered(self):
        self.client.force_login(self.owner)
        response = self.client.post("/products/inventory/adjust/", {
            "product": self.product.pk, "quantity": -1, "reason": "Count correction", "reference": "COUNT-UI",
            "unit_identifiers": "", "unit_condition": "NEW",
        })
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)
        movement = record_stock_movement(
            product=self.product, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Receipt for history", reference="HISTORY-UI",
        )
        response = self.client.get("/products/inventory/history/", {"product": self.product.pk, "movement_type": MovementType.STOCK_RECEIVED})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HISTORY-UI")
        self.assertNotContains(response, "COUNT-UI")
        self.assertFalse(hasattr(movement, "edit"))
        self.assertEqual(self.client.get("/products/inventory/history/").status_code, 200)

    def test_inventory_frontend_redirects_anonymous_users_to_login(self):
        response = self.client.get("/products/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
