from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from apps.accounts.models import Role
from apps.audit.models import AuditEvent
from apps.customers.services import create_customer
from apps.products.models import Category, Product
from apps.products.services import record_stock_movement
from apps.sales.models import PaymentMethod, SaleStatus, SaleType
from apps.sales.services import create_sale

from .models import ShopProfile


User = get_user_model()


class ShopSettingsAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="settings-owner", email="settings-owner@example.invalid", password="Owner-Settings-Test-2026!"
        )
        self.secretary = User.objects.create_user(
            username="settings-secretary", password="Secretary-Settings-Test-2026!", role=Role.SECRETARY,
        )
        self.employee = User.objects.create_user(
            username="settings-employee", password="Employee-Settings-Test-2026!", role=Role.EMPLOYEE,
        )

    def _payload(self, **overrides):
        data = {
            "shop_name": "Accra Tech Shop",
            "contact_phone": "+233 24 123 4567",
            "business_email": "shop@example.invalid",
            "physical_address": "Accra, Ghana",
            "notify_low_stock": "on",
            "notify_pending_credit": "on",
            "notify_pending_momo": "on",
        }
        data.update(overrides)
        return data

    def test_owner_can_create_and_update_single_profile_and_audit_changes(self):
        self.client.force_login(self.owner)
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ShopProfile.objects.exists(), "Viewing settings must not write configuration.")
        self.assertContains(response, "GHS (GH₵)")
        response = self.client.post("/settings/", self._payload())
        self.assertRedirects(response, "/settings/")
        profile = ShopProfile.objects.get(singleton_key=1)
        self.assertEqual(profile.shop_name, "Accra Tech Shop")
        self.assertTrue(profile.notify_low_stock)
        self.assertEqual(ShopProfile.objects.count(), 1)
        created_event = AuditEvent.objects.get(action="SHOP_SETTINGS_CREATED")
        self.assertEqual(created_event.actor, self.owner)
        self.assertEqual(created_event.object_reference, "1")
        self.assertIn("shop_name", created_event.details["affected_settings"])
        self.assertNotIn("shop@example.invalid", str(created_event.details))

        response = self.client.post("/settings/", self._payload(shop_name="Accra Gaming & Tech"))
        self.assertRedirects(response, "/settings/")
        profile.refresh_from_db()
        self.assertEqual(profile.shop_name, "Accra Gaming & Tech")
        update_event = AuditEvent.objects.get(action="SHOP_SETTINGS_UPDATED")
        self.assertEqual(update_event.actor, self.owner)
        self.assertIn("shop_name", update_event.details["affected_settings"])
        self.assertEqual(ShopProfile.objects.count(), 1)

    def test_secretary_can_view_read_only_but_cannot_submit_changes(self):
        ShopProfile.objects.create(singleton_key=1, shop_name="Read Only Shop")
        self.client.force_login(self.secretary)
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Read Only Shop")
        self.assertContains(response, "read-only access")
        self.assertNotContains(response, "Save settings")
        self.assertContains(response, "disabled")
        response = self.client.post("/settings/", self._payload(shop_name="Tampered Shop"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ShopProfile.objects.get().shop_name, "Read Only Shop")
        self.assertFalse(AuditEvent.objects.filter(action__startswith="SHOP_SETTINGS_").exists())

    def test_employee_is_denied_direct_get_and_post(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get("/settings/").status_code, 403)
        self.assertEqual(self.client.post("/settings/", self._payload()).status_code, 403)
        self.assertFalse(ShopProfile.objects.exists())

    def test_owner_settings_changes_require_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post("/settings/", self._payload()).status_code, 403)
        self.assertFalse(ShopProfile.objects.exists())
        get_response = csrf_client.get("/settings/")
        self.assertEqual(get_response.status_code, 200)
        token = csrf_client.cookies["csrftoken"].value
        response = csrf_client.post("/settings/", self._payload(), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ShopProfile.objects.get().shop_name, "Accra Tech Shop")

    def test_invalid_values_do_not_change_profile_or_create_audit_event(self):
        profile = ShopProfile.objects.create(singleton_key=1, shop_name="Original Shop")
        self.client.force_login(self.owner)
        response = self.client.post("/settings/", self._payload(shop_name="", business_email="not-an-email", contact_phone="call me"))
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.shop_name, "Original Shop")
        self.assertFalse(AuditEvent.objects.filter(action__startswith="SHOP_SETTINGS_").exists())

    def test_settings_cannot_be_changed_or_deleted_around_audited_workflow(self):
        profile = ShopProfile.objects.create(singleton_key=1, shop_name="Protected")
        with self.assertRaises(ValidationError):
            profile.shop_name = "Bypass"
            profile.save()
        with self.assertRaises(ValidationError):
            ShopProfile.objects.filter(pk=profile.pk).update(shop_name="Bypass")
        with self.assertRaises(ValidationError):
            profile.delete()
        self.assertEqual(ShopProfile.objects.get().shop_name, "Protected")


class DashboardPreferenceIntegrationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="alert-owner", email="alert-owner@example.invalid", password="Owner-Alerts-Test-2026!"
        )
        self.secretary = User.objects.create_user(
            username="alert-secretary", password="Secretary-Alerts-Test-2026!", role=Role.SECRETARY,
        )
        self.employee = User.objects.create_user(
            username="alert-employee", password="Employee-Alerts-Test-2026!", role=Role.EMPLOYEE,
        )
        category = Category.objects.create(name="Notification test category")
        self.product = Product.objects.create(
            name="Notification test phone", category=category, sku="NOTIFY-001",
            purchase_price="100.00", selling_price="200.00", minimum_stock_level=9,
        )
        record_stock_movement(
            product=self.product, quantity=10, movement_type="STOCK_RECEIVED",
            user=self.owner, reason="Notification test opening stock",
        )
        self.customer = create_customer(
            user=self.secretary, full_name="Notification Test Customer", phone_number="0241234567",
        )
        self.pending_credit = create_sale(
            user=self.employee, sale_type=SaleType.CREDIT, customer=self.customer,
            lines=[{"product": self.product, "quantity": 1}],
        )
        self.paid_momo_sale = create_sale(
            user=self.employee, sale_type=SaleType.CASH,
            lines=[{"product": self.product, "quantity": 1}],
            initial_payment={"amount": "200.00", "method": PaymentMethod.MTN_MOMO,
                             "momo_reference": "SETTINGS-ALERT-MOMO"},
        )

    def _save_preferences(self, **values):
        profile = ShopProfile.get_solo()
        data = {
            "shop_name": profile.shop_name or "Alert Test Shop",
            "contact_phone": profile.contact_phone,
            "business_email": profile.business_email,
            "physical_address": profile.physical_address,
            "notify_low_stock": profile.notify_low_stock,
            "notify_pending_credit": profile.notify_pending_credit,
            "notify_pending_momo": profile.notify_pending_momo,
        }
        for key, value in values.items():
            data[key] = "on" if value else ""
        self.client.force_login(self.owner)
        return self.client.post("/settings/", data)

    def test_each_preference_controls_the_dashboard_alert_it_represents(self):
        self.client.force_login(self.secretary)
        response = self.client.get("/")
        self.assertEqual(response.context["pending_credit_count"], 1)
        self.assertEqual(response.context["pending_momo_count"], 1)
        self.assertEqual(response.context["low_stock_count"], 1)
        self.assertContains(response, "Low stock alerts")
        labels = {item["label"] for item in response.context["notification_items"]}
        self.assertEqual(labels, {
            "Low-stock products", "Credit sales awaiting approval", "MoMo confirmations pending",
        })

        self._save_preferences(notify_low_stock=False)
        self.client.force_login(self.secretary)
        response = self.client.get("/")
        self.assertIsNone(response.context["low_stock_count"])
        self.assertNotContains(response, "Low stock alerts")
        self.assertNotIn("Low-stock products", {item["label"] for item in response.context["notification_items"]})

        self._save_preferences(notify_low_stock=True, notify_pending_credit=False)
        self.client.force_login(self.secretary)
        response = self.client.get("/")
        self.assertIsNone(response.context["pending_credit_count"])
        self.assertNotIn("Credit sales awaiting approval", {item["label"] for item in response.context["notification_items"]})

        self._save_preferences(notify_pending_credit=True, notify_pending_momo=False)
        self.client.force_login(self.secretary)
        response = self.client.get("/")
        self.assertIsNone(response.context["pending_momo_count"])
        self.assertNotIn("MoMo confirmations pending", {item["label"] for item in response.context["notification_items"]})
        self.assertEqual(self.pending_credit.status, SaleStatus.PENDING_APPROVAL)

    def test_employee_keeps_own_momo_visibility_and_never_sees_approval_alert(self):
        self.client.force_login(self.employee)
        response = self.client.get("/")
        self.assertIsNone(response.context["pending_credit_count"])
        self.assertEqual(response.context["pending_momo_count"], 1)
        labels = {item["label"] for item in response.context["notification_items"]}
        self.assertNotIn("Credit sales awaiting approval", labels)
        self.assertIn("MoMo confirmations pending", labels)
