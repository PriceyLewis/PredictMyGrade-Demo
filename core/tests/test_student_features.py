from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    ExamChecklistProgress, Module, PastPaperRecord, PersonalStatementProgress,
    RevisionSession, SuperCurricularProgress, UcasOffer,
)


@override_settings(BILLING_MOCK_MODE=True)
class StudentFeatureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("feature-owner", password="test-password")
        self.other = get_user_model().objects.create_user("other-feature-owner", password="test-password")
        self.client.force_login(self.user)
        self.user.profile.set_premium(True)

    def post(self, name, data=None, pk=None):
        return self.client.post(reverse(f"core:{name}", args=[pk] if pk else None), data or {})

    def test_ucas_offer_add_update_delete(self):
        self.post("add_ucas_offer", {"institution": "Test University", "course": "Computing", "points": 120})
        offer = UcasOffer.objects.get(user=self.user)
        self.assertEqual(offer.required_points, 120)
        self.post("update_ucas_offer", {"points": 0, "notes": "Updated"}, offer.pk)
        offer.refresh_from_db()
        self.assertEqual((offer.required_points, offer.notes), (0, "Updated"))
        self.post("delete_ucas_offer", pk=offer.pk)
        self.assertFalse(UcasOffer.objects.filter(pk=offer.pk).exists())

    def test_personal_statement_progress_persists(self):
        self.post("save_personal_statement", {"word_count": 500, "target": 1000, "deadline": "2027-01-15"})
        progress = PersonalStatementProgress.objects.get(user=self.user)
        self.assertEqual((progress.word_count, progress.target_word_count, str(progress.deadline)), (500, 1000, "2027-01-15"))

    def test_both_checklists_toggle_and_keep_ownership(self):
        for endpoint, model in (("toggle_super_curricular", SuperCurricularProgress), ("toggle_exam_checklist", ExamChecklistProgress)):
            for done in (True, False):
                self.post(endpoint, {"key": "qa-item", "completed": str(done).lower()})
                item = model.objects.get(user=self.user, key="qa-item")
                self.assertEqual(item.completed, done)
                self.assertEqual(item.completed_at is not None, done)
            self.assertFalse(model.objects.filter(user=self.other).exists())

    def test_revision_session_add_delete_and_invalid_input(self):
        self.post("add_revision_session", {"subject": "Maths", "date": "2027-01-15", "time": "10:30"})
        item = RevisionSession.objects.get(user=self.user)
        self.assertEqual(str(item.scheduled_time), "10:30:00")
        self.post("add_revision_session", {"subject": "Bad date", "date": "invalid"})
        self.assertEqual(RevisionSession.objects.filter(user=self.user).count(), 1)
        self.post("delete_revision_session", pk=item.pk)
        self.assertFalse(RevisionSession.objects.filter(pk=item.pk).exists())

    def test_past_paper_add_update_clear_delete(self):
        self.post("add_past_paper", {"name": "Maths paper", "score": 0})
        paper = PastPaperRecord.objects.get(user=self.user)
        self.assertEqual(paper.score_percent, 0)
        self.post("update_past_paper", {"score": 85}, paper.pk)
        paper.refresh_from_db()
        self.assertEqual(paper.score_percent, 85)
        self.post("update_past_paper", {"score": ""}, paper.pk)
        paper.refresh_from_db()
        self.assertIsNone(paper.score_percent)
        self.post("delete_past_paper", pk=paper.pk)
        self.assertFalse(PastPaperRecord.objects.filter(pk=paper.pk).exists())

    def test_past_paper_rejects_invalid_scores_without_mutation(self):
        paper = PastPaperRecord.objects.create(user=self.user, name="Existing", score_percent=60)
        for score in ("nan", "inf", "-1", "101", "not a number"):
            self.post("add_past_paper", {"name": "Invalid", "score": score})
            self.post("update_past_paper", {"score": score}, paper.pk)
            paper.refresh_from_db()
            self.assertEqual(paper.score_percent, 60)
            self.assertEqual(PastPaperRecord.objects.filter(user=self.user).count(), 1)

    def test_other_users_records_cannot_be_updated_or_deleted(self):
        records = [
            (UcasOffer.objects.create(user=self.other, institution="Private", course="Computing"), ("update_ucas_offer", "delete_ucas_offer")),
            (PastPaperRecord.objects.create(user=self.other, name="Private"), ("update_past_paper", "delete_past_paper")),
            (RevisionSession.objects.create(user=self.other, subject="Private", scheduled_date="2027-01-15"), ("delete_revision_session",)),
            (Module.objects.create(user=self.other, name="Private", credits=20), ("module_update", "module_delete")),
        ]
        for record, endpoints in records:
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(self.post(endpoint, pk=record.pk).status_code, 404)
                    self.assertTrue(type(record).objects.filter(pk=record.pk).exists())

    def test_student_pages_render_for_premium_and_free_accounts(self):
        names = ("dashboard", "college", "gcse", "modules_list", "prediction_history", "snapshot_history", "snapshot_comparison", "progress_timeline", "compare_levels", "compare_all_levels", "what_if_simulation", "what_if_basic", "what_if_history", "milestone_history", "ai_reports", "smart_insights_page", "target_grade_calculator_page", "study_suggestions", "backup_history")
        for premium in (True, False):
            self.user.profile.set_premium(premium)
            for name in names:
                with self.subTest(premium=premium, name=name):
                    response = self.client.get(reverse(f"core:{name}"), follow=True)
                    self.assertEqual(response.status_code, 200)
