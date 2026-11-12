from datetime import timedelta
from decimal import Decimal
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models import Sum
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent
from apps.customers.services import create_customer, credit_totals
from apps.products.models import (
    Brand, Category, InventoryMovement, InventoryUnit, MovementType, Product,
    UnitCondition, UnitStatus,
)
from apps.products.services import record_stock_movement
from apps.sales.models import Payment, PaymentMethod, PaymentStatus, Sale, SaleStatus, SaleType
from apps.sales.services import confirm_momo_payment, create_sale, record_payment
from apps.shop_settings.models import ShopProfile

from .models import (
    ExchangeLink, RefundDisbursement, RefundDisbursementStatus, RefundRequest,
    RefundRequestStatus, ReturnCase, ReturnCondition, ReturnDisposition,
    ReturnItem, ReturnKind, ReturnStatus,
)
from .services import (
    approve_refund_request, approve_return_request, confirm_refund_momo_disbursement,
    create_refund_request, create_return_request, link_exchange_sale,
    record_refund_disbursement, reject_refund_momo_disbursement,
    reject_refund_request, reject_return_request,
)


User = get_user_model()


class ReturnsWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="returns-owner", email="returns-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="returns-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="returns-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="returns-other", password="test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Return Customer", phone_number="0241112233"
        )
        self.category = Category.objects.create(name="Return Consoles")
        self.brand = Brand.objects.create(name="Return Brand")
        self.product = Product.objects.create(
            name="Return Console", category=self.category, brand=self.brand,
            sku="RETURN-CONSOLE", purchase_price=Decimal("70.00"),
            selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=10, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Initial test stock",
        )

    def make_sale(self, *, quantity=1, sale_type=SaleType.CASH, user=None, customer=None,
                  payment_method=PaymentMethod.CASH, unit_identifiers=()):
        user = user or self.employee
        customer = customer or (self.customer if sale_type == SaleType.CREDIT else None)
        total = self.product.selling_price * quantity
        initial_payment = None
        if sale_type == SaleType.CASH:
            initial_payment = {"amount": total, "method": payment_method}
            if payment_method == PaymentMethod.MTN_MOMO:
                initial_payment["momo_reference"] = f"MOMO-{Sale.objects.count()}"
        return create_sale(
            user=user, sale_type=sale_type, customer=customer,
            lines=[{"product": self.product, "quantity": quantity, "unit_identifiers": unit_identifiers}],
            initial_payment=initial_payment,
        )

    def request_return(self, sale, *, quantity=1, user=None, identifiers=(), kind=ReturnKind.RETURN):
        return create_return_request(
            sale=sale, user=user or self.employee, reason="Customer changed their mind",
            kind=kind,
            items=[{
                "sale_item": sale.items.get(), "quantity": quantity,
                "unit_identifiers": identifiers,
            }],
        )

    def approve_return(self, case, *, disposition=ReturnDisposition.RESTOCK, condition=ReturnCondition.GOOD, user=None):
        item = case.items.get()
        return approve_return_request(
            return_case=case, user=user or self.secretary,
            item_decisions={item.pk: {"condition": condition, "disposition": disposition}},
        )

    def test_pending_and_rejected_sales_cannot_be_returned(self):
        pending = self.make_sale(sale_type=SaleType.CREDIT)
        self.assertEqual(pending.status, SaleStatus.PENDING_APPROVAL)
        with self.assertRaises(ValidationError):
            self.request_return(pending)
        from apps.sales.services import reject_sale

        rejected = reject_sale(sale=pending, user=self.secretary, reason="Credit request declined")
        with self.assertRaises(ValidationError):
            self.request_return(rejected)
        self.assertEqual(ReturnCase.objects.count(), 0)

    def test_return_window_defaults_to_seven_days_and_owner_setting_is_used(self):
        self.assertEqual(ShopProfile.get_solo().return_window_days, 7)
        sale = self.make_sale()
        with patch("apps.returns.services.timezone.localdate", return_value=timezone.localdate() + timedelta(days=8)):
            with self.assertRaises(ValidationError):
                self.request_return(sale)
        profile = ShopProfile.objects.create(return_window_days=14)
        with patch("apps.returns.services.ShopProfile.get_solo", return_value=profile):
            with patch("apps.returns.services.timezone.localdate", return_value=timezone.localdate() + timedelta(days=8)):
                self.assertEqual(self.request_return(sale).status, ReturnStatus.PENDING_APPROVAL)

    def test_partial_and_repeated_returns_allow_only_remaining_quantity(self):
        sale = self.make_sale(quantity=3)
        first = self.request_return(sale, quantity=1)
        self.approve_return(first)
        second = self.request_return(sale, quantity=1)
        with self.assertRaises(ValidationError):
            self.request_return(sale, quantity=2)
        self.assertEqual(ReturnItem.objects.filter(sale_item=sale.items.get()).count(), 2)
        reject_return_request(return_case=second, user=self.secretary, reason="Customer did not return the item")
        third = self.request_return(sale, quantity=2)
        self.assertEqual(third.items.get().quantity, 2)

    def test_request_reason_and_unauthorized_roles_are_enforced(self):
        sale = self.make_sale()
        with self.assertRaises(ValidationError):
            create_return_request(sale=sale, user=self.employee, reason=" ", items=[{"sale_item": sale.items.get(), "quantity": 1}])
        with self.assertRaises(PermissionDenied):
            self.request_return(sale, user=self.other_employee)
        case = self.request_return(sale)
        with self.assertRaises(PermissionDenied):
            approve_return_request(return_case=case, user=self.employee, item_decisions={})
        with self.assertRaises(PermissionDenied):
            approve_return_request(
                return_case=case, user=self.employee,
                item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
            )

    def test_service_reasons_require_at_least_five_non_whitespace_characters(self):
        sale = self.make_sale()
        for reason in ("", "   ", "no"):
            with self.assertRaises(ValidationError):
                create_return_request(
                    sale=sale, user=self.employee, reason=reason,
                    items=[{"sale_item": sale.items.get(), "quantity": 1}],
                )

        case = self.request_return(sale)
        for reason in (" ", "no"):
            with self.assertRaises(ValidationError):
                reject_return_request(return_case=case, user=self.secretary, reason=reason)

        self.approve_return(case, user=self.secretary)
        refund = create_refund_request(
            return_case=case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, reason="Valid refund reason.",
        )
        for reason in (" ", "no"):
            with self.assertRaises(ValidationError):
                reject_refund_request(refund_request=refund, user=self.secretary, reason=reason)
        approve_refund_request(refund_request=refund, user=self.secretary)
        disbursement = record_refund_disbursement(
            refund_request=refund, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="SHORT-REASON-MOMO",
        )
        for reason in (" ", "no"):
            with self.assertRaises(ValidationError):
                reject_refund_momo_disbursement(disbursement=disbursement, user=self.owner, reason=reason)

    def test_requester_cannot_approve_own_return_and_rejection_is_audited(self):
        sale = self.make_sale(user=self.secretary)
        case = self.request_return(sale, user=self.secretary)
        with self.assertRaises(PermissionDenied):
            approve_return_request(
                return_case=case, user=self.secretary,
                item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
            )
        with self.assertRaises(ValidationError):
            reject_return_request(return_case=case, user=self.owner, reason=" ")
        reject_return_request(return_case=case, user=self.owner, reason="Item has no proof of purchase")
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_REJECTED", object_reference=case.reference).exists())

    def test_return_value_uses_saved_sale_price_not_current_product_price(self):
        sale = self.make_sale()
        item = sale.items.get()
        self.product.refresh_from_db()
        self.product.selling_price = Decimal("135.00")
        self.product.save()
        case = self.request_return(sale)
        self.assertEqual(case.total_value, Decimal("100.00"))
        self.assertEqual(case.items.get().unit_price, item.unit_price)

    def test_resellable_return_restocks_and_defective_return_is_quarantined(self):
        sale = self.make_sale()
        case = self.request_return(sale)
        self.approve_return(case)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        movement = InventoryMovement.objects.get(reference=case.reference, movement_type=MovementType.CUSTOMER_RETURN)
        self.assertEqual((movement.quantity, movement.previous_stock, movement.resulting_stock), (1, 9, 10))

        tracked = Product.objects.create(
            name="Tracked Return Device", category=self.category, sku="TRACKED-RETURN",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"), track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Tracked test stock", unit_identifiers=("IMEI-RETURN-1",),
        )
        tracked_sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": tracked, "quantity": 1, "unit_identifiers": ["IMEI-RETURN-1"]}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        defective = self.request_return(tracked_sale, identifiers=("IMEI-RETURN-1",))
        self.approve_return(
            defective, condition=ReturnCondition.DEFECTIVE, disposition=ReturnDisposition.QUARANTINE,
        )
        unit = InventoryUnit.objects.get(product=tracked, identifier="IMEI-RETURN-1")
        tracked.refresh_from_db()
        self.assertEqual(unit.status, UnitStatus.QUARANTINED)
        self.assertEqual(tracked.stock_quantity, 0)
        self.assertFalse(InventoryMovement.objects.filter(reference=defective.reference).exists())

    def test_defective_or_incomplete_item_cannot_be_restocked(self):
        sale = self.make_sale()
        case = self.request_return(sale)
        with self.assertRaises(ValidationError):
            self.approve_return(case, condition=ReturnCondition.DEFECTIVE, disposition=ReturnDisposition.RESTOCK)
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        self.assertEqual(InventoryMovement.objects.filter(reference=case.reference).count(), 0)

    def test_serial_must_match_exact_sale_unit_and_cannot_be_returned_twice(self):
        tracked = Product.objects.create(
            name="Tracked Phone", category=self.category, sku="TRACKED-PHONE",
            purchase_price=Decimal("60.00"), selling_price=Decimal("100.00"), track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Phone delivery", unit_identifiers=("IMEI-A", "IMEI-B"),
        )
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": tracked, "quantity": 1, "unit_identifiers": ["IMEI-A"]}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        with self.assertRaises(ValidationError):
            self.request_return(sale, identifiers=("IMEI-B",))
        case = self.request_return(sale, identifiers=("IMEI-A",))
        with self.assertRaises(ValidationError):
            self.request_return(sale, identifiers=("IMEI-A",))
        reject_return_request(return_case=case, user=self.secretary, reason="Inspection did not pass")
        retried = self.request_return(sale, identifiers=("IMEI-A",))
        self.assertEqual(retried.status, ReturnStatus.PENDING_APPROVAL)

    def test_credit_return_reduces_debt_first_and_refunds_only_confirmed_surplus(self):
        sale = self.make_sale(quantity=2, sale_type=SaleType.CREDIT, user=self.secretary)
        record_payment(sale=sale, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))
        case = self.request_return(sale, user=self.secretary)
        case = self.approve_return(case, user=self.owner)
        self.customer.refresh_from_db()
        self.assertEqual(case.debt_credit_applied, Decimal("100.00"))
        self.assertEqual(case.refund_credit_available, Decimal("0.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))
        self.assertEqual(credit_totals(self.customer.approved_credit_sales)[3], Decimal("0.00"))

        another_sale = self.make_sale(quantity=2, sale_type=SaleType.CREDIT, user=self.secretary)
        record_payment(sale=another_sale, user=self.secretary, amount=Decimal("200.00"), method=PaymentMethod.CASH)
        refund_case = self.request_return(another_sale, user=self.secretary)
        refund_case = self.approve_return(refund_case, user=self.owner)
        self.assertEqual(refund_case.debt_credit_applied, Decimal("0.00"))
        self.assertEqual(refund_case.refund_credit_available, Decimal("100.00"))
        request = create_refund_request(
            return_case=refund_case, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Customer paid for returned item",
        )
        with self.assertRaises(PermissionDenied):
            approve_refund_request(refund_request=request, user=self.secretary)
        approve_refund_request(refund_request=request, user=self.owner)
        record_refund_disbursement(
            refund_request=request, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH,
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.total_confirmed_credit_refunds, Decimal("100.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_REQUESTED", object_reference=request.reference).exists())
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_APPROVED", object_reference=request.reference).exists())
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_DISBURSEMENT_RECORDED").exists())
        later_sale = self.make_sale(quantity=1, sale_type=SaleType.CREDIT, user=self.secretary)
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))
        self.assertEqual(credit_totals(self.customer.approved_credit_sales)[3], Decimal("100.00"))

    def test_unconfirmed_and_rejected_momo_payments_cannot_fund_refunds(self):
        from apps.sales.services import reject_momo_payment

        sale = self.make_sale(quantity=2, payment_method=PaymentMethod.MTN_MOMO)
        case = self.request_return(sale)
        self.approve_return(case)
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.employee, amount=Decimal("100.00"),
                method=PaymentMethod.MTN_MOMO, reason="Return payment requested",
            )
        payment = sale.payments.get()
        with self.assertRaises(ValidationError):
            confirm_momo_payment(payment=payment, user=self.owner, confirmation_reference="STATEMENT-RETURN-1")
        reject_momo_payment(payment=payment, user=self.owner, reason="The return reduced the sale balance")
        replacement_payment = record_payment(
            sale=sale, user=self.employee, amount=Decimal("100.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="RETURN-ADJUSTED-MOMO",
        )
        confirm_momo_payment(
            payment=replacement_payment, user=self.owner, confirmation_reference="RETURN-ADJUSTED-STATEMENT",
        )
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.employee, amount=Decimal("100.00"),
                method=PaymentMethod.MTN_MOMO, reason="Only confirmed cash received may fund a refund",
            )

    def test_rejected_momo_payment_cannot_fund_a_refund(self):
        from apps.sales.services import reject_momo_payment

        sale = self.make_sale(quantity=2, payment_method=PaymentMethod.MTN_MOMO)
        case = self.request_return(sale)
        self.approve_return(case)
        reject_momo_payment(payment=sale.payments.get(), user=self.owner, reason="Provider transfer was declined")
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.employee, amount=Decimal("100.00"),
                method=PaymentMethod.MTN_MOMO, reason="Refund requested after rejected payment",
            )

    def test_refund_capacity_limits_duplicates_and_rejected_momo_releases_reservation(self):
        sale = self.make_sale(quantity=2)
        case = self.request_return(sale)
        self.approve_return(case)
        request = create_refund_request(
            return_case=case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Refund requested",
        )
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.employee, amount=Decimal("0.01"),
                method=PaymentMethod.CASH, reason="Second refund request",
            )
        approve_refund_request(refund_request=request, user=self.secretary)
        disbursement = record_refund_disbursement(
            refund_request=request, user=self.owner, amount=Decimal("60.00"), method=PaymentMethod.CASH,
            idempotency_key="12345678-1234-5678-1234-567812345678",
        )
        same = record_refund_disbursement(
            refund_request=request, user=self.owner, amount=Decimal("60.00"), method=PaymentMethod.CASH,
            idempotency_key="12345678-1234-5678-1234-567812345678",
        )
        self.assertEqual(disbursement.pk, same.pk)
        with self.assertRaises(ValidationError):
            record_refund_disbursement(
                refund_request=request, user=self.owner, amount=Decimal("50.00"), method=PaymentMethod.CASH,
            )

        momo_case = self.request_return(sale, user=self.employee)
        self.approve_return(momo_case)
        momo_request = create_refund_request(
            return_case=momo_case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, reason="MoMo refund requested",
        )
        approve_refund_request(refund_request=momo_request, user=self.secretary)
        pending = record_refund_disbursement(
            refund_request=momo_request, user=self.owner, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="REFUND-MOMO-REJECT",
        )
        reject_refund_momo_disbursement(disbursement=pending, user=self.secretary, reason="Provider rejected transfer")
        with self.assertRaises(ValidationError):
            RefundDisbursement.objects.create(
                refund_request=momo_request, amount=Decimal("1.00"), method=PaymentMethod.CASH,
                status=RefundDisbursementStatus.CONFIRMED, recorded_by=self.owner,
            )

    def test_momo_refund_reference_must_be_unique(self):
        sale = self.make_sale(quantity=2)
        case = self.request_return(sale)
        self.approve_return(case)
        request = create_refund_request(
            return_case=case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, reason="Refund requested",
        )
        approve_refund_request(refund_request=request, user=self.secretary)
        record_refund_disbursement(
            refund_request=request, user=self.owner, amount=Decimal("50.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="DUPLICATE-REF",
        )
        with self.assertRaises(ValidationError):
            record_refund_disbursement(
                refund_request=request, user=self.owner, amount=Decimal("50.00"),
                method=PaymentMethod.MTN_MOMO, momo_reference="DUPLICATE-REF",
            )

    def test_exchange_links_an_approved_return_to_a_distinct_replacement_sale(self):
        original = self.make_sale(user=self.secretary, customer=self.customer)
        case = self.request_return(original, user=self.secretary, kind=ReturnKind.EXCHANGE)
        self.approve_return(case, user=self.owner)
        replacement = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary, customer=self.customer)
        link = link_exchange_sale(return_case=case, replacement_sale=replacement, user=self.owner)
        self.assertEqual(link.return_case_id, case.pk)
        self.assertEqual(link.replacement_sale_id, replacement.pk)
        self.assertEqual(ExchangeLink.objects.count(), 1)

    def test_exchange_higher_replacement_uses_return_credit_then_collects_difference(self):
        original = self.make_sale(user=self.secretary, customer=self.customer)
        case = self.request_return(original, user=self.secretary, kind=ReturnKind.EXCHANGE)
        self.approve_return(case, user=self.owner)
        replacement = self.make_sale(
            quantity=2, sale_type=SaleType.CREDIT, user=self.secretary, customer=self.customer,
        )
        link_exchange_sale(return_case=case, replacement_sale=replacement, user=self.owner)
        self.assertEqual(replacement.outstanding_balance, Decimal("100.00"))
        record_payment(sale=replacement, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        self.assertEqual(replacement.outstanding_balance, Decimal("0.00"))
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.secretary, amount=Decimal("1.00"),
                method=PaymentMethod.CASH, reason="No residual exchange credit",
            )

    def test_exchange_lower_replacement_leaves_only_residual_for_refund(self):
        original = self.make_sale(user=self.secretary, customer=self.customer)
        case = self.request_return(original, user=self.secretary, kind=ReturnKind.EXCHANGE)
        self.approve_return(case, user=self.owner)
        lower_product = Product.objects.create(
            name="Lower value replacement", category=self.category, brand=self.brand,
            sku="RETURN-LOWER", purchase_price=Decimal("20.00"), selling_price=Decimal("50.00"),
        )
        record_stock_movement(
            product=lower_product, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Test replacement stock",
        )
        replacement = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": lower_product, "quantity": 1}],
        )
        link_exchange_sale(return_case=case, replacement_sale=replacement, user=self.owner)
        self.assertEqual(replacement.outstanding_balance, Decimal("0.00"))
        request = create_refund_request(
            return_case=case, user=self.secretary, amount=Decimal("50.00"),
            method=PaymentMethod.CASH, reason="Residual exchange value",
        )
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.secretary, amount=Decimal("1.00"),
                method=PaymentMethod.CASH, reason="Attempt to reuse exchange value",
            )
        approve_refund_request(refund_request=request, user=self.owner)
        record_refund_disbursement(
            refund_request=request, user=self.secretary, amount=Decimal("50.00"), method=PaymentMethod.CASH,
        )
        self.assertEqual(replacement.outstanding_balance, Decimal("0.00"))

    def test_disbursement_revalidates_after_new_approved_credit_sale(self):
        sale = self.make_sale(quantity=2, sale_type=SaleType.CREDIT, user=self.secretary)
        record_payment(sale=sale, user=self.secretary, amount=Decimal("200.00"), method=PaymentMethod.CASH)
        case = self.request_return(sale, user=self.secretary)
        self.approve_return(case, user=self.owner)
        request = create_refund_request(
            return_case=case, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Return refund",
        )
        approve_refund_request(refund_request=request, user=self.owner)
        self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        with self.assertRaises(ValidationError):
            record_refund_disbursement(
                refund_request=request, user=self.owner, amount=Decimal("100.00"), method=PaymentMethod.CASH,
            )

    def test_return_credit_reduces_actual_balance_and_payment_cannot_collect_more(self):
        fully_paid = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        record_payment(sale=fully_paid, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        case = self.request_return(fully_paid, user=self.secretary)
        self.assertEqual(fully_paid.customer.outstanding_debt, Decimal("0.00"), "Pending return must not change debt.")
        self.approve_return(case, user=self.owner)
        self.assertEqual(fully_paid.customer.outstanding_debt, Decimal("0.00"))
        with self.assertRaises(ValidationError):
            record_payment(sale=fully_paid, user=self.secretary, amount=Decimal("1.00"), method=PaymentMethod.CASH)

    def test_partial_return_credit_is_allocated_original_sale_then_oldest_other_sale(self):
        first = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        second = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        record_payment(sale=first, user=self.secretary, amount=Decimal("50.00"), method=PaymentMethod.CASH)
        case = self.request_return(first, user=self.secretary)
        self.approve_return(case, user=self.owner)
        self.assertEqual(first.outstanding_balance, Decimal("0.00"))
        self.assertEqual(second.outstanding_balance, Decimal("50.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("50.00"))

    def test_momo_confirmation_rechecks_return_adjusted_balance(self):
        sale = self.make_sale(quantity=2, sale_type=SaleType.CREDIT, user=self.secretary)
        pending = record_payment(
            sale=sale, user=self.secretary, amount=Decimal("150.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="PENDING-BEFORE-RETURN",
        )
        case = self.request_return(sale, user=self.secretary)
        self.approve_return(case, user=self.owner)
        with self.assertRaises(ValidationError):
            confirm_momo_payment(payment=pending, user=self.owner, confirmation_reference="STATEMENT-TOO-MUCH")
        self.assertEqual(Payment.objects.get(pk=pending.pk).status, PaymentStatus.PENDING_CONFIRMATION)

    def test_pending_momo_reserves_customer_debt_across_other_credit_sales(self):
        from apps.sales.services import reject_momo_payment

        first = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        second = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        pending = record_payment(
            sale=first, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="CROSS-SALE-PENDING",
        )
        case = self.request_return(first, user=self.secretary)
        self.approve_return(case, user=self.owner)
        self.assertEqual(first.outstanding_balance, Decimal("0.00"))
        self.assertEqual(second.outstanding_balance, Decimal("100.00"))
        with self.assertRaises(ValidationError):
            record_payment(sale=second, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        with self.assertRaises(ValidationError):
            confirm_momo_payment(payment=pending, user=self.owner, confirmation_reference="CROSS-SALE-TOO-MUCH")
        reject_momo_payment(payment=pending, user=self.owner, reason="Return changed the invoiced balance")
        record_payment(sale=second, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))

    def test_pending_return_and_unconfirmed_or_rejected_momo_are_excluded_from_ledger(self):
        from apps.sales.services import reject_momo_payment

        sale = self.make_sale(quantity=2, sale_type=SaleType.CREDIT, user=self.secretary)
        pending = record_payment(
            sale=sale, user=self.secretary, amount=Decimal("50.00"), method=PaymentMethod.MTN_MOMO,
            momo_reference="LEDGER-PENDING",
        )
        case = self.request_return(sale, user=self.secretary)
        self.assertEqual(self.customer.outstanding_debt, Decimal("200.00"))
        reject_momo_payment(payment=pending, user=self.owner, reason="Not visible in provider statement")
        self.assertEqual(self.customer.outstanding_debt, Decimal("200.00"))
        self.approve_return(case, user=self.owner)
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))

    def test_rejected_return_does_not_reduce_customer_debt(self):
        from .services import reject_return_request

        sale = self.make_sale(sale_type=SaleType.CREDIT, user=self.employee)
        from apps.sales.services import approve_sale

        approve_sale(sale=sale, user=self.secretary)
        case = self.request_return(sale, user=self.employee)
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))
        reject_return_request(return_case=case, user=self.owner, reason="Inspection did not support this return")
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))

    def test_customer_linked_cash_return_credit_reduces_credit_debt_before_refund(self):
        cash_sale = self.make_sale(user=self.secretary, customer=self.customer)
        credit_sale = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        case = self.request_return(cash_sale, user=self.secretary)
        self.approve_return(case, user=self.owner)
        self.assertEqual(credit_sale.outstanding_balance, Decimal("0.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("0.00"))
        with self.assertRaises(ValidationError):
            create_refund_request(
                return_case=case, user=self.secretary, amount=Decimal("1.00"),
                method=PaymentMethod.CASH, reason="Credit was consumed by existing debt",
            )

    def test_confirmed_refund_consumes_return_credit_before_later_credit_sales(self):
        cash_sale = self.make_sale(user=self.secretary, customer=self.customer)
        case = self.request_return(cash_sale, user=self.secretary)
        self.approve_return(case, user=self.owner)
        request = create_refund_request(
            return_case=case, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Refund confirmed return value",
        )
        approve_refund_request(refund_request=request, user=self.owner)
        record_refund_disbursement(
            refund_request=request, user=self.owner, amount=Decimal("100.00"), method=PaymentMethod.CASH,
        )
        later_credit = self.make_sale(sale_type=SaleType.CREDIT, user=self.secretary)
        self.assertEqual(later_credit.outstanding_balance, Decimal("100.00"))
        self.assertEqual(self.customer.outstanding_debt, Decimal("100.00"))

    def test_return_models_block_bulk_create_update_and_raw_delete_shortcuts(self):
        sale = self.make_sale()
        case = self.request_return(sale)
        with self.assertRaises(ValidationError):
            ReturnCase.objects.bulk_create([ReturnCase(sale=sale, reason="Bypass", requested_by=self.employee)])
        with self.assertRaises(ValidationError):
            ReturnCase.objects.bulk_update([case], ["reason"])
        with self.assertRaises(ValidationError):
            ReturnCase.objects.filter(pk=case.pk)._raw_delete("default")

    def test_return_failures_roll_back_stock_case_and_audit(self):
        sale = self.make_sale()
        case = self.request_return(sale)
        initial_stock = Product.objects.get(pk=self.product.pk).stock_quantity
        item = case.items.get()
        with patch("apps.returns.services.record_audit_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                approve_return_request(
                    return_case=case, user=self.secretary,
                    item_decisions={item.pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
                )
        self.product.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, initial_stock)
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        self.assertFalse(InventoryMovement.objects.filter(reference=case.reference).exists())

    def test_sales_and_payment_history_remain_immutable_and_return_actions_audited(self):
        sale = self.make_sale()
        payment = sale.payments.get()
        case = self.request_return(sale)
        self.approve_return(case)
        with self.assertRaises(ValidationError):
            Sale.objects.filter(pk=sale.pk).update(total=Decimal("1.00"))
        with self.assertRaises(ValidationError):
            sale.delete()
        with self.assertRaises(ValidationError):
            Payment.objects.filter(pk=payment.pk).update(amount=Decimal("1.00"))
        with self.assertRaises(ValidationError):
            payment.delete()
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_SUBMITTED", object_reference=case.reference).exists())
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_APPROVED", object_reference=case.reference).exists())
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_INVENTORY_RESTOCKED", object_reference=str(InventoryMovement.objects.get(reference=case.reference).pk)).exists())
        with self.assertRaises(ValidationError):
            ReturnCase.objects.filter(pk=case.pk).update(reason="Edited history")
        with self.assertRaises(ValidationError):
            case.delete()


class ReturnsFrontendViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="returns-ui-owner", email="returns-ui-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="returns-ui-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="returns-ui-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="returns-ui-other", password="test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Returns Search Customer", phone_number="0248001122"
        )
        category = Category.objects.create(name="Returns UI Category")
        self.product = Product.objects.create(
            name="Returns UI Console", category=category, sku="RETURNS-UI-CONSOLE",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=40, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Returns frontend test stock",
        )
        self.employee_case = self._return_for(self.employee, kind=ReturnKind.EXCHANGE)
        self.other_employee_case = self._return_for(self.other_employee)
        customer_sale = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        self.customer_case = create_return_request(
            sale=customer_sale, user=self.secretary, reason="Customer search return",
            items=[{"sale_item": customer_sale.items.get(), "quantity": 1}],
        )

    def _return_for(self, user, *, kind=ReturnKind.RETURN):
        sale = create_sale(
            user=user, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        return create_return_request(
            sale=sale, user=user, kind=kind, reason="Frontend return request",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(reverse("returns:list"))
        self.assertRedirects(response, f"/accounts/login/?next={reverse('returns:list')}")

    def test_owner_and_secretary_can_access_workspace(self):
        for user in (self.owner, self.secretary):
            self.client.force_login(user)
            response = self.client.get(reverse("returns:list"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.employee_case.reference)
            self.assertContains(response, self.other_employee_case.reference)
            self.client.logout()

    def test_employee_list_and_detail_are_scoped_to_own_sales(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("returns:list"))
        self.assertContains(response, self.employee_case.reference)
        self.assertNotContains(response, self.other_employee_case.reference)
        self.assertNotContains(response, self.customer_case.reference)

        own_detail = self.client.get(reverse("returns:detail", args=[self.employee_case.pk]))
        self.assertEqual(own_detail.status_code, 200)
        self.assertContains(own_detail, self.product.name)
        other_detail = self.client.get(reverse("returns:detail", args=[self.other_employee_case.pk]))
        self.assertEqual(other_detail.status_code, 404)

    def test_invalid_or_unscoped_return_detail_returns_404(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("returns:detail", args=[999999])).status_code, 404)

    def test_search_and_status_type_filters(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("returns:list"), {"q": "Returns Search Customer"})
        self.assertContains(response, self.customer_case.reference)
        self.assertNotContains(response, self.employee_case.reference)

        response = self.client.get(reverse("returns:list"), {"q": self.employee_case.sale.reference})
        self.assertContains(response, self.employee_case.reference)
        self.assertNotContains(response, self.other_employee_case.reference)

        response = self.client.get(reverse("returns:list"), {"status": ReturnStatus.PENDING_APPROVAL, "kind": ReturnKind.EXCHANGE})
        self.assertContains(response, self.employee_case.reference)
        self.assertNotContains(response, self.other_employee_case.reference)
        self.assertNotContains(response, self.customer_case.reference)

    def test_workspace_pagination_preserves_read_only_results(self):
        for _ in range(8):
            self._return_for(self.employee)
        self.client.force_login(self.owner)
        first_page = self.client.get(reverse("returns:list"))
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context["returns"].number, 1)
        self.assertTrue(first_page.context["returns"].has_next())
        second_page = self.client.get(reverse("returns:list"), {"page": 2})
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.context["returns"].number, 2)

    def test_sidebar_link_is_visible_and_active_for_all_shop_roles(self):
        for user in (self.owner, self.secretary, self.employee):
            self.client.force_login(user)
            response = self.client.get(reverse("returns:list"))
            self.assertContains(response, 'href="/returns/"')
            self.assertContains(response, "sidebar-link active")
            self.client.logout()


class ReturnSubmissionViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="return-submit-owner", email="return-submit-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="return-submit-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="return-submit-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="return-submit-other", password="test-password", role=Role.EMPLOYEE
        )
        self.category = Category.objects.create(name="Return Submission Category")
        self.product = Product.objects.create(
            name="Return Submission Accessory", category=self.category, sku="RETURN-SUBMIT",
            purchase_price=Decimal("10.00"), selling_price=Decimal("25.00"),
        )
        record_stock_movement(
            product=self.product, quantity=30, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Return submission test stock",
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Return Submission Customer", phone_number="0248332211"
        )

    def _sale(self, user, *, product=None, sale_type=SaleType.CASH, unit_identifiers=()):
        product = product or self.product
        total = product.selling_price
        return create_sale(
            user=user, sale_type=sale_type, customer=self.customer if sale_type == SaleType.CREDIT else None,
            lines=[{"product": product, "quantity": 1, "unit_identifiers": unit_identifiers}],
            initial_payment={"amount": total, "method": PaymentMethod.CASH} if sale_type == SaleType.CASH else None,
        )

    def _payload(self, sale, **overrides):
        data = {
            "kind": ReturnKind.RETURN,
            "reason": "Customer requested a return",
            f"quantity_{sale.items.get().pk}": "1",
        }
        data.update(overrides)
        return data

    def test_anonymous_user_cannot_open_or_submit_request(self):
        sale = self._sale(self.employee)
        url = reverse("returns:request_create", args=[sale.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, self._payload(sale)).status_code, 302)
        self.assertFalse(ReturnCase.objects.exists())

    def test_owner_secretary_and_employee_can_submit_visible_approved_sales(self):
        for user in (self.owner, self.secretary, self.employee):
            sale = self._sale(user)
            self.client.force_login(user)
            response = self.client.post(reverse("returns:request_create", args=[sale.pk]), self._payload(sale))
            self.assertRedirects(response, reverse("returns:detail", args=[ReturnCase.objects.latest("pk").pk]))
            self.assertEqual(ReturnCase.objects.latest("pk").requested_by, user)
            self.client.logout()

    def test_employee_cannot_submit_or_view_another_employees_sale(self):
        sale = self._sale(self.other_employee)
        self.client.force_login(self.employee)
        url = reverse("returns:request_create", args=[sale.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self._payload(sale)).status_code, 404)
        self.assertFalse(ReturnCase.objects.exists())
        select = self.client.get(reverse("returns:request_select"))
        self.assertNotContains(select, sale.reference)

    def test_pending_and_rejected_sales_are_not_returnable(self):
        pending = self._sale(self.employee, sale_type=SaleType.CREDIT)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("returns:request_create", args=[pending.pk])).status_code, 404)
        from apps.sales.services import reject_sale

        rejected = reject_sale(sale=pending, user=self.secretary, reason="Declined credit")
        self.assertEqual(self.client.post(reverse("returns:request_create", args=[rejected.pk]), self._payload(rejected)).status_code, 404)
        self.assertFalse(ReturnCase.objects.exists())

    def test_invalid_quantities_and_repeated_returns_do_not_create_extra_records(self):
        sale = self._sale(self.employee)
        self.client.force_login(self.employee)
        url = reverse("returns:request_create", args=[sale.pk])
        invalid = self.client.post(url, self._payload(sale, **{f"quantity_{sale.items.get().pk}": "2"}))
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Ensure this value is less than or equal to 1")
        self.assertFalse(ReturnCase.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="RETURN_SUBMITTED").exists())

        self.client.post(url, self._payload(sale))
        self.assertEqual(ReturnCase.objects.count(), 1)
        repeated = self.client.post(url, self._payload(sale))
        self.assertEqual(repeated.status_code, 404)
        self.assertEqual(ReturnCase.objects.count(), 1)

    def test_serial_identifier_must_match_the_exact_sale(self):
        tracked = Product.objects.create(
            name="Return Submission Phone", category=self.category, sku="RETURN-SUBMIT-PHONE",
            purchase_price=Decimal("100.00"), selling_price=Decimal("200.00"), track_individual_units=True,
        )
        record_stock_movement(
            product=tracked, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Tracked submission stock", unit_identifiers=("RETURN-IMEI-A", "RETURN-IMEI-B"),
        )
        sale = self._sale(self.employee, product=tracked, unit_identifiers=("RETURN-IMEI-A",))
        self.client.force_login(self.employee)
        item = sale.items.get()
        response = self.client.post(reverse("returns:request_create", args=[sale.pk]), self._payload(
            sale, **{f"units_{item.pk}": ["RETURN-IMEI-B"]}
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReturnCase.objects.exists())
        accepted = self.client.post(reverse("returns:request_create", args=[sale.pk]), self._payload(
            sale, **{f"units_{item.pk}": ["RETURN-IMEI-A"], "kind": ReturnKind.EXCHANGE}
        ))
        self.assertEqual(accepted.status_code, 302)
        case = ReturnCase.objects.get()
        self.assertEqual(case.kind, ReturnKind.EXCHANGE)
        self.assertEqual(case.items.get().returned_units.get().sale_item_unit.unit.identifier, "RETURN-IMEI-A")

    def test_expired_window_and_csrf_are_enforced(self):
        sale = self._sale(self.employee)
        self.client.force_login(self.employee)
        with patch("apps.returns.views.timezone.localdate", return_value=timezone.localdate() + timedelta(days=8)):
            self.assertEqual(self.client.get(reverse("returns:request_create", args=[sale.pk])).status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.employee)
        response = csrf_client.post(reverse("returns:request_create", args=[sale.pk]), self._payload(sale))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReturnCase.objects.exists())


class ReturnDecisionViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="return-decision-owner", email="return-decision-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="return-decision-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="return-decision-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.category = Category.objects.create(name="Return Decision Category")
        self.product = Product.objects.create(
            name="Return Decision Controller", category=self.category, sku="RETURN-DECISION",
            purchase_price=Decimal("30.00"), selling_price=Decimal("75.00"),
        )
        record_stock_movement(
            product=self.product, quantity=12, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Return decision test stock",
        )

    def _case(self, *, requester=None, quantity=1):
        sale = create_sale(
            user=requester or self.employee, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": quantity}],
            initial_payment={"amount": self.product.selling_price * quantity, "method": PaymentMethod.CASH},
        )
        return create_return_request(
            sale=sale, user=requester or self.employee, reason="Inspection workflow test",
            items=[{"sale_item": sale.items.get(), "quantity": quantity}],
        )

    def test_owner_and_secretary_can_inspect_and_approve(self):
        for manager in (self.owner, self.secretary):
            case = self._case()
            item = case.items.get()
            self.client.force_login(manager)
            response = self.client.post(reverse("returns:approve", args=[case.pk]), {
                f"condition_{item.pk}": ReturnCondition.GOOD,
                f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
            })
            self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
            case.refresh_from_db()
            self.assertEqual(case.status, ReturnStatus.APPROVED)
            self.assertEqual(case.items.get().condition, ReturnCondition.GOOD)
            self.assertTrue(InventoryMovement.objects.filter(reference=case.reference, movement_type=MovementType.CUSTOMER_RETURN).exists())
            self.assertTrue(AuditEvent.objects.filter(action="RETURN_APPROVED", object_reference=case.reference, actor=manager).exists())
            self.client.logout()

    def test_employees_cannot_access_or_post_decision_actions(self):
        case = self._case()
        item = case.items.get()
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("returns:approve", args=[case.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.GOOD,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        }).status_code, 403)
        self.assertEqual(self.client.post(reverse("returns:reject", args=[case.pk]), {"reason": "No"}).status_code, 403)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)

    def test_requester_cannot_approve_own_request(self):
        case = self._case(requester=self.owner)
        item = case.items.get()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.GOOD,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(response.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        self.assertFalse(InventoryMovement.objects.filter(reference=case.reference).exists())

    def test_rejection_requires_meaningful_reason_and_remains_historical(self):
        case = self._case()
        self.client.force_login(self.owner)
        invalid = self.client.post(reverse("returns:reject", args=[case.pk]), {"reason": "   "})
        self.assertEqual(invalid.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        response = self.client.post(reverse("returns:reject", args=[case.pk]), {"reason": "Item was not returned with required parts."})
        self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.REJECTED)
        self.assertEqual(case.decision_reason, "Item was not returned with required parts.")
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_REJECTED", object_reference=case.reference).exists())

    def test_invalid_inspection_restock_rejection_and_repeat_approval_leave_safe_state(self):
        case = self._case()
        item = case.items.get()
        self.client.force_login(self.owner)
        invalid_choice = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": "NOT_A_CONDITION",
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(invalid_choice.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        forbidden = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.DAMAGED,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(forbidden.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        self.assertFalse(InventoryMovement.objects.filter(reference=case.reference).exists())
        self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.GOOD,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        repeat = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.GOOD,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(InventoryMovement.objects.filter(reference=case.reference).count(), 1)

    def test_approval_requires_an_inspection_for_every_returned_item(self):
        second = Product.objects.create(
            name="Return Decision Headset", category=self.category, sku="RETURN-DECISION-HEADSET",
            purchase_price=Decimal("20.00"), selling_price=Decimal("45.00"),
        )
        record_stock_movement(
            product=second, quantity=4, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Second return decision test stock",
        )
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}, {"product": second, "quantity": 1}],
            initial_payment={"amount": Decimal("120.00"), "method": PaymentMethod.CASH},
        )
        case = create_return_request(
            sale=sale, user=self.employee, reason="Two item inspection test",
            items=[{"sale_item": item.pk, "quantity": 1} for item in sale.items.all()],
        )
        first_item = case.items.first()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{first_item.pk}": ReturnCondition.GOOD,
            f"disposition_{first_item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(response.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.PENDING_APPROVAL)
        self.assertFalse(InventoryMovement.objects.filter(reference=case.reference).exists())

    def test_rejected_request_cannot_later_be_approved_and_decision_posts_require_csrf(self):
        case = self._case()
        item = case.items.get()
        self.client.force_login(self.owner)
        self.client.post(reverse("returns:reject", args=[case.pk]), {"reason": "Rejected after inspection."})
        approval = self.client.post(reverse("returns:approve", args=[case.pk]), {
            f"condition_{item.pk}": ReturnCondition.GOOD,
            f"disposition_{item.pk}": ReturnDisposition.RESTOCK,
        })
        self.assertEqual(approval.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, ReturnStatus.REJECTED)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.secretary)
        fresh = self._case()
        fresh_item = fresh.items.get()
        self.assertEqual(csrf_client.post(reverse("returns:approve", args=[fresh.pk]), {
            f"condition_{fresh_item.pk}": ReturnCondition.GOOD,
            f"disposition_{fresh_item.pk}": ReturnDisposition.RESTOCK,
        }).status_code, 403)


class RefundRequestViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="refund-ui-owner", email="refund-ui-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="refund-ui-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="refund-ui-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="refund-ui-other", password="test-password", role=Role.EMPLOYEE
        )
        category = Category.objects.create(name="Refund UI Category")
        self.product = Product.objects.create(
            name="Refund UI Console", category=category, sku="REFUND-UI-CONSOLE",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=20, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Refund UI test stock",
        )

    def _approved_case(self, *, requester=None):
        requester = requester or self.employee
        sale = create_sale(
            user=requester, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        case = create_return_request(
            sale=sale, user=requester, reason="Refund request test",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        approver = self.secretary if requester != self.secretary else self.owner
        approve_return_request(
            return_case=case, user=approver,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        return case

    def _payload(self, **overrides):
        data = {
            "amount": "100.00", "method": PaymentMethod.CASH,
            "reason": "Customer requests a refund after the approved return.",
        }
        data.update(overrides)
        return data

    def test_employee_secretary_and_owner_can_request_eligible_refunds(self):
        for requester in (self.employee, self.secretary, self.owner):
            case = self._approved_case(requester=requester)
            self.client.force_login(requester)
            response = self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload())
            refund = RefundRequest.objects.latest("pk")
            self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
            self.assertEqual(refund.requested_by, requester)
            self.assertEqual(refund.status, RefundRequestStatus.PENDING_APPROVAL)
            self.assertFalse(refund.disbursements.exists())
            self.assertTrue(AuditEvent.objects.filter(action="REFUND_REQUESTED", object_reference=refund.reference).exists())
            self.client.logout()

    def test_only_approved_visible_returns_with_capacity_can_request_refunds(self):
        sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH, lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        pending = create_return_request(
            sale=sale, user=self.employee, reason="Pending return",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("returns:refund_request_create", args=[pending.pk])).status_code, 404)
        case = self._approved_case(requester=self.other_employee)
        self.assertEqual(self.client.get(reverse("returns:refund_request_create", args=[case.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload()).status_code, 404)

    def test_excessive_or_duplicate_refunds_are_not_created_and_reservations_are_respected(self):
        case = self._approved_case()
        self.client.force_login(self.employee)
        url = reverse("returns:refund_request_create", args=[case.pk])
        excessive = self.client.post(url, self._payload(amount="100.01"))
        self.assertEqual(excessive.status_code, 200)
        self.assertFalse(RefundRequest.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="REFUND_REQUESTED").exists())
        self.client.post(url, self._payload(amount="60.00"))
        self.assertEqual(RefundRequest.objects.count(), 1)
        second = self.client.post(url, self._payload(amount="50.00"))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(RefundRequest.objects.count(), 1)
        self.client.post(url, self._payload(amount="40.00"))
        self.assertEqual(RefundRequest.objects.count(), 2)
        self.assertEqual(sum(RefundRequest.objects.values_list("amount", flat=True)), Decimal("100.00"))

    def test_manager_approval_rechecks_live_reservations_and_audits_without_disbursement(self):
        for manager in (self.owner, self.secretary):
            case = self._approved_case()
            self.client.force_login(self.employee)
            self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload(amount="60.00"))
            refund = RefundRequest.objects.latest("pk")
            self.client.force_login(manager)
            response = self.client.post(reverse("returns:refund_approve", args=[refund.pk]))
            self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
            refund.refresh_from_db()
            self.assertEqual(refund.status, RefundRequestStatus.APPROVED)
            self.assertEqual(refund.decided_by, manager)
            self.assertFalse(refund.disbursements.exists())
            self.assertTrue(AuditEvent.objects.filter(action="REFUND_APPROVED", object_reference=refund.reference, actor=manager).exists())

    def test_requester_cannot_approve_own_refund_and_employee_cannot_decide(self):
        case = self._approved_case()
        self.client.force_login(self.employee)
        self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload())
        refund = RefundRequest.objects.get()
        self.assertEqual(self.client.post(reverse("returns:refund_approve", args=[refund.pk])).status_code, 403)
        self.client.force_login(self.owner)
        owner_case = self._approved_case(requester=self.owner)
        self.client.post(reverse("returns:refund_request_create", args=[owner_case.pk]), self._payload())
        owner_refund = RefundRequest.objects.latest("pk")
        response = self.client.post(reverse("returns:refund_approve", args=[owner_refund.pk]))
        self.assertEqual(response.status_code, 302)
        owner_refund.refresh_from_db()
        self.assertEqual(owner_refund.status, RefundRequestStatus.PENDING_APPROVAL)

    def test_refund_rejection_validation_and_repeated_decisions_are_safe(self):
        case = self._approved_case()
        self.client.force_login(self.employee)
        self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload())
        refund = RefundRequest.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse("returns:refund_reject", args=[refund.pk]), {"reason": "no"})
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.PENDING_APPROVAL)
        self.client.post(reverse("returns:refund_reject", args=[refund.pk]), {"reason": "Payment proof cannot support this refund."})
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.REJECTED)
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_REJECTED", object_reference=refund.reference).exists())
        self.client.post(reverse("returns:refund_approve", args=[refund.pk]))
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.REJECTED)

    def test_refund_decision_posts_require_csrf(self):
        case = self._approved_case()
        self.client.force_login(self.employee)
        self.client.post(reverse("returns:refund_request_create", args=[case.pk]), self._payload())
        refund = RefundRequest.objects.get()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(reverse("returns:refund_approve", args=[refund.pk])).status_code, 403)
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.PENDING_APPROVAL)


@skipUnless(connection.features.has_select_for_update, "This concurrency check requires database row locking (PostgreSQL).")
class ReturnQuantityConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_requests_cannot_reserve_more_than_purchased_quantity(self):
        owner = User.objects.create_superuser(
            username="return-race-owner", email="return-race@example.invalid", password="test-password"
        )
        employee = User.objects.create_user(
            username="return-race-employee", password="test-password", role=Role.EMPLOYEE
        )
        category = Category.objects.create(name="Return Race Category")
        product = Product.objects.create(
            name="Return Race Product", category=category, sku="RETURN-RACE",
            purchase_price=Decimal("5.00"), selling_price=Decimal("10.00"),
        )
        record_stock_movement(
            product=product, quantity=1, movement_type=MovementType.STOCK_RECEIVED,
            user=owner, reason="Concurrency test stock",
        )
        sale = create_sale(
            user=employee, sale_type=SaleType.CASH, lines=[{"product": product, "quantity": 1}],
            initial_payment={"amount": Decimal("10.00"), "method": PaymentMethod.CASH},
        )
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def submit_request():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    create_return_request(
                        sale=sale.pk,
                        user=User.objects.get(pk=employee.pk), reason="Concurrent return request",
                        items=[{"sale_item": sale.items.get().pk, "quantity": 1}],
                    )
                    result = "created"
                except ValidationError:
                    result = "reserved"
                with outcome_lock:
                    outcomes.append(result)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=submit_request) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "Concurrent return submissions did not finish.")
        self.assertCountEqual(outcomes, ["created", "reserved"])


@skipUnless(connection.features.has_select_for_update, "Payment/refund concurrency needs PostgreSQL row locking.")
class CustomerLedgerConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="ledger-race-owner", email="ledger-race-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="ledger-race-secretary", password="test-password", role=Role.SECRETARY,
        )
        self.employee = User.objects.create_user(
            username="ledger-race-employee", password="test-password", role=Role.EMPLOYEE,
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Ledger Race Customer", phone_number="0245559988",
        )
        category = Category.objects.create(name="Ledger Race Category")
        product = Product.objects.create(
            name="Ledger Race Product", category=category, sku="LEDGER-RACE",
            purchase_price=Decimal("5.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=product, quantity=5, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Concurrency fixture stock",
        )
        self.product = product

    def _concurrent(self, actions):
        barrier = threading.Barrier(len(actions))
        outcomes = []
        outcome_lock = threading.Lock()

        def run(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    action()
                    result = "accepted"
                except ValidationError:
                    result = "rejected"
                with outcome_lock:
                    outcomes.append(result)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=run, args=(action,)) for action in actions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "Concurrent ledger operation did not finish.")
        return outcomes

    def test_concurrent_repayments_cannot_collect_more_than_current_balance(self):
        sale = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        actions = [
            lambda: record_payment(sale=sale.pk, user=self.secretary, amount=Decimal("60.00"), method=PaymentMethod.CASH),
            lambda: record_payment(sale=sale.pk, user=self.owner, amount=Decimal("60.00"), method=PaymentMethod.CASH),
        ]
        self.assertCountEqual(self._concurrent(actions), ["accepted", "rejected"])
        self.assertEqual(sale.payments.filter(status=PaymentStatus.CONFIRMED).aggregate(total=Sum("amount"))["total"], Decimal("60.00"))

    def test_concurrent_refund_disbursements_cannot_exceed_approved_request(self):
        sale = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        record_payment(sale=sale, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        case = create_return_request(
            sale=sale, user=self.secretary, reason="Concurrency refund return",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        request = create_refund_request(
            return_case=case, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Concurrent refund test",
        )
        approve_refund_request(refund_request=request, user=self.owner)
        actions = [
            lambda: record_refund_disbursement(refund_request=request.pk, user=self.owner, amount=Decimal("60.00"), method=PaymentMethod.CASH),
            lambda: record_refund_disbursement(refund_request=request.pk, user=self.secretary, amount=Decimal("60.00"), method=PaymentMethod.CASH),
        ]
        self.assertCountEqual(self._concurrent(actions), ["accepted", "rejected"])
        self.assertEqual(
            request.disbursements.filter(status=RefundDisbursementStatus.CONFIRMED).aggregate(total=Sum("amount"))["total"],
            Decimal("60.00"),
        )

    def test_concurrent_momo_confirmations_confirm_a_disbursement_only_once(self):
        reviewer = User.objects.create_user(
            username="ledger-race-reviewer", password="test-password", role=Role.SECRETARY,
        )
        sale = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        record_payment(sale=sale, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        case = create_return_request(
            sale=sale, user=self.secretary, reason="Concurrent MoMo confirmation return",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        request = create_refund_request(
            return_case=case, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, reason="Concurrent MoMo refund confirmation",
        )
        approve_refund_request(refund_request=request, user=self.owner)
        disbursement = record_refund_disbursement(
            refund_request=request, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="CONCURRENT-MOMO-OUT",
        )
        actions = [
            lambda: confirm_refund_momo_disbursement(
                disbursement=disbursement.pk, user=self.owner, confirmation_reference="CONCURRENT-CONF-OWNER",
            ),
            lambda: confirm_refund_momo_disbursement(
                disbursement=disbursement.pk, user=reviewer, confirmation_reference="CONCURRENT-CONF-REVIEWER",
            ),
        ]
        self.assertCountEqual(self._concurrent(actions), ["accepted", "rejected"])
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.CONFIRMED)
        self.assertIn(disbursement.confirmation_reference, {"CONCURRENT-CONF-OWNER", "CONCURRENT-CONF-REVIEWER"})
        self.assertEqual(
            request.disbursements.filter(status=RefundDisbursementStatus.CONFIRMED).aggregate(total=Sum("amount"))["total"],
            Decimal("100.00"),
        )

    def test_concurrent_exchange_settlements_link_return_credit_only_once(self):
        original = create_sale(
            user=self.secretary, sale_type=SaleType.CASH, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        case = create_return_request(
            sale=original, user=self.secretary, kind=ReturnKind.EXCHANGE,
            reason="Concurrent exchange settlement", items=[{"sale_item": original.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        first = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        second = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        actions = [
            lambda: link_exchange_sale(return_case=case.pk, replacement_sale=first.pk, user=self.owner),
            lambda: link_exchange_sale(return_case=case.pk, replacement_sale=second.pk, user=self.secretary),
        ]
        self.assertCountEqual(self._concurrent(actions), ["accepted", "rejected"])
        link = ExchangeLink.objects.get(return_case=case)
        self.assertIn(link.replacement_sale_id, {first.pk, second.pk})
        self.assertEqual(ExchangeLink.objects.filter(return_case=case).count(), 1)

class RefundDisbursementViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="refund-disbursement-owner", email="refund-disbursement-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="refund-disbursement-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="refund-disbursement-employee", password="test-password", role=Role.EMPLOYEE
        )
        category = Category.objects.create(name="Refund Disbursement UI Category")
        self.product = Product.objects.create(
            name="Refund Disbursement UI Product", category=category, sku="REFUND-DISBURSE-UI",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=20, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Refund disbursement UI test stock",
        )

    def _approved_refund(self, *, method=PaymentMethod.CASH, amount=Decimal("100.00"), requester=None):
        requester = requester or self.employee
        sale = create_sale(
            user=requester, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        case = create_return_request(
            sale=sale, user=requester, reason="Customer requested a refund after return.",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.secretary if requester != self.secretary else self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        refund = create_refund_request(
            return_case=case, user=requester, amount=amount, method=method,
            reason="Approved refund requested after product return.",
        )
        approve_refund_request(refund_request=refund, user=self.owner if requester != self.owner else self.secretary)
        return case, refund

    @staticmethod
    def _payload(*, amount="100.00", momo_reference="", key="00000000-0000-4000-8000-000000000001"):
        return {"amount": amount, "momo_reference": momo_reference, "idempotency_key": key}

    def test_authorized_cash_disbursement_is_recorded_and_audited(self):
        case, refund = self._approved_refund()
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse("returns:refund_disbursement_create", args=[refund.pk]), self._payload(),
        )
        self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
        disbursement = RefundDisbursement.objects.get(refund_request=refund)
        self.assertEqual(disbursement.method, PaymentMethod.CASH)
        self.assertEqual(disbursement.status, RefundDisbursementStatus.CONFIRMED)
        self.assertEqual(disbursement.recorded_by, self.secretary)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("100.00"))
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.COMPLETED)
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_DISBURSEMENT_RECORDED", object_reference=disbursement.pk).exists())

    def test_momo_reference_is_required_unique_and_pending_until_independently_confirmed(self):
        case, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        self.client.force_login(self.secretary)
        url = reverse("returns:refund_disbursement_create", args=[refund.pk])
        missing = self.client.post(url, self._payload(momo_reference=""))
        self.assertEqual(missing.status_code, 200)
        self.assertFalse(RefundDisbursement.objects.exists())
        self.client.post(url, self._payload(momo_reference="OUT-REF-001"))
        disbursement = RefundDisbursement.objects.get()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("0.00"))
        self.assertEqual(refund.pending_disbursement_amount, Decimal("100.00"))
        self.assertEqual(self.client.post(
            reverse("returns:refund_momo_confirm", args=[disbursement.pk]),
            {"confirmation_reference": "CONFIRM-001"},
        ).status_code, 302)
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("returns:refund_momo_confirm", args=[disbursement.pk]),
            {"confirmation_reference": "CONFIRM-001"},
        )
        self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.CONFIRMED)
        self.assertEqual(disbursement.decided_by, self.owner)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("100.00"))
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_MOMO_CONFIRMED", object_reference=disbursement.pk).exists())

    def test_duplicate_momo_reference_and_overpayment_are_rejected(self):
        _, first_refund = self._approved_refund(method=PaymentMethod.MTN_MOMO, amount=Decimal("50.00"))
        self.client.force_login(self.secretary)
        self.client.post(
            reverse("returns:refund_disbursement_create", args=[first_refund.pk]),
            self._payload(amount="50.00", momo_reference="DUPLICATE-OUT", key="00000000-0000-4000-8000-000000000002"),
        )
        _, second_refund = self._approved_refund(method=PaymentMethod.MTN_MOMO, amount=Decimal("50.00"))
        duplicate = self.client.post(
            reverse("returns:refund_disbursement_create", args=[second_refund.pk]),
            self._payload(amount="50.00", momo_reference="DUPLICATE-OUT", key="00000000-0000-4000-8000-000000000003"),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(RefundDisbursement.objects.filter(refund_request=second_refund).count(), 0)
        _, cash_refund = self._approved_refund(amount=Decimal("100.00"))
        excessive = self.client.post(
            reverse("returns:refund_disbursement_create", args=[cash_refund.pk]), self._payload(amount="100.01"),
        )
        self.assertEqual(excessive.status_code, 200)
        self.assertFalse(RefundDisbursement.objects.filter(refund_request=cash_refund).exists())

    def test_partial_disbursements_complete_only_after_full_approved_amount(self):
        _, refund = self._approved_refund(amount=Decimal("100.00"))
        self.client.force_login(self.owner)
        url = reverse("returns:refund_disbursement_create", args=[refund.pk])
        self.client.post(url, self._payload(amount="40.00", key="00000000-0000-4000-8000-000000000004"))
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.APPROVED)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("40.00"))
        self.client.post(url, self._payload(amount="60.00", key="00000000-0000-4000-8000-000000000005"))
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundRequestStatus.COMPLETED)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("100.00"))

    def test_employee_is_forbidden_and_only_approved_refunds_can_be_disbursed(self):
        _, refund = self._approved_refund()
        self.client.force_login(self.employee)
        url = reverse("returns:refund_disbursement_create", args=[refund.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self._payload()).status_code, 403)
        refund.status = RefundRequestStatus.PENDING_APPROVAL
        from .models import allow_return_write
        with allow_return_write():
            RefundRequest.objects.filter(pk=refund.pk).update(status=RefundRequestStatus.PENDING_APPROVAL, decided_by=None, decided_at=None)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_confirmation_requires_post_csrf_and_cannot_repeat(self):
        _, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        disbursement = record_refund_disbursement(
            refund_request=refund, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="POST-ONLY-REF",
        )
        url = reverse("returns:refund_momo_confirm", args=[disbursement.pk])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(url, {"confirmation_reference": "CSRF-CONF"}).status_code, 403)
        self.client.post(url, {"confirmation_reference": "POST-CONF"})
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.CONFIRMED)
        self.client.post(url, {"confirmation_reference": "SECOND-CONF"})
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.confirmation_reference, "POST-CONF")
        disbursement.amount = Decimal("1.00")
        with self.assertRaises(ValidationError):
            disbursement.save()
        with self.assertRaises(ValidationError):
            disbursement.delete()

    def test_manager_can_reject_pending_momo_refund_and_release_its_reservation(self):
        case, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        disbursement = record_refund_disbursement(
            refund_request=refund, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="REJECT-PENDING-MOMO",
        )
        url = reverse("returns:refund_momo_reject", args=[disbursement.pk])

        self.client.force_login(self.employee)
        self.assertEqual(self.client.post(url, {"reason": "Provider declined transfer."}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(url, {"reason": "Provider declined transfer."}).status_code, 403)

        short = self.client.post(url, {"reason": "no"})
        self.assertRedirects(short, reverse("returns:detail", args=[case.pk]))
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)

        self.client.force_login(self.secretary)
        self.client.post(url, {"reason": "Provider declined transfer."})
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)

        self.client.force_login(self.owner)
        response = self.client.post(url, {"reason": "Provider declined transfer."})
        self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
        disbursement.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.REJECTED)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("0.00"))
        self.assertEqual(refund.pending_disbursement_amount, Decimal("0.00"))
        self.assertEqual(refund.status, RefundRequestStatus.APPROVED)
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_MOMO_REJECTED", object_reference=disbursement.pk).exists())

        replacement = record_refund_disbursement(
            refund_request=refund, user=self.secretary, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="REPLACEMENT-PENDING-MOMO",
        )
        self.assertEqual(replacement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)

    def test_owner_and_secretary_can_reject_each_others_pending_momo_refunds(self):
        for recorder, reviewer, reference in (
            (self.owner, self.secretary, "OWNER-RECORDED-MOMO"),
            (self.secretary, self.owner, "SECRETARY-RECORDED-MOMO"),
        ):
            _, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
            disbursement = record_refund_disbursement(
                refund_request=refund, user=recorder, amount=Decimal("100.00"),
                method=PaymentMethod.MTN_MOMO, momo_reference=reference,
            )
            self.client.force_login(reviewer)
            response = self.client.post(
                reverse("returns:refund_momo_reject", args=[disbursement.pk]),
                {"reason": "Provider rejected the transfer."},
            )
            self.assertEqual(response.status_code, 302)
            disbursement.refresh_from_db()
            self.assertEqual(disbursement.status, RefundDisbursementStatus.REJECTED)

    def test_owner_sees_momo_disbursement_link_for_approved_refund_with_balance(self):
        case, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("returns:detail", args=[case.pk]))
        self.assertContains(response, "Record MTN Mobile Money")
        self.assertContains(response, reverse("returns:refund_disbursement_create", args=[refund.pk]))

    def test_secretary_sees_momo_disbursement_link_for_approved_refund_with_balance(self):
        case, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        self.client.force_login(self.secretary)
        response = self.client.get(reverse("returns:detail", args=[case.pk]))
        self.assertContains(response, "Record MTN Mobile Money")
        self.assertContains(response, reverse("returns:refund_disbursement_create", args=[refund.pk]))

    def test_employee_cannot_see_disbursement_link(self):
        case, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        self.client.force_login(self.employee)
        response = self.client.get(reverse("returns:detail", args=[case.pk]))
        self.assertNotContains(response, "Record MTN Mobile Money")
        self.assertNotContains(response, reverse("returns:refund_disbursement_create", args=[refund.pk]))

    def test_disbursement_link_is_hidden_for_pending_rejected_and_fully_disbursed_refunds(self):
        case, pending = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        from .models import allow_return_write
        with allow_return_write():
            RefundRequest.objects.filter(pk=pending.pk).update(
                status=RefundRequestStatus.PENDING_APPROVAL, decided_by=None, decided_at=None,
            )
        self.client.force_login(self.owner)
        pending_response = self.client.get(reverse("returns:detail", args=[case.pk]))
        self.assertNotContains(pending_response, reverse("returns:refund_disbursement_create", args=[pending.pk]))

        rejected_case, rejected = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        with allow_return_write():
            RefundRequest.objects.filter(pk=rejected.pk).update(
                status=RefundRequestStatus.PENDING_APPROVAL, decided_by=None, decided_at=None,
            )
        reject_refund_request(
            refund_request=rejected, user=self.secretary, reason="Refund proof was not sufficient.",
        )
        rejected_response = self.client.get(reverse("returns:detail", args=[rejected_case.pk]))
        self.assertNotContains(rejected_response, reverse("returns:refund_disbursement_create", args=[rejected.pk]))

        completed_case, completed = self._approved_refund()
        record_refund_disbursement(
            refund_request=completed, user=self.owner, amount=Decimal("100.00"), method=PaymentMethod.CASH,
        )
        completed_response = self.client.get(reverse("returns:detail", args=[completed_case.pk]))
        self.assertNotContains(completed_response, reverse("returns:refund_disbursement_create", args=[completed.pk]))

    def test_momo_disbursement_form_requires_reference_and_rejects_amount_above_remaining_balance(self):
        _, refund = self._approved_refund(method=PaymentMethod.MTN_MOMO)
        self.client.force_login(self.owner)
        url = reverse("returns:refund_disbursement_create", args=[refund.pk])
        response = self.client.get(url)
        self.assertContains(response, "MTN MoMo transaction reference")
        self.assertContains(response, 'max="100.00"', html=False)

        missing_reference = self.client.post(url, self._payload(momo_reference=""))
        self.assertEqual(missing_reference.status_code, 200)
        self.assertContains(missing_reference, "This field is required.")
        self.assertFalse(RefundDisbursement.objects.filter(refund_request=refund).exists())

        excessive = self.client.post(url, self._payload(amount="100.01", momo_reference=""))
        self.assertEqual(excessive.status_code, 200)
        self.assertContains(excessive, "The amount exceeds the remaining approved refund amount.")
        self.assertFalse(RefundDisbursement.objects.filter(refund_request=refund).exists())

class ExchangeSettlementViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="exchange-ui-owner", email="exchange-ui-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="exchange-ui-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="exchange-ui-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Exchange UI Customer", phone_number="0249012345",
        )
        self.other_customer = create_customer(
            user=self.secretary, full_name="Other Exchange Customer", phone_number="0249012346",
        )
        category = Category.objects.create(name="Exchange UI Category")
        self.product = Product.objects.create(
            name="Exchange UI Console", category=category, sku="EXCHANGE-UI-CONSOLE",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=20, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Exchange UI test stock",
        )

    def _approved_exchange(self, *, customer=None):
        customer = customer or self.customer
        original = create_sale(
            user=self.secretary, sale_type=SaleType.CASH, customer=customer,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        case = create_return_request(
            sale=original, user=self.secretary, kind=ReturnKind.EXCHANGE,
            reason="Customer selected an exchange.", items=[{"sale_item": original.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        return case

    def _replacement(self, *, customer=None, quantity=1, status=SaleStatus.APPROVED):
        replacement = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=customer or self.customer,
            lines=[{"product": self.product, "quantity": quantity}],
        )
        if status != SaleStatus.APPROVED:
            from apps.sales.models import allow_sale_mutations
            with allow_sale_mutations():
                Sale.objects.filter(pk=replacement.pk).update(status=status)
        return replacement

    def test_manager_can_settle_same_value_exchange_and_view_live_summary(self):
        case = self._approved_exchange()
        replacement = self._replacement()
        self.client.force_login(self.owner)
        url = reverse("returns:exchange_settlement", args=[case.pk])
        response = self.client.post(url, {"replacement_sale": replacement.pk})
        self.assertRedirects(response, reverse("returns:detail", args=[case.pk]))
        link = ExchangeLink.objects.get(return_case=case)
        self.assertEqual(link.replacement_sale, replacement)
        self.assertEqual(replacement.outstanding_balance, Decimal("0.00"))
        self.assertTrue(AuditEvent.objects.filter(action="EXCHANGE_LINKED", object_reference=link.pk, actor=self.owner).exists())
        detail = self.client.get(reverse("returns:detail", args=[case.pk]))
        self.assertContains(detail, "Same value")
        self.assertContains(detail, replacement.reference)
        self.assertContains(detail, "GH₵100.00")

    def test_higher_and_lower_replacement_amounts_use_live_ledger_and_refund_capacity(self):
        higher_case = self._approved_exchange()
        higher = self._replacement(quantity=2)
        self.client.force_login(self.owner)
        self.client.post(reverse("returns:exchange_settlement", args=[higher_case.pk]), {"replacement_sale": higher.pk})
        self.assertEqual(higher.outstanding_balance, Decimal("100.00"))
        record_payment(sale=higher, user=self.secretary, amount=Decimal("40.00"), method=PaymentMethod.CASH)
        self.assertEqual(higher.outstanding_balance, Decimal("60.00"))

        lower_product = Product.objects.create(
            name="Exchange UI Lower Product", category=self.product.category, sku="EXCHANGE-UI-LOWER",
            purchase_price=Decimal("20.00"), selling_price=Decimal("50.00"),
        )
        record_stock_movement(
            product=lower_product, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Exchange lower replacement stock",
        )
        lower_customer = create_customer(
            user=self.secretary, full_name="Lower Exchange Customer", phone_number="0249012347",
        )
        lower_case = self._approved_exchange(customer=lower_customer)
        lower = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=lower_customer,
            lines=[{"product": lower_product, "quantity": 1}],
        )
        self.client.post(reverse("returns:exchange_settlement", args=[lower_case.pk]), {"replacement_sale": lower.pk})
        self.assertEqual(lower.outstanding_balance, Decimal("0.00"))
        self.assertEqual(self.client.get(reverse("returns:detail", args=[lower_case.pk])).status_code, 200)
        request = create_refund_request(
            return_case=lower_case, user=self.secretary, amount=Decimal("50.00"),
            method=PaymentMethod.CASH, reason="Residual credit from lower value exchange.",
        )
        self.assertEqual(request.amount, Decimal("50.00"))

    def test_employee_is_forbidden_and_invalid_replacement_sales_cannot_be_linked(self):
        case = self._approved_exchange()
        replacement = self._replacement()
        url = reverse("returns:exchange_settlement", args=[case.pk])
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"replacement_sale": replacement.pk}).status_code, 403)

        self.client.force_login(self.owner)
        another_customer_sale = self._replacement(customer=self.other_customer)
        invalid = self.client.post(url, {"replacement_sale": another_customer_sale.pk})
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(ExchangeLink.objects.filter(return_case=case).exists())
        record_payment(sale=replacement, user=self.secretary, amount=Decimal("1.00"), method=PaymentMethod.MTN_MOMO, momo_reference="EXCHANGE-PENDING-MOMO")
        pending_invalid = self.client.post(url, {"replacement_sale": replacement.pk})
        self.assertEqual(pending_invalid.status_code, 200)
        self.assertFalse(ExchangeLink.objects.filter(return_case=case).exists())

    def test_unapproved_return_or_replacement_and_repeat_link_are_rejected(self):
        original = create_sale(
            user=self.secretary, sale_type=SaleType.CASH, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": Decimal("100.00"), "method": PaymentMethod.CASH},
        )
        pending_case = create_return_request(
            sale=original, user=self.secretary, kind=ReturnKind.EXCHANGE,
            reason="Pending exchange", items=[{"sale_item": original.items.get(), "quantity": 1}],
        )
        replacement = self._replacement()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("returns:exchange_settlement", args=[pending_case.pk])).status_code, 404)
        case = self._approved_exchange()
        self.client.post(reverse("returns:exchange_settlement", args=[case.pk]), {"replacement_sale": replacement.pk})
        self.assertEqual(self.client.get(reverse("returns:exchange_settlement", args=[case.pk])).status_code, 404)
        with self.assertRaises(ValidationError):
            link_exchange_sale(return_case=case, replacement_sale=replacement, user=self.owner)

class ReturnsFinalIntegrationTests(TestCase):
    """End-to-end cross-module checks for the completed returns workflow."""

    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="returns-final-owner", email="returns-final-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="returns-final-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="returns-final-employee", password="test-password", role=Role.EMPLOYEE
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Final Integration Customer", phone_number="0247001234",
        )
        category = Category.objects.create(name="Final Integration Category")
        self.product = Product.objects.create(
            name="Final Integration Console", category=category, sku="FINAL-INTEGRATION-CONSOLE",
            purchase_price=Decimal("50.00"), selling_price=Decimal("100.00"),
        )
        record_stock_movement(
            product=self.product, quantity=20, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Final integration fixture stock",
        )

    def _approved_return(self, *, kind=ReturnKind.RETURN, customer=None, sale_type=SaleType.CASH):
        customer = customer or self.customer
        initial = {"amount": Decimal("100.00"), "method": PaymentMethod.CASH} if sale_type == SaleType.CASH else None
        sale = create_sale(
            user=self.employee, sale_type=sale_type, customer=customer,
            lines=[{"product": self.product, "quantity": 1}], initial_payment=initial,
        )
        if sale.status == SaleStatus.PENDING_APPROVAL:
            approve_sale(sale=sale, user=self.secretary)
        case = create_return_request(
            sale=sale, user=self.employee, kind=kind, reason="Final integration return",
            items=[{"sale_item": sale.items.get(), "quantity": 1}],
        )
        approve_return_request(
            return_case=case, user=self.owner,
            item_decisions={case.items.get().pk: {"condition": ReturnCondition.GOOD, "disposition": ReturnDisposition.RESTOCK}},
        )
        return sale, case

    def test_cash_return_refund_workflow_updates_stock_and_audit_without_mutating_sale(self):
        sale, case = self._approved_return()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 20)
        refund = create_refund_request(
            return_case=case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.CASH, reason="Confirmed incoming cash supports refund.",
        )
        approve_refund_request(refund_request=refund, user=self.secretary)
        disbursement = record_refund_disbursement(
            refund_request=refund, user=self.owner, amount=Decimal("100.00"), method=PaymentMethod.CASH,
        )
        refund.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(disbursement.status, RefundDisbursementStatus.CONFIRMED)
        self.assertEqual(refund.status, RefundRequestStatus.COMPLETED)
        self.assertEqual(sale.total, Decimal("100.00"))
        self.assertTrue(InventoryMovement.objects.filter(reference=case.reference, movement_type=MovementType.CUSTOMER_RETURN).exists())
        self.assertTrue(AuditEvent.objects.filter(action="RETURN_APPROVED", object_reference=case.reference).exists())
        self.assertTrue(AuditEvent.objects.filter(action="REFUND_DISBURSEMENT_RECORDED", object_reference=disbursement.pk).exists())

    def test_momo_refund_only_affects_confirmed_disbursement_after_independent_confirmation(self):
        _, case = self._approved_return()
        refund = create_refund_request(
            return_case=case, user=self.employee, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, reason="Customer asked for MTN MoMo refund.",
        )
        approve_refund_request(refund_request=refund, user=self.secretary)
        disbursement = record_refund_disbursement(
            refund_request=refund, user=self.owner, amount=Decimal("100.00"),
            method=PaymentMethod.MTN_MOMO, momo_reference="FINAL-OUT-MOMO",
        )
        self.assertEqual(disbursement.status, RefundDisbursementStatus.PENDING_CONFIRMATION)
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("0.00"))
        with self.assertRaises(PermissionDenied):
            confirm_refund_momo_disbursement(
                disbursement=disbursement, user=self.owner, confirmation_reference="FINAL-OUT-CONFIRM",
            )
        confirm_refund_momo_disbursement(
            disbursement=disbursement, user=self.secretary, confirmation_reference="FINAL-OUT-CONFIRM",
        )
        refund.refresh_from_db()
        self.assertEqual(refund.confirmed_disbursed_amount, Decimal("100.00"))
        self.assertEqual(refund.status, RefundRequestStatus.COMPLETED)

    def test_exchange_reserves_credit_until_linked_then_uses_live_ledger_for_debt_and_residual_refund(self):
        _, case = self._approved_return(kind=ReturnKind.EXCHANGE)
        self.assertEqual(case.debt_credit_applied, Decimal("0.00"))
        self.assertEqual(case.refund_credit_available, Decimal("0.00"))
        replacement = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 2}],
        )
        link_exchange_sale(return_case=case, replacement_sale=replacement, user=self.owner)
        self.assertEqual(replacement.outstanding_balance, Decimal("100.00"))
        record_payment(sale=replacement, user=self.secretary, amount=Decimal("100.00"), method=PaymentMethod.CASH)
        self.assertEqual(replacement.outstanding_balance, Decimal("0.00"))

        lower_customer = create_customer(
            user=self.secretary, full_name="Final Lower Customer", phone_number="0247001235",
        )
        lower_product = Product.objects.create(
            name="Final Lower Replacement", category=self.product.category, sku="FINAL-LOWER",
            purchase_price=Decimal("20.00"), selling_price=Decimal("50.00"),
        )
        record_stock_movement(
            product=lower_product, quantity=2, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Final lower replacement fixture",
        )
        _, lower_case = self._approved_return(kind=ReturnKind.EXCHANGE, customer=lower_customer)
        lower_replacement = create_sale(
            user=self.secretary, sale_type=SaleType.CREDIT, customer=lower_customer,
            lines=[{"product": lower_product, "quantity": 1}],
        )
        link_exchange_sale(return_case=lower_case, replacement_sale=lower_replacement, user=self.owner)
        self.assertEqual(lower_replacement.outstanding_balance, Decimal("0.00"))
        residual = create_refund_request(
            return_case=lower_case, user=self.employee, amount=Decimal("50.00"),
            method=PaymentMethod.CASH, reason="Residual lower-exchange credit is refundable.",
        )
        self.assertEqual(residual.amount, Decimal("50.00"))
