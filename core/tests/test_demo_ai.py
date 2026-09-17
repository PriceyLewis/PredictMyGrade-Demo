from django.test import SimpleTestCase, override_settings

from core.services.openai_client import OpenAIClient, OpenAIConfigurationError
from core.tasks import fetch_chat_completion


@override_settings(BILLING_MOCK_MODE=True, OPENAI_API_KEY="should-not-be-used")
class DemoAISafetyTests(SimpleTestCase):
    def test_demo_assistant_returns_local_mock_response(self):
        response = fetch_chat_completion(
            messages=[
                {"role": "system", "content": "You are a mentor."},
                {"role": "user", "content": "How am I doing with my grades?"},
            ]
        )

        self.assertIsNotNone(response)
        self.assertTrue(response.raw.get("demo"))
        self.assertEqual(response.raw.get("provider"), "mock")
        self.assertIn("simulated", response.message.lower())
        self.assertIn("no external ai service was called", response.message.lower())

    def test_external_openai_client_is_blocked_in_demo_mode(self):
        with self.assertRaises(OpenAIConfigurationError):
            OpenAIClient(api_key="should-not-be-used")
