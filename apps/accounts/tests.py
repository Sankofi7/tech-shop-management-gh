from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone
from apps.audit.models import AuditEvent, record_audit_event
from apps.customers.models import Customer
from apps.customers.services import create_customer
from apps.products.models import Category, Product
from apps.sales.models import Payment, PaymentMethod, PaymentStatus, Sale, SaleStatus, SaleType
from apps.sales.services import approve_sale, confirm_momo_payment, create_sale, record_payment, reject_momo_payment

from .admin import UserAdmin
from .models import Role
from .permissions import role_required
from .services import create_staff_account, update_staff_account


User = get_user_model()


class UserRoleTests(TestCase):
    def test_create_user_hashes_password_and_defaults_to_employee(self):
        user = User.objects.create_user(username="staff1", password="unique-test-password")
        self.assertEqual(user.role, Role.EMPLOYEE)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotEqual(user.password, "unique-test-password")
        self.assertTrue(user.check_password("unique-test-password"))

    def test_role_assignment_sets_django_access_flags(self):
        secretary = User.objects.create_user(
            username="secretary1", password="unique-test-password", role=Role.SECRETARY
        )
        owner = User.objects.create_superuser(
            username="owner1", email="owner@example.invalid", password="unique-test-password"
        )
        self.assertTrue(secretary.is_staff)
        self.assertFalse(secretary.is_superuser)
        self.assertEqual(owner.role, Role.OWNER)
        self.assertTrue(owner.is_staff)
        self.assertTrue(owner.is_superuser)

    def test_employee_cannot_be_promoted_by_setting_superuser_flag(self):
        employee = User.objects.create_user(username="staff2", password="unique-test-password")
        employee.is_superuser = True
        employee.save()
        employee.refresh_from_db()
        self.assertFalse(employee.is_superuser)
        self.assertEqual(employee.role, Role.EMPLOYEE)


class AccountAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.model_admin = UserAdmin(User, self.site)
        self.owner = User.objects.create_superuser(
            username="owner2", email="owner@example.invalid", password="unique-test-password"
        )
        self.secretary = User.objects.create_user(
            username="secretary2", password="unique-test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(username="staff3", password="unique-test-password")

    def test_only_owner_can_manage_accounts_in_admin(self):
        for user in (self.secretary, self.employee):
            request = RequestFactory().get("/admin/")
            request.user = user
            self.assertFalse(self.model_admin.has_module_permission(request))
            self.assertFalse(self.model_admin.has_add_permission(request))
            self.assertFalse(self.model_admin.has_change_permission(request))

        request = RequestFactory().get("/admin/")
        request.user = User.objects.get(pk=self.owner.pk)
        self.assertTrue(self.model_admin.has_module_permission(request))
        self.assertTrue(self.model_admin.has_add_permission(request))
        self.assertTrue(self.model_admin.has_change_permission(request))

    def test_account_creation_form_does_not_offer_owner_role(self):
        from .admin import AccountCreationForm

        form = AccountCreationForm(data={
            "username": "attempted-owner",
            "email": "new@example.invalid",
            "role": Role.OWNER,
            "password1": "Unique-Test-Password-2026!",
            "password2": "Unique-Test-Password-2026!",
        })
        self.assertFalse(form.is_valid())
        request = RequestFactory().post("/admin/accounts/user/add/")
        request.user = self.secretary
        self.assertFalse(self.model_admin.has_add_permission(request))

    def test_admin_disallows_deleting_user_accounts(self):
        request = RequestFactory().post("/admin/")
        request.user = self.owner
        self.assertFalse(self.model_admin.has_delete_permission(request, self.employee))


class ServerSideRolePermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_role_required_allows_authorized_role_and_rejects_employee(self):
        @role_required(Role.OWNER, Role.SECRETARY)
        def restricted_view(request):
            return HttpResponse("allowed")

        secretary = User.objects.create_user(
            username="secretary3", password="unique-test-password", role=Role.SECRETARY
        )
        employee = User.objects.create_user(username="staff4", password="unique-test-password")
        request = self.factory.get("/restricted/")
        request.user = secretary
        self.assertEqual(restricted_view(request).status_code, 200)
        request.user = employee
        with self.assertRaises(PermissionDenied):
            restricted_view(request)


class StaffManagementTests(TestCase):
    temporary_password = "Shop-Staff-Temp-2026!K9"

    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="staff-owner", email="staff-owner@example.invalid", password="Owner-Test-Password-2026!"
        )
        self.secretary = User.objects.create_user(
            username="staff-secretary", password="Secretary-Test-Password-2026!", role=Role.SECRETARY,
        )
        self.employee = User.objects.create_user(
            username="staff-employee", password="Employee-Test-Password-2026!", role=Role.EMPLOYEE,
        )

    def _staff_payload(self, username, role):
        return {
            "first_name": "Test", "last_name": "Staff", "username": username,
            "email": f"{username}@example.invalid", "role": role, "is_active": "on",
            "temporary_password": self.temporary_password,
            "confirm_temporary_password": self.temporary_password,
        }

    def test_owner_can_create_employee_and_secretary_with_hashed_temporary_password(self):
        self.client.force_login(self.owner)
        for username, role in (("new-staff-employee", Role.EMPLOYEE), ("new-staff-secretary", Role.SECRETARY)):
            response = self.client.post("/accounts/staff/new/", self._staff_payload(username, role))
            self.assertEqual(response.status_code, 302)
            staff = User.objects.get(username=username)
            self.assertEqual(staff.role, role)
            self.assertTrue(staff.is_active)
            self.assertTrue(staff.must_change_password)
            self.assertNotEqual(staff.password, self.temporary_password)
            self.assertTrue(staff.check_password(self.temporary_password))
            event = AuditEvent.objects.get(action="STAFF_ACCOUNT_CREATED", object_reference=str(staff.pk))
            self.assertNotIn(self.temporary_password, str(event.details))

    def test_secretary_cannot_create_staff_and_employee_cannot_access_staff_pages(self):
        payload = self._staff_payload("forbidden-staff", Role.EMPLOYEE)
        for user in (self.secretary, self.employee):
            self.client.force_login(user)
            self.assertEqual(self.client.get("/accounts/staff/").status_code, 403)
            self.assertEqual(self.client.post("/accounts/staff/new/", payload).status_code, 403)
        self.assertFalse(User.objects.filter(username="forbidden-staff").exists())

    def test_role_tampering_and_self_promotion_are_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post("/accounts/staff/new/", self._staff_payload("promoted", Role.OWNER))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="promoted").exists())
        self.client.force_login(self.employee)
        response = self.client.post(f"/accounts/staff/{self.employee.pk}/edit/", {
            "first_name": "Test", "last_name": "Staff", "email": "", "role": Role.OWNER,
        })
        self.assertEqual(response.status_code, 403)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.role, Role.EMPLOYEE)

    def test_only_last_active_owner_cannot_be_deactivated_or_demoted(self):
        self.client.force_login(self.owner)
        response = self.client.post(f"/accounts/staff/{self.owner.pk}/status/", {"active": "false"})
        self.assertEqual(response.status_code, 302)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(self.owner.role, Role.OWNER)
        with self.assertRaises(ValidationError):
            update_staff_account(actor=self.owner, target=self.owner, role=Role.EMPLOYEE)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, Role.OWNER)

    def test_owner_can_deactivate_account_and_inactive_user_cannot_authenticate(self):
        self.client.force_login(self.owner)
        response = self.client.post(f"/accounts/staff/{self.employee.pk}/status/", {"active": "false"})
        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_active)
        self.assertFalse(self.client.login(username=self.employee.username, password="Employee-Test-Password-2026!"))
        self.assertTrue(AuditEvent.objects.filter(action="STAFF_ACCOUNT_DEACTIVATED", object_reference=str(self.employee.pk)).exists())

    def test_admin_password_reset_hashes_secret_and_logs_no_password(self):
        staff = create_staff_account(
            actor=self.owner, username="reset-target", first_name="Reset", last_name="Target",
            role=Role.EMPLOYEE, password=self.temporary_password,
        )
        self.client.force_login(self.owner)
        new_password = "Reset-Staff-Temporary-2026!"
        response = self.client.post(f"/accounts/staff/{staff.pk}/password-reset/", {
            "temporary_password": new_password, "confirm_temporary_password": new_password,
        })
        self.assertEqual(response.status_code, 302)
        staff.refresh_from_db()
        self.assertTrue(staff.check_password(new_password))
        self.assertNotEqual(staff.password, new_password)
        self.assertTrue(staff.must_change_password)
        event = AuditEvent.objects.get(action="STAFF_PASSWORD_RESET", object_reference=str(staff.pk))
        self.assertNotIn(new_password, str(event.details))

    def test_temporary_password_requires_change_and_self_change_is_audited(self):
        staff = create_staff_account(
            actor=self.owner, username="forced-change", first_name="Forced", last_name="Change",
            role=Role.EMPLOYEE, password=self.temporary_password,
        )
        self.client.logout()
        self.assertTrue(self.client.login(username=staff.username, password=self.temporary_password))
        self.assertRedirects(self.client.get("/reports/"), "/accounts/password/change/")
        changed_password = "Staff-Changed-Password-2026!X3"
        response = self.client.post("/accounts/password/change/", {
            "old_password": self.temporary_password, "new_password1": changed_password,
            "new_password2": changed_password,
        })
        self.assertEqual(response.status_code, 302)
        staff.refresh_from_db()
        self.assertFalse(staff.must_change_password)
        self.assertTrue(staff.check_password(changed_password))
        self.assertTrue(AuditEvent.objects.filter(action="STAFF_PASSWORD_CHANGED", actor=staff).exists())

    def test_staff_directory_filters_and_profile_hides_audit_details(self):
        record_audit_event(
            action="STAFF_PROFILE_UPDATED", actor=self.owner, object_type="user",
            object_reference=self.employee.pk, details={"sensitive": "private token"},
        )
        self.client.force_login(self.owner)
        response = self.client.get("/accounts/staff/", {"role": Role.EMPLOYEE, "status": "active", "q": "staff-employee"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "staff-employee")
        self.assertNotContains(response, "staff-secretary")
        response = self.client.get(f"/accounts/staff/{self.employee.pk}/")
        self.assertContains(response, "Staff activity")
        self.assertNotContains(response, "private token")


class FrontendAuthenticationAndDashboardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="frontend-owner", email="frontend-owner@example.invalid", password="unique-test-password"
        )
        self.secretary = User.objects.create_user(
            username="frontend-secretary", password="unique-test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="frontend-employee", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.other_employee = User.objects.create_user(
            username="frontend-other", password="unique-test-password", role=Role.EMPLOYEE
        )
        self.category = Category.objects.create(name="Dashboard Category")
        self.product = Product.objects.create(
            name="Dashboard Console", category=self.category, sku="DASHBOARD-001",
            purchase_price="100.00", selling_price="200.00", minimum_stock_level=1,
        )
        from apps.products.models import MovementType
        from apps.products.services import record_stock_movement

        record_stock_movement(
            product=self.product, quantity=30, movement_type=MovementType.STOCK_RECEIVED,
            user=self.owner, reason="Dashboard test opening inventory",
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Dashboard Customer", phone_number="0241234567",
        )

    def _sale(self, user, *, sale_type=SaleType.CASH, status=SaleStatus.APPROVED, day=None):
        created_at_field = Sale._meta.get_field("created_at")
        previous_default = created_at_field.default
        if day is not None:
            created_at_field.default = lambda: day
        try:
            sale = create_sale(
                user=user,
                sale_type=sale_type,
                customer=self.customer if sale_type == SaleType.CREDIT else None,
                lines=[{"product": self.product, "quantity": 1}],
                initial_payment={"amount": "200.00", "method": PaymentMethod.CASH} if sale_type == SaleType.CASH else None,
            )
        finally:
            created_at_field.default = previous_default
        if sale_type == SaleType.CREDIT and user.role == Role.EMPLOYEE:
            if status == SaleStatus.APPROVED:
                approve_sale(sale=sale, user=self.secretary)
            elif status == SaleStatus.REJECTED:
                from apps.sales.services import reject_sale

                reject_sale(sale=sale, user=self.secretary, reason="Credit not approved")
        return sale

    def test_login_uses_django_auth_and_reports_invalid_credentials(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, "data-password-toggle")
        response = self.client.post("/accounts/login/", {"username": self.owner.username, "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")
        response = self.client.post("/accounts/login/", {
            "username": self.owner.username, "password": "unique-test-password",
        })
        self.assertRedirects(response, "/")
        self.client.logout()
        admin_login = self.client.get("/admin/login/")
        self.assertEqual(admin_login.status_code, 200)
        self.assertContains(admin_login, "Sign in to your account")

    def test_dashboard_requires_login_and_logout_is_posted(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/accounts/login/?next=/")
        self.client.force_login(self.employee)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Low stock alerts")
        response = self.client.post("/accounts/logout/")
        self.assertRedirects(response, "/accounts/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_navigation_features_follow_user_role(self):
        self.client.force_login(self.owner)
        owner_response = self.client.get("/")
        self.assertContains(owner_response, "Staff Management")
        self.assertContains(owner_response, "Audit Logs")

        self.client.force_login(self.secretary)
        secretary_response = self.client.get("/")
        self.assertContains(secretary_response, "Audit Logs")
        self.assertNotContains(secretary_response, "Staff Management")

        self.client.force_login(self.employee)
        employee_response = self.client.get("/")
        self.assertContains(employee_response, "Point of Sale")
        self.assertContains(employee_response, "Customers")
        self.assertContains(employee_response, "Reports")
        self.assertNotContains(employee_response, "Audit Logs")
        self.assertNotContains(employee_response, "Staff Management")

    def test_employee_dashboard_only_shows_their_own_activity(self):
        record_audit_event(
            action="SALE_CREATED", actor=self.employee, object_type="sale", object_reference="own-sale-ref",
            details={"amount_ghs": "not-rendered"},
        )
        record_audit_event(
            action="SALE_CREATED", actor=self.secretary, object_type="sale", object_reference="restricted-sale-ref",
        )
        self.client.force_login(self.employee)
        response = self.client.get("/")
        self.assertContains(response, "Sale created")
        self.assertNotContains(response, "restricted-sale-ref")
        self.assertNotContains(response, "not-rendered")

    def test_dashboard_sales_totals_exclude_pending_and_rejected_and_use_local_day(self):
        now = timezone.now()
        self._sale(self.employee, day=now)
        self._sale(self.other_employee, day=now)
        self._sale(self.employee, sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL, day=now)
        self._sale(self.employee, sale_type=SaleType.CREDIT, status=SaleStatus.REJECTED, day=now)
        self.client.force_login(self.owner)
        response = self.client.get("/")
        self.assertEqual(response.context["today_sales_total"], 400)
        self.assertEqual(response.context["today_sales_count"], 2)
        self.assertContains(response, "Expenses")
        self.assertContains(response, "Not yet available")
        self.assertNotContains(response, "Expenses</div><div class=\"summary-value\">GH₵0.00")
        self.assertNotIn("expenses_total", response.context)
        self.client.force_login(self.employee)
        response = self.client.get("/")
        self.assertEqual(response.context["today_sales_total"], 200)
        self.assertEqual(response.context["today_sales_count"], 1)

    def test_dashboard_separates_sale_revenue_from_confirmed_payments_received(self):
        self._sale(self.employee)
        self._sale(self.other_employee)
        approved_credit = self._sale(self.employee, sale_type=SaleType.CREDIT)
        record_payment(sale=approved_credit, user=self.employee, amount="25.00", method=PaymentMethod.CASH)
        confirmed_momo = record_payment(
            sale=approved_credit, user=self.employee, amount="15.00", method=PaymentMethod.MTN_MOMO,
            momo_reference="DASHBOARD-CONFIRMED-MOMO",
        )
        confirm_momo_payment(payment=confirmed_momo, user=self.secretary, confirmation_reference="CONFIRMED-TODAY")
        rejected_momo = record_payment(
            sale=approved_credit, user=self.employee, amount="10.00", method=PaymentMethod.MTN_MOMO,
            momo_reference="DASHBOARD-REJECTED-MOMO",
        )
        reject_momo_payment(payment=rejected_momo, user=self.secretary, reason="Reference not found")
        record_payment(
            sale=approved_credit, user=self.employee, amount="5.00", method=PaymentMethod.MTN_MOMO,
            momo_reference="DASHBOARD-PENDING-MOMO-2",
        )

        self.client.force_login(self.owner)
        owner_response = self.client.get("/")
        self.assertEqual(owner_response.context["today_sales_total"], 600)
        self.assertEqual(owner_response.context["payments_received_today"], 440)
        self.assertContains(owner_response, "Confirmed payments by confirmation date")

        self.client.force_login(self.employee)
        employee_response = self.client.get("/")
        self.assertEqual(employee_response.context["today_sales_total"], 400)
        self.assertEqual(employee_response.context["payments_received_today"], 240)

    def test_outstanding_debt_uses_approved_credit_and_confirmed_payments_only_once(self):
        approved = self._sale(self.employee, sale_type=SaleType.CREDIT)
        record_payment(sale=approved, user=self.employee, amount="25.00", method=PaymentMethod.CASH)
        pending_momo = record_payment(
            sale=approved, user=self.employee, amount="15.00", method=PaymentMethod.MTN_MOMO,
            momo_reference="DASHBOARD-PENDING-MOMO",
        )
        self._sale(self.employee, sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL)
        self._sale(self.employee, sale_type=SaleType.CREDIT, status=SaleStatus.REJECTED)
        self.client.force_login(self.owner)
        response = self.client.get("/")
        self.assertEqual(response.context["outstanding_debt"], 175)
        self.assertEqual(pending_momo.status, PaymentStatus.PENDING_CONFIRMATION)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get("/").context["outstanding_debt"], 175)

    def test_low_stock_excludes_inactive_and_active_customer_count(self):
        from apps.products.services import record_stock_movement
        from apps.products.models import MovementType

        record_stock_movement(product=self.product, quantity=-29, movement_type=MovementType.SUPPLIER_RETURN,
                              user=self.owner, reason="Dashboard test stock")
        Product.objects.create(
            name="Inactive Item", category=self.category, sku="DASHBOARD-INACTIVE",
            purchase_price="1.00", selling_price="2.00", minimum_stock_level=1, is_active=False,
        )
        self.client.force_login(self.owner)
        response = self.client.get("/")
        self.assertEqual(response.context["low_stock_count"], 1)
        self.assertEqual([product.sku for product in response.context["low_stock_products"]], [self.product.sku])
        self.assertEqual(response.context["active_customer_count"], 1)

    def test_seven_day_chart_has_zero_days_and_manager_and_employee_scopes(self):
        today = timezone.localtime()
        self._sale(self.employee, day=today - timezone.timedelta(days=2))
        self._sale(self.other_employee, day=today - timezone.timedelta(days=1))
        self.client.force_login(self.owner)
        owner_data = self.client.get("/").context["chart_data"]
        self.assertEqual(len(owner_data), 7)
        self.assertEqual(sum(float(point["amount"]) for point in owner_data), 400)
        self.assertTrue(any(float(point["amount"]) == 0 for point in owner_data))
        self.client.force_login(self.employee)
        employee_data = self.client.get("/").context["chart_data"]
        self.assertEqual(sum(float(point["amount"]) for point in employee_data), 200)

    def test_notifications_are_role_scoped(self):
        pending = self._sale(self.employee, sale_type=SaleType.CREDIT, status=SaleStatus.PENDING_APPROVAL)
        own_sale = self._sale(self.employee, sale_type=SaleType.CREDIT)
        other_sale = self._sale(self.other_employee, sale_type=SaleType.CREDIT)
        record_payment(sale=own_sale, user=self.employee, amount="10.00", method=PaymentMethod.MTN_MOMO,
                       momo_reference="DASHBOARD-OWN-MOMO")
        record_payment(sale=other_sale, user=self.other_employee, amount="10.00", method=PaymentMethod.MTN_MOMO,
                       momo_reference="DASHBOARD-OTHER-MOMO")
        self.client.force_login(self.secretary)
        manager_context = self.client.get("/").context
        self.assertEqual(manager_context["pending_credit_count"], 1)
        self.assertEqual(manager_context["pending_momo_count"], 2)
        self.assertContains(self.client.get("/"), "Credit sales awaiting approval")
        self.client.force_login(self.employee)
        employee_response = self.client.get("/")
        self.assertIsNone(employee_response.context["pending_credit_count"])
        self.assertEqual(employee_response.context["pending_momo_count"], 1)
        self.assertNotContains(employee_response, "Credit sales awaiting approval")
        self.assertNotContains(employee_response, "DASHBOARD-OTHER-MOMO")
        self.assertEqual(pending.status, SaleStatus.PENDING_APPROVAL)

    def test_employee_dashboard_has_no_company_financial_summary_or_other_users_activity(self):
        self._sale(self.other_employee)
        record_audit_event(action="PAYMENT_MOMO_CONFIRMED", actor=self.other_employee,
                           object_type="payment", object_reference="secret-payment-reference",
                           details={"bank_reference": "secret-detail"})
        self.client.force_login(self.employee)
        response = self.client.get("/")
        self.assertNotContains(response, "secret-payment-reference")
        self.assertNotContains(response, "secret-detail")
        self.assertNotContains(response, "frontend-other")
        self.assertContains(response, "Your sales revenue today")
