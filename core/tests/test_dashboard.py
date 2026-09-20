import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import (
    AIChatSession,
    AIInsightFeedback,
    Module,
    PlannedModule,
    SmartInsight,
    StudyPlan,
)
from core.utils import PredictionResult


class DashboardPlannerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='alice', password='password123')
        self.client.force_login(self.user)
        Module.objects.create(user=self.user, name='Algorithms', level='UNI', credits=20, grade_percent=72)

    def test_ajax_target_planner_payload_contains_expected_fields(self):
        url = reverse('core:dashboard')
        response = self.client.get(
            url,
            {'target_class': 'First', 'total_credits': 120},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in [
            'completed_credits',
            'total_credits',
            'target_class',
            'target_avg',
            'remaining_credits',
            'required_avg_remaining',
        ]:
            self.assertIn(key, payload)

    def test_dashboard_includes_bootstrap_json(self):
        response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        bootstrap = response.context.get('dashboard_bootstrap')
        self.assertIsNotNone(bootstrap)
        # Ensure the JSON blob is parseable
        data = json.loads(bootstrap)
        self.assertIn('averages', data)
        self.assertIn('urls', data)

    def test_free_dashboard_does_not_advertise_premium_insight_endpoints(self):
        self.user.profile.set_premium(False)
        response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context['dashboard_bootstrap'])
        self.assertIsNone(data['urls']['aiInsights'])
        self.assertIsNone(data['urls']['aiInsightFeedback'])

    def test_premium_dashboard_advertises_insight_endpoints(self):
        self.user.profile.set_premium(True)
        response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context['dashboard_bootstrap'])
        self.assertEqual(data['urls']['aiInsights'], reverse('core:ai_insights_feed'))
        self.assertEqual(data['urls']['aiInsightFeedback'], reverse('core:ai_insight_feedback'))


class DashboardViewContextTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='charlie', password='pass1234')
        self.client.force_login(self.user)
        Module.objects.create(
            user=self.user,
            name='User Research',
            level='UNI',
            credits=15,
            grade_percent=80,
        )

    def test_dashboard_context_exposes_mentor_and_ai_keys(self):
        prediction = SimpleNamespace(
            average=68.2,
            confidence=82.5,
            model_label='Test Ridge',
            personal_weight=12,
        )
        with mock.patch(
            'core.views.personalised_prediction',
            return_value=prediction,
        ), mock.patch(
            'core.views.generate_ai_study_tip',
            return_value='Keep refining focus',
        ), mock.patch(
            'core.views.generate_ai_mentor_message',
            return_value='Motivational prompt',
        ):
            response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        context = response.context
        self.assertEqual(context['ai_tip'], 'Keep refining focus')
        self.assertEqual(context['mentor_tip'], 'Keep refining focus')
        self.assertEqual(context['ai_model_label'], 'Test Ridge')
        self.assertEqual(context['ai_confidence'], 82.5)
        self.assertEqual(context['personal_weight'], 12)
        self.assertEqual(context['completedCredits'], 15)


class PlannerApisTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='bob', password='secret456')
        self.client.force_login(self.user)

    def test_save_future_modules_stores_entries(self):
        url = reverse('core:save_future_modules')
        payload = [
            {'name': 'Research Project', 'credits': 40, 'grade': 68},
            {'name': 'Dissertation', 'credits': 20, 'grade': 70},
        ]
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlannedModule.objects.filter(user=self.user).count(), 2)

    def test_save_future_modules_rejects_invalid_payload(self):
        url = reverse('core:save_future_modules')
        response = self.client.post(url, data='not-json', content_type='text/plain')
        self.assertEqual(response.status_code, 400)


class AIInsightFeedbackTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='insight', password='feedback123')
        self.other_user = User.objects.create_user(username='guest', password='pass5678')
        self.insight = SmartInsight.objects.create(user=self.user, title='Keep going', summary='Focus review')
        self.client.force_login(self.user)

    def post_feedback(self, **payload):
        data = {'insight_id': self.insight.id, 'rating': 1}
        data.update(payload)
        return self.client.post(
            reverse('core:ai_insight_feedback'),
            data=json.dumps(data),
            content_type='application/json',
        )

    def test_create_feedback_creates_record(self):
        response = self.post_feedback(rating=1)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['feedback']['helpful'], 1)
        self.assertEqual(body['feedback']['not_helpful'], 0)
        self.assertEqual(body['feedback']['user_rating'], 1)
        self.assertTrue(
            AIInsightFeedback.objects.filter(user=self.user, insight=self.insight, rating=1).exists()
        )

    def test_switching_feedback_updates_vote(self):
        self.post_feedback(rating=1)
        response = self.post_feedback(rating=-1)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['feedback']['helpful'], 0)
        self.assertEqual(body['feedback']['not_helpful'], 1)
        self.assertEqual(body['feedback']['user_rating'], -1)
        self.assertEqual(
            AIInsightFeedback.objects.filter(user=self.user, insight=self.insight).count(),
            1,
        )
        self.assertTrue(
            AIInsightFeedback.objects.filter(user=self.user, insight=self.insight, rating=-1).exists()
        )

    def test_zero_rating_removes_feedback(self):
        self.post_feedback(rating=1)
        response = self.post_feedback(rating=0)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['feedback']['helpful'], 0)
        self.assertEqual(body['feedback']['not_helpful'], 0)
        self.assertEqual(body['feedback']['user_rating'], 0)
        self.assertFalse(
            AIInsightFeedback.objects.filter(user=self.user, insight=self.insight).exists()
        )

    def test_invalid_rating_rejected(self):
        response = self.post_feedback(rating=3)
        self.assertEqual(response.status_code, 422)
        self.assertIn('error', response.json())

    def test_cannot_rate_another_users_insight(self):
        other_insight = SmartInsight.objects.create(user=self.other_user, title='Other', summary='No access')
        response = self.client.post(
            reverse('core:ai_insight_feedback'),
            data=json.dumps({'insight_id': other_insight.id, 'rating': 1}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)


class AIPredictionConfidenceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='confidence-user', password='pw123456')
        self.client.force_login(self.user)
        Module.objects.create(
            user=self.user,
            name='Predictive Modelling',
            level='UNI',
            credits=20,
            grade_percent=70,
        )

    def _prediction_result(self):
        return PredictionResult(
            average=84.2,
            confidence=91.5,
            model_label='Test Ridge',
            personal_weight=33.0,
        )

    @mock.patch('core.views.personalised_prediction')
    def test_dashboard_context_exposes_confidence(self, mock_prediction):
        mock_prediction.return_value = self._prediction_result()
        response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['ai_predicted_average'], 84.2)
        self.assertEqual(response.context['ai_confidence'], 91.5)
        self.assertEqual(response.context['ai_model_label'], 'Test Ridge')
        self.assertEqual(response.context['personal_weight'], 33.0)

    @mock.patch('core.views.personalised_prediction')
    def test_live_dashboard_payload_includes_ai_confidence(self, mock_prediction):
        mock_prediction.return_value = self._prediction_result()
        response = self.client.get(reverse('core:dashboard_live_data'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['ai_predicted_average'], 84.2)
        self.assertEqual(payload['ai_confidence'], 91.5)
        self.assertEqual(payload['ai_model'], 'Test Ridge')
        self.assertEqual(payload['ai_personal_weight'], 33.0)


class AssistantPreviewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='freebie', password='preview123')
        self.client.force_login(self.user)
        profile = self.user.profile
        profile.trial_cancelled = True
        profile.trial_started_at = None
        profile.trial_ends_at = None
        profile.plan_type = 'free'
        profile.save(update_fields=['trial_cancelled', 'trial_started_at', 'trial_ends_at', 'plan_type'])

    def test_free_user_receives_preview_response(self):
        response = self.client.post(
            reverse('core:ai_assistant'),
            data=json.dumps({'message': 'Hello mentor!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertTrue(payload.get('requires_upgrade'))
        self.assertIn('answer', payload)
        self.assertIn('history', payload)


class AssistantPremiumTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='premium-chat', password='pass123')
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.profile.set_premium(True)
        Module.objects.create(
            user=self.user,
            name='Machine Learning',
            level='UNI',
            credits=20,
            grade_percent=75,
        )

    def _post(self, payload: dict):
        return self.client.post(
            reverse('core:ai_assistant'),
            data=json.dumps(payload),
            content_type='application/json',
        )

    @mock.patch('core.views.fetch_chat_completion')
    def test_persona_selection_persists_for_premium_users(self, mock_fetch):
        mock_fetch.return_value = SimpleNamespace(message='Coach answer')

        response = self._post({'message': 'Hello mentor!', 'persona': 'coach'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['persona'], 'coach')
        self.assertFalse(payload['requires_upgrade'])
        self.assertFalse(payload['free_preview'])

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.ai_persona, 'coach')
        self.assertEqual(AIChatSession.objects.filter(user=self.user).count(), 1)

        response = self._post({'message': 'Still here?'})
        self.assertEqual(response.status_code, 200)
        repeat_payload = response.json()
        self.assertEqual(repeat_payload['persona'], 'coach')
        self.assertGreaterEqual(len(repeat_payload['history']), 2)
        self.assertEqual(AIChatSession.objects.filter(user=self.user).count(), 1)

    @mock.patch('core.views.fetch_chat_completion')
    def test_reset_clears_history_and_updates_persona(self, mock_fetch):
        mock_fetch.return_value = SimpleNamespace(message='Initial answer')
        self._post({'message': 'Kick things off', 'persona': 'coach'})
        session = AIChatSession.objects.get(user=self.user)
        self.assertGreater(session.messages.count(), 0)
        self.assertEqual(session.persona, 'coach')

        response = self._post({'reset': True, 'persona': 'analyst'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['history'], [])
        self.assertEqual(payload['persona'], 'analyst')

        session.refresh_from_db()
        self.assertEqual(session.persona, 'analyst')
        self.assertEqual(session.messages.count(), 0)


class StudyPlanCalendarTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='premium', password='calendar123')
        self.client.force_login(self.user)
        self.user.profile.set_premium(True)
        StudyPlan.objects.create(user=self.user, title='Focus Session', date=date.today(), duration_hours=2)

    def test_premium_receives_ics_calendar(self):
        response = self.client.get(reverse('core:study_plan_calendar'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar')
        body = response.content.decode()
        self.assertIn('BEGIN:VCALENDAR', body)
        self.assertIn('SUMMARY:Focus Session', body)

    def test_google_redirect_includes_calendar_link(self):
        response = self.client.get(reverse('core:study_plan_calendar') + '?target=google')
        self.assertEqual(response.status_code, 302)
        self.assertIn('calendar.google.com', response['Location'])

    def test_free_user_blocked(self):
        User = get_user_model()
        free_user = User.objects.create_user(username='visitor', password='visitor123')
        profile = free_user.profile
        profile.trial_cancelled = True
        profile.trial_started_at = None
        profile.trial_ends_at = None
        profile.plan_type = 'free'
        profile.save(update_fields=['trial_cancelled', 'trial_started_at', 'trial_ends_at', 'plan_type'])
        self.client.force_login(free_user)
        response = self.client.get(reverse('core:study_plan_calendar'))
        self.assertEqual(response.status_code, 403)
