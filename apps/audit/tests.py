from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role

from .models import record_audit_event


User = get_user_model()


class AuditLogWorkspaceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="audit-owner", email="audit-owner@example.invalid", password="test-password"
        )
        self.secretary = User.objects.create_user(
            username="audit-secretary", password="test-password", role=Role.SECRETARY
        )
        self.employee = User.objects.create_user(
            username="audit-employee", password="test-password", role=Role.EMPLOYEE
        )
        record_audit_event(
            action="AUDIT_TEST_EVENT", actor=self.owner, object_type="test", object_reference="1"
        )

    def test_owner_and_secretary_use_shared_layout_for_audit_history(self):
        for user in (self.owner, self.secretary):
            self.client.force_login(user)
            response = self.client.get("/audit/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="app-sidebar"')
            self.assertContains(response, "Audit_Test_Event")
            self.assertContains(response, 'href="/audit/"')

    def test_employee_cannot_access_audit_history_or_see_its_navigation_link(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get("/audit/").status_code, 403)
        dashboard = self.client.get("/")
        self.assertNotContains(dashboard, 'href="/audit/"')
