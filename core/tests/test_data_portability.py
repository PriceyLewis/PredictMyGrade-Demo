import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.models import Module


class DataPortabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("backup-owner", password="test-password")
        self.other = get_user_model().objects.create_user("other-backup-owner", password="test-password")
        self.client.force_login(self.user)
        Module.objects.create(user=self.user, name="Maths", level="UNI", credits=20, grade_percent=0, completion_percent=75)
        Module.objects.create(user=self.user, name="Maths", level="GCSE", credits=10, grade_percent=65)
        self.private = Module.objects.create(user=self.other, name="Private", credits=20)

    def test_backup_round_trip_preserves_levels_zero_and_completion(self):
        payload = self.client.get(reverse("core:backup_json")).json()
        self.assertEqual(len(payload["modules"]), 2)
        response = self.client.post(reverse("core:restore_backup"), {"backup_json": json.dumps(payload)})
        self.assertEqual(response.status_code, 302)
        restored = Module.objects.get(user=self.user, name="Maths", level="UNI")
        self.assertEqual((restored.grade_percent, restored.completion_percent), (0, 75))
        self.assertEqual(Module.objects.filter(user=self.user).count(), 2)
        self.assertTrue(Module.objects.filter(pk=self.private.pk).exists())

    def test_invalid_backup_never_deletes_or_partially_replaces_data(self):
        valid = {"name": "New", "level": "UNI", "credits": 20, "grade_percent": 70}
        payloads = [[], {}, {"modules": "bad"}, {"modules": [valid, None]}, {"modules": [valid, valid]}]
        for field, value in (("credits", "inf"), ("credits", 2.5), ("grade_percent", "nan"), ("level", "INVALID"), ("grade_percent", 101)):
            payloads.append({"modules": [valid, {**valid, "name": "Invalid", field: value}]})
        original = list(Module.objects.filter(user=self.user).values())
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post(reverse("core:restore_backup"), {"backup_json": json.dumps(payload)})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(list(Module.objects.filter(user=self.user).values()), original)

    def test_full_csv_round_trip_does_not_merge_same_name_across_levels(self):
        exported = self.client.get(reverse("core:export_user_data"))
        upload = SimpleUploadedFile("export.csv", exported.content, content_type="text/csv")
        response = self.client.post(reverse("core:import_user_data"), {"file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(Module.objects.filter(user=self.user).count(), 2)
        self.assertEqual(Module.objects.get(user=self.user, level="UNI").grade_percent, 0)
        self.assertTrue(Module.objects.filter(pk=self.private.pk).exists())

    def test_calendar_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("core:study_plan_calendar"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)
