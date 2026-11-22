from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.accounts.models import Role
from apps.audit.models import AuditEvent
from apps.products.models import Category, MovementType, Product
from apps.products.services import record_stock_movement
from apps.sales.models import PaymentMethod, PaymentStatus, SaleStatus, SaleType
from apps.sales.services import approve_sale, confirm_momo_payment, create_sale, record_payment, reject_momo_payment

from .forms import CustomerForm
from .models import Customer
from .services import create_customer, update_customer


User = get_user_model()


class CustomerDebtTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="customer-owner", email="owner@example.invalid", password="test-password")
        self.secretary = User.objects.create_user(username="customer-secretary", password="test-password", role=Role.SECRETARY)
        self.employee = User.objects.create_user(username="customer-employee", password="test-password", role=Role.EMPLOYEE)
        self.other_employee = User.objects.create_user(username="customer-other", password="test-password", role=Role.EMPLOYEE)
        self.customer = create_customer(
            user=self.secretary, full_name="Ama Mensah", phone_number="024 123 4567", email="ama@example.invalid",
        )
        self.category = Category.objects.create(name="Customer Test Goods")
        self.product = Product.objects.create(
            name="Test laptop", category=self.category, sku="CUSTOMER-TEST-001",
            purchase_price=Decimal("3000.00"), selling_price=Decimal("5000.00"),
        )
        record_stock_movement(
            product=self.product, quantity=30, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test fixture stock",
        )

    def make_credit_sale(self, *, user=None, initial_payment=None, customer=None):
        return create_sale(
            user=user or self.secretary,
            sale_type=SaleType.CREDIT,
            customer=customer or self.customer,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment=initial_payment,
        )

    def test_customer_creation_and_normalized_duplicate_phone_handling(self):
        self.assertEqual(self.customer.normalized_phone, "233241234567")
        with self.assertRaises(ValidationError):
            create_customer(user=self.employee, full_name="Duplicate Ama", phone_number="+233 24 123 4567")
        self.assertEqual(Customer.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="CUSTOMER_CREATED", object_reference=str(self.customer.pk)).exists())
        form = CustomerForm({"full_name": "Duplicate", "phone_number": "0241234567"})
        self.assertFalse(form.is_valid())

    def test_customer_updates_are_restricted_audited_and_do_not_change_sale_snapshots(self):
        sale = self.make_credit_sale(user=self.secretary)
        with self.assertRaises(PermissionDenied):
            update_customer(customer=self.customer, user=self.employee, full_name="Changed")
        update_customer(
            customer=self.customer, user=self.secretary, full_name="Ama New Name",
            phone_number="0251112233", is_active=False,
        )
        self.customer.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(sale.customer_name_snapshot, "Ama Mensah")
        self.assertEqual(sale.customer_phone_snapshot, "024 123 4567")
        self.assertFalse(self.customer.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="CUSTOMER_DEACTIVATED").exists())
        self.assertTrue(AuditEvent.objects.filter(action="CUSTOMER_UPDATED").exists())

    def test_cash_and_credit_sales_can_be_linked_and_credit_requires_customer(self):
        cash = create_sale(
            user=self.employee, sale_type=SaleType.CASH, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("5000.00"), "method": PaymentMethod.CASH},
        )
        credit = self.make_credit_sale(user=self.employee)
        self.assertEqual(cash.customer, self.customer)
        self.assertEqual(credit.customer, self.customer)
        self.assertEqual(credit.status, SaleStatus.PENDING_APPROVAL)
        with self.assertRaises(ValidationError):
            create_sale(user=self.employee, sale_type=SaleType.CREDIT, lines=[{"product": self.product, "quantity": 1}])

    def test_pending_and_rejected_credit_sales_do_not_create_confirmed_debt(self):
        pending = self.make_credit_sale(user=self.employee)
        self.assertEqual(self.customer.total_approved_credit_sales, Decimal("0.00"))
        from apps.sales.services import reject_sale
        reject_sale(sale=pending, user=self.secretary, reason="Credit terms not accepted")
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))

    def test_initial_credit_payment_is_counted_once_and_partial_then_full_repayment(self):
        sale = self.make_credit_sale(
            initial_payment={"amount": Decimal("1000.00"), "method": PaymentMethod.CASH}
        )
        self.assertEqual(sale.status, SaleStatus.APPROVED)
        self.assertEqual(self.customer.total_approved_credit_sales, Decimal("5000.00"))
        self.assertEqual(self.customer.total_confirmed_credit_payments, Decimal("1000.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("4000.00"))

        partial = record_payment(sale=sale, user=self.employee if sale.created_by == self.employee else self.secretary,
                                 amount=Decimal("1500.00"), method=PaymentMethod.CASH)
        self.assertTrue(partial.payment_reference.startswith("PMT-"))
        self.assertTrue(AuditEvent.objects.filter(
            action="CUSTOMER_REPAYMENT_ALLOCATED", object_reference=partial.payment_reference,
            actor=self.secretary,
        ).exists())
        self.assertEqual(self.customer.outstanding_debt, Decimal("2500.00"))
        record_payment(sale=sale, user=self.secretary, amount=Decimal("2500.00"), method=PaymentMethod.CASH)
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))
        self.assertEqual(sale.payments.filter(status=PaymentStatus.CONFIRMED).count(), 3)

    def test_overpayment_and_duplicate_submission_are_prevented(self):
        sale = self.make_credit_sale(user=self.employee)
        approve_sale(sale=sale, user=self.secretary)
        token = uuid.uuid4()
        first = record_payment(
            sale=sale, user=self.employee, amount=Decimal("2000.00"), method=PaymentMethod.CASH,
            idempotency_key=token,
        )
        repeated = record_payment(
            sale=sale, user=self.employee, amount=Decimal("2000.00"), method=PaymentMethod.CASH,
            idempotency_key=token,
        )
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(sale.payments.count(), 1)
        with self.assertRaises(ValidationError):
            record_payment(sale=sale, user=self.secretary, amount=Decimal("3000.01"), method=PaymentMethod.CASH)
        self.assertEqual(self.customer.outstanding_debt, Decimal("3000.00"))

    def test_pending_momo_repayment_needs_manager_confirmation_and_audit(self):
        sale = self.make_credit_sale(user=self.employee)
        approve_sale(sale=sale, user=self.secretary)
        payment = record_payment(
            sale=sale, user=self.employee, amount=Decimal("1200.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="CUSTOMER-MOMO-001",
        )
        self.assertEqual(payment.status, PaymentStatus.PENDING_CONFIRMATION)
        self.assertEqual(self.customer.outstanding_debt, Decimal("5000.00"))
        with self.assertRaises(PermissionDenied):
            confirm_momo_payment(payment=payment, user=self.employee, confirmation_reference="STATEMENT-1")
        confirm_momo_payment(payment=payment, user=self.owner, confirmation_reference="STATEMENT-1")
        self.assertEqual(self.customer.outstanding_debt, Decimal("3800.00"))
        self.assertTrue(AuditEvent.objects.filter(action="PAYMENT_MOMO_CONFIRMED", object_reference=str(payment.pk)).exists())

    def test_rejected_momo_repayment_does_not_reduce_debt(self):
        sale = self.make_credit_sale(user=self.employee)
        approve_sale(sale=sale, user=self.secretary)
        payment = record_payment(
            sale=sale, user=self.employee, amount=Decimal("1000.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="CUSTOMER-MOMO-REJECT",
        )
        reject_momo_payment(payment=payment, user=self.secretary, reason="Reference not found")
        self.assertEqual(self.customer.outstanding_debt, Decimal("5000.00"))
        self.assertEqual(payment.sale_id, sale.pk)
        self.assertTrue(AuditEvent.objects.filter(action="PAYMENT_MOMO_REJECTED").exists())

    def test_employee_profile_and_repayment_receipt_are_scoped_and_accurate(self):
        own = self.make_credit_sale(user=self.employee)
        approve_sale(sale=own, user=self.secretary)
        other = self.make_credit_sale(user=self.other_employee)
        approve_sale(sale=other, user=self.secretary)
        payment = record_payment(
            sale=own, user=self.employee, amount=Decimal("800.00"), method=PaymentMethod.CASH,
            idempotency_key=uuid.uuid4(),
        )
        self.client.force_login(self.employee)
        response = self.client.get(f"/customers/{self.customer.pk}/")
        self.assertContains(response, 'id="app-sidebar"')
        self.assertContains(response, "1ST POINT TECH HUB")
        self.assertContains(response, 'href="/customers/"')
        self.assertContains(response, own.reference)
        self.assertNotContains(response, other.reference)
        receipt = self.client.get(f"/customers/repayments/{payment.pk}/receipt/")
        self.assertEqual(receipt.status_code, 200)
        self.assertContains(receipt, payment.payment_reference)
        self.assertContains(receipt, "GHS 800.00")
        self.assertContains(receipt, "GHS 4200.00")

    def test_pending_momo_receipt_is_explicitly_unconfirmed(self):
        sale = self.make_credit_sale(user=self.employee)
        approve_sale(sale=sale, user=self.secretary)
        payment = record_payment(
            sale=sale, user=self.employee, amount=Decimal("500.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="CUSTOMER-MOMO-PENDING",
        )
        self.client.force_login(self.employee)
        response = self.client.get(f"/customers/repayments/{payment.pk}/receipt/")
        self.assertContains(response, "PENDING CONFIRMATION")
        self.assertContains(response, "GHS 5000.00")

    def test_employee_customer_list_and_search_are_limited_to_own_sale_customers(self):
        own_customer = self.customer
        other_customer = create_customer(
            user=self.secretary, full_name="Other Employee Customer", phone_number="0241234568",
        )
        own_sale = self.make_credit_sale(user=self.employee, customer=own_customer)
        other_sale = self.make_credit_sale(user=self.other_employee, customer=other_customer)
        approve_sale(sale=own_sale, user=self.secretary)
        approve_sale(sale=other_sale, user=self.secretary)

        self.client.force_login(self.employee)
        response = self.client.get("/customers/")
        self.assertContains(response, own_customer.full_name)
        self.assertNotContains(response, other_customer.full_name)

        search = self.client.get("/customers/", {"q": other_customer.phone_number})
        self.assertNotContains(search, other_customer.full_name)

    def test_employee_cannot_open_or_repay_unrelated_customer_but_can_use_own_customer(self):
        other_customer = create_customer(
            user=self.secretary, full_name="Unrelated Customer", phone_number="0241234568",
        )
        own_sale = self.make_credit_sale(user=self.employee, customer=self.customer)
        other_sale = self.make_credit_sale(user=self.other_employee, customer=other_customer)
        approve_sale(sale=own_sale, user=self.secretary)
        approve_sale(sale=other_sale, user=self.secretary)

        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(f"/customers/{other_customer.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/customers/{other_customer.pk}/repayments/new/").status_code, 404)

        own_detail = self.client.get(f"/customers/{self.customer.pk}/")
        self.assertEqual(own_detail.status_code, 200)
        self.assertContains(own_detail, own_sale.reference)
        self.assertNotContains(own_detail, other_sale.reference)
        own_repayment = self.client.get(f"/customers/{self.customer.pk}/repayments/new/")
        self.assertEqual(own_repayment.status_code, 200)
        self.assertContains(own_repayment, own_sale.reference)
        self.assertNotContains(own_repayment, other_sale.reference)

    def test_managers_retain_customer_list_detail_and_repayment_access(self):
        other_customer = create_customer(
            user=self.secretary, full_name="Manager Visible Customer", phone_number="0241234568",
        )
        sale = self.make_credit_sale(user=self.other_employee, customer=other_customer)
        approve_sale(sale=sale, user=self.secretary)

        for manager in (self.owner, self.secretary):
            self.client.force_login(manager)
            listing = self.client.get("/customers/", {"q": other_customer.full_name})
            self.assertContains(listing, other_customer.full_name)
            self.assertEqual(self.client.get(f"/customers/{other_customer.pk}/").status_code, 200)
            repayment = self.client.get(f"/customers/{other_customer.pk}/repayments/new/")
            self.assertEqual(repayment.status_code, 200)
            self.assertContains(repayment, sale.reference)
