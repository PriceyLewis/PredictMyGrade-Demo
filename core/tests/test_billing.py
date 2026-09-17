from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import BillingEventLog


@override_settings(BILLING_MOCK_MODE=True)
class MockBillingFlowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="demo-user",
            email="demo@example.com",
            password="pass1234",
        )
        self.client.force_login(self.user)

    def test_mock_checkout_returns_only_local_success_url(self):
        response = self.client.post(
            reverse("core:create_checkout_session_default"),
            {"plan_type": "yearly"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["mock"])
        self.assertIn(reverse("core:payment_success"), payload["checkout_url"])
        self.assertIn("plan_type=yearly", payload["checkout_url"])

        parsed = urlparse(payload["checkout_url"])
        self.assertEqual(parsed.netloc, "testserver")
        self.assertNotIn("stripe", payload["checkout_url"].lower())

    def test_mock_payment_success_upgrades_user_without_real_payment(self):
        response = self.client.get(reverse("core:payment_success"), {"plan_type": "monthly"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No card was charged")

        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.is_premium)
        self.assertEqual(self.user.profile.plan_type, "premium")
        self.assertTrue(self.user.profile.stripe_customer_id.startswith("mock_cus_"))

        completed = BillingEventLog.objects.filter(
            user=self.user,
            event="upgrade",
            reason="mock_checkout_completed",
        ).first()
        self.assertIsNotNone(completed)
        self.assertEqual(completed.metadata.get("plan_type"), "monthly")
        self.assertTrue(completed.metadata.get("mock"))

    def test_mock_cancel_subscription_downgrades_immediately(self):
        profile = self.user.profile
        profile.set_premium(True)
        profile.stripe_customer_id = "mock_cus_existing"
        profile.save(update_fields=["stripe_customer_id"])

        response = self.client.post(reverse("core:cancel_subscription"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "canceled")

        profile.refresh_from_db()
        self.assertFalse(profile.is_premium)
        self.assertEqual(profile.plan_type, "free")

    def test_reviewer_can_walk_free_to_premium_and_back_to_free(self):
        premium_route = reverse("core:ai_reports")
        upgrade_route = reverse("core:upgrade")

        free_response = self.client.get(premium_route)
        self.assertEqual(free_response.status_code, 302)
        self.assertEqual(free_response.url, upgrade_route)

        checkout = self.client.post(
            reverse("core:create_checkout_session_default"),
            {"plan_type": "yearly"},
        )
        self.assertEqual(checkout.status_code, 200)
        self.assertTrue(checkout.json()["mock"])

        success = self.client.get(
            reverse("core:payment_success"),
            {"plan_type": "yearly", "mock_checkout": "1"},
        )
        self.assertEqual(success.status_code, 200)

        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.is_premium)
        premium_response = self.client.get(premium_route)
        self.assertEqual(premium_response.status_code, 200)

        manage = self.client.get(reverse("core:manage_subscription"))
        self.assertEqual(manage.status_code, 200)
        self.assertContains(manage, "Switch to Demo Monthly")
        self.assertContains(manage, "Switch to Demo Yearly")
        self.assertContains(manage, "Return to Free")

        cancel = self.client.post(reverse("core:cancel_subscription"))
        self.assertEqual(cancel.status_code, 200)
        self.assertTrue(cancel.json()["ok"])

        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_premium)
        self.assertEqual(self.user.profile.plan_type, "free")

        locked_again = self.client.get(premium_route)
        self.assertEqual(locked_again.status_code, 302)
        self.assertEqual(locked_again.url, upgrade_route)

    def test_invalid_mock_plan_falls_back_to_monthly(self):
        response = self.client.post(
            reverse("core:create_checkout_session_default"),
            {"plan_type": "enterprise-live"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("plan_type=monthly", response.json()["checkout_url"])
