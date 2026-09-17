from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from django.conf import settings

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled in deployment environment
    OpenAI = None  # type: ignore


class OpenAIConfigurationError(RuntimeError):
    """Raised when the OpenAI client is unavailable or intentionally disabled."""


@dataclass(slots=True)
class OpenAIResponse:
    """
    Lightweight wrapper for AI responses so callers do not rely on SDK internals.
    """

    message: str
    raw: Dict[str, Any]


class OpenAIClient:
    """
    Thin wrapper around the OpenAI SDK that hides environment lookups and
    provides guard-rails for missing configuration.

    PredictMyGrade-Demo deliberately blocks external AI while mock billing is
    enabled. The portfolio build uses deterministic local mentor responses so a
    reviewer cannot accidentally trigger paid API traffic by adding a key.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        if getattr(settings, "BILLING_MOCK_MODE", True):
            raise OpenAIConfigurationError(
                "External AI is disabled while PredictMyGrade is running in demo mode."
            )

        api_key = api_key or getattr(settings, "OPENAI_API_KEY", "")
        model = model or getattr(settings, "OPENAI_CHAT_MODEL", "gpt-4o-mini")

        if not api_key:
            raise OpenAIConfigurationError("OPENAI_API_KEY is not configured.")
        if OpenAI is None:
            raise OpenAIConfigurationError("openai package is not installed.")

        self._model = model
        self._client = OpenAI(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    def chat_completion(
        self,
        messages: Iterable[Dict[str, str]],
        temperature: float = 0.3,
        max_output_tokens: int = 512,
    ) -> OpenAIResponse:
        """Execute a chat completion request and normalise the response payload."""
        chat = self._client.chat.completions.create(
            model=self._model,
            messages=list(messages),
            temperature=temperature,
            max_tokens=max_output_tokens,
        )
        content = chat.choices[0].message.content if chat.choices else ""
        return OpenAIResponse(message=content or "", raw=chat.model_dump())


_cached_client: Optional[OpenAIClient] = None


def get_openai_client() -> OpenAIClient:
    """Fetch a singleton OpenAI client instance outside the demo environment."""
    global _cached_client
    if _cached_client is None:
        _cached_client = OpenAIClient()
    return _cached_client
