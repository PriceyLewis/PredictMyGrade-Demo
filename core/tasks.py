from __future__ import annotations

import logging
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
from django.utils.text import slugify

from .models import DataExportLog, UserProfile
from .services.openai_client import (
    OpenAIClient,
    OpenAIConfigurationError,
    OpenAIResponse,
    get_openai_client,
)
from .services.insights import (
    capture_prediction_snapshot,
    generate_insights_for_user,
)
from .utils import build_user_data_export

logger = logging.getLogger(__name__)


def _call_management_command(name: str, *args: str, **kwargs) -> None:
    """
    Helper to wrap management command invocation with structured logging. The
    commands already exist in the project and operate synchronously, so we
    simply call them within the Celery worker.
    """
    logger.info("Running management command %s", name)
    call_command(name, *args, **kwargs)
    logger.info("Finished management command %s", name)


@shared_task(name="core.tasks.weekly_retrain_models")
def weekly_retrain_models() -> None:
    """
    Kick off the existing retrain_ai management command. This keeps the ML
    models fresh by incorporating the latest user data.
    """
    _call_management_command("retrain_ai")


@shared_task(name="core.tasks.generate_weekly_ai_insights")
def generate_weekly_ai_insights() -> None:
    """
    Generate AI insights for premium users and persist them for dashboard consumption.
    Mock/free accounts are intentionally excluded.
    """
    profiles = UserProfile.objects.filter(is_premium=True).select_related("user").iterator()
    total_insights = 0
    processed = 0
    for profile in profiles:
        processed += 1
        try:
            insights = generate_insights_for_user(profile)
            total_insights += len(insights)
        except Exception:
            logger.exception("Failed generating insights for user %s", profile.user_id)
    logger.info(
        "AI insights job completed: %s users processed, %s insights generated",
        processed,
        total_insights,
    )


@shared_task(name="core.tasks.dispatch_weekly_reports")
def dispatch_weekly_reports() -> None:
    """
    Send the current admin analytics digest on a cadence.
    """
    _call_management_command("send_weekly_report")


@shared_task(name="core.tasks.run_premium_backup")
def run_premium_backup() -> None:
    """
    Write premium user exports to disk for later retrieval.
    """
    now = timezone.now()
    backup_dir = Path(settings.PREMIUM_BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    formatted_time = now.strftime("%Y%m%dT%H%M%S")

    premium_profiles = list(
        UserProfile.objects.filter(is_premium=True).select_related("user")
    )

    def _backup_profile(profile: UserProfile, tag: str) -> bool:
        user = profile.user
        payload, record_count = build_user_data_export(user)
        if not payload:
            logger.warning(
                "Skipping backup for user %s (%s); export generated no rows.",
                user.get_username(),
                user.id,
            )
            return False
        safe_name = slugify(user.username or "") or f"user{user.id}"
        filename = f"backup-{tag}-{safe_name}-{user.id}-{formatted_time}.csv"
        path = backup_dir / filename
        try:
            path.write_text(payload, encoding="utf-8")
        except OSError:
            logger.exception("Failed to persist backup for user %s", user.id)
            return False
        DataExportLog.objects.create(
            user=user,
            format="csv",
            record_count=record_count,
            notes=f"premium_backup ({tag})",
        )
        logger.info("Wrote backup for user %s (%s rows) to %s", user.id, record_count, path)
        return True

    premium_saved = sum(1 for profile in premium_profiles if _backup_profile(profile, "premium"))
    logger.info(
        "Premium backup completed: %s premium exports stored in %s",
        premium_saved,
        backup_dir,
    )


@shared_task(name="core.tasks.capture_daily_progress_snapshot")
def capture_daily_progress_snapshot() -> None:
    """
    Automatically collect performance snapshots for all active users so the AI
    progress timeline stays fresh without manual intervention.
    """
    profiles = UserProfile.objects.select_related("user").iterator()
    captured = 0
    for profile in profiles:
        try:
            snapshot = capture_prediction_snapshot(profile)
            if snapshot:
                captured += 1
        except Exception:
            logger.exception("Failed capturing snapshot for user %s", profile.user_id)
    logger.info("Daily progress snapshot run captured %s snapshots", captured)


def _demo_chat_completion(
    prompt: str | None = None, *, messages: list[dict[str, str]] | None = None
) -> OpenAIResponse:
    """Return a deterministic mentor response without calling an external API."""
    user_message = prompt or ""
    if messages:
        for item in reversed(messages):
            if item.get("role") == "user" and item.get("content"):
                user_message = item["content"]
                break

    context_hint = ""
    lowered = user_message.lower()
    if "deadline" in lowered or "schedule" in lowered or "plan" in lowered:
        context_hint = " Prioritise the nearest deadline, then split revision into short focused blocks."
    elif "grade" in lowered or "average" in lowered or "doing" in lowered:
        context_hint = " Use the dashboard trend and your lowest weighted module to choose the next study target."
    elif "motivat" in lowered or "stuck" in lowered:
        context_hint = " Pick one small task you can finish now, then reassess after a short break."

    message = (
        "Demo mentor response: focus on the highest-impact next action from your saved academic data."
        f"{context_hint} This response is simulated for the portfolio demo; no external AI service was called."
    )
    return OpenAIResponse(
        message=message,
        raw={
            "demo": True,
            "provider": "mock",
            "model": "predictmygrade-demo-mentor",
        },
    )


def fetch_chat_completion(
    prompt: str | None = None, *, messages: list[dict[str, str]] | None = None
) -> OpenAIResponse | None:
    """
    Utility used by views to request an AI-generated answer.

    This repository is demo-first. While mock billing is enabled, assistant
    replies are generated locally and deterministically so premium flows can be
    reviewed without API keys, network access, cost, or accidental live calls.
    """
    if getattr(settings, "BILLING_MOCK_MODE", True):
        return _demo_chat_completion(prompt, messages=messages)

    try:
        client: OpenAIClient = get_openai_client()
    except OpenAIConfigurationError as exc:  # pragma: no cover - configuration dependent
        logger.warning("OpenAI unavailable: %s", exc)
        return None

    try:
        if messages is None:
            if not prompt:
                raise ValueError("Prompt or messages must be provided")
            base_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are PredictMyGrade's academic mentor. Keep answers concise, motivational, "
                        "and grounded in student performance metrics supplied by the system."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            return client.chat_completion(base_messages)
        return client.chat_completion(messages)
    except Exception:  # pragma: no cover - rely on logging for observability
        logger.exception("OpenAI chat completion failed")
        return None
