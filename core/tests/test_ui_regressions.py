from pathlib import Path

from django.contrib.auth import get_user_model
from django.template import engines
from django.test import TestCase
from django.urls import reverse

from core.models import Module, PredictionSnapshot


class TemplateIntegrityTests(TestCase):
    def test_every_template_compiles(self):
        engine = engines["django"].engine
        project_root = Path(__file__).resolve().parents[2]
        template_names = set()
        for root in (project_root / "templates", project_root / "core" / "templates"):
            for template in root.rglob("*.html"):
                template_names.add(str(template.relative_to(root)))

        failures = []
        for name in sorted(template_names):
            try:
                engine.get_template(name)
            except Exception as exc:  # pragma: no cover - assertion reports exact template
                failures.append(f"{name}: {exc}")
        self.assertEqual(failures, [], "\n".join(failures))


class UiRegressionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ui-auditor", password="test-password"
        )
        self.client.force_login(self.user)

    def test_modules_page_uses_valid_levels_and_renders_empty_table(self):
        response = self.client.get(reverse("core:modules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="ALEVEL"')
        self.assertNotContains(response, 'value="COLLEGE"')
        self.assertContains(response, 'id="moduleBody"')

    def test_inline_module_payload_updates_requested_field(self):
        module = Module.objects.create(
            user=self.user, name="Original", level="UNI", credits=20, grade_percent=60
        )
        response = self.client.post(
            reverse("core:module_update", args=[module.pk]),
            {"field": "grade_percent", "value": "74.5"},
        )
        self.assertEqual(response.status_code, 200)
        module.refresh_from_db()
        self.assertEqual(module.grade_percent, 74.5)

    def test_module_api_rejects_invalid_level_and_credit_range(self):
        add_response = self.client.post(
            reverse("core:module_add"),
            {"name": "Invalid", "level": "COLLEGE", "credits": 20, "grade_percent": 70},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(add_response.status_code, 400)

        module = Module.objects.create(user=self.user, name="Valid", credits=20)
        update_response = self.client.post(
            reverse("core:module_update", args=[module.pk]),
            {"field": "credits", "value": "101"},
        )
        self.assertEqual(update_response.status_code, 400)

    def test_snapshot_history_has_serialized_chart_data(self):
        PredictionSnapshot.objects.create(
            user=self.user, average_percent=67.5, classification="2:1"
        )
        response = self.client.get(reverse("core:snapshot_history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "67.5")
        self.assertContains(response, "Snapshot History")

    def test_basic_what_if_form_calculates_outcomes(self):
        response = self.client.post(
            reverse("core:what_if_basic"),
            {"sim_mark": ["70", "60"], "sim_credits": ["20", "20"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "70.0%")
        self.assertContains(response, "65.0%")

    def test_mobile_navigation_keeps_account_controls_available(self):
        response = self.client.get(reverse("core:modules_list"))
        self.assertContains(response, "mobile-only")
        self.assertContains(response, "Log out")
        self.assertContains(response, "data-theme-toggle")


class PublicUiRegressionTests(TestCase):
    def test_login_legal_links_are_real_routes(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("core:terms_of_service"))
        self.assertContains(response, reverse("core:privacy_policy"))
        self.assertNotContains(response, 'href="#"')
