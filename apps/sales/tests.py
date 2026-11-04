import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.accounts.models import Role
from apps.audit.models import AuditEvent
from apps.customers.services import create_customer
from apps.products.models import (
    Brand,
    Category,
    InventoryMovement,
    InventoryUnit,
    MovementType,
    Product,
    UnitStatus,
)
from apps.products.services import record_stock_movement
from apps.shop_settings.models import ShopProfile

from .models import Payment, PaymentMethod, PaymentStatus, Sale, SaleItem, SaleStatus, SaleType
from .services import (
    approve_sale,
    confirm_momo_payment,
    create_sale,
    record_payment,
    reject_momo_payment,
    reject_sale,
)


User = get_user_model()


class SalesWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="sales-owner", email="owner@example.invalid", password="unique-test-password"
        )
        self.secretary = User.objects.create_user(
            username="sales-secretary", password="unique-test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="sales-employee", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Sales Test Customer", phone_number="0240000000"
        )
        self.category = Category.objects.create(name="Consoles")
        self.brand = Brand.objects.create(name="Sony")
        self.product = Product.objects.create(
            name="PlayStation 5", category=self.category, brand=self.brand, model="Slim",
            sku="SONY-PS5-SALES", purchase_price=Decimal("4000.00"),
            selling_price=Decimal("5000.00"), minimum_stock_level=1,
        )
        record_stock_movement(
            product=self.product, quantity=10, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test fixture stock",
        )

    def test_cash_sale_records_items_payment_and_reduces_stock(self):
        sale = create_sale(
            user=self.employee,
            sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 2}],
            initial_payment={"amount": Decimal("10000.00"), "method": PaymentMethod.CASH},
        )
        self.product.refresh_from_db()
        self.assertEqual(sale.status, SaleStatus.APPROVED)
        self.assertEqual(sale.total, Decimal("10000.00"))
        self.assertTrue(sale.reference.startswith("TS-"))
        self.assertEqual(sale.items.get().line_total, Decimal("10000.00"))
        self.assertEqual(sale.amount_paid, Decimal("10000.00"))
        self.assertEqual(sale.outstanding_balance, Decimal("0.00"))
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertTrue(InventoryMovement.objects.filter(reference=sale.reference, movement_type=MovementType.STOCK_SOLD).exists())

    def test_employee_credit_sale_waits_for_approval_before_stock_changes(self):
        sale = create_sale(
            user=self.employee,
            sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        self.product.refresh_from_db()
        self.assertEqual(sale.status, SaleStatus.PENDING_APPROVAL)
        self.assertEqual(self.product.stock_quantity, 10)
        approved = approve_sale(sale=sale, user=self.secretary)
        self.product.refresh_from_db()
        self.assertEqual(approved.status, SaleStatus.APPROVED)
        self.assertEqual(approved.decided_by, self.secretary)
        self.assertEqual(self.product.stock_quantity, 9)

    def test_approval_rechecks_stock_when_multiple_credit_sales_are_pending(self):
        first = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[{"product": self.product, "quantity": 6}],
        )
        second = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[{"product": self.product, "quantity": 6}],
        )
        approve_sale(sale=first, user=self.owner)
        with self.assertRaises(ValidationError):
            approve_sale(sale=second, user=self.secretary)
        self.product.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 4)
        self.assertEqual(second.status, SaleStatus.PENDING_APPROVAL)

    def test_rejected_credit_sale_is_retained_with_reason_and_audit_event(self):
        sale = create_sale(user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        rejected = reject_sale(sale=sale, user=self.owner, reason="Customer credit not approved")
        self.product.refresh_from_db()
        self.assertEqual(rejected.status, SaleStatus.REJECTED)
        self.assertEqual(rejected.rejection_reason, "Customer credit not approved")
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertTrue(AuditEvent.objects.filter(action="SALE_REJECTED", object_reference=sale.reference).exists())
        self.assertEqual(Sale.objects.filter(pk=sale.pk).count(), 1)

    def test_rejection_reason_must_be_meaningful(self):
        sale = create_sale(user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        with self.assertRaises(ValidationError):
            reject_sale(sale=sale, user=self.owner, reason="no")
        self.assertEqual(sale.status, SaleStatus.PENDING_APPROVAL)

    def test_only_owner_or_secretary_can_decide_credit_and_reason_is_required(self):
        sale = create_sale(user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        with self.assertRaises(PermissionDenied):
            approve_sale(sale=sale, user=self.employee)
        with self.assertRaises(ValidationError):
            reject_sale(sale=sale, user=self.owner, reason=" ")
        with self.assertRaises(PermissionDenied):
            approve_sale(sale=sale, user=self.employee)

    def test_credit_partial_payments_and_overpayment_guard(self):
        sale = create_sale(
            user=self.secretary,
            sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        record_payment(sale=sale, user=self.secretary, amount=Decimal("2000.00"), method=PaymentMethod.CASH)
        self.assertEqual(sale.amount_paid, Decimal("2000.00"))
        self.assertEqual(sale.outstanding_balance, Decimal("3000.00"))
        with self.assertRaises(ValidationError):
            record_payment(sale=sale, user=self.secretary, amount=Decimal("3000.01"), method=PaymentMethod.CASH)
        self.assertEqual(sale.payments.count(), 1)

    def test_momo_payment_is_not_counted_until_confirmation_record(self):
        sale = create_sale(
            user=self.employee,
            sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={
                "amount": Decimal("5000.00"), "method": PaymentMethod.MTN_MOMO,
                "momo_reference": "MOMO-TXN-001",
            },
        )
        payment = sale.payments.get()
        self.assertEqual(payment.status, PaymentStatus.PENDING_CONFIRMATION)
        self.assertEqual(sale.amount_paid, Decimal("0.00"))
        with self.assertRaises(ValidationError):
            confirm_momo_payment(payment=payment, user=self.owner, confirmation_reference=" ")
        confirm_momo_payment(payment=payment, user=self.owner, confirmation_reference="STATEMENT-001")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.confirmation_reference, "STATEMENT-001")
        self.assertEqual(sale.amount_paid, Decimal("5000.00"))
        with self.assertRaises(ValidationError):
            confirm_momo_payment(payment=payment, user=self.secretary, confirmation_reference="STATEMENT-002")
        self.assertEqual(sale.amount_paid, Decimal("5000.00"))

    def test_duplicate_momo_references_are_rejected(self):
        sale = create_sale(user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        record_payment(
            sale=sale, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="DUPLICATE-MOMO-REF",
        )
        other_sale = create_sale(user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        with self.assertRaises(ValidationError):
            record_payment(
                sale=other_sale, user=self.secretary, amount=Decimal("100.00"),
                method=PaymentMethod.MTN_MOMO, momo_reference="DUPLICATE-MOMO-REF",
            )

    def test_rejected_momo_payment_stays_in_history_but_not_paid(self):
        sale = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}]
        )
        payment = record_payment(
            sale=sale, user=self.secretary, amount=Decimal("1000.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="MOMO-TXN-REJECT",
        )
        reject_momo_payment(payment=payment, user=self.secretary, reason="Reference not found")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.REJECTED)
        self.assertEqual(sale.amount_paid, Decimal("0.00"))
        self.assertEqual(sale.payments.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="PAYMENT_MOMO_REJECTED").exists())

    def test_serial_tracked_product_units_are_linked_and_marked_sold(self):
        tracked = Product.objects.create(
            name="Tracked laptop", category=self.category, sku="LAPTOP-SERIAL-SALES",
            purchase_price=Decimal("4000.00"), selling_price=Decimal("5000.00"),
            track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test fixture", unit_identifiers=("LAPTOP-IMEI-1",),
        )
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": tracked, "quantity": 1, "unit_identifiers": ["LAPTOP-IMEI-1"]}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )
        unit = InventoryUnit.objects.get(product=tracked, identifier="LAPTOP-IMEI-1")
        item = sale.items.get()
        self.assertEqual(unit.status, UnitStatus.SOLD)
        self.assertEqual(item.units.get(), unit)

    def test_historical_sale_prices_do_not_change_with_current_product_price(self):
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )
        Product.objects.filter(pk=self.product.pk).update(selling_price=Decimal("5500.00"))
        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal("5000.00"))
        self.assertEqual(item.line_total, Decimal("5000.00"))
        self.assertEqual(sale.total, Decimal("5000.00"))

    def test_failed_multi_item_approval_rolls_back_prior_stock_movement(self):
        tracked = Product.objects.create(
            name="Tracked laptop", category=self.category, sku="LAPTOP-ROLLBACK",
            purchase_price=Decimal("4000.00"), selling_price=Decimal("5000.00"), track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test fixture", unit_identifiers=("LAPTOP-ROLLBACK-1",),
        )
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[
                {"product": self.product, "quantity": 1},
                {"product": tracked, "quantity": 1, "unit_identifiers": ["LAPTOP-ROLLBACK-1"]},
            ],
        )
        # Simulate a unit becoming unavailable after the pending sale was entered,
        # while aggregate stock remains unchanged. Approval must roll back its first item.
        InventoryUnit.objects.filter(product=tracked, identifier="LAPTOP-ROLLBACK-1").update(status=UnitStatus.SOLD)
        previous_movements = InventoryMovement.objects.filter(movement_type=MovementType.STOCK_SOLD).count()
        with self.assertRaises(ValidationError):
            approve_sale(sale=sale, user=self.owner)
        self.product.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(sale.status, SaleStatus.PENDING_APPROVAL)
        self.assertEqual(InventoryMovement.objects.filter(movement_type=MovementType.STOCK_SOLD).count(), previous_movements)

    def test_employee_cannot_record_payment_for_another_employees_sale(self):
        sale = create_sale(
            user=self.employee,
            sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )
        other = User.objects.create_user(username="other-employee", password="unique-test-password", role=Role.EMPLOYEE)
        with self.assertRaises(PermissionDenied):
            record_payment(sale=sale, user=other, amount=Decimal("1.00"), method=PaymentMethod.CASH)

    def test_pending_credit_sales_are_not_in_approved_queryset(self):
        sale = create_sale(user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        self.assertFalse(Sale.objects.approved().filter(pk=sale.pk).exists())
        approve_sale(sale=sale, user=self.owner)
        self.assertTrue(Sale.objects.approved().filter(pk=sale.pk).exists())

    def test_sales_history_admin_and_receipt_permissions(self):
        sale = create_sale(
            user=self.employee,
            sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )
        other = User.objects.create_user(username="another-employee", password="unique-test-password", role=Role.EMPLOYEE)
        other.first_name = "Avery"
        other.last_name = "Staffmember"
        other.save(update_fields=("first_name", "last_name"))
        self.client.force_login(other)
        response = self.client.get(f"/sales/{sale.pk}/receipt/")
        self.assertEqual(response.status_code, 404)
        other_employee_sale = create_sale(
            user=other, sale_type=SaleType.CREDIT,
            customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        self.client.force_login(self.employee)
        response = self.client.get(f"/sales/{sale.pk}/receipt/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, sale.reference)
        pending = create_sale(user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}])
        response = self.client.get(f"/sales/{pending.pk}/receipt/")
        self.assertContains(response, "PENDING APPROVAL")
        self.assertNotContains(response, "APPROVED</span>")
        response = self.client.get(
            f"/sales/?search={self.product.sku}&product={self.product.pk}&payment_status=CONFIRMED"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, sale.reference)
        self.assertNotContains(response, other_employee_sale.reference)

    def _cash_sale(self, *, user=None, method=PaymentMethod.CASH, reference=None, customer=None):
        user = user or self.employee
        suffix = reference or uuid.uuid4().hex[:10]
        return create_sale(
            user=user, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            customer=customer,
            initial_payment={
                "amount": Decimal("5000.00"), "method": method,
                "momo_reference": f"MOMO-{suffix}" if method == PaymentMethod.MTN_MOMO else "",
            },
        )

    def test_sales_history_searches_customer_and_employee_display_name(self):
        self._cash_sale(customer=self.customer)
        self.employee.first_name = "Kofi"
        self.employee.last_name = "Mensah"
        self.employee.save(update_fields=("first_name", "last_name"))
        self.client.force_login(self.owner)
        for query in ("Sales Test Customer", "Kofi Mensah"):
            response = self.client.get("/sales/", {"search": query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context["sales"]), 1)

    def test_sales_history_sale_status_filters_are_distinct(self):
        pending = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        approved = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        rejected = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        reject_sale(sale=rejected, user=self.owner, reason="Credit terms not accepted")
        self.client.force_login(self.owner)
        for status, expected in (
            (SaleStatus.PENDING_APPROVAL, pending),
            (SaleStatus.APPROVED, approved),
            (SaleStatus.REJECTED, rejected),
        ):
            response = self.client.get("/sales/", {"sale_status": status})
            self.assertEqual([sale.pk for sale in response.context["sales"]], [expected.pk])

    def test_sales_history_payment_method_status_and_combined_filters(self):
        cash = self._cash_sale()
        momo = self._cash_sale(method=PaymentMethod.MTN_MOMO)
        credit = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        repayment = record_payment(
            sale=credit, user=self.secretary, amount=Decimal("500.00"), method=PaymentMethod.CASH,
        )
        rejected_credit = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        rejected_payment = record_payment(
            sale=rejected_credit, user=self.secretary, amount=Decimal("250.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="MOMO-REJECTED-TEST",
        )
        reject_momo_payment(payment=rejected_payment, user=self.owner, reason="Reference not found")
        self.client.force_login(self.owner)

        cases = (
            ({"payment_method": PaymentMethod.CASH}, {cash.pk, credit.pk}),
            ({"payment_method": PaymentMethod.MTN_MOMO}, {momo.pk, rejected_credit.pk}),
            ({"payment_status": PaymentStatus.CONFIRMED}, {cash.pk, credit.pk}),
            ({"payment_status": PaymentStatus.PENDING_CONFIRMATION}, {momo.pk}),
            ({"payment_status": PaymentStatus.REJECTED}, {rejected_credit.pk}),
            ({"payment_method": PaymentMethod.CASH, "payment_status": PaymentStatus.CONFIRMED,
              "sale_status": SaleStatus.APPROVED}, {cash.pk, credit.pk}),
            ({"payment_method": PaymentMethod.MTN_MOMO, "payment_status": PaymentStatus.CONFIRMED}, set()),
        )
        for params, expected in cases:
            response = self.client.get("/sales/", params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual({sale.pk for sale in response.context["sales"]}, expected)
        self.assertEqual(repayment.status, PaymentStatus.CONFIRMED)

        pending_row = self.client.get("/sales/", {"search": momo.reference})
        self.assertContains(pending_row, "MTN PENDING CONFIRMATION")

    def test_sales_history_date_validation_preserves_other_filters(self):
        sale = self._cash_sale()
        self.client.force_login(self.owner)
        response = self.client.get("/sales/", {
            "date_from": "2026-02-31", "date_to": "2099-12-31",
            "sale_status": SaleStatus.APPROVED,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid start date.")
        self.assertContains(response, 'value="2026-02-31"')
        self.assertContains(response, 'value="2099-12-31"')
        self.assertEqual({item.pk for item in response.context["sales"]}, {sale.pk})

        valid_end_filter = self.client.get("/sales/", {
            "date_from": "2026-02-31", "date_to": "2000-01-01",
            "sale_status": SaleStatus.APPROVED,
        })
        self.assertContains(valid_end_filter, "Enter a valid start date.")
        self.assertEqual(list(valid_end_filter.context["sales"]), [])

        reversed_dates = self.client.get("/sales/", {
            "date_from": "2026-11-04", "date_to": "2026-11-03",
            "sale_status": SaleStatus.APPROVED,
        })
        self.assertContains(reversed_dates, "End date must be on or after the start date.")
        self.assertContains(reversed_dates, 'value="2026-11-04"')

    def test_receipt_uses_configured_shop_name_or_generic_fallback_and_ghs(self):
        sale = self._cash_sale()
        self.client.force_login(self.employee)
        fallback = self.client.get(f"/sales/{sale.pk}/receipt/")
        self.assertContains(fallback, "Tech Shop")
        self.assertContains(fallback, "GH₵5000.00")
        self.assertNotContains(fallback, "shop name placeholder")

        ShopProfile.objects.create(singleton_key=1, shop_name="Accra Device House")
        configured = self.client.get(f"/sales/{sale.pk}/receipt/")
        self.assertContains(configured, "Accra Device House")
        self.assertContains(configured, "Unit price (GH₵)")
        self.assertContains(configured, "Confirmed amount paid")

    def test_employee_search_filters_and_direct_receipts_stay_scoped(self):
        own_sale = self._cash_sale()
        other = User.objects.create_user(
            username="hidden-staff", password="unique-test-password", role=Role.EMPLOYEE,
            first_name="Hidden", last_name="Employee",
        )
        other_sale = self._cash_sale(user=other)
        self.client.force_login(self.employee)
        for params in (
            {"search": "Hidden Employee"},
            {"employee": other.pk},
            {"payment_status": PaymentStatus.CONFIRMED},
            {"sale_status": SaleStatus.APPROVED},
            {"payment_method": PaymentMethod.CASH},
        ):
            response = self.client.get("/sales/", params)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, other_sale.reference)
        self.assertContains(self.client.get("/sales/", {"search": own_sale.reference}), own_sale.reference)
        self.assertEqual(self.client.get(f"/sales/{other_sale.pk}/receipt/").status_code, 404)

    def test_employee_records_sale_through_view_but_cannot_approve_pending_sale(self):
        self.client.force_login(self.employee)
        response = self.client.post("/sales/new/", {
            "sale_type": SaleType.CASH,
            "payment_method": PaymentMethod.CASH,
            "payment_amount": "5000.00",
            "new_customer_name": "New Sale Customer",
            "new_customer_phone": "027 999 0011",
            "items-TOTAL_FORMS": "4",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-product": str(self.product.pk),
            "items-0-quantity": "1",
            "items-0-unit_identifiers": "",
            "items-1-product": "",
            "items-1-quantity": "",
            "items-1-unit_identifiers": "",
            "items-2-product": "",
            "items-2-quantity": "",
            "items-2-unit_identifiers": "",
            "items-3-product": "",
            "items-3-quantity": "",
            "items-3-unit_identifiers": "",
        })
        self.assertEqual(response.status_code, 302)
        created_sale = Sale.objects.filter(created_by=self.employee).latest("pk")
        self.assertEqual(created_sale.customer.full_name, "New Sale Customer")
        pending = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer, lines=[{"product": self.product, "quantity": 1}]
        )
        response = self.client.post(f"/sales/{pending.pk}/approve/")
        self.assertEqual(response.status_code, 403)

    def test_pos_get_uses_shared_layout_and_loads_catalog_through_authorized_endpoint(self):
        self.client.force_login(self.employee)
        page = self.client.get("/sales/new/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="pos-form"')
        self.assertContains(page, "Product catalogue")
        self.assertContains(page, "Current sale")
        self.assertContains(page, "pos.js")
        self.assertContains(page, "Workspace")
        self.assertContains(page, "Point of Sale")
        self.assertNotContains(page, 'class="pos-server-errors"')

        search = self.client.get("/sales/catalog/", {"q": self.product.sku})
        self.assertEqual(search.status_code, 200)
        payload = search.json()
        self.assertEqual([item["sku"] for item in payload["products"]], [self.product.sku])
        self.assertEqual(payload["products"][0]["selling_price"], "5000.00")
        self.assertNotIn("purchase_price", payload["products"][0])

        invalid_page = self.client.post("/sales/new/", self._pos_payload(quantity="0"))
        self.assertContains(invalid_page, 'class="pos-server-errors"')

        other_category = Category.objects.create(name="Dashboard Accessories")
        inactive_product = Product.objects.create(
            name="Inactive phone", category=other_category, sku="POS-INACTIVE",
            purchase_price=Decimal("1.00"), selling_price=Decimal("2.00"), is_active=False,
        )
        visible_product = Product.objects.create(
            name="USB adapter", category=other_category, sku="POS-USB-001",
            purchase_price=Decimal("3.00"), selling_price=Decimal("10.00"),
        )
        category_response = self.client.get("/sales/catalog/", {"category": other_category.pk})
        self.assertEqual([item["sku"] for item in category_response.json()["products"]], [visible_product.sku])
        self.assertNotIn(inactive_product.sku, str(category_response.content))
        self.assertEqual(self.client.get("/sales/catalog/", {"category": "not-a-category"}).status_code, 400)

    def test_serial_unit_lookup_returns_only_available_units_and_rechecks_at_checkout(self):
        tracked = Product.objects.create(
            name="Tracked phone", category=self.category, sku="POS-TRACKED-001",
            purchase_price=Decimal("300.00"), selling_price=Decimal("500.00"), track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="POS serial test stock", unit_identifiers=["IMEI-POS-001", "IMEI-POS-002"],
        )
        sold = create_sale(
            user=self.owner, sale_type=SaleType.CASH,
            lines=[{"product": tracked, "quantity": 1, "unit_identifiers": ["IMEI-POS-001"]}],
            initial_payment={"amount": Decimal("500.00"), "method": PaymentMethod.CASH},
        )
        self.client.force_login(self.employee)
        response = self.client.get(f"/sales/products/{tracked.pk}/units/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([unit["identifier"] for unit in response.json()["units"]], ["IMEI-POS-002"])
        self.assertEqual(list(sold.items.get().units.values_list("identifier", flat=True)), ["IMEI-POS-001"])
        checkout = self.client.post("/sales/new/", self._pos_payload(
            payment_amount="500.00",
            **{
                "items-0-product": str(tracked.pk),
                "items-0-unit_identifiers": "IMEI-POS-002",
            },
        ))
        self.assertEqual(checkout.status_code, 302)
        tracked_sale = Sale.objects.get(created_by=self.employee)
        self.assertEqual(list(tracked_sale.items.get().units.values_list("identifier", flat=True)), ["IMEI-POS-002"])

    def _pos_payload(self, *, submission_key=None, sale_type=SaleType.CASH, quantity="1", **overrides):
        payload = {
            "submission_key": str(submission_key or uuid.uuid4()),
            "sale_type": sale_type,
            "customer": str(self.customer.pk) if sale_type == SaleType.CREDIT else "",
            "new_customer_name": "",
            "new_customer_phone": "",
            "new_customer_email": "",
            "new_customer_address": "",
            "payment_method": PaymentMethod.CASH if sale_type == SaleType.CASH else "",
            "payment_amount": "5000.00" if sale_type == SaleType.CASH else "",
            "momo_reference": "",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "12",
            "items-0-product": str(self.product.pk),
            "items-0-quantity": quantity,
            "items-0-unit_identifiers": "",
        }
        payload.update(overrides)
        return payload

    def test_cash_pos_checkout_is_idempotent_and_receipt_offers_next_sale(self):
        self.client.force_login(self.employee)
        key = uuid.uuid4()
        payload = self._pos_payload(submission_key=key)
        first = self.client.post("/sales/new/", payload)
        self.assertEqual(first.status_code, 302)
        sale = Sale.objects.get(submission_key=key)
        self.assertEqual(first.url, f"/sales/{sale.pk}/receipt/")
        self.assertTrue(self.client.get(first.url).context["sale"].status == SaleStatus.APPROVED)
        self.assertContains(self.client.get(first.url), "Record another sale")
        self.assertEqual(sale.payments.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 9)

        repeated = self.client.post("/sales/new/", payload)
        self.assertRedirects(repeated, first.url)
        self.assertEqual(Sale.objects.filter(submission_key=key).count(), 1)
        self.assertEqual(sale.payments.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 9)
        self.assertEqual(InventoryMovement.objects.filter(reference=sale.reference).count(), 1)
        second_employee = User.objects.create_user(
            username="sales-retry-other", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.client.force_login(second_employee)
        forbidden_retry = self.client.post("/sales/new/", payload)
        self.assertEqual(forbidden_retry.status_code, 403)

    def test_duplicate_retry_after_last_unit_sold_still_returns_original_receipt(self):
        single_stock = Product.objects.create(
            name="One-unit accessory", category=self.category, sku="POS-LAST-UNIT",
            purchase_price=Decimal("100.00"), selling_price=Decimal("250.00"),
        )
        record_stock_movement(
            product=single_stock, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="One-unit POS retry fixture",
        )
        self.client.force_login(self.employee)
        key = uuid.uuid4()
        payload = self._pos_payload(
            submission_key=key,
            payment_amount="250.00",
            **{"items-0-product": str(single_stock.pk)},
        )
        first = self.client.post("/sales/new/", payload)
        self.assertEqual(first.status_code, 302)
        single_stock.refresh_from_db()
        self.assertEqual(single_stock.stock_quantity, 0)
        retry = self.client.post("/sales/new/", payload)
        self.assertRedirects(retry, first.url)
        self.assertEqual(Sale.objects.filter(submission_key=key).count(), 1)
        self.assertEqual(InventoryMovement.objects.filter(reference=Sale.objects.get(submission_key=key).reference).count(), 1)

    def test_momo_pos_checkout_is_pending_confirmation_and_employee_credit_waits_for_approval(self):
        self.client.force_login(self.employee)
        momo_response = self.client.post("/sales/new/", self._pos_payload(
            payment_method=PaymentMethod.MTN_MOMO, momo_reference="POS-MOMO-REFERENCE-001",
        ))
        self.assertEqual(momo_response.status_code, 302)
        momo_sale = Sale.objects.get(created_by=self.employee)
        momo_payment = momo_sale.payments.get()
        self.assertEqual(momo_payment.status, PaymentStatus.PENDING_CONFIRMATION)
        self.assertEqual(momo_sale.amount_paid, Decimal("0.00"))
        self.assertContains(self.client.get(momo_response.url), "PENDING CONFIRMATION")

        credit_response = self.client.post("/sales/new/", self._pos_payload(sale_type=SaleType.CREDIT))
        self.assertEqual(credit_response.status_code, 302)
        pending = Sale.objects.exclude(pk=momo_sale.pk).get(created_by=self.employee)
        self.assertEqual(pending.status, SaleStatus.PENDING_APPROVAL)
        self.assertEqual(self.client.get(credit_response.url).context["sale"].status, SaleStatus.PENDING_APPROVAL)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 9)
        denied = self.client.post(f"/sales/{pending.pk}/approve/")
        self.assertEqual(denied.status_code, 403)

    def test_pos_rejects_invalid_quantities_and_insufficient_stock(self):
        self.client.force_login(self.employee)
        invalid_quantity = self.client.post("/sales/new/", self._pos_payload(quantity="0"))
        self.assertEqual(invalid_quantity.status_code, 200)
        self.assertEqual(Sale.objects.count(), 0)
        insufficient = self.client.post("/sales/new/", self._pos_payload(quantity="11"))
        self.assertEqual(insufficient.status_code, 200)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertIn("Insufficient stock", str(insufficient.context["header"].non_field_errors()))

    def test_pos_catalog_requires_shop_account(self):
        response = self.client.get("/sales/catalog/")
        self.assertEqual(response.status_code, 302)
