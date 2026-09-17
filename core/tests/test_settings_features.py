import csv
import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import AccountDeletionLog, DataExportLog, Module
from core.views import available_personas


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", BILLING_MOCK_MODE=True)
class SettingsFeatureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("settings-owner", "owner@example.com", "test-password")
        self.other = get_user_model().objects.create_user("other-settings-owner", "other@example.com", "test-password")
        self.client.force_login(self.user)

    def save(self, **data):
        return self.client.post(reverse("core:update_settings"), data, follow=True)

    def test_theme_saves_profile_session_and_browser_override(self):
        for theme in ("light", "dark"):
            response = self.save(action="theme", theme=theme)
            self.user.profile.refresh_from_db()
            self.assertEqual(self.user.profile.theme, theme)
            self.assertEqual(self.client.session["theme"], theme)
            self.assertContains(response, f"const override = '{theme}'")
            self.assertNotIn("theme_override", self.client.session)

    def test_ajax_theme_saves_without_redirect(self):
        response = self.client.post(reverse("core:update_settings"), {"action": "theme", "theme": "light"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json(), {"ok": True, "theme": "light"})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, "light")

    def test_invalid_theme_does_not_overwrite_preference(self):
        original = self.user.profile.theme
        response = self.save(action="theme", theme="invalid")
        self.assertContains(response, "Choose a valid theme")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, original)

    def test_settings_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("core:update_settings"), {"action": "theme", "theme": "dark"})
        self.assertEqual(response.status_code, 403)

    def test_legacy_theme_toggle_uses_saved_account_theme(self):
        original = self.user.profile.theme
        response = self.client.post(reverse("core:toggle_theme"), follow=True)
        self.user.profile.refresh_from_db()
        self.assertNotEqual(self.user.profile.theme, original)
        self.assertContains(response, f"const override = '{self.user.profile.theme}'")

    def test_every_persona_saves_and_renders_selected(self):
        for persona in available_personas():
            response = self.save(action="persona", persona=persona["id"])
            self.user.profile.refresh_from_db()
            self.assertEqual(self.user.profile.ai_persona, persona["id"])
            self.assertContains(response, f'value="{persona["id"]}" selected')

    def test_invalid_persona_does_not_reset_current_selection(self):
        self.save(action="persona", persona="coach")
        response = self.save(action="persona", persona="invalid")
        self.assertContains(response, "Choose a valid assistant persona")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.ai_persona, "coach")

    def test_celebrations_toggle_both_directions(self):
        for enabled in (False, True):
            data = {"action": "milestones"}
            if enabled:
                data["milestone_effects"] = "on"
            response = self.save(**data)
            self.assertEqual(response.status_code, 200)
            self.user.profile.refresh_from_db()
            self.assertEqual(self.user.profile.milestone_effects_enabled, enabled)

    def test_unknown_action_changes_nothing(self):
        response = self.save(action="unknown")
        self.assertContains(response, "Unknown settings action")

    def test_settings_require_login_and_post_for_mutations(self):
        self.assertEqual(self.client.get(reverse("core:update_settings")).status_code, 405)
        self.client.logout()
        for name in ("settings", "privacy_dashboard", "download_personal_data", "export_data"):
            response = self.client.get(reverse(f"core:{name}"))
            self.assertEqual(response.status_code, 302)
            self.assertIn("login", response.url)

    def test_csv_preserves_zero_grade_and_excludes_other_users(self):
        Module.objects.create(user=self.user, name="Zero mark", credits=20, grade_percent=0)
        Module.objects.create(user=self.other, name="Private result", credits=20, grade_percent=90)
        response = self.client.get(reverse("core:export_data"))
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(float(rows[1][3]), 0)
        self.assertEqual(len(rows), 2)
        self.assertTrue(DataExportLog.objects.filter(user=self.user, format="csv").exists())

    def test_json_export_ownership_and_download_headers(self):
        Module.objects.create(user=self.user, name="Mine", credits=20)
        Module.objects.create(user=self.other, name="Not mine", credits=20)
        response = self.client.get(reverse("core:download_personal_data"))
        self.assertEqual([m["name"] for m in response.json()["modules"]], ["Mine"])
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertContains(self.client.get(reverse("core:privacy_dashboard")), "JSON")

    def test_feedback_sends_with_user_and_topic(self):
        response = self.client.post(reverse("core:settings_support"), {"category": "feedback", "subject": "Feature idea", "message": "Add a calendar", "topic": "Feature request"}, follow=True)
        self.assertContains(response, "Your message has been submitted")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("owner@example.com", mail.outbox[0].body)
        self.assertIn("Feature request", mail.outbox[0].body)

    def test_bug_report_attaches_screenshot(self):
        upload = SimpleUploadedFile("screen.png", b"test screenshot", content_type="image/png")
        response = self.client.post(reverse("core:settings_support"), {"category": "bug", "subject": "UI problem", "message": "Test detail", "severity": "High", "screenshot": upload}, follow=True)
        self.assertContains(response, "Your message has been submitted")
        self.assertEqual(len(mail.outbox[0].attachments), 1)

    def test_missing_support_fields_or_bug_attachment_do_not_send(self):
        for data in ({"subject": "Missing details"}, {"category": "bug", "subject": "Bug", "message": "Details"}):
            response = self.client.post(reverse("core:settings_support"), data, follow=True)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    @patch("core.views.EmailMessage.send", side_effect=RuntimeError("mail unavailable"))
    def test_mail_failure_shows_error_not_false_success(self, send):
        response = self.client.post(reverse("core:settings_support"), {"subject": "Feedback", "message": "Details"}, follow=True)
        self.assertContains(response, "couldn&#x27;t send", html=False)
        self.assertNotContains(response, "Your message has been submitted")

    def test_settings_destinations_render(self):
        for name in ("settings", "privacy_dashboard", "help", "whats_new", "how_it_works", "delete_account", "manage_subscription"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"core:{name}"), follow=True)
                self.assertEqual(response.status_code, 200)

    def test_account_deletion_only_deletes_disposable_owner(self):
        Module.objects.create(user=self.user, name="Disposable", credits=20)
        user_id = self.user.pk
        self.client.get(reverse("core:delete_account"))
        self.assertTrue(get_user_model().objects.filter(pk=user_id).exists())
        response = self.client.post(reverse("core:delete_account"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertFalse(Module.objects.filter(user_id=user_id).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.other.pk).exists())
        self.assertTrue(AccountDeletionLog.objects.filter(user_id=user_id).exists())

    @patch("core.views._cancel_active_subscription", return_value=(False, None, "Cancellation failed", None))
    def test_failed_subscription_cancellation_preserves_account(self, cancel):
        response = self.client.post(reverse("core:delete_account"))
        self.assertContains(response, "Cancellation failed")
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
