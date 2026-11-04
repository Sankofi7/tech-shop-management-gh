from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, record_audit_event
from apps.customers.services import create_customer
from apps.products.models import Category, InventoryMovement, MovementType, Product
from apps.products.services import record_stock_movement
from apps.sales.models import PaymentMethod, PaymentStatus, SaleStatus, SaleType
from apps.sales.services import approve_sale, confirm_momo_payment, create_sale, record_payment, reject_sale


User = get_user_model()


class ReportsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="reports-owner", email="reports-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="reports-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="reports-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="reports-other", password="test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Report Customer", phone_number="0241112222"
        )
        category = Category.objects.create(name="Report Products")
        self.product = Product.objects.create(
            name="Report Laptop", category=category, sku="REPORT-LAPTOP",
            purchase_price=Decimal("3000.00"), selling_price=Decimal("5000.00"), minimum_stock_level=3,
        )
        record_stock_movement(
            product=self.product, quantity=20, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Report test stock", reference="REPORT-DELIVERY",
        )

    def make_cash_sale(self, user, reference=None):
        return create_sale(
            user=user, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )

    def make_credit_sale(self, user=None, initial_amount=None):
        initial = None if initial_amount is None else {"amount": Decimal(initial_amount), "method": PaymentMethod.CASH}
        return create_sale(
            user=user or self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}], initial_payment=initial,
        )

    def test_sales_report_period_filter_excludes_pending_and_rejected_credit(self):
        approved = self.make_cash_sale(self.employee)
        pending = self.make_credit_sale(self.employee)
        reject_sale(sale=pending, user=self.owner, reason="Credit terms declined")
        rejected = self.make_credit_sale(self.other_employee)
        reject_sale(sale=rejected, user=self.owner, reason="Duplicate application")
        # A second owner-approved employee credit sale is included after approval.
        approved_credit = self.make_credit_sale(self.employee)
        approve_sale(sale=approved_credit, user=self.secretary)
        pending_open = self.make_credit_sale(self.employee)

        self.client.force_login(self.owner)
        today = timezone.localdate()
        response = self.client.get("/reports/sales/", {
            "period": "custom", "date_from": today.isoformat(), "date_to": today.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sales_count"], 2)
        self.assertEqual(response.context["sales_total"], Decimal("10000.00"))
        self.assertNotContains(response, pending.reference)
        self.assertNotContains(response, rejected.reference)
        self.assertNotContains(response, pending_open.reference)
        outside = self.client.get("/reports/sales/", {
            "period": "custom", "date_from": (today + timedelta(days=1)).isoformat(),
            "date_to": (today + timedelta(days=2)).isoformat(),
        })
        self.assertEqual(outside.context["sales_count"], 0)

    def test_confirmed_and_pending_momo_are_separate_and_not_double_counted(self):
        sale = self.make_credit_sale(initial_amount="1000.00")
        confirmed_momo = record_payment(
            sale=sale, user=self.secretary, amount=Decimal("500.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="REPORT-MOMO-CONFIRMED",
        )
        confirm_momo_payment(payment=confirmed_momo, user=self.owner, confirmation_reference="CONFIRM-REPORT-1")
        record_payment(
            sale=sale, user=self.secretary, amount=Decimal("300.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="REPORT-MOMO-PENDING",
        )
        self.client.force_login(self.owner)
        response = self.client.get("/reports/payments/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cash_total"], Decimal("1000.00"))
        self.assertEqual(response.context["momo_total"], Decimal("500.00"))
        self.assertEqual(response.context["confirmed_total"], Decimal("1500.00"))
        self.assertEqual(response.context["pending_total"], Decimal("300.00"))
        self.assertEqual(response.context["pending_count"], 1)
        self.assertContains(response, "REPORT-MOMO-PENDING")
        self.assertContains(response, "CONFIRM-REPORT-1")

    def test_credit_report_reuses_customer_debt_calculation_and_lists_paid_and_pending(self):
        partial = self.make_credit_sale(initial_amount="1200.00")
        paid = self.make_credit_sale()
        record_payment(sale=paid, user=self.secretary, amount=Decimal("5000.00"), method=PaymentMethod.CASH)
        pending = self.make_credit_sale(self.employee)
        self.client.force_login(self.owner)
        response = self.client.get("/reports/credit/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["credit_total"], Decimal("10000.00"))
        self.assertEqual(response.context["paid_total"], Decimal("6200.00"))
        self.assertEqual(response.context["outstanding"], Decimal("3800.00"))
        self.assertEqual(response.context["pending_count"], 1)
        self.assertEqual(list(response.context["paid_sales"]), [paid])
        self.assertNotEqual(partial.status, SaleStatus.PENDING_APPROVAL)
        self.assertEqual(pending.status, SaleStatus.PENDING_APPROVAL)
        self.assertEqual(self.client.get("/reports/credit/", {"format": "csv"}).status_code, 200)

    def test_inventory_report_uses_current_products_and_period_movements(self):
        record_stock_movement(
            product=self.product, quantity=-2, movement_type=MovementType.APPROVED_ADJUSTMENT,
            user=self.owner, reason="Report test adjustment",
        )
        self.client.force_login(self.secretary)
        today = timezone.localdate()
        response = self.client.get("/reports/inventory/", {
            "period": "custom", "date_from": today.isoformat(), "date_to": today.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_count"], 1)
        self.assertEqual(response.context["available_units"], 18)
        self.assertEqual(response.context["received"], 20)
        self.assertEqual(response.context["deducted"], 2)
        self.assertTrue(InventoryMovement.objects.filter(product=self.product).exists())

    def test_employee_reports_are_scoped_and_company_report_endpoints_are_forbidden(self):
        own_sale = self.make_cash_sale(self.employee)
        other_sale = self.make_cash_sale(self.other_employee)
        own_pending = self.make_credit_sale(self.employee)
        record_audit_event(action="EMPLOYEE_TEST_EVENT", actor=self.employee, object_type="test", object_reference="own")
        record_audit_event(action="OTHER_TEST_EVENT", actor=self.other_employee, object_type="test", object_reference="other")
        self.client.force_login(self.employee)
        response = self.client.get("/reports/sales/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sales_count"], 1)
        self.assertEqual(response.context["pending_count"], 1)
        self.assertContains(response, own_sale.reference)
        self.assertNotContains(response, other_sale.reference)
        self.assertNotContains(response, own_pending.reference)
        self.assertEqual(self.client.get("/reports/credit/").status_code, 403)
        self.assertEqual(self.client.get("/reports/inventory/").status_code, 403)
        self.assertEqual(self.client.get("/reports/employees/").status_code, 403)
        own_csv = self.client.get("/reports/sales/?format=csv")
        self.assertEqual(own_csv.status_code, 200)
        self.assertIn(own_sale.reference.encode(), own_csv.content)
        self.assertNotIn(other_sale.reference.encode(), own_csv.content)
        activity = self.client.get("/reports/")
        self.assertContains(activity, "Employee Test Event")
        self.assertNotContains(activity, "Other Test Event")

    def test_employee_payment_csv_omits_other_employees_and_transaction_references(self):
        sale = self.make_cash_sale(self.employee)
        own_payment = sale.payments.get()
        self.client.force_login(self.employee)
        response = self.client.get("/reports/payments/?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(sale.reference.encode(), response.content)
        self.assertNotIn(own_payment.payment_reference.encode(), response.content)
        self.assertNotIn(b"MoMo transaction reference", response.content)

    def test_employee_performance_counts_approved_and_pending_separately(self):
        self.make_cash_sale(self.employee)
        self.make_cash_sale(self.other_employee)
        pending = self.make_credit_sale(self.employee)
        rejected = self.make_credit_sale(self.other_employee)
        reject_sale(sale=rejected, user=self.owner, reason="Not approved by manager")
        self.client.force_login(self.secretary)
        response = self.client.get("/reports/employees/")
        self.assertEqual(response.status_code, 200)
        rows = {row["employee"].username: row for row in response.context["employee_data"]}
        self.assertEqual(rows[self.employee.username]["sales_count"], 1)
        self.assertEqual(rows[self.employee.username]["sales_total"], Decimal("5000.00"))
        self.assertEqual(rows[self.employee.username]["pending_count"], 1)
        self.assertEqual(rows[self.employee.username]["pending_total"], Decimal("5000.00"))
        self.assertEqual(rows[self.other_employee.username]["pending_count"], 0)
        self.assertNotContains(response, rejected.reference)
        self.assertNotEqual(pending.status, SaleStatus.REJECTED)

    def test_role_permissions_and_report_category_landing(self):
        self.client.force_login(self.secretary)
        secretary_home = self.client.get("/reports/")
        self.assertEqual(secretary_home.status_code, 200)
        self.assertContains(secretary_home, "Employee performance")
        self.client.force_login(self.employee)
        employee_home = self.client.get("/reports/")
        self.assertEqual(employee_home.status_code, 200)
        self.assertNotContains(employee_home, "Credit & debt")
        self.assertNotContains(employee_home, "Employee performance")
