"""
Application views powering the PredictMyGrade dashboard.
"""
# Update Summary (2025-02-11): Added snapshot comparison, AI assistant backend, timeline events,
# freemium gating, data portability, UI sync endpoints, and related helpers.
from __future__ import annotations

import csv
import secrets
import io
import json
import random
import logging
import os
import math
from collections import OrderedDict, defaultdict, Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict, List
import textwrap

from urllib.parse import urlencode, quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Q, F
from django.db.models.functions import TruncDate
from django.core.cache import cache
from django.core.mail import EmailMessage, send_mail
from django.core.exceptions import DisallowedHost, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, NoReverseMatch
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.html import strip_tags
from django.views.decorators.http import require_POST, require_http_methods
from django.template.loader import render_to_string

from allauth.socialaccount.models import SocialAccount

from .models import (
    AccountDeletionLog,
    BugReport,
    DataExportLog,
    Feedback,
    ExamChecklistProgress,
    GradeBoundary,
    Module,
    PastPaperRecord,
    PersonalStatementProgress,
    PlannedModule,
    PredictionSnapshot,
    RevisionSession,
    SmartInsight,
    SuperCurricularProgress,
    StudyPlan,
    StudyGoal,
    UpcomingDeadline,
    UcasOffer,
    UserProfile,
    AIInsightFeedback,
    UserAchievement,
    AIChatSession,
    AIChatMessage,
    TimelineEvent,
    TimelineComparison,
    AIInsightSummary,
    WhatsNewEntry,
    sync_module_progress_for_goal,
    AIModelStatus,
    BillingEventLog,
)
from .achievements import achievement_status, evaluate_achievements
from .tasks import fetch_chat_completion
from .services.assistant import (
    available_personas,
    normalise_persona,
    build_chat_messages,
    serialize_history,
)
from .constants import AI_PERSONA_DEFAULT
from .services.insights import generate_insights_for_user
from .services.onboarding import maybe_seed_onboarding_dataset
from .utils import (
    build_user_data_export,
    calculate_future_target,
    classify_percent,
    generate_ai_mentor_message,
    generate_ai_study_tip,
    generate_smart_insight_from_comparisons,
    generate_timeline_comparison,
    resolve_premium_status,
    next_threshold,
    personalised_prediction,
    smart_tip,
)
from .decorators import premium_required

User = get_user_model()
logger = logging.getLogger(__name__)
APP_START_TIME = timezone.now()
CANCELLATION_NOTICE_SESSION_KEY = "premium_cancellation_notice"
MOCK_BILLING_CUSTOMER_PREFIX = "mock_cus_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_profile(user) -> UserProfile:
    profile = getattr(user, "profile", None) or getattr(user, "userprofile", None)
    if profile:
        profile.start_free_trial()
        return profile
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.start_free_trial()
    return profile


def _billing_live_enabled() -> bool:
    return False


def _billing_checkout_enabled() -> bool:
    return bool(_billing_live_enabled() or getattr(settings, "BILLING_MOCK_MODE", True))


def _mock_customer_id(user) -> str:
    return f"{MOCK_BILLING_CUSTOMER_PREFIX}{user.pk}"


def _mock_subscription_summary(plan_type: str | None = None) -> dict[str, object]:
    selected_plan = "year" if plan_type == "yearly" else "month"
    amount_display = "99.00 GBP/year" if selected_plan == "year" else "9.99 GBP/month"
    plan_display = "Premium Yearly" if selected_plan == "year" else "Premium Monthly"
    return {
        "status": "active",
        "plan_interval": selected_plan,
        "plan_display": plan_display,
        "amount_display": amount_display,
        "days_remaining": 30 if selected_plan == "month" else 365,
    }


def _mock_plan_type_for_profile(profile: UserProfile) -> str:
    latest_event = (
        BillingEventLog.objects.filter(user=profile.user, event="upgrade")
        .exclude(metadata={})
        .first()
    )
    metadata = getattr(latest_event, "metadata", {}) or {}
    return metadata.get("plan_type") or "monthly"


def _weighted_average(modules) -> float:
    total_score = 0.0
    total_weight = 0.0
    for module in modules:
        if module.grade_percent is None:
            continue
        credits = module.credits or 0
        total_weight += credits
        total_score += module.grade_percent * credits
    if not total_weight:
        return 0.0
    return total_score / total_weight


def _completed_credits(modules) -> float:
    return sum(module.credits or 0 for module in modules if module.grade_percent is not None)


def _simple_average(modules) -> float:
    values = [module.grade_percent for module in modules if module.grade_percent is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _ics_escape(value: str | None) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _safe_next_url(request) -> str | None:
    """
    Validate ?next= or posted next targets to avoid open redirects.
    """
    candidate = request.GET.get("next") or request.POST.get("next")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, {request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return None


def _total_credits_target(profile: UserProfile) -> int:
    return getattr(profile, "credit_goal", 120) or 120


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _json_error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _insight_feedback_totals(user, insights):
    insight_ids = [insight.pk for insight in insights]
    totals = {
        insight_id: {"helpful": 0, "not_helpful": 0, "user_rating": 0}
        for insight_id in insight_ids
    }
    if not insight_ids:
        return totals

    feedback_qs = AIInsightFeedback.objects.filter(insight_id__in=insight_ids)
    for feedback in feedback_qs:
        entry = totals.get(feedback.insight_id)
        if not entry:
            continue
        if feedback.rating > 0:
            entry["helpful"] += 1
        elif feedback.rating < 0:
            entry["not_helpful"] += 1
        if feedback.user_id == user.id:
            entry["user_rating"] = feedback.rating
    return totals


def _serialize_insight(insight: SmartInsight, feedback: dict | None = None) -> dict:
    feedback_data = {"helpful": 0, "not_helpful": 0, "user_rating": 0}
    if feedback:
        feedback_data.update(
            {
                "helpful": feedback.get("helpful", 0),
                "not_helpful": feedback.get("not_helpful", 0),
                "user_rating": feedback.get("user_rating", 0),
            }
        )

    metadata = insight.metadata or {}

    return {
        "id": insight.pk,
        "title": insight.title or "AI Insight",
        "summary": insight.summary,
        "impact_score": round(insight.impact_score or 0, 3),
        "tag": metadata.get("tag"),
        "feedback": feedback_data,
        "created_at": timezone.localtime(insight.created_at).isoformat(),
    }


def _get_user_role(user) -> str:
    if getattr(user, "is_superuser", False):
        return "admin"
    if getattr(user, "is_staff", False):
        return "instructor"
    return "student"


def _pdf_escape(text: str) -> str:
    safe = text.encode("ascii", "replace").decode("ascii")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_lines(text: str, width: int = 88) -> List[str]:
    return textwrap.wrap(text, width=width) or [text]


def _generate_simple_pdf(title: str, lines: List[str]) -> bytes:
    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")

    offsets: List[int] = [0]

    def write_object(number: int, content: bytes) -> None:
        while len(offsets) <= number:
            offsets.append(0)
        offsets[number] = buffer.tell()
        buffer.write(f"{number} 0 obj\n".encode("ascii"))
        buffer.write(content)
        buffer.write(b"\nendobj\n")

    stream_commands: List[str] = []
    y_position = 780
    stream_commands.append(
        f"BT /F1 18 Tf 72 {y_position} Td ({_pdf_escape(title)}) Tj ET"
    )
    y_position -= 28
    for raw_line in lines[:40]:
        for wrapped in _wrap_lines(raw_line):
            if y_position < 40:
                break
            stream_commands.append(
                f"BT /F1 11 Tf 72 {y_position} Td ({_pdf_escape(wrapped)}) Tj ET"
            )
            y_position -= 18
        if y_position < 40:
            break

    stream_data = "\n".join(stream_commands).encode("ascii", "replace")
    content = (
        f"<< /Length {len(stream_data)} >>\n".encode("ascii")
        + b"stream\n"
        + stream_data
        + b"\nendstream"
    )

    write_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    write_object(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    write_object(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    )
    write_object(4, content)
    write_object(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    startxref = buffer.tell()
    total_objects = len(offsets) - 1

    buffer.write(b"xref\n")
    buffer.write(f"0 {total_objects + 1}\n".encode("ascii"))
    buffer.write(b"0000000000 65535 f \n")
    for number in range(1, total_objects + 1):
        position = offsets[number]
        buffer.write(f"{position:010d} 00000 n \n".encode("ascii"))

    buffer.write(
        b"trailer\n"
        + f"<< /Size {total_objects + 1} /Root 1 0 R >>\n".encode("ascii")
    )
    buffer.write(b"startxref\n")
    buffer.write(f"{startxref}\n".encode("ascii"))
    buffer.write(b"%%EOF")

    return buffer.getvalue()


def _evaluate_milestones(profile: UserProfile, average: float) -> List[str]:
    if average is None:
        return []
    if not profile.milestone_effects_enabled:
        return []

    milestones = [
        ("milestone_50_unlocked", 50, "Milestone unlocked: 50% average achieved! Keep the momentum."),
        ("milestone_60_unlocked", 60, "Great work! 60% milestone unlocked — you're tracking for a 2:1."),
        ("milestone_70_unlocked", 70, "Outstanding! 70% milestone unlocked — first-class trajectory."),
    ]
    unlocked_messages: List[str] = []
    updated_fields: List[str] = []

    for field_name, threshold, message in milestones:
        if average >= threshold and not getattr(profile, field_name, False):
            setattr(profile, field_name, True)
            unlocked_messages.append(message)
            updated_fields.append(field_name)

    if unlocked_messages and not profile.first_milestone_reached:
        profile.first_milestone_reached = True
        updated_fields.append("first_milestone_reached")

    if updated_fields:
        profile.save(update_fields=list(dict.fromkeys(updated_fields)))

    return unlocked_messages


def _level_average_for_user(user, levels: List[str]) -> float:
    if isinstance(levels, str):
        levels = [levels]
    average = (
        Module.objects.filter(
            user=user,
            level__in=levels,
            grade_percent__isnull=False,
        )
        .aggregate(avg=Avg("grade_percent"))
        .get("avg")
    )
    return float(average or 0.0)


def _collect_admin_metrics(period_days: int = 7) -> dict:
    today = timezone.now().date()
    start = today - timedelta(days=period_days - 1)
    previous_start = start - timedelta(days=period_days)
    previous_end = start - timedelta(days=1)

    days_current = [start + timedelta(days=i) for i in range(period_days)]
    days_previous = [previous_start + timedelta(days=i) for i in range(period_days)]

    def series_for(queryset, field_name: str, days_window):
        lookup = f"{field_name}__date__range"
        aggregated = (
            queryset.filter(**{lookup: (days_window[0], days_window[-1])})
            .annotate(day=TruncDate(field_name))
            .values("day")
            .annotate(total=Count("id"))
        )
        mapping = {entry["day"]: entry["total"] for entry in aggregated}
        return [mapping.get(day, 0) for day in days_window]

    bug_qs = BugReport.objects.all()
    support_qs = Feedback.objects.all()
    export_qs = PredictionSnapshot.objects.all()
    deletion_qs = AccountDeletionLog.objects.all()
    active_qs = User.objects.filter(last_login__isnull=False)

    bug_series = series_for(bug_qs, "created_at", days_current)
    support_series = series_for(support_qs, "created_at", days_current)
    export_series = series_for(export_qs, "created_at", days_current)
    deletion_series = series_for(deletion_qs, "deleted_at", days_current)

    bug_series_prev = series_for(bug_qs, "created_at", days_previous)
    support_series_prev = series_for(support_qs, "created_at", days_previous)
    export_series_prev = series_for(export_qs, "created_at", days_previous)
    deletion_series_prev = series_for(deletion_qs, "deleted_at", days_previous)

    active_users = active_qs.filter(last_login__date__range=(days_current[0], days_current[-1])).count()
    active_users_prev = active_qs.filter(last_login__date__range=(days_previous[0], days_previous[-1])).count()
    premium_users = UserProfile.objects.filter(is_premium=True).count()

    def calc_trend(current: int, previous: int) -> int:
        if previous == 0:
            return 100 if current > 0 else 0
        return round(((current - previous) / previous) * 100)

    bug_total = sum(bug_series)
    support_total = sum(support_series)
    export_total = sum(export_series)
    deletion_total = sum(deletion_series)

    trends = SimpleNamespace(
        bug_reports=calc_trend(bug_total, sum(bug_series_prev)),
        support_msgs=calc_trend(support_total, sum(support_series_prev)),
        exports=calc_trend(export_total, sum(export_series_prev)),
        deletions=calc_trend(deletion_total, sum(deletion_series_prev)),
        active_users=calc_trend(active_users, active_users_prev),
    )

    provider_rows = (
        SocialAccount.objects.values("provider")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    provider_data = OrderedDict(
        (row["provider"].replace("_", " ").title(), row["total"]) for row in provider_rows
    )

    chart_labels = json.dumps([day.strftime("%d %b") for day in days_current])
    chart_data = SimpleNamespace(
        bug_reports=json.dumps(bug_series),
        support_msgs=json.dumps(support_series),
        exports=json.dumps(export_series),
        deletions=json.dumps(deletion_series),
    )

    raw = {
        "days_current": [day.isoformat() for day in days_current],
        "bug_series": bug_series,
        "support_series": support_series,
        "export_series": export_series,
        "deletion_series": deletion_series,
    }

    date_range_display = f"{start.strftime('%d %b %Y')} - {today.strftime('%d %b %Y')}"

    return {
        "date_range": date_range_display,
        "bug_reports": bug_total,
        "support_msgs": support_total,
        "exports": export_total,
        "deletions": deletion_total,
        "active_users": active_users,
        "premium_users": premium_users,
        "trends": trends,
        "provider_data": provider_data,
        "chart_labels": chart_labels,
        "chart_data": chart_data,
        "raw": raw,
    }


def _collect_system_health_metrics() -> dict:
    db_info = {"name": None, "size_bytes": None}
    db_name = settings.DATABASES.get("default", {}).get("NAME")
    if db_name:
        db_info["name"] = db_name
        try:
            if os.path.exists(db_name):
                db_info["size_bytes"] = os.path.getsize(db_name)
        except OSError:
            db_info["size_bytes"] = None

    counts = {
        "modules": Module.objects.count(),
        "goals": StudyGoal.objects.count(),
        "snapshots": PredictionSnapshot.objects.count(),
        "timeline_events": TimelineEvent.objects.count(),
        "smart_insights": SmartInsight.objects.count(),
        "chat_sessions": AIChatSession.objects.count(),
    }

    ai_status = AIModelStatus.objects.first()
    latest_snapshot = (
        PredictionSnapshot.objects.filter().order_by("-created_at").first()
    )
    latest_insight = SmartInsight.objects.order_by("-created_at").first()
    pending_deadlines = UpcomingDeadline.objects.filter(completed=False).count()

    uptime = timezone.now() - APP_START_TIME

    return {
        "db": db_info,
        "counts": counts,
        "ai_status": ai_status,
        "latest_snapshot": latest_snapshot,
        "latest_insight": latest_insight,
        "pending_deadlines": pending_deadlines,
        "uptime": uptime,
    }


def _record_timeline_event(user, event_type: str, message: str) -> TimelineEvent:
    """
    Persist a TimelineEvent entry while guarding against overly long messages.
    """
    return TimelineEvent.objects.create(
        user=user,
        event_type=event_type,
        message=(message or "")[:255],
    )


def _serialize_timeline_event(event: TimelineEvent) -> dict:
    local_ts = timezone.localtime(event.created_at) if event.created_at else timezone.now()
    return {
        "id": event.pk,
        "type": event.event_type,
        "message": event.message,
        "created_at": local_ts.isoformat(),
        "display_time": local_ts.strftime('%d %b %Y %H:%M'),
    }


def _serialize_goal(goal: StudyGoal) -> dict:
    due_iso = goal.due_date.isoformat() if goal.due_date else None
    today = timezone.localdate()
    due_in = None
    overdue = False
    if goal.due_date:
        due_in = (goal.due_date - today).days
        overdue = due_in < 0 and goal.status != "completed"
    return {
        "id": goal.pk,
        "title": goal.title,
        "description": goal.description,
        "category": goal.category,
        "status": goal.status,
        "status_label": goal.get_status_display(),
        "due_date": due_iso,
        "due_in_days": due_in,
        "overdue": overdue,
        "target_percent": goal.target_percent,
        "progress": goal.progress,
        "module_name": goal.module_name,
        "created_at": timezone.localtime(goal.created_at).isoformat(),
        "updated_at": timezone.localtime(goal.updated_at).isoformat(),
    }


def _goal_summary(goals) -> dict:
    today = timezone.localdate()
    total = goals.count()
    completed = goals.filter(status="completed").count()
    active = goals.filter(status="active").count()
    planning = goals.filter(status="planning").count()
    paused = goals.filter(status="paused").count()
    overdue = goals.filter(
        status__in={"planning", "active"},
        due_date__isnull=False,
        due_date__lt=today,
    ).count()
    due_this_week = goals.filter(
        status__in={"planning", "active"},
        due_date__isnull=False,
        due_date__gte=today,
        due_date__lte=today + timedelta(days=7),
    ).count()
    average_progress = (
        round(sum(goal.progress for goal in goals) / total, 1) if total else 0.0
    )
    return {
        "total": total,
        "completed": completed,
        "active": active,
        "planning": planning,
        "paused": paused,
        "overdue": overdue,
        "due_soon": due_this_week,
        "average_progress": average_progress,
    }


# ---------------------------------------------------------------------------
# Level analytics helpers
# ---------------------------------------------------------------------------
UCAS_TARIFF = {
    "ALEVEL": OrderedDict(
        [
            ("A*", 56),
            ("A", 48),
            ("B", 40),
            ("C", 32),
            ("D", 24),
            ("E", 16),
            ("U", 0),
        ]
    ),
    "AS_LEVEL": OrderedDict(
        [
            ("A", 20),
            ("B", 16),
            ("C", 12),
            ("D", 10),
            ("E", 6),
            ("U", 0),
        ]
    ),
    "BTEC_EXT": OrderedDict(
        [
            ("D*", 56),
            ("D", 48),
            ("M", 32),
            ("P", 16),
            ("U", 0),
        ]
    ),
    "EPQ": OrderedDict(
        [
            ("A*", 28),
            ("A", 24),
            ("B", 20),
            ("C", 16),
            ("D", 12),
            ("E", 8),
            ("U", 0),
        ]
    ),
    "SCOTTISH_HIGHER": OrderedDict(
        [
            ("A", 33),
            ("B", 27),
            ("C", 21),
            ("D", 15),
            ("U", 0),
        ]
    ),
    "SCOTTISH_ADVANCED": OrderedDict(
        [
            ("A", 56),
            ("B", 48),
            ("C", 40),
            ("D", 32),
            ("U", 0),
        ]
    ),
}

UCAS_TARIFF_LABELS = {
    "ALEVEL": "A Level",
    "AS_LEVEL": "AS Level",
    "BTEC_EXT": "BTEC Nationals",
    "EPQ": "Extended Project (EPQ)",
    "SCOTTISH_HIGHER": "Scottish Higher",
    "SCOTTISH_ADVANCED": "Scottish Advanced Higher",
}

MODULE_LEVEL_TO_TARIFF = {
    "ALEVEL": "ALEVEL",
    "BTEC": "BTEC_EXT",
}

UCAS_GRADE_BANDS = {
    "ALEVEL": [
        (85, "A*"),
        (75, "A"),
        (65, "B"),
        (55, "C"),
        (45, "D"),
        (35, "E"),
        (0, "U"),
    ],
    "AS_LEVEL": [
        (80, "A"),
        (70, "B"),
        (60, "C"),
        (50, "D"),
        (40, "E"),
        (0, "U"),
    ],
    "BTEC_EXT": [
        (88, "D*"),
        (78, "D"),
        (65, "M"),
        (55, "P"),
        (0, "U"),
    ],
    "EPQ": [
        (85, "A*"),
        (75, "A"),
        (65, "B"),
        (55, "C"),
        (45, "D"),
        (35, "E"),
        (0, "U"),
    ],
    "SCOTTISH_HIGHER": [
        (75, "A"),
        (65, "B"),
        (55, "C"),
        (45, "D"),
        (0, "U"),
    ],
    "SCOTTISH_ADVANCED": [
        (80, "A"),
        (70, "B"),
        (60, "C"),
        (50, "D"),
        (0, "U"),
    ],
}

UCAS_QUALIFICATIONS = [
    {
        "key": key,
        "label": UCAS_TARIFF_LABELS.get(key, key.replace("_", " ").title()),
        "grades": list(options.keys()),
    }
    for key, options in UCAS_TARIFF.items()
]


def _tariff_key_for_level(level: str | None) -> str | None:
    if not level:
        return None
    mapped = MODULE_LEVEL_TO_TARIFF.get(level.upper())
    if mapped:
        return mapped
    return "ALEVEL" if level.upper().startswith("A") else None


def _grade_letter_from_percent(percent: float | None, tariff_key: str | None) -> str:
    if percent is None or not tariff_key:
        return "U"
    bands = UCAS_GRADE_BANDS.get(tariff_key, UCAS_GRADE_BANDS["ALEVEL"])
    for threshold, label in bands:
        if percent >= threshold:
            return label
    return "U"


def _target_percent(percent: float | None) -> float | None:
    if percent is None:
        return None
    return min(92.0, round(percent + 8, 1))


def _tariff_points(tariff_key: str | None, grade: str) -> int:
    if not tariff_key:
        return 0
    return UCAS_TARIFF.get(tariff_key, {}).get(grade, 0)


def _build_ucas_breakdown(items: List[dict]) -> dict:
    breakdown: List[dict] = []
    points_by_level: dict[str, int] = defaultdict(int)
    predicted_by_level: dict[str, int] = defaultdict(int)
    total_points = 0
    predicted_points = 0

    for item in items:
        tariff_key = item.get("tariff_key")
        if not tariff_key:
            continue
        name = item.get("name") or "Module"
        percent = item.get("percent")
        grade_override = item.get("grade_override")
        current_grade = grade_override or _grade_letter_from_percent(percent, tariff_key)
        current_points = _tariff_points(tariff_key, current_grade)

        predicted_percent = item.get("target_percent")
        if predicted_percent is None and percent is not None:
            predicted_percent = _target_percent(percent)
        predicted_grade = _grade_letter_from_percent(
            predicted_percent if predicted_percent is not None else percent,
            tariff_key,
        )
        predicted_points_value = _tariff_points(tariff_key, predicted_grade)

        level_label = item.get("level_label") or UCAS_TARIFF_LABELS.get(
            tariff_key, tariff_key.replace("_", " ").title()
        )

        breakdown.append(
            {
                "name": name,
                "level": level_label,
                "grade": current_grade,
                "points": current_points,
                "percent": round(percent, 1) if percent is not None else None,
                "predicted_grade": predicted_grade,
                "predicted_points": predicted_points_value,
                "predicted_percent": (
                    round(predicted_percent, 1) if predicted_percent is not None else None
                ),
                "potential_gain": max(0, predicted_points_value - current_points),
            }
        )

        total_points += current_points
        predicted_points += predicted_points_value
        points_by_level[tariff_key] += current_points
        predicted_by_level[tariff_key] += predicted_points_value

    top_gain = None
    if breakdown:
        sorted_rows = sorted(breakdown, key=lambda row: row["potential_gain"], reverse=True)
        top_gain = next((row for row in sorted_rows if row["potential_gain"] > 0), None)

    return {
        "total_points": total_points,
        "predicted_points": predicted_points,
        "points_by_level": dict(points_by_level),
        "predicted_by_level": dict(predicted_by_level),
        "breakdown": breakdown,
        "potential_gain": max(0, predicted_points - total_points),
        "top_gain": top_gain,
    }


def _ucas_points_summary(modules) -> dict:
    items = []
    for module in modules:
        tariff_key = _tariff_key_for_level(module.level)
        if not tariff_key:
            continue
        items.append(
            {
                "name": module.name,
                "tariff_key": tariff_key,
                "percent": module.grade_percent,
                "target_percent": _target_percent(module.grade_percent),
                "level_label": module.get_level_display()
                if hasattr(module, "get_level_display")
                else UCAS_TARIFF_LABELS.get(tariff_key, tariff_key),
            }
        )
    return _build_ucas_breakdown(items)


def _ucas_offer_hint(ucas_summary: dict, delta: int) -> str | None:
    if delta <= 0:
        return "Offer requirement met. Hold your current grades."
    top_gain = ucas_summary.get("top_gain")
    if top_gain and top_gain.get("potential_gain"):
        gain = top_gain["potential_gain"]
        target_grade = top_gain.get("predicted_grade")
        return (
            f"+{gain} pts if {top_gain['name']} reaches {target_grade}."
            if target_grade
            else f"+{gain} pts available from {top_gain['name']}."
        )
    if ucas_summary.get("breakdown"):
        return "Log predicted grades to map out where extra tariff points can come from."
    return "Add more graded modules to start building tariff points."


def _safe_positive_int(value, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _ai_suggestions(level: str, metrics: dict) -> List[str]:
    suggestions: List[str] = []
    average = metrics.get("average") or 0
    trend = metrics.get("trend_delta") or 0

    if level == "college":
        if average < 60:
            suggestions.append("Schedule a tutor session for your lowest scoring subject this week.")
        else:
            suggestions.append("Book mock interviews or aptitude practice to stay ahead of offers.")
        if trend < 0:
            suggestions.append("Revisit last term's notes and log a fresh snapshot to recover momentum.")
        suggestions.append("Upload predicted grades to the UCAS planner to keep offers updated.")
    else:
        if average < 55:
            suggestions.append("Plan two short revision sprints for your weakest topics before the weekend.")
        else:
            suggestions.append("Attempt a timed past paper and log the score to reinforce progress.")
        if trend < 0:
            suggestions.append("Use spaced repetition cards for topics that recently dipped.")
        suggestions.append("Share your parent summary export to align support at home.")

    if not suggestions:
        suggestions.append("Keep logging snapshots - consistency is fuelling your results.")
    return suggestions


def _feature_guard(request, feature: str, daily_limit: int = 3) -> tuple[bool, str | None]:
    profile = get_profile(request.user)
    if resolve_premium_status(request.user, profile=profile)["has_access"]:
        return True, None
    today_key = timezone.now().date().isoformat()
    cache_key = f"core:feature:{feature}:{request.user.pk}:{today_key}"
    count = cache.get(cache_key, 0)
    if count >= daily_limit:
        return False, (
            "Daily limit reached for this AI feature. Upgrade to Premium for unlimited access."
        )
    cache.set(cache_key, count + 1, 60 * 60 * 24)
    remaining = daily_limit - (count + 1)
    note = (
        f"{remaining} free use{'s' if remaining != 1 else ''} left today."
        if remaining >= 0
        else None
    )
    return True, note


def _collect_level_metrics(modules_qs):
    modules = list(modules_qs)
    module_count = len(modules)
    credits_total = sum(module.credits or 0 for module in modules)

    graded = [module for module in modules if module.grade_percent is not None]
    average = round(
        sum(module.grade_percent for module in graded) / len(graded), 1
    ) if graded else 0.0

    graded_sorted = sorted(
        graded,
        key=lambda module: (
            module.grade_percent or 0,
            module.created_at or timezone.now(),
        ),
    )
    best_module = graded_sorted[-1] if graded_sorted else None
    lowest_module = graded_sorted[0] if graded_sorted else None

    timeline = sorted(
        graded,
        key=lambda module: module.created_at or timezone.now(),
    )
    progress_recent = timeline[-8:]
    progress_labels = [
        (entry.created_at or timezone.now()).strftime("%d %b")
        if entry.created_at
        else entry.name[:12]
        for entry in progress_recent
    ]
    progress_values = [
        round(entry.grade_percent or 0, 1) for entry in progress_recent
    ]

    distribution_bands = [
        ("High Distinction", lambda grade: grade >= 75),
        ("Merit", lambda grade: 65 <= grade < 75),
        ("Pass", lambda grade: 50 <= grade < 65),
        ("Support Needed", lambda grade: grade < 50),
    ]
    distribution_values = [
        sum(1 for module in graded if band_rule(module.grade_percent or 0))
        for _, band_rule in distribution_bands
    ]
    distribution_labels = [label for label, _ in distribution_bands]

    focus_modules = sorted(
        (module for module in graded if (module.grade_percent or 0) < 60),
        key=lambda module: module.grade_percent or 0,
    )[:4]
    needs_focus = [
        {
            "name": module.name,
            "grade": round(module.grade_percent or 0, 1),
            "gap": round(60 - (module.grade_percent or 0), 1),
        }
        for module in focus_modules
    ]

    recent_modules = [
        {
            "name": module.name,
            "grade": round(module.grade_percent or 0, 1)
            if module.grade_percent is not None
            else None,
            "created": module.created_at,
        }
        for module in modules[:5]
    ]

    trend_delta = 0.0
    if len(progress_values) >= 2:
        trend_delta = round(progress_values[-1] - progress_values[-2], 1)

    return {
        "module_count": module_count,
        "credits_total": credits_total,
        "average": average,
        "best_module": {
            "name": best_module.name,
            "grade": round(best_module.grade_percent or 0, 1),
        }
        if best_module
        else None,
        "lowest_module": {
            "name": lowest_module.name,
            "grade": round(lowest_module.grade_percent or 0, 1),
        }
        if lowest_module
        else None,
        "progress_labels": progress_labels,
        "progress_values": progress_values,
        "distribution_labels": distribution_labels,
        "distribution_values": distribution_values,
        "needs_focus": needs_focus,
        "recent_modules": recent_modules,
        "trend_delta": trend_delta,
    }


def _upcoming_deadlines(user, limit: int = 3):
    deadlines = (
        UpcomingDeadline.objects.filter(user=user, completed=False)
        .order_by("due_date")[:limit]
    )
    today = date.today()
    items = []
    for deadline in deadlines:
        due = deadline.due_date
        if not due:
            continue
        days = (due - today).days
        items.append(
            {
                "title": deadline.title,
                "due": due.strftime("%d %b %Y"),
                "days": days,
                "status": "overdue" if days < 0 else ("soon" if days <= 7 else "scheduled"),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Admin analytics
# ---------------------------------------------------------------------------
@login_required
def admin_analytics(request):
    if not request.user.is_superuser:
        messages.error(request, "You do not have access to the admin analytics.")
        return redirect("core:dashboard")

    metrics = _collect_admin_metrics()

    if request.GET.get("export") == "1":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Date", "Bug Reports", "Support Messages", "Exports", "Deletions"])
        raw = metrics["raw"]
        for idx, day in enumerate(raw["days_current"]):
            writer.writerow(
                [
                    day,
                    raw["bug_series"][idx],
                    raw["support_series"][idx],
                    raw["export_series"][idx],
                    raw["deletion_series"][idx],
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="admin-analytics-{timezone.now():%Y%m%d}.csv"'
        )
        return response

    return render(
        request,
        "core/admin_analytics.html",
        {
            "date_range": metrics["date_range"],
            "bug_reports": metrics["bug_reports"],
            "support_msgs": metrics["support_msgs"],
            "exports": metrics["exports"],
            "deletions": metrics["deletions"],
            "active_users": metrics["active_users"],
            "premium_users": metrics["premium_users"],
            "trends": metrics["trends"],
            "provider_data": metrics["provider_data"],
            "chart_labels": metrics["chart_labels"],
            "chart_data": metrics["chart_data"],
        },
    )


@login_required
def admin_hub(request):
    """
    Consolidated admin hub that blends analytics, user management, billing watchlist,
    and operational health in one place.
    """
    if not request.user.is_superuser:
        messages.error(request, "You do not have access to the admin hub.")
        return redirect("core:dashboard")

    now = timezone.now()
    signup_start = now.date() - timedelta(days=6)  # last 7 days inclusive
    signup_rows = (
        User.objects.filter(date_joined__date__gte=signup_start)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(total=Count("id"))
    )
    signup_map = {row["day"]: row["total"] for row in signup_rows}
    signup_days = [signup_start + timedelta(days=i) for i in range(7)]
    signup_labels = [day.strftime("%d %b") for day in signup_days]
    signup_counts = [signup_map.get(day, 0) for day in signup_days]

    total_users = User.objects.count()
    premium_users = UserProfile.objects.filter(is_premium=True).count()
    modules_total = Module.objects.count()
    recent_users = User.objects.filter(date_joined__date__gte=signup_start).count()
    active_last_7 = UserProfile.objects.filter(user__last_login__gte=now - timedelta(days=7)).count()

    metrics = _collect_admin_metrics()
    health = _collect_system_health_metrics()

    search_query = request.GET.get("q", "").strip()
    plan_filter = request.GET.get("plan", "all")
    users_qs = User.objects.select_related("profile")
    if search_query:
        users_qs = users_qs.filter(
            Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )
    if plan_filter == "premium":
        users_qs = users_qs.filter(profile__is_premium=True)
    elif plan_filter == "free":
        users_qs = users_qs.filter(profile__is_premium=False)

    user_limit = 120
    users = list(users_qs.order_by("username")[:user_limit])
    user_list_truncated = users_qs.count() > user_limit

    context = {
        "total_users": total_users,
        "premium_users": premium_users,
        "modules_total": modules_total,
        "recent_users": recent_users,
        "active_last_7": active_last_7,
        "now": now,
        "plan_filter": plan_filter,
        "search_query": search_query,
        "users": users,
        "user_list_truncated": user_list_truncated,
        "days": json.dumps(signup_labels),
        "counts": json.dumps(signup_counts),
        "bug_reports": metrics["bug_reports"],
        "support_msgs": metrics["support_msgs"],
        "exports": metrics["exports"],
        "deletions": metrics["deletions"],
        "provider_data": metrics["provider_data"],
        "health_counts": health["counts"],
        "pending_deadlines": health["pending_deadlines"],
    }
    return render(request, "core/admin_hub.html", context)


@login_required
def admin_billing_expiring(request):
    if not request.user.is_staff:
        messages.error(request, "You do not have access to billing reports.")
        return redirect("core:dashboard")

    now = timezone.now()
    soon = now + timedelta(days=14)
    trial_qs = (
        UserProfile.objects.select_related("user")
        .filter(
            trial_started_at__isnull=False,
            trial_cancelled=False,
            trial_ends_at__isnull=False,
            trial_ends_at__gte=now,
            trial_ends_at__lte=soon,
        )
        .order_by("trial_ends_at")
    )
    sub_qs = (
        UserProfile.objects.select_related("user")
        .filter(
            is_premium=True,
            plan_period_end__isnull=False,
            plan_period_end__gte=now,
            plan_period_end__lte=soon,
        )
        .order_by("plan_period_end")
    )

    def serialize(profile, plan_end_field):
        plan_end = getattr(profile, plan_end_field, None)
        days_left = None
        if plan_end:
            delta = plan_end - now
            days_left = max(math.ceil(delta.total_seconds() / 86400), 0)
        return {
            "user": profile.user,
            "email": profile.user.email,
            "days_left": days_left,
            "plan_end": plan_end,
            "plan_type": profile.plan_type,
            "cancel_at_period_end": profile.cancel_at_period_end,
        }

    expiring_trials = [serialize(p, "trial_ends_at") for p in trial_qs]
    expiring_subs = [serialize(p, "plan_period_end") for p in sub_qs]

    return render(
        request,
        "core/admin_billing_expiring.html",
        {"expiring_trials": expiring_trials, "expiring_subs": expiring_subs},
    )



@login_required
def admin_user_management(request):
    if not request.user.is_staff:
        messages.error(request, "You do not have access to the user management dashboard.")
        return redirect("core:dashboard")

    now = timezone.now()
    search_query = request.GET.get("q", "").strip()
    plan_filter = request.GET.get("plan", "all")

    base_profiles = UserProfile.objects.select_related("user")
    total_users = base_profiles.count()
    premium_users = base_profiles.filter(is_premium=True).count()
    trialing_users = 0
    free_users = max(total_users - premium_users, 0)
    active_last_7 = base_profiles.filter(user__last_login__gte=now - timedelta(days=7)).count()
    active_last_30 = base_profiles.filter(user__last_login__gte=now - timedelta(days=30)).count()
    premium_last_7 = base_profiles.filter(
        is_premium=True,
        premium_since__isnull=False,
        premium_since__gte=now - timedelta(days=7),
    ).count()

    plan_breakdown = [
        {"label": "Premium", "value": premium_users},
        {"label": "Free", "value": free_users},
    ]

    signup_window_start = now.date() - timedelta(days=13)
    signup_counts = (
        User.objects.filter(date_joined__date__gte=signup_window_start)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(total=Count("id"))
    )
    signup_map = {entry["day"]: entry["total"] for entry in signup_counts}
    chart_labels = []
    chart_values = []
    for offset in range(14):
        day = signup_window_start + timedelta(days=offset)
        chart_labels.append(day.strftime("%d %b"))
        chart_values.append(signup_map.get(day, 0))

    filtered_profiles = base_profiles
    if search_query:
        filtered_profiles = filtered_profiles.filter(
            Q(user__username__icontains=search_query)
            | Q(user__email__icontains=search_query)
            | Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
        )
    if plan_filter == "premium":
        filtered_profiles = filtered_profiles.filter(is_premium=True)
    elif plan_filter == "free":
        filtered_profiles = filtered_profiles.filter(is_premium=False)

    table_limit_setting = getattr(settings, "ADMIN_USER_TABLE_LIMIT", 250)
    try:
        table_limit = int(table_limit_setting)
    except (TypeError, ValueError):
        table_limit = 250
    table_limit = max(table_limit, 50)

    table_profiles = list(filtered_profiles.order_by("-user__date_joined")[: table_limit + 1])
    table_truncated = len(table_profiles) > table_limit
    if table_truncated:
        table_profiles = table_profiles[:table_limit]

    context = {
        "total_users": total_users,
        "premium_users": premium_users,
        "trialing_users": trialing_users,
        "free_users": free_users,
        "active_last_7": active_last_7,
        "active_last_30": active_last_30,
        "premium_last_7": premium_last_7,
        "plan_breakdown": plan_breakdown,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "table_profiles": table_profiles,
        "table_truncated": table_truncated,
        "table_limit": table_limit,
        "search_query": search_query,
        "plan_filter": plan_filter,
        "recent_signups": User.objects.order_by("-date_joined")[:5],
        "recent_premium": base_profiles.filter(
            is_premium=True, premium_since__isnull=False
        ).order_by("-premium_since")[:5],
        "premium_percentage": round((premium_users / total_users) * 100, 1) if total_users else 0,
        "trial_percentage": 0,
        "free_percentage": round((free_users / total_users) * 100, 1) if total_users else 0,
        "now": now,
    }
    return render(request, "core/admin_users.html", context)


@login_required
@require_POST
def admin_toggle_premium(request, user_id: int):
    if not request.user.is_staff:
        return _json_error("You do not have permission to perform this action.", status=403)

    profile = UserProfile.objects.select_related("user").filter(user__pk=user_id).first()
    if not profile:
        return _json_error("User not found.", status=404)

    mode = request.POST.get("make_premium")
    if mode == "1":
        profile.set_premium(True)
    elif mode == "0":
        profile.set_premium(False)
    else:
        profile.set_premium(not profile.is_premium)

    logger.info(
        "Admin %s toggled premium for user %s to %s",
        request.user.pk,
        profile.user.pk,
        "premium" if profile.is_premium else "free",
    )

    return JsonResponse(
        {
            "ok": True,
            "user": profile.user.username,
            "is_premium": profile.is_premium,
            "plan_type": profile.plan_type,
            "premium_since": profile.premium_since.isoformat() if profile.premium_since else None,
        }
    )


def send_weekly_admin_report() -> bool:
    metrics = _collect_admin_metrics()
    admin_recipients = [
        email for _, email in getattr(settings, "ADMINS", []) if email
    ]
    if not admin_recipients:
        default_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
        if default_email:
            admin_recipients.append(default_email)
    if not admin_recipients:
        logger.info("Skipping weekly admin report: no recipients configured.")
        return False

    context = {
        "date": timezone.now().date(),
        "bug_reports": metrics["bug_reports"],
        "support_msgs": metrics["support_msgs"],
        "exports": metrics["exports"],
        "deletions": metrics["deletions"],
        "premium_users": metrics["premium_users"],
        "active_users": metrics["active_users"],
    }
    subject = f"PredictMyGrade Weekly Analytics Report ({metrics['date_range']})"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@predictmygrade.local")
    html_body = render_to_string("core/email_weekly_report.html", context)
    text_body = strip_tags(html_body)

    try:
        send_mail(
            subject=subject,
            message=text_body,
            from_email=from_email,
            recipient_list=admin_recipients,
            html_message=html_body,
        )
        logger.info("Weekly admin report sent to %s", ", ".join(admin_recipients))
        return True
    except Exception:
        logger.exception("Failed to send weekly admin report")
        return False


@login_required
def system_health(request):
    if not (request.user.is_superuser or request.user.is_staff):
        messages.error(request, "System health is available to staff accounts.")
        return redirect("core:dashboard")

    metrics = _collect_system_health_metrics()
    db_size_bytes = metrics["db"].get("size_bytes")
    db_size_mb = (
        round(db_size_bytes / (1024 * 1024), 2)
        if isinstance(db_size_bytes, (int, float))
        else None
    )
    db_size_display = f"{db_size_mb:.2f} MB" if db_size_mb is not None else "n/a"
    uptime_display = str(metrics["uptime"]).split(".")[0]
    cancellation_notice = request.session.pop(CANCELLATION_NOTICE_SESSION_KEY, None)

    context = {
        "db": metrics["db"],
        "db_size_mb": db_size_mb,
        "db_size_display": db_size_display,
        "counts": metrics["counts"],
        "ai_status": metrics["ai_status"],
        "latest_snapshot": metrics["latest_snapshot"],
        "latest_insight": metrics["latest_insight"],
        "pending_deadlines": metrics["pending_deadlines"],
        "uptime": metrics["uptime"],
        "uptime_display": uptime_display,
        "system_time": timezone.now(),
        "user_role": _get_user_role(request.user),
    }
    return render(request, "core/system_health.html", context)


def _comparison_payload(user) -> List[Dict[str, float]]:
    snapshots = list(
        PredictionSnapshot.objects.filter(user=user).order_by("-created_at")[:2]
    )
    if len(snapshots) < 2:
        return [{"label": "Not enough snapshots yet", "change": 0.0}]
    current, previous = snapshots[0], snapshots[1]
    comparisons: List[Dict[str, float]] = [
        {
            "label": "Overall Average",
            "change": round((current.average_percent or 0) - (previous.average_percent or 0), 1),
        }
    ]
    recent_modules = Module.objects.filter(user=user, level="UNI").order_by("-created_at")[:5]
    baseline = previous.average_percent or 0
    for module in recent_modules:
        delta = (module.grade_percent or 0) - baseline
        comparisons.append({"label": module.name, "change": round(delta, 1)})
    return comparisons


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------
def home_redirect(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return redirect("account_login")


@login_required
def post_login_redirect(request):
    profile = get_profile(request.user)
    next_url = _safe_next_url(request)
    skip_requested = request.GET.get("skip_welcome") == "1"
    if skip_requested and not profile.has_seen_welcome:
        profile.has_seen_welcome = True
        profile.save(update_fields=["has_seen_welcome"])
    if not skip_requested and not profile.has_seen_welcome:
        welcome_url = reverse("core:welcome")
        if next_url:
            welcome_url = f"{welcome_url}?next={quote(next_url)}"
        return redirect(welcome_url)
    return redirect(next_url or "core:dashboard")


@login_required
def welcome_hub(request):
    profile = get_profile(request.user)
    if not profile.has_seen_welcome:
        profile.has_seen_welcome = True
        profile.save(update_fields=["has_seen_welcome"])

    modules_qs = Module.objects.filter(user=request.user, level="UNI")
    modules_total = modules_qs.count()
    modules_with_grades = modules_qs.filter(grade_percent__isnull=False).count()

    goals_total = StudyGoal.objects.filter(user=request.user).count()

    today = date.today()
    deadlines_qs = UpcomingDeadline.objects.filter(
        user=request.user, completed=False, due_date__gte=today
    ).order_by("due_date")
    deadlines_total = deadlines_qs.count()
    next_deadline_obj = deadlines_qs.first()
    next_deadline = None
    if next_deadline_obj:
        days_remaining = (next_deadline_obj.due_date - today).days
        next_deadline = {
            "title": next_deadline_obj.title,
            "due_display": next_deadline_obj.due_date.strftime("%d %b"),
            "module": getattr(getattr(next_deadline_obj, "module", None), "name", ""),
            "days_remaining": days_remaining,
        }

    at_risk_module = None
    at_risk_candidate = (
        modules_qs.filter(grade_percent__isnull=False)
        .order_by("grade_percent", "-credits", "name")
        .first()
    )
    if at_risk_candidate and (at_risk_candidate.grade_percent or 0) < 60:
        target_score, target_label = next_threshold(at_risk_candidate.grade_percent or 0)
        at_risk_module = {
            "name": at_risk_candidate.name,
            "grade": round(at_risk_candidate.grade_percent or 0, 1),
            "credits": at_risk_candidate.credits or 0,
            "target_score": target_score,
            "target_label": target_label,
        }

    def _cta(icon, label, url_name, url_kwargs=None):
        return {
            "icon": icon,
            "label": label,
            "url": reverse(url_name, kwargs=url_kwargs or {}),
        }

    hero_primary_cta = _cta("fa-chart-column", "Go to Grade Tracker", "core:dashboard")
    if modules_total == 0:
        hero_primary_cta = _cta("fa-layer-group", "Add your first module", "core:modules_list")
    elif modules_with_grades == 0:
        hero_primary_cta = _cta("fa-pen-to-square", "Log an assessment", "core:modules_list")
    elif deadlines_total == 0:
        hero_primary_cta = _cta("fa-calendar-plus", "Schedule a deadline", "core:study_goals")

    hero_secondary_cta = _cta("fa-flask", "Open What-If Lab", "core:what_if_simulation")
    if at_risk_module:
        hero_secondary_cta = _cta(
            "fa-person-running",
            f"Stabilise {at_risk_module['name']}",
            "core:modules_list",
        )
    hero_help_cta = _cta("fa-circle-question", "Help & tours", "core:help")

    onboarding_steps = [
        {
            "id": "modules",
            "title": "Add your modules",
            "description": "List every course you want PredictMyGrade to track.",
            "done": modules_total > 0,
            "action": reverse("core:modules_list"),
        },
        {
            "id": "assessments",
            "title": "Record an assessment",
            "description": "Log at least one grade so we can trend your progress.",
            "done": modules_with_grades > 0,
            "action": reverse("core:modules_list"),
        },
        {
            "id": "goals",
            "title": "Pin a study goal",
            "description": "Create a weekly goal or milestone to keep momentum.",
            "done": goals_total > 0,
            "action": reverse("core:study_goals"),
        },
        {
            "id": "deadlines",
            "title": "Track a deadline",
            "description": "Add an upcoming submission to surface reminders here.",
            "done": deadlines_total > 0,
            "action": reverse("core:study_goals"),
        },
    ]
    steps_completed = sum(1 for step in onboarding_steps if step["done"])
    onboarding_complete = steps_completed == len(onboarding_steps)
    onboarding_progress_label = (
        "Setup complete" if onboarding_complete else f"{steps_completed}/{len(onboarding_steps)} steps done"
    )

    personalised_note = None
    if next_deadline:
        if next_deadline["days_remaining"] <= 0:
            personalised_note = f"{next_deadline['title']} is due today."
        else:
            personalised_note = f"{next_deadline['title']} is due in {next_deadline['days_remaining']} day(s)."
    elif at_risk_module:
        personalised_note = (
            f"{at_risk_module['name']} needs {at_risk_module['target_score']}% to reach the next band."
        )

    continue_url = _safe_next_url(request)

    context = {
        "next_deadline": next_deadline,
        "at_risk_module": at_risk_module,
        "hero_primary_cta": hero_primary_cta,
        "hero_secondary_cta": hero_secondary_cta,
        "hero_help_cta": hero_help_cta,
        "onboarding_steps": onboarding_steps,
        "onboarding_complete": onboarding_complete,
        "onboarding_progress_label": onboarding_progress_label,
        "personalised_note": personalised_note,
        "continue_url": continue_url,
    }
    return render(request, "core/welcome.html", context)


@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("account_login")


def login_required_view(request):
    next_url = request.GET.get("next")
    if next_url and not url_has_allowed_host_and_scheme(next_url, {request.get_host()}, require_https=request.is_secure()):
        next_url = None

    login_url = None
    for name in ("account_login", "login", "admin:login"):
        try:
            login_url = reverse(name)
            break
        except NoReverseMatch:
            continue

    if not login_url:
        login_url = "/"

    query = {}
    if next_url:
        query["next"] = next_url

    if query:
        login_url = f"{login_url}?{urlencode(query)}"

    messages.info(request, "Please sign in to continue.")
    return redirect(login_url)


def signup_disabled_view(request):
    return HttpResponse("Sign ups are currently disabled.", status=403)


@require_POST
def mock_login(request):
    if not getattr(settings, "BILLING_MOCK_MODE", True):
        return HttpResponse(status=404)

    next_url = _safe_next_url(request)
    mock_user = None
    mock_user_id = request.session.get("mock_demo_user_id")
    if mock_user_id:
        mock_user = User.objects.filter(pk=mock_user_id).first()

    if mock_user is None:
        username = f"demo_{secrets.token_hex(4)}"
        mock_user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=secrets.token_urlsafe(16),
        )
        request.session["mock_demo_user_id"] = mock_user.pk

    login(request, mock_user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Signed in with a local demo account.")

    redirect_url = reverse("core:post_login_redirect")
    if next_url:
        redirect_url = f"{redirect_url}?{urlencode({'next': next_url})}"
    return redirect(redirect_url)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _confidence_trend_from_snapshots(snapshots, current_avg, base_confidence):
    if not snapshots:
        return []
    reference = current_avg if current_avg is not None else (snapshots[0].average_percent or 0)
    baseline = float(base_confidence) if base_confidence is not None else 75.0
    trend = []
    for snapshot in reversed(snapshots):
        avg = snapshot.average_percent if snapshot.average_percent is not None else reference
        delta = avg - reference
        value = baseline + delta * 0.3
        trend.append(round(max(30.0, min(100.0, value)), 1))
    return trend


@login_required
def dashboard(request):
    profile = get_profile(request.user)
    onboarding_seed = maybe_seed_onboarding_dataset(profile)
    if onboarding_seed.seeded:
        messages.info(
            request,
            "We added a sample study plan so you can explore the dashboard. Replace anything tagged 'Sample' with your own data when you're ready.",
        )
    onboarding_cta = onboarding_seed.cta
    premium_status = resolve_premium_status(request.user, profile=profile)
    has_access = premium_status["has_access"]
    plan_type = premium_status["plan_type"]
    billing_plan_label = None
    billing_plan_interval = None
    if has_access:
        mock_plan_type = _mock_plan_type_for_profile(profile)
        if mock_plan_type == "monthly":
            billing_plan_label = "Monthly Premium"
            billing_plan_interval = "month"
        elif mock_plan_type == "yearly":
            billing_plan_label = "Yearly Premium"
            billing_plan_interval = "year"
    if not billing_plan_label and has_access:
        if premium_status["is_trial_active"]:
            billing_plan_label = "Trial access"
        elif request.user.is_staff and not profile.is_premium:
            billing_plan_label = "Staff access"
        else:
            billing_plan_label = "Premium access"
    cancellation_notice = request.session.pop(CANCELLATION_NOTICE_SESSION_KEY, None)
    modules = list(
        Module.objects.filter(user=request.user, level="UNI").order_by("-created_at")
    )
    current_avg = _weighted_average(modules)
    average_grade = _simple_average(modules)
    average_grade_display = round(average_grade, 2)
    completed_credits = _completed_credits(modules)
    comparison_data = _comparison_payload(request.user) or []
    milestone_messages = _evaluate_milestones(profile, current_avg or 0)

    if _is_ajax(request):
        target_class = request.GET.get("target_class", "First")
        total_credits_raw = request.GET.get("total_credits")
        total_credits = (
            float(total_credits_raw)
            if total_credits_raw
            else _total_credits_target(profile)
        )
        plan = calculate_future_target(
            current_avg=current_avg,
            completed_credits=completed_credits,
            target_class=target_class,
            total_credits=total_credits,
            is_premium=has_access,
        )
        plan["completed_credits"] = round(completed_credits, 2)
        plan["total_credits"] = round(total_credits, 2)
        plan["comparison"] = comparison_data
        return JsonResponse(plan)

    gcse_avg = (
        Module.objects.filter(user=request.user, level="GCSE")
        .aggregate(avg=Avg("grade_percent"))
        .get("avg")
        or 0
    )
    college_avg = (
        Module.objects.filter(user=request.user, level__in=["ALEVEL", "BTEC"])
        .aggregate(avg=Avg("grade_percent"))
        .get("avg")
        or 0
    )

    total_credits_target = request.session.get(
        "dashboard_total_credits", _total_credits_target(profile)
    )
    request.session["dashboard_total_credits"] = total_credits_target

    target_class = "Upper Second (2:1)" if has_access else "First"
    target_plan = calculate_future_target(
        current_avg=current_avg,
        completed_credits=completed_credits,
        target_class=target_class,
        total_credits=total_credits_target,
        is_premium=has_access,
    )

    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:12]
    )
    new_achievements = evaluate_achievements(
        request.user,
        modules=modules,
        snapshots=snapshots,
        current_avg=current_avg or 0,
    )
    achievements_qs = list(
        UserAchievement.objects.filter(user=request.user).order_by("-unlocked_at")
    )
    achievements_lookup = {achievement.code: achievement for achievement in achievements_qs}
    achievement_overview = achievement_status(
        request.user, achievements=achievements_qs
    )
    new_achievement_payload = [
        {
            "code": achievement.code,
            "title": achievement.title,
            "description": achievement.description,
            "emoji": achievement.metadata.get("emoji")
            if isinstance(achievement.metadata, dict)
            else None,
            "share_url": reverse("core:achievement_share", args=[achievement.share_token]),
            "unlocked_at": timezone.localtime(achievement.unlocked_at).isoformat(),
        }
        for achievement in new_achievements
    ]
    achievement_overview_payload = []
    for item in achievement_overview:
        achievement_obj = achievements_lookup.get(item["code"])
        achievement_overview_payload.append(
            {
                **item,
                "share_url": reverse("core:achievement_share", args=[achievement_obj.share_token])
                if achievement_obj
                else None,
                "unlocked_at": timezone.localtime(achievement_obj.unlocked_at).isoformat()
                if achievement_obj
                else None,
            }
        )
    timeline_labels = [snap.created_at.strftime("%d %b") for snap in reversed(snapshots)]
    timeline_values = [snap.average_percent or 0 for snap in reversed(snapshots)]

    ai_prediction = personalised_prediction(
        user=request.user,
        avg_so_far=current_avg or 0,
        credits_done=completed_credits,
        difficulty=0.5,
        variance=0.2,
        engagement=0.7,
    )
    ai_predicted_average = ai_prediction.average
    ai_confidence = ai_prediction.confidence
    ai_model_label = ai_prediction.model_label or "Adaptive Ridge"
    confidence_trend = _confidence_trend_from_snapshots(
        snapshots, current_avg, ai_confidence
    )

    trend = "steady"
    if len(snapshots) >= 2:
        delta = (snapshots[0].average_percent or 0) - (snapshots[1].average_percent or 0)
        if delta > 0.5:
            trend = "improving"
        elif delta < -0.5:
            trend = "dropping"

    mentor_tip = generate_ai_study_tip(request.user, current_avg, trend) or "Keep refining focus."
    mentor_prompt = generate_ai_mentor_message(current_avg, trend, "motivational")

    ai_status = AIModelStatus.objects.first()

    planned_modules = list(
        PlannedModule.objects.filter(user=request.user).order_by("-created_at")
    )
    deadlines = list(
        UpcomingDeadline.objects.filter(user=request.user, completed=False).order_by("due_date")
    )
    study_goals_qs = StudyGoal.objects.filter(user=request.user).order_by("status", "due_date")
    study_goals_payload = [_serialize_goal(goal) for goal in study_goals_qs] or []
    goals_summary = _goal_summary(study_goals_qs)
    today = date.today()
    calendar_items = list(
        StudyPlan.objects.filter(
            user=request.user,
            date__range=(today, today + timedelta(days=6)),
        ).order_by("date", "title")
    )
    calendar_payload = [
        {"title": plan.title, "date": plan.date.isoformat(), "hours": float(plan.duration_hours)}
        for plan in calendar_items
    ]
    weekly_activity_data = {"labels": [], "values": []}
    for offset in range(7):
        day = today + timedelta(days=offset)
        iso = day.isoformat()
        total_hours = sum(
            plan["hours"] for plan in calendar_payload if plan["date"] == iso
        )
        weekly_activity_data["labels"].append(day.strftime("%a"))
        weekly_activity_data["values"].append(round(total_hours, 2))

    smart_alerts: List[Dict[str, str]] = []
    if current_avg and current_avg < 60:
        smart_alerts.append(
            {
                "severity": "warning",
                "text": "Your weighted average is below a 2:1. Plan revision for weaker modules.",
            }
        )
    if deadlines:
        soonest = deadlines[0]
        days_until = (soonest.due_date - today).days
        if days_until <= 3:
            smart_alerts.append(
                {
                    "severity": "critical",
                    "text": (
                        f"Deadline '{soonest.title}' is due in {days_until} day"
                        f"{'s' if days_until != 1 else ''}."
                    ),
                }
            )

    cohort_avg = (
        Module.objects.filter(level="UNI", grade_percent__isnull=False)
        .aggregate(avg=Avg("grade_percent"))
        .get("avg")
    )
    if cohort_avg is None:
        cohort_avg = current_avg

    benchmarking = {
        "user_avg": round(current_avg, 2) if current_avg else None,
        "cohort_avg": round(cohort_avg, 2) if cohort_avg else None,
        "delta": round((current_avg or 0) - (cohort_avg or 0), 2),
        "position": "On track" if (current_avg or 0) >= (cohort_avg or 0) else "Behind cohort",
        "sample_size": Module.objects.filter(level="UNI", grade_percent__isnull=False).count(),
    }

    benchmarking_pack = None
    if has_access:
        cohort_values = list(
            Module.objects.filter(level="UNI", grade_percent__isnull=False).values_list("grade_percent", flat=True)
        )
        cohort_values_numbers = [float(value) for value in cohort_values if value is not None]
        if cohort_values_numbers:
            cohort_values_numbers.sort()

            def _percentile(p: float) -> float:
                if not cohort_values_numbers:
                    return float(current_avg or 0.0)
                k = (len(cohort_values_numbers) - 1) * p
                lower = math.floor(k)
                upper = math.ceil(k)
                if lower == upper:
                    return cohort_values_numbers[int(k)]
                return cohort_values_numbers[lower] + (
                    cohort_values_numbers[upper] - cohort_values_numbers[lower]
                ) * (k - lower)

            percentiles = {
                "p25": round(_percentile(0.25), 2),
                "p50": round(_percentile(0.5), 2),
                "p75": round(_percentile(0.75), 2),
                "p90": round(_percentile(0.9), 2),
            }
            benchmarking["percentiles"] = percentiles
            avg_value = float(current_avg or 0.0)
            gap_to_top = max(0.0, percentiles["p75"] - avg_value)
            graded_modules_all = [m for m in modules if m.grade_percent is not None]
            focus_module = min(
                graded_modules_all,
                key=lambda module: module.grade_percent or 0,
                default=None,
            )
            top_module = max(
                graded_modules_all,
                key=lambda module: module.grade_percent or 0,
                default=None,
            )
            improvement_routes: list[dict[str, str]] = []
            if gap_to_top > 0.1:
                focus_label = focus_module.name if focus_module else "your next assessment"
                improvement_routes.append(
                    {
                        "title": "Sprint to the top quartile",
                        "detail": (
                            f"Close the {gap_to_top:.1f}% gap to the cohort's top quartile by planning an extra review for {focus_label}."
                        ),
                    }
                )
            if top_module and (not focus_module or top_module.id != focus_module.id):
                improvement_routes.append(
                    {
                        "title": "Leverage strengths",
                        "detail": (
                            f"Use your {top_module.name} performance ({top_module.grade_percent:.1f}%) as a template for other modules."
                        ),
                    }
                )
            if not improvement_routes:
                improvement_routes.append(
                    {
                        "title": "Maintain momentum",
                        "detail": "Keep logging weekly snapshots to protect your lead above the cohort average.",
                    }
                )
            benchmarking_pack = {
                "percentiles": percentiles,
                "gap_to_top": round(gap_to_top, 2),
                "current_avg": round(avg_value, 2),
                "routes": improvement_routes,
            }

    streak = 0
    if snapshots:
        today = timezone.now().date()
        for offset in range(0, 30):
            day = today - timedelta(days=offset)
            if not PredictionSnapshot.objects.filter(user=request.user, created_at__date=day).exists():
                break
            streak += 1

    radar_labels = [module.name for module in modules[:6]]
    radar_values = [module.grade_percent or 0 for module in modules[:6]]

    insights_qs = SmartInsight.objects.filter(user=request.user).order_by("-created_at")[:5]
    insight_feedback = _insight_feedback_totals(request.user, insights_qs)
    insights_payload = [
        _serialize_insight(insight, insight_feedback.get(insight.id))
        for insight in insights_qs
    ]

    term_stats = defaultdict(lambda: {"credits": 0, "modules": 0, "workload": Decimal("0.0")})
    status_counts = defaultdict(int)
    total_workload = Decimal("0.0")
    for module in planned_modules:
        stats = term_stats[module.term]
        workload_value = module.workload_hours if module.workload_hours is not None else Decimal("0.0")
        stats["credits"] += module.credits
        stats["modules"] += 1
        stats["workload"] += workload_value
        total_workload += workload_value
        status_counts[module.status] += 1

    planner_summary = [
        {
            "term": term,
            "credits": data["credits"],
            "modules": data["modules"],
            "workload": float(data["workload"]),
        }
        for term, data in term_stats.items()
    ]
    planner_status_summary = [
        {"status": status, "count": count} for status, count in status_counts.items()
    ]

    timeline_series = [
        {
            "date": snap.created_at.isoformat(),
            "average": snap.average_percent or 0,
            "label": snap.label or "",
            "classification": snap.classification or classify_percent(snap.average_percent or 0),
        }
        for snap in reversed(snapshots)
    ]

    timeline_events_qs = TimelineEvent.objects.filter(user=request.user).order_by("-created_at")[:12]
    timeline_events_payload = [_serialize_timeline_event(event) for event in timeline_events_qs] or []

    commands = [
        {
            "id": "snapshot",
            "label": "Take snapshot",
            "description": "Save your live dashboard metrics.",
        },
        {
            "id": "plan",
            "label": "Generate study plan",
            "description": "Let the AI assistant schedule next week.",
        },
        {
            "id": "refresh",
            "label": "Refresh dashboard",
            "description": "Sync the latest analytics instantly.",
        },
        {
            "id": "motivate",
            "label": "Daily motivation",
            "description": "Play a new motivation boost.",
        },
        {
            "id": "export",
            "label": "Export my data",
            "description": "Download a CSV backup of your workspace.",
        },
        {
            "id": "import",
            "label": "Import data",
            "description": "Restore modules and goals from CSV.",
        },
        {
            "id": "sync",
            "label": "Live sync",
            "description": "Refresh the dashboard without a full page reload.",
        },
    ]

    free_chat_limit = max(1, int(getattr(settings, "FREE_AI_CHAT_LIMIT", 2) or 2))
    assistant_personas = available_personas()
    persona = normalise_persona(getattr(profile, "ai_persona", None))
    if not has_access:
        persona = AI_PERSONA_DEFAULT
        assistant_personas = assistant_personas[:1]
        if profile.ai_persona != persona:
            profile.set_persona(persona)
    elif profile.ai_persona != persona:
        profile.set_persona(persona)

    chat_history_serialized: List[Dict[str, object]] = []
    chat_session = (
        AIChatSession.objects.filter(user=request.user, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if not chat_session:
        chat_session = AIChatSession.objects.create(user=request.user, persona=persona)
    else:
        session_persona = normalise_persona(chat_session.persona)
        target_persona = persona if has_access else AI_PERSONA_DEFAULT
        if session_persona != target_persona:
            chat_session.persona = target_persona
            chat_session.save(update_fields=["persona", "updated_at"])
        persona = target_persona
    history_window = 20 if has_access else 8
    history_qs = list(chat_session.messages.order_by("-created_at")[:history_window])
    history_qs.reverse()
    chat_history_serialized = serialize_history(history_qs)
    assistant_state_url = reverse("core:ai_forecast_state")
    study_plan_calendar_path = reverse("core:study_plan_calendar")
    try:
        study_plan_calendar_url = request.build_absolute_uri(study_plan_calendar_path)
    except DisallowedHost:
        study_plan_calendar_url = study_plan_calendar_path

    bootstrap_payload = {
        "isPremium": has_access,
        "isSubscriber": profile.is_premium,
        "planType": plan_type,
        "premiumRestricted": not has_access,
        "averages": {
            "gcse": round(gcse_avg, 2),
            "college": round(college_avg, 2),
            "university": round(current_avg, 2),
        },
        "averageGrade": average_grade_display,
        "targetPlan": target_plan,
        "timeline": {"labels": timeline_labels, "values": timeline_values},
        "timelineEvents": timeline_events_payload,
        "radar": {"labels": radar_labels, "values": radar_values},
        "plannedModules": [
            {
                "name": module.name,
                "credits": module.credits,
                "grade": module.expected_grade,
                "term": module.term,
                "category": module.category,
                "status": module.status,
                "workload": float(module.workload_hours or 0),
            }
            for module in planned_modules
        ],
        "deadlines": [
            {
                "id": deadline.pk,
                "title": deadline.title,
                "module": deadline.module.name if deadline.module else "",
                "due_date": deadline.due_date.isoformat(),
                "weight": deadline.weight,
            }
            for deadline in deadlines
        ],
        "ai": {
            "predictedAverage": ai_predicted_average,
            "confidence": ai_confidence,
            "model": ai_model_label,
            "personalWeight": ai_prediction.personal_weight,
            "tip": mentor_tip,
            "persona": persona,
            "personas": assistant_personas,
            "history": chat_history_serialized,
            "freePreview": not has_access,
            "allowPersonas": has_access,
            "previewLimit": free_chat_limit,
            "upgradeHint": (
                f"Enjoy {free_chat_limit} mentor replies per day on the free plan. "
                "Upgrade to unlock unlimited chat, persona switching, and deeper insights."
                if not has_access
                else ""
            ),
            "trial": {
                "active": profile.is_trial_active,
                "ends_at": profile.trial_ends_at.isoformat() if profile.trial_ends_at else None,
            },
            "insights": insights_payload,
            "timelineSeries": timeline_series,
            "timelineEvents": timeline_events_payload,
            "confidenceTrend": confidence_trend,
        },
        "urls": {
            "targetPlanner": reverse("core:dashboard"),
            "saveFutureModules": reverse("core:save_future_modules"),
            "saveDeadlines": reverse("core:save_upcoming_deadlines"),
            "studyGoals": reverse("core:study_goals"),
            "liveData": reverse("core:dashboard_live_data"),
            # Free accounts receive preview insights in the bootstrap/live payload.
            # Avoid calling premium-only endpoints that deliberately return 403.
            "aiInsights": reverse("core:ai_insights_feed") if has_access else None,
            "aiInsightFeedback": reverse("core:ai_insight_feedback") if has_access else None,
            "weeklyGoals": reverse("core:weekly_goals_data"),
            "studyHabits": reverse("core:study_habits_data"),
            "mentorTip": reverse("core:ai_mentor_tip"),
            "forecastChat": reverse("core:ai_forecast_chat"),
            "assistantChat": reverse("core:ai_assistant"),
            "forecastState": assistant_state_url,
            "voiceMentor": reverse("core:ai_voice_mentor"),
            "weeklyDigest": reverse("core:weekly_digest"),
            "takeSnapshot": reverse("core:create_snapshot"),
            "generatePlan": reverse("core:ai_generate_study_plan"),
            "addStudyPlan": reverse("core:add_study_plan"),
            "targetCalculator": reverse("core:target_calculator"),
            "comparison": reverse("core:snapshot_comparison"),
            "sync": reverse("core:sync_dashboard"),
            "exportData": reverse("core:export_user_data"),
            "importData": reverse("core:import_user_data"),
            "planCalendar": study_plan_calendar_path,
        },
        "completedCredits": round(completed_credits, 2),
        "totalCredits": round(total_credits_target, 2),
        "comparison": comparison_data,
        "calendar": calendar_payload,
        "commands": commands,
        "plannerSummary": planner_summary,
        "plannerStatus": planner_status_summary,
        "plannerWorkload": float(total_workload),
        "goals": {
            "items": study_goals_payload,
            "summary": goals_summary,
        },
    }
    bootstrap_payload["userRole"] = _get_user_role(request.user)
    bootstrap_payload["milestones"] = {
        "50": profile.milestone_50_unlocked,
        "60": profile.milestone_60_unlocked,
        "70": profile.milestone_70_unlocked,
    }
    bootstrap_payload["achievements"] = {
        "recent": new_achievement_payload,
        "overview": achievement_overview_payload,
    }
    if benchmarking_pack:
        bootstrap_payload["benchmarkingPack"] = benchmarking_pack

    context = {
        "is_premium": profile.is_premium,
        "has_premium_access": has_access,
        "premium_restricted": not has_access,
        "plan_type": plan_type,
        "onboarding_cta": onboarding_cta,
        "user_role": _get_user_role(request.user),
        "trial_active": premium_status["is_trial_active"],
        "trial_ends_at": profile.trial_ends_at,
        "ai_persona": persona,
        "ai_personas": assistant_personas,
        "assistant_preview_limit": free_chat_limit,
        "assistant_upgrade_hint": (
            f"Enjoy {free_chat_limit} mentor replies per day on the free plan. "
            "Upgrade to unlock unlimited chat and persona switching."
            if not has_access
            else ""
        ),
        "modules": modules,
        "avg": round(current_avg, 2) if current_avg else None,
        "average_grade": average_grade_display,
        "band": classify_percent(current_avg),
        "ai_predicted_average": ai_predicted_average,
        "ai_confidence": ai_confidence,
        "ai_model_label": ai_model_label,
        "ai_status": ai_status,
        "personal_weight": ai_prediction.personal_weight,
        "next_threshold": next_threshold(current_avg)[0] if current_avg else None,
        "smart_tip": smart_tip(current_avg),
        "mentor_tip": mentor_tip,
        "ai_tip": mentor_tip,
        "mentor_prompt": mentor_prompt,
        "smart_alerts": smart_alerts,
        "benchmarking": benchmarking,
        "benchmarking_pack": benchmarking_pack,
        "snapshot_streak": streak,
        "timeline_labels": timeline_labels,
        "timeline_values": timeline_values,
        "milestone_messages": milestone_messages,
        "planned_modules": planned_modules,
        "deadlines": deadlines,
        "study_goals": study_goals_qs,
        "goals_summary": goals_summary,
        "planner_summary": planner_summary,
        "planner_status_summary": planner_status_summary,
        "planner_workload_total": float(total_workload),
        "planner_total_goal": total_credits_target,
        "study_plan_calendar_path": study_plan_calendar_path,
        "study_plan_calendar_url": study_plan_calendar_url,
        "completedCredits": round(completed_credits, 2),
        "progress_to_target": (round(min(100.0, (completed_credits / total_credits_target) * 100), 1) if total_credits_target else 0),
        "target_label": target_class,
        "dashboard_bootstrap": json.dumps(bootstrap_payload),
        "live_snapshots": snapshots[:5],
        "smart_insights": insights_qs,
        "ai_insights": insights_payload,
        "comparison_data": comparison_data,
        "confidence_trend": confidence_trend,
        "command_palette": commands,
        "weekly_activity_data": weekly_activity_data,
        "timeline_events": timeline_events_payload,
        "calendar_data": calendar_payload,
        "new_achievements": new_achievement_payload,
        "achievement_overview": achievement_overview_payload,
        "billing_plan_label": billing_plan_label,
        "billing_plan_interval": billing_plan_interval,
        "cancellation_notice": cancellation_notice,
    }
    return render(request, "core/dashboard.html", context)

@login_required
def college_dashboard(request):
    modules_qs = Module.objects.filter(
        user=request.user, level__in=['ALEVEL', 'BTEC']
    ).order_by("-created_at")
    metrics = _collect_level_metrics(modules_qs)
    profile = get_profile(request.user)

    credit_target = 120
    completion = (
        min(100.0, round((metrics['credits_total'] / credit_target) * 100, 1))
        if metrics['credits_total']
        else 0.0
    )
    readiness_score = min(
        100.0,
        round((metrics['average'] * 0.7) + (completion * 0.3), 1),
    )

    ucas_summary = _ucas_points_summary(modules_qs)
    total_ucas_points = ucas_summary.get("total_points", 0)
    predicted_ucas_points = ucas_summary.get("predicted_points", 0)
    level_breakdown = []
    points_by_level = ucas_summary.get("points_by_level", {})
    predicted_by_level = ucas_summary.get("predicted_by_level", {})
    for key, value in points_by_level.items():
        level_breakdown.append(
            {
                'key': key,
                'label': UCAS_TARIFF_LABELS.get(key, key.replace('_', ' ').title()),
                'actual': value,
                'predicted': predicted_by_level.get(key, value),
            }
        )
    if not level_breakdown and predicted_by_level:
        for key, value in predicted_by_level.items():
            level_breakdown.append(
                {
                    'key': key,
                    'label': UCAS_TARIFF_LABELS.get(key, key.replace('_', ' ').title()),
                    'actual': 0,
                    'predicted': value,
                }
            )
    level_breakdown.sort(key=lambda row: row['label'])
    offers_qs = list(
        UcasOffer.objects.filter(user=request.user).order_by('required_points')
    )
    offer_tracker: list[dict] = []
    if offers_qs:
        for offer in offers_qs:
            target_points = offer.target_points or offer.required_points or 0
            required_delta = (offer.required_points or 0) - total_ucas_points
            predicted_delta = (offer.required_points or 0) - predicted_ucas_points
            target_delta = target_points - predicted_ucas_points
            offer_tracker.append(
                {
                    'id': offer.id,
                    'institution': offer.institution,
                    'course': offer.course,
                    'points': offer.required_points,
                    'target_points': target_points,
                    'status': offer.status,
                    'decision_type': offer.decision_type,
                    'decision_type_label': offer.get_decision_type_display(),
                    'notes': offer.notes,
                    'delta': required_delta,
                    'predicted_delta': predicted_delta,
                    'target_delta': target_delta,
                    'deadline': offer.deadline,
                    'hint': _ucas_offer_hint(ucas_summary, required_delta),
                    'is_satisfied': required_delta <= 0,
                    'predicted_on_track': predicted_delta <= 0,
                }
            )
    else:
        suggested_offers = [
            {'institution': 'Durham', 'course': 'Computer Science', 'points': 128},
            {'institution': 'Manchester', 'course': 'Business and Management', 'points': 120},
            {'institution': 'Lancaster', 'course': 'Psychology', 'points': 116},
        ]
        for offer in suggested_offers:
            delta = offer['points'] - total_ucas_points
            offer_tracker.append(
                {
                    'id': None,
                    'institution': offer['institution'],
                    'course': offer['course'],
                    'points': offer['points'],
                    'target_points': offer['points'],
                    'status': 'draft',
                    'decision_type': 'conditional',
                    'decision_type_label': 'Conditional',
                    'notes': '',
                    'delta': delta,
                    'predicted_delta': offer['points'] - predicted_ucas_points,
                    'target_delta': offer['points'] - predicted_ucas_points,
                    'deadline': None,
                    'hint': _ucas_offer_hint(ucas_summary, delta),
                    'is_satisfied': delta <= 0,
                    'predicted_on_track': (offer['points'] - predicted_ucas_points) <= 0,
                }
            )

    status_counts = Counter()
    for offer in offer_tracker:
        status_counts[offer['status']] = status_counts.get(offer['status'], 0) + 1
    offer_status_summary = [
        {'value': value, 'label': label, 'count': status_counts.get(value, 0)}
        for value, label in UcasOffer.STATUS_CHOICES
    ]
    offers_attention = sum(
        1 for offer in offer_tracker if offer['delta'] > 0 or offer['predicted_delta'] > 0
    )

    matrix_rows = []
    for module in modules_qs:
        tariff_key = _tariff_key_for_level(module.level)
        predicted_percent = module.grade_percent or 0.0
        target_percent = _target_percent(predicted_percent) or predicted_percent
        matrix_rows.append(
            {
                'name': module.name,
                'level': module.get_level_display(),
                'predicted': _grade_letter_from_percent(predicted_percent, tariff_key),
                'predicted_percent': round(predicted_percent, 1),
                'target': _grade_letter_from_percent(target_percent, tariff_key),
                'target_percent': round(target_percent, 1),
                'gap': round(target_percent - predicted_percent, 1),
            }
        )

    ps_progress, _ = PersonalStatementProgress.objects.get_or_create(
        user=request.user,
        defaults={'target_word_count': 4000},
    )
    ps_deadline = ps_progress.deadline
    if not ps_deadline:
        deadline_hit = UpcomingDeadline.objects.filter(
            user=request.user, title__icontains='statement'
        ).order_by('due_date').first()
        ps_deadline = deadline_hit.due_date if deadline_hit else None

    personal_statement_percent = 0.0
    if ps_progress.target_word_count:
        personal_statement_percent = min(
            100.0,
            round((ps_progress.word_count / ps_progress.target_word_count) * 100, 1),
        )
    personal_statement = {
        'word_count': ps_progress.word_count,
        'target': ps_progress.target_word_count or 4000,
        'last_updated': timezone.localtime(ps_progress.updated_at) if ps_progress.updated_at else None,
        'deadline': ps_deadline,
    }

    skill_states = {
        item.key: item.completed
        for item in SuperCurricularProgress.objects.filter(user=request.user)
    }
    skills_catalogue = [
        ('skill_epq', 'Complete EPQ research log'),
        ('skill_open_day', 'Attend university open day'),
        ('skill_volunteering', 'Log volunteering hours'),
        ('skill_reading', 'Add super-curricular reading reflection'),
        ('skill_admissions', 'Practice admissions test questions'),
    ]
    skills_checklist = [
        {'key': key, 'label': label, 'completed': skill_states.get(key, False)}
        for key, label in skills_catalogue
    ]

    progress_labels_json = json.dumps(metrics['progress_labels'])
    progress_values_json = json.dumps(metrics['progress_values'])
    distribution_labels_json = json.dumps(metrics['distribution_labels'])
    distribution_data_json = json.dumps(metrics['distribution_values'])

    recent_modules = [
        {
            'name': entry['name'],
            'grade': entry['grade'],
            'created': entry['created'].strftime('%d %b %Y')
            if entry['created']
            else '-',
        }
        for entry in metrics['recent_modules']
    ]

    ai_suggestions = _ai_suggestions('college', metrics)
    reflection_prompts = [
        'What was your biggest academic win this week?',
        'Which subject needs the most attention next?',
        'What support do you need before offers are released?',
    ]

    pathway_goals: List[str] = []
    if metrics['average'] < 60:
        pathway_goals.append(
            'Lift your overall average above 60% to unlock more university options.'
        )
    elif metrics['average'] < 70:
        pathway_goals.append(
            'Target distinction grades in your strongest modules to push the average higher.'
        )
    else:
        pathway_goals.append(
            'Keep your distinction average by logging progress snapshots each week.'
        )
    if completion < 80:
        pathway_goals.append('Aim to record at least 80% of your yearly credits by next term.')
    if readiness_score < 85:
        pathway_goals.append('Add a mock interview or personal statement review to your plan.')

    parent_summary = {
        'average': metrics['average'],
        'ucas_points': total_ucas_points,
        'predicted_ucas_points': predicted_ucas_points,
        'readiness': readiness_score,
        'offers': [
            {
                'institution': offer['institution'],
                'course': offer['course'],
                'points': offer['points'],
                'delta': offer['delta'],
                'predicted_delta': offer.get('predicted_delta'),
            }
            for offer in offer_tracker
        ],
        'upcoming_deadlines': _upcoming_deadlines(request.user, limit=3),
    }

    context = {
        'profile': profile,
        'average': metrics['average'],
        'module_count': metrics['module_count'],
        'credits_total': metrics['credits_total'],
        'best_module': metrics['best_module'],
        'lowest_module': metrics['lowest_module'],
        'readiness_score': readiness_score,
        'pathway_completion': completion,
        'trend_delta': metrics['trend_delta'],
        'progress_labels_json': progress_labels_json,
        'progress_values_json': progress_values_json,
        'distribution_labels_json': distribution_labels_json,
        'distribution_data_json': distribution_data_json,
        'needs_focus': metrics['needs_focus'],
        'recent_modules': recent_modules,
        'upcoming_deadlines': _upcoming_deadlines(request.user, limit=4),
        'pathway_goals': pathway_goals,
        'ucas_summary': ucas_summary,
        'offer_tracker': offer_tracker,
        'predicted_matrix': matrix_rows,
        'skills_checklist': skills_checklist,
        'personal_statement': personal_statement,
        'personal_statement_percent': personal_statement_percent,
        'offer_status_choices': UcasOffer.STATUS_CHOICES,
        'offer_status_summary': offer_status_summary,
        'offer_decision_choices': UcasOffer.DECISION_CHOICES,
        'offers_attention': offers_attention,
        'ai_suggestions': ai_suggestions,
        'reflection_prompts': reflection_prompts,
        'parent_summary': parent_summary,
        'is_premium': profile.is_premium,
        'ucas_level_labels': UCAS_TARIFF_LABELS,
        'ucas_qualifications': UCAS_QUALIFICATIONS,
        'ucas_level_breakdown': level_breakdown,
    }
    return render(request, 'core/college.html', context)


@login_required
def gcse_dashboard(request):
    modules_qs = Module.objects.filter(user=request.user, level='GCSE').order_by("-created_at")
    metrics = _collect_level_metrics(modules_qs)
    profile = get_profile(request.user)

    subject_goal = 8
    completion_rate = (
        min(100.0, round((metrics['module_count'] / subject_goal) * 100, 1))
        if subject_goal
        else 0.0
    )
    confidence_score = min(100.0, round(metrics['average'] * 1.1, 1))

    progress_labels_json = json.dumps(metrics['progress_labels'])
    progress_values_json = json.dumps(metrics['progress_values'])
    distribution_labels_json = json.dumps(metrics['distribution_labels'])
    distribution_data_json = json.dumps(metrics['distribution_values'])

    heatmap_rows = []
    for module in modules_qs:
        grade = round(module.grade_percent or 0, 1)
        confidence = 'High' if grade >= 75 else ('Medium' if grade >= 55 else 'Low')
        heatmap_rows.append(
            {
                'name': module.name,
                'grade': grade,
                'confidence': confidence,
            }
        )
    heatmap_focus = None
    heatmap_strongest = None
    if heatmap_rows:
        sorted_heatmap = sorted(heatmap_rows, key=lambda row: row['grade'])
        heatmap_focus = sorted_heatmap[0]
        heatmap_strongest = sorted_heatmap[-1]

    recent_modules = [
        {
            'name': entry['name'],
            'grade': entry['grade'],
            'created': entry['created'].strftime('%d %b %Y')
            if entry['created']
            else '-',
        }
        for entry in metrics['recent_modules']
    ]

    upcoming_deadlines = _upcoming_deadlines(request.user, limit=5)
    next_exam = next((item for item in upcoming_deadlines if item['days'] >= 0), None)

    revision_sessions = list(
        RevisionSession.objects.filter(user=request.user).order_by('scheduled_date', 'scheduled_time')
    )
    revision_schedule = []
    for session in revision_sessions:
        scheduled_date = session.scheduled_date
        scheduled_time = session.scheduled_time
        revision_schedule.append(
            {
                'id': session.id,
                'subject': session.subject,
                'date': scheduled_date.strftime('%Y-%m-%d') if scheduled_date else '',
                'display': scheduled_date.strftime('%d %b %Y') if scheduled_date else 'Date not set',
                'time': scheduled_time.strftime('%H:%M') if scheduled_time else '',
            }
        )

    past_papers = [
        {
            'id': record.id,
            'name': record.name,
            'score': record.score_percent,
            'status': record.status,
        }
        for record in PastPaperRecord.objects.filter(user=request.user)
    ]

    checklist_states = {
        item.key: item.completed
        for item in ExamChecklistProgress.objects.filter(user=request.user)
    }
    checklist_catalogue = [
        ('check_calc', 'Pack calculator and spare batteries'),
        ('check_timetable', 'Print exam timetable'),
        ('check_route', 'Plan travel route and timing'),
        ('check_snacks', 'Prepare water bottle and snacks'),
        ('check_sleep', 'Set bedtime alarm before exam day'),
    ]
    exam_checklist = [
        {'key': key, 'label': label, 'completed': checklist_states.get(key, False)}
        for key, label in checklist_catalogue
    ]

    grade_boundaries_qs = GradeBoundary.objects.filter(level='GCSE').order_by('subject', 'grade')
    if grade_boundaries_qs.exists():
        grade_boundaries = [
            {
                'subject': boundary.subject,
                'grade': boundary.grade,
                'boundary': boundary.boundary_text,
                'exam_board': boundary.exam_board,
            }
            for boundary in grade_boundaries_qs
        ]
    else:
        grade_boundaries = [
            {'subject': 'Mathematics (AQA)', 'grade': '7', 'boundary': '177 / 240', 'exam_board': 'AQA'},
            {'subject': 'English Language (Edexcel)', 'grade': '6', 'boundary': '135 / 200', 'exam_board': 'Edexcel'},
            {'subject': 'Biology (OCR)', 'grade': '7', 'boundary': '154 / 210', 'exam_board': 'OCR'},
        ]

    revision_tips: List[str] = []
    if metrics['needs_focus']:
        revision_tips.append('Start each study block with topics from your focus list below.')
    else:
        revision_tips.append('Log a fresh snapshot after each mock to keep tracking progress.')
    if metrics['trend_delta'] < 0:
        revision_tips.append('Add a quick recap session to reverse the recent dip in grades.')
    else:
        revision_tips.append('Your momentum is positive - lock in gains with a weekly practice paper.')

    trend_delta_abs = abs(metrics['trend_delta'])
    ai_suggestions = _ai_suggestions('gcse', metrics)
    reflection_prompts = [
        'What revision technique worked best this week?',
        'Which topic needs a quick recap tomorrow?',
        'How confident do you feel about your next mock?',
    ]

    parent_summary = {
        'average': metrics['average'],
        'confidence': confidence_score,
        'next_exam': next_exam,
        'upcoming_deadlines': upcoming_deadlines,
        'revision_sessions': [
            {'subject': session['subject'], 'date': session['display']}
            for session in revision_schedule[:5]
        ],
    }

    context = {
        'profile': profile,
        'average': metrics['average'],
        'module_count': metrics['module_count'],
        'completion_rate': completion_rate,
        'confidence_score': confidence_score,
        'trend_delta': metrics['trend_delta'],
        'progress_labels_json': progress_labels_json,
        'progress_values_json': progress_values_json,
        'distribution_labels_json': distribution_labels_json,
        'distribution_data_json': distribution_data_json,
        'needs_focus': metrics['needs_focus'],
        'heatmap_rows': heatmap_rows,
        'recent_modules': recent_modules,
        'upcoming_deadlines': upcoming_deadlines,
        'next_exam': next_exam,
        'revision_tips': revision_tips,
        'revision_schedule': revision_schedule,
        'past_papers': past_papers,
        'grade_boundaries': grade_boundaries,
        'exam_checklist': exam_checklist,
        'past_paper_status_choices': PastPaperRecord.STATUS_CHOICES,
        'ai_suggestions': ai_suggestions,
        'reflection_prompts': reflection_prompts,
        'parent_summary': parent_summary,
        'is_premium': profile.is_premium,
        'heatmap_strongest': heatmap_strongest,
        'heatmap_focus': heatmap_focus,
        'trend_delta_abs': trend_delta_abs,
    }
    return render(request, 'core/gcse.html', context)


@login_required
def compare_levels_view(request):
    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    gcse_avg = round(_level_average_for_user(request.user, ["GCSE"]), 2)
    college_avg = round(_level_average_for_user(request.user, ["ALEVEL", "BTEC"]), 2)
    uni_avg = round(_level_average_for_user(request.user, ["UNI"]), 2)

    context = {
        "is_premium": premium_status["has_access"],
        "gcse_avg": gcse_avg,
        "gcse_class": classify_percent(gcse_avg),
        "college_avg": college_avg,
        "college_class": classify_percent(college_avg),
        "uni_avg": uni_avg,
        "uni_class": classify_percent(uni_avg),
        "user_role": _get_user_role(request.user),
    }
    return render(request, "core/compare_levels.html", context)


@login_required
def compare_all_levels_view(request):
    gcse_avg = _level_average_for_user(request.user, ["GCSE"])
    college_avg = _level_average_for_user(request.user, ["ALEVEL", "BTEC"])
    uni_avg = _level_average_for_user(request.user, ["UNI"])
    labels = ["GCSE", "College", "University"]
    values = [round(gcse_avg, 2), round(college_avg, 2), round(uni_avg, 2)]
    insight = (
        SmartInsight.objects.filter(user=request.user)
        .order_by("-created_at")
        .values_list("summary", flat=True)
        .first()
    )

    context = {
        "labels": json.dumps(labels),
        "values": json.dumps(values),
        "insight": insight,
    }
    return render(request, "core/compare_all_levels.html", context)


@login_required
def progress_timeline_view(request):
    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user)
        .order_by("created_at")
    )
    labels = [snap.created_at.strftime("%d %b") for snap in snapshots]
    data = [round(snap.average_percent or 0, 2) for snap in snapshots]
    if not labels:
        labels = ["No snapshots"]
        data = [0]

    modules = Module.objects.filter(user=request.user, level="UNI")
    current_avg = _weighted_average(modules)
    band = classify_percent(current_avg)

    change = 0.0
    trend = "steady"
    if len(snapshots) >= 2:
        change = round((snapshots[-1].average_percent or 0) - (snapshots[-2].average_percent or 0), 2)
        if change > 0.5:
            trend = "improving"
        elif change < -0.5:
            trend = "declining"

    milestones = [
        {"label": "50% milestone", "reached": profile.milestone_50_unlocked},
        {"label": "60% milestone", "reached": profile.milestone_60_unlocked},
        {"label": "70% milestone", "reached": profile.milestone_70_unlocked},
    ]

    insights = SmartInsight.objects.filter(user=request.user).order_by("-created_at")[:5]
    comparisons = TimelineComparison.objects.filter(user=request.user).order_by("-created_at")[:10]
    ai_summary_obj = (
        AIInsightSummary.objects.filter(user=request.user)
        .order_by("-created_at")
        .first()
    )

    context = {
        "is_premium": premium_status["has_access"],
        "ai_summary": ai_summary_obj.summary_text if ai_summary_obj else None,
        "labels": json.dumps(labels),
        "data": json.dumps(data),
        "avg": round(current_avg, 2) if current_avg else 0,
        "band": band,
        "trend": trend,
        "change": change,
        "milestones": milestones,
        "insights": insights,
        "comparisons": comparisons,
    }
    return render(request, "core/progress_timeline.html", context)


# ---------------------------------------------------------------------------
# College / GCSE actions
# ---------------------------------------------------------------------------
def _redirect_or_json(request, target, payload=None):
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        if payload is None:
            payload = {'ok': True}
        return JsonResponse(payload)
    return redirect(target)


@login_required
@require_POST
def add_ucas_offer(request):
    institution = (request.POST.get('institution') or '').strip()
    course = (request.POST.get('course') or '').strip()
    points_raw = request.POST.get('points')
    status = request.POST.get('status') or 'draft'
    decision_type = request.POST.get('decision_type') or 'conditional'
    target_points_raw = request.POST.get('target_points')
    deadline = _parse_iso_date(request.POST.get('deadline'))
    if not institution or not course:
        messages.error(request, 'Institution and course are required.')
        return redirect('core:college')
    try:
        points = int(points_raw or 0)
    except (TypeError, ValueError):
        points = 0
    target_points = _safe_positive_int(target_points_raw)
    status_choices = dict(UcasOffer.STATUS_CHOICES)
    decision_choices = dict(UcasOffer.DECISION_CHOICES)
    UcasOffer.objects.create(
        user=request.user,
        institution=institution[:150],
        course=course[:150],
        required_points=max(0, points),
        target_points=target_points,
        status=status if status in status_choices else 'draft',
        decision_type=decision_type if decision_type in decision_choices else 'conditional',
        deadline=deadline,
        notes=(request.POST.get('notes') or '').strip()[:255],
    )
    messages.success(request, 'UCAS offer saved.')
    return redirect('core:college')


@login_required
@require_POST
def update_ucas_offer(request, pk: int):
    offer = get_object_or_404(UcasOffer, pk=pk, user=request.user)
    status = request.POST.get('status') or offer.status
    status_choices = dict(UcasOffer.STATUS_CHOICES)
    decision_choices = dict(UcasOffer.DECISION_CHOICES)
    if status in status_choices:
        offer.status = status
    decision_type = request.POST.get('decision_type')
    if decision_type in decision_choices:
        offer.decision_type = decision_type
    notes = request.POST.get('notes')
    if notes is not None:
        offer.notes = notes[:255]
    points = request.POST.get('points')
    if points:
        try:
            offer.required_points = max(0, int(points))
        except (TypeError, ValueError):
            pass
    target_points_raw = request.POST.get('target_points')
    if target_points_raw is not None:
        offer.target_points = _safe_positive_int(target_points_raw)
    deadline_raw = request.POST.get('deadline')
    if deadline_raw is not None:
        offer.deadline = _parse_iso_date(deadline_raw)
    offer.save()
    messages.success(request, 'Offer updated.')
    return redirect('core:college')


@login_required
@require_POST
def delete_ucas_offer(request, pk: int):
    offer = get_object_or_404(UcasOffer, pk=pk, user=request.user)
    offer.delete()
    messages.success(request, 'Offer removed.')
    return redirect('core:college')


@login_required
@require_POST
def simulate_ucas_scenario(request):
    try:
        payload = json.loads(request.body or "[]")
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    if isinstance(payload, dict):
        entries = payload.get('entries', [])
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []

    if not isinstance(entries, list):
        return JsonResponse({'error': 'Entries must be a list.'}, status=400)

    items: List[dict] = []
    base_modules = Module.objects.filter(user=request.user, level__in=['ALEVEL', 'BTEC'])
    for module in base_modules:
        tariff_key = _tariff_key_for_level(module.level)
        if not tariff_key:
            continue
        items.append(
            {
                'name': module.name,
                'tariff_key': tariff_key,
                'percent': module.grade_percent,
                'target_percent': _target_percent(module.grade_percent),
                'level_label': module.get_level_display()
                if hasattr(module, "get_level_display")
                else UCAS_TARIFF_LABELS.get(tariff_key, tariff_key.replace('_', ' ').title()),
                'grade_override': None,
            }
        )

    scenario_items: List[dict] = []
    for idx, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        qualification_raw = (entry.get('qualification') or entry.get('level') or 'ALEVEL').upper()
        tariff_key = qualification_raw
        if tariff_key not in UCAS_TARIFF:
            tariff_key = MODULE_LEVEL_TO_TARIFF.get(qualification_raw) or qualification_raw
        if tariff_key not in UCAS_TARIFF:
            continue
        subject = (entry.get('subject') or entry.get('name') or f'Scenario {idx}')[:128]
        grade_override = (entry.get('grade') or '').strip().upper()
        percent_raw = entry.get('percent')
        target_percent_raw = entry.get('target_percent')
        percent_value = None
        target_value = None
        if percent_raw not in (None, ''):
            try:
                percent_value = float(percent_raw)
            except (TypeError, ValueError):
                percent_value = None
        if target_percent_raw not in (None, ''):
            try:
                target_value = float(target_percent_raw)
            except (TypeError, ValueError):
                target_value = None
        scenario_items.append(
            {
                'name': subject,
                'tariff_key': tariff_key,
                'percent': percent_value,
                'target_percent': target_value,
                'level_label': UCAS_TARIFF_LABELS.get(tariff_key, tariff_key.replace('_', ' ').title()),
                'grade_override': grade_override or None,
            }
        )

    summary = _build_ucas_breakdown(items + scenario_items)
    offers_projection = []
    for offer in UcasOffer.objects.filter(user=request.user).order_by('required_points'):
        offers_projection.append(
            {
                'institution': offer.institution,
                'course': offer.course,
                'points': offer.required_points,
                'delta': (offer.required_points or 0) - summary['total_points'],
                'predicted_delta': (offer.required_points or 0) - summary['predicted_points'],
                'status': offer.get_status_display(),
                'decision_type': offer.get_decision_type_display(),
            }
        )

    return JsonResponse({'summary': summary, 'offers': offers_projection})


@login_required
@require_POST
def save_personal_statement(request):
    progress, _ = PersonalStatementProgress.objects.get_or_create(
        user=request.user,
        defaults={'target_word_count': 4000},
    )
    word_count_raw = request.POST.get('word_count')
    target_raw = request.POST.get('target')
    deadline_raw = request.POST.get('deadline')
    if word_count_raw is not None:
        try:
            progress.word_count = max(0, int(word_count_raw))
        except (TypeError, ValueError):
            pass
    if target_raw:
        try:
            progress.target_word_count = max(1, int(target_raw))
        except (TypeError, ValueError):
            pass
    if deadline_raw:
        try:
            progress.deadline = datetime.strptime(deadline_raw, '%Y-%m-%d').date()
        except ValueError:
            progress.deadline = None
    else:
        progress.deadline = None
    progress.save()
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse(
            {
                'ok': True,
                'word_count': progress.word_count,
                'target': progress.target_word_count,
                'deadline': progress.deadline.isoformat() if progress.deadline else None,
            }
        )
    messages.success(request, 'Personal statement progress updated.')
    return redirect('core:college')


@login_required
@require_POST
def toggle_super_curricular(request):
    key = (request.POST.get('key') or '').strip()
    completed = request.POST.get('completed') == 'true'
    if not key:
        return _redirect_or_json(request, 'core:college', {'ok': False, 'error': 'Missing key'})
    record, _ = SuperCurricularProgress.objects.get_or_create(
        user=request.user,
        key=key,
    )
    record.completed = completed
    record.completed_at = timezone.now() if completed else None
    record.save(update_fields=['completed', 'completed_at'])
    return _redirect_or_json(request, 'core:college', {'ok': True, 'completed': completed})


@login_required
@require_POST
def add_revision_session(request):
    subject = (request.POST.get('subject') or '').strip()
    date_raw = request.POST.get('date')
    time_raw = request.POST.get('time')
    if not subject or not date_raw:
        messages.error(request, 'Subject and date are required.')
        return redirect('core:gcse')
    try:
        scheduled_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, 'Invalid date provided.')
        return redirect('core:gcse')
    scheduled_time = None
    if time_raw:
        try:
            scheduled_time = datetime.strptime(time_raw, '%H:%M').time()
        except ValueError:
            scheduled_time = None
    RevisionSession.objects.create(
        user=request.user,
        subject=subject[:120],
        scheduled_date=scheduled_date,
        scheduled_time=scheduled_time,
    )
    messages.success(request, 'Revision session added.')
    return redirect('core:gcse')


@login_required
@require_POST
def delete_revision_session(request, pk: int):
    session = get_object_or_404(RevisionSession, pk=pk, user=request.user)
    session.delete()
    messages.success(request, 'Revision session removed.')
    return redirect('core:gcse')


@login_required
@require_POST
def add_past_paper(request):
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'Paper name is required.')
        return redirect('core:gcse')
    score_raw = request.POST.get('score')
    status = request.POST.get('status') or 'queued'
    try:
        score = float(score_raw) if score_raw not in (None, '') else None
        if score is not None and (not math.isfinite(score) or not 0 <= score <= 100):
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, 'Score must be a number between 0 and 100.')
        return redirect('core:gcse')
    PastPaperRecord.objects.create(
        user=request.user,
        name=name[:160],
        score_percent=score,
        status=status if status in dict(PastPaperRecord.STATUS_CHOICES) else 'queued',
    )
    messages.success(request, 'Past paper recorded.')
    return redirect('core:gcse')


@login_required
@require_POST
def update_past_paper(request, pk: int):
    record = get_object_or_404(PastPaperRecord, pk=pk, user=request.user)
    status = request.POST.get('status')
    score_raw = request.POST.get('score')
    if status in dict(PastPaperRecord.STATUS_CHOICES):
        record.status = status
    if score_raw is not None:
        try:
            score = None if score_raw == '' else float(score_raw)
            if score is not None and (not math.isfinite(score) or not 0 <= score <= 100):
                raise ValueError
            record.score_percent = score
        except (TypeError, ValueError):
            messages.error(request, 'Score must be a number between 0 and 100.')
            return redirect('core:gcse')
    record.save()
    messages.success(request, 'Past paper updated.')
    return redirect('core:gcse')


@login_required
@require_POST
def delete_past_paper(request, pk: int):
    record = get_object_or_404(PastPaperRecord, pk=pk, user=request.user)
    record.delete()
    messages.success(request, 'Past paper removed.')
    return redirect('core:gcse')


@login_required
@require_POST
def toggle_exam_checklist(request):
    key = (request.POST.get('key') or '').strip()
    completed = request.POST.get('completed') == 'true'
    if not key:
        return _redirect_or_json(request, 'core:gcse', {'ok': False, 'error': 'Missing key'})
    record, _ = ExamChecklistProgress.objects.get_or_create(
        user=request.user,
        key=key,
    )
    record.completed = completed
    record.completed_at = timezone.now() if completed else None
    record.save(update_fields=['completed', 'completed_at'])
    return _redirect_or_json(request, 'core:gcse', {'ok': True, 'completed': completed})



@login_required
def dashboard_live_data(request):
    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    average_grade = _simple_average(modules)
    completed = _completed_credits(modules)
    profile = get_profile(request.user)
    total_goal = _total_credits_target(profile)

    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:12]
    )
    timeline_labels = [snap.created_at.strftime("%d %b") for snap in reversed(snapshots)]
    timeline_values = [snap.average_percent or 0 for snap in reversed(snapshots)]

    prediction = personalised_prediction(
        request.user,
        avg_so_far=avg or 0,
        credits_done=completed,
        difficulty=0.5,
        variance=0.2,
        engagement=0.7,
    )
    predicted_avg = prediction.average
    predicted_confidence = prediction.confidence
    predicted_model = prediction.model_label or "Adaptive Ridge"
    confidence_trend = _confidence_trend_from_snapshots(
        snapshots, avg, predicted_confidence
    )

    progress = min(100.0, (completed / total_goal) * 100 if total_goal else 0.0)
    today = date.today()
    calendar_items = list(
        StudyPlan.objects.filter(
            user=request.user,
            date__range=(today, today + timedelta(days=6)),
        ).order_by("date", "title")
    )
    calendar_payload = [
        {"title": plan.title, "date": plan.date.isoformat(), "hours": float(plan.duration_hours)}
        for plan in calendar_items
    ]
    insights_subset = SmartInsight.objects.filter(user=request.user).order_by("-created_at")[:3]
    subset_feedback = _insight_feedback_totals(request.user, insights_subset)
    insights_payload = [
        _serialize_insight(insight, subset_feedback.get(insight.id))
        for insight in insights_subset
    ]
    timeline_series = [
        {
            "date": snap.created_at.isoformat(),
            "average": snap.average_percent or 0,
            "classification": snap.classification or classify_percent(snap.average_percent or 0),
        }
        for snap in reversed(snapshots)
    ]
    timeline_events = TimelineEvent.objects.filter(user=request.user).order_by("-created_at")[:12]
    timeline_events_payload = [_serialize_timeline_event(event) for event in timeline_events]
    goals_summary = _goal_summary(StudyGoal.objects.filter(user=request.user))

    payload = {
        "avg": round(avg, 2),
        "average_grade": round(average_grade, 2),
        "band": classify_percent(avg),
        "progress": round(progress, 2),
        "confidence_trend": confidence_trend,
        "timeline_labels": timeline_labels,
        "timeline_values": timeline_values,
        "timeline_series": timeline_series,
        "timeline_events": timeline_events_payload,
        "generated_at": timezone.now().isoformat(),
        "comparison": _comparison_payload(request.user),
        "calendar": [
            {
                "title": plan.title,
                "date": plan.date.isoformat(),
                "hours": float(plan.duration_hours),
            }
            for plan in calendar_items
        ],
    }
    payload.update(
        {
            "ai_predicted_average": predicted_avg,
            "ai_confidence": predicted_confidence,
            "ai_model": predicted_model,
            "ai_personal_weight": prediction.personal_weight,
            "ai_insights": insights_payload,
        }
    )
    payload["goal_summary"] = goals_summary
    return JsonResponse(payload)


@login_required
def ai_insights_feed(request):
    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    if not premium_status["has_access"]:
        return _json_error("Upgrade to unlock AI insights.", status=403)

    insights_qs = list(
        SmartInsight.objects.filter(user=request.user).order_by("-created_at")[:5]
    )
    if not insights_qs:
        try:
            insights_qs = generate_insights_for_user(profile)
        except Exception:
            logger.exception("Failed to generate AI insights on demand for %s", request.user.pk)
            insights_qs = []

    feedback_map = _insight_feedback_totals(request.user, insights_qs)
    insights_payload = [
        _serialize_insight(insight, feedback_map.get(insight.pk))
        for insight in insights_qs
    ]
    return JsonResponse({"ok": True, "insights": insights_payload})


@login_required
@require_POST
def record_ai_insight_feedback(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    insight_id = payload.get("insight_id")
    if not insight_id:
        return _json_error("Insight identifier is required.", status=422)

    try:
        rating = int(payload.get("rating", 0))
    except (TypeError, ValueError):
        return _json_error("Rating value is invalid.", status=422)

    if rating not in (-1, 0, 1):
        return _json_error("Rating must be -1, 0, or 1.", status=422)

    insight = get_object_or_404(SmartInsight, pk=insight_id, user=request.user)

    if rating == 0:
        AIInsightFeedback.objects.filter(user=request.user, insight=insight).delete()
    else:
        AIInsightFeedback.objects.update_or_create(
            user=request.user,
            insight=insight,
            defaults={
                "rating": rating,
                "comment": (payload.get("comment") or "").strip(),
            },
        )

    feedback_map = _insight_feedback_totals(request.user, [insight])
    feedback = feedback_map.get(insight.pk, {"helpful": 0, "not_helpful": 0, "user_rating": 0})
    return JsonResponse({"ok": True, "feedback": feedback})


@premium_required
@login_required
def ai_reports(request):

    summaries = list(
        AIInsightSummary.objects.filter(user=request.user)
        .order_by("-created_at", "-id")[:12]
    )
    insights = list(
        SmartInsight.objects.filter(user=request.user).order_by("-created_at", "-id")[:10]
    )
    comparisons = list(
        TimelineComparison.objects.filter(user=request.user).order_by("-created_at", "-id")[:12]
    )
    summary_text = summaries[0].summary_text if summaries else "No AI summaries yet."

    context = {
        "summary_text": summary_text,
        "summaries": summaries,
        "insights": insights,
        "comparisons": comparisons,
    }
    return render(request, "core/ai_reports.html", context)


@login_required
@require_POST
def export_ai_report_pdf(request):
    profile = get_profile(request.user)
    if not resolve_premium_status(request.user, profile=profile)["has_access"]:
        return _json_error("Upgrade to Premium to export AI reports.", status=403)

    try:
        json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        pass

    summaries = list(
        AIInsightSummary.objects.filter(user=request.user)
        .order_by("-created_at")[:5]
    )
    insights = list(
        SmartInsight.objects.filter(user=request.user).order_by("-created_at")[:5]
    )
    comparisons = list(
        TimelineComparison.objects.filter(user=request.user).order_by("-created_at")[:5]
    )
    modules = Module.objects.filter(user=request.user, level="UNI")
    current_avg = _weighted_average(modules)
    completed_credits = _completed_credits(modules)

    generated_at = timezone.localtime(timezone.now())
    display_name = request.user.get_full_name() or request.user.username

    lines: List[str] = [
        f"Student: {display_name}",
        f"Generated: {generated_at:%d %b %Y %H:%M}",
        f"Current average: {current_avg:.1f}% across {modules.count()} modules",
        f"Completed credits: {completed_credits:.1f}",
    ]

    if summaries:
        lines.append("Latest AI summary:")
        lines.extend(summaries[0].summary_text.splitlines())
        for summary in summaries[1:]:
            lines.append(
                f"- {summary.created_at:%d %b}: engagement {summary.average_engagement:.2f}, "
                f"difficulty {summary.average_difficulty:.2f}, predicted {summary.average_predicted:.2f}%"
            )

    if insights:
        lines.append("Recent smart insights:")
        for insight in insights:
            lines.extend([f"- {insight.summary}"])

    if comparisons:
        lines.append("Trend comparisons:")
        for comp in comparisons:
            direction = "up" if comp.change_percent >= 0 else "down"
            lines.append(
                f"- {comp.start_date:%d %b} to {comp.end_date:%d %b}: "
                f"{comp.change_percent:+.2f}% ({direction})"
            )

    pdf_bytes = _generate_simple_pdf("PredictMyGrade AI Report", lines)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="predictmygrade_ai_report.pdf"'
    return response


# ---------------------------------------------------------------------------
# Planner storage
# ---------------------------------------------------------------------------
@login_required
@require_POST
def save_future_modules(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    if not isinstance(payload, list):
        return _json_error("Expecting a list of modules.")

    PlannedModule.objects.filter(user=request.user).delete()
    UpcomingDeadline.objects.filter(
        user=request.user, notes__startswith="AUTO-PLANNER"
    ).delete()
    StudyPlan.objects.filter(user=request.user, notes="AUTO-PLANNER").delete()

    created = 0
    auto_deadlines = 0
    auto_plans = 0
    workload_total = Decimal("0.0")
    term_breakdown: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"credits": 0, "modules": 0, "workload": Decimal("0.0")}
    )
    today = timezone.now().date()
    valid_terms = {choice[0] for choice in PlannedModule.TERM_CHOICES}
    valid_categories = {choice[0] for choice in PlannedModule.CATEGORY_CHOICES}
    valid_statuses = {choice[0] for choice in PlannedModule.STATUS_CHOICES}
    term_offsets = {
        "Term 1": 30,
        "Term 2": 120,
        "Term 3": 210,
        "Semester 1": 60,
        "Semester 2": 180,
        "Year-long": 240,
    }

    for index, entry in enumerate(payload):
        name = (entry.get("name") or "").strip()
        credits = entry.get("credits")
        grade = entry.get("grade")
        if not name:
            continue
        term = (entry.get("term") or "Term 1").strip() or "Term 1"
        category = (entry.get("category") or "Core").strip() or "Core"
        status = (entry.get("status") or "Planned").strip() or "Planned"
        workload = entry.get("workload")
        if term not in valid_terms:
            term = "Term 1"
        if category not in valid_categories:
            category = "Core"
        if status not in valid_statuses:
            status = "Planned"
        try:
            workload_value = Decimal(str(workload or 0)).quantize(Decimal("0.1"))
        except (ArithmeticError, ValueError):
            workload_value = Decimal("0.0")
        module_obj = PlannedModule.objects.create(
            user=request.user,
            name=name[:128],
            credits=max(0, int(float(credits or 0))),
            expected_grade=float(grade) if grade not in (None, "") else None,
            term=term,
            category=category,
            status=status,
            workload_hours=workload_value,
        )
        created += 1
        workload_total += workload_value
        term_stats = term_breakdown[term]
        term_stats["credits"] += module_obj.credits
        term_stats["modules"] += 1
        term_stats["workload"] += workload_value

        # Auto-generate a milestone deadline and study goal for each planned module
        base_offset = term_offsets.get(term, 30 * (index + 1))
        due_date = today + timedelta(days=base_offset)
        UpcomingDeadline.objects.create(
            user=request.user,
            module=module_obj,
            title=f"{module_obj.name} milestone",
            due_date=due_date,
            weight=1.0,
            notes="AUTO-PLANNER",
        )
        auto_deadlines += 1

        plan_date = max(today, due_date - timedelta(days=7))
        StudyPlan.objects.create(
            user=request.user,
            module=None,
            title=f"Prep for {module_obj.name}",
            date=plan_date,
            duration_hours=Decimal("2.0"),
            notes="AUTO-PLANNER",
        )
        auto_plans += 1

    term_summary = [
        {
            "term": term,
            "credits": int(data["credits"]),
            "modules": int(data["modules"]),
            "workload": float(data["workload"]),
        }
        for term, data in term_breakdown.items()
    ]

    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "auto_deadlines": auto_deadlines,
            "auto_plans": auto_plans,
            "workload_total": float(workload_total),
            "term_summary": term_summary,
        }
    )


@login_required
@require_POST
def save_upcoming_deadlines(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    if not isinstance(payload, list):
        return _json_error("Expecting a list of deadlines.")

    UpcomingDeadline.objects.filter(user=request.user, completed=False).delete()
    created = 0
    for entry in payload:
        title = (entry.get("title") or "Task").strip()
        due = entry.get("due_date")
        weight = float(entry.get("weight") or 1.0)
        module_name = entry.get("module")
        if not due:
            continue
        due_date = datetime.strptime(due, "%Y-%m-%d").date()
        module = None
        if module_name:
            module = PlannedModule.objects.filter(user=request.user, name__iexact=module_name).first()
        UpcomingDeadline.objects.create(
            user=request.user,
            module=module,
            title=title[:150],
            due_date=due_date,
            weight=max(0.1, weight),
        )
        created += 1
    return JsonResponse({"ok": True, "created": created})


@login_required
@require_POST
def deadline_complete(request, pk: int):
    deadline = get_object_or_404(UpcomingDeadline, pk=pk, user=request.user)
    deadline.completed = True
    deadline.save(update_fields=["completed"])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def deadline_reschedule(request, pk: int):
    deadline = get_object_or_404(UpcomingDeadline, pk=pk, user=request.user)
    raw_date = request.POST.get("due_date")
    raw_days = request.POST.get("days")
    new_date = None
    if raw_date:
        try:
            new_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            return _json_error("Invalid date format.")
    elif raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            return _json_error("Invalid snooze window.")
        new_date = deadline.due_date + timedelta(days=days)
    else:
        return _json_error("No reschedule value provided.")

    today = date.today()
    new_date = max(today, new_date)
    deadline.due_date = new_date
    deadline.save(update_fields=["due_date"])
    return JsonResponse(
        {
            "ok": True,
            "due_date": new_date.isoformat(),
            "due_display": new_date.strftime("%b %d, %Y"),
            "days_remaining": (new_date - today).days,
        }
    )


@login_required
@require_POST
def deadline_update(request, pk: int):
    deadline = get_object_or_404(UpcomingDeadline, pk=pk, user=request.user)
    field = request.POST.get("field")
    value = (request.POST.get("value") or "").strip()
    if field not in {"title", "module", "weight"}:
        return _json_error("Unsupported field.")
    if field == "title":
        if not value:
            return _json_error("Title cannot be empty.")
        deadline.title = value[:150]
        deadline.save(update_fields=["title"])
        return JsonResponse({"ok": True, "title": deadline.title})
    if field == "weight":
        try:
            weight = float(value)
        except ValueError:
            return _json_error("Weight must be numeric.")
        weight = max(0.1, min(weight, 20.0))
        deadline.weight = weight
        deadline.save(update_fields=["weight"])
        return JsonResponse({"ok": True, "weight": weight})
    # module update
    module_obj = None
    if value:
        module_obj = PlannedModule.objects.filter(user=request.user, name__iexact=value).first()
    deadline.module = module_obj
    deadline.save(update_fields=["module"])
    return JsonResponse(
        {"ok": True, "module": module_obj.name if module_obj else "", "module_id": module_obj.pk if module_obj else None}
    )


@login_required
@require_POST
def deadline_move_to_plan(request, pk: int):
    deadline = get_object_or_404(UpcomingDeadline, pk=pk, user=request.user)
    hours_raw = request.POST.get("hours")
    try:
        duration_hours = float(hours_raw) if hours_raw is not None else max(1.0, deadline.weight * 1.5)
    except ValueError:
        duration_hours = max(1.0, deadline.weight * 1.5)
    duration_hours = max(0.5, min(duration_hours, 8.0))
    plan = StudyPlan.objects.create(
        user=request.user,
        module=None,
        title=f"Prep: {deadline.title}",
        date=deadline.due_date,
        duration_hours=duration_hours,
        notes="Auto-created from deadline",
    )
    payload = {
        "title": plan.title,
        "date": plan.date.isoformat(),
        "hours": float(plan.duration_hours),
    }
    return JsonResponse({"ok": True, "plan_item": payload})


@login_required
@require_http_methods(["GET", "POST"])
def study_goals(request):
    if request.method == "GET":
        goals = StudyGoal.objects.filter(user=request.user).order_by("status", "due_date")
        return JsonResponse(
            {
                "items": [_serialize_goal(goal) for goal in goals],
                "summary": _goal_summary(goals),
            }
        )

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    title = (data.get("title") or "").strip()
    if not title:
        return _json_error("Goal title is required.", status=422)

    description = (data.get("description") or "").strip()
    category = (data.get("category") or "academic").strip() or "academic"
    valid_categories = {choice[0] for choice in StudyGoal.CATEGORY_CHOICES}
    if category not in valid_categories:
        category = "academic"

    status = (data.get("status") or "planning").strip() or "planning"
    valid_statuses = {choice[0] for choice in StudyGoal.STATUS_CHOICES}
    if status not in valid_statuses:
        status = "planning"

    due_date = None
    due_str = data.get("due_date")
    if due_str:
        try:
            due_date = date.fromisoformat(due_str)
        except ValueError:
            return _json_error("Invalid due date format.", status=422)

    target_percent = data.get("target_percent")
    try:
        target_percent = float(target_percent) if target_percent not in (None, "") else None
    except (TypeError, ValueError):
        target_percent = None

    module_name = (data.get("module_name") or "").strip()

    progress_value = data.get("progress")
    try:
        progress_value = int(progress_value)
    except (TypeError, ValueError):
        progress_value = 0
    progress_value = max(0, min(100, progress_value or 0))

    goal = StudyGoal.objects.create(
        user=request.user,
        title=title[:160],
        description=description,
        category=category,
        status=status,
        due_date=due_date,
        target_percent=target_percent,
        progress=progress_value,
        module_name=module_name[:120],
    )
    module_for_event = None
    if goal.status == "completed" or goal.progress >= 100:
        goal.mark_completed()
        module_for_event = sync_module_progress_for_goal(goal)
    goal.refresh_from_db()
    if goal.status == "completed":
        if module_for_event:
            message = (
                f"Completed goal \"{goal.title}\" — {module_for_event.name} now "
                f"{module_for_event.completion_percent:.0f}% complete."
            )
        else:
            message = f"Completed goal \"{goal.title}\"."
        _record_timeline_event(request.user, "goal_completed", message)

    goals = StudyGoal.objects.filter(user=request.user)
    return JsonResponse(
        {
            "ok": True,
            "goal": _serialize_goal(goal),
            "summary": _goal_summary(goals),
        },
        status=201,
    )


@login_required
@require_POST
def study_goal_update(request, pk: int):
    goal = get_object_or_404(StudyGoal, pk=pk, user=request.user)
    was_completed = goal.status == "completed" or goal.progress >= 100
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    if data.get("action") == "delete":
        goal.delete()
        goals = StudyGoal.objects.filter(user=request.user)
        return JsonResponse({"ok": True, "deleted": True, "summary": _goal_summary(goals)})

    update_fields: set[str] = set()
    status = data.get("status")
    valid_statuses = {choice[0] for choice in StudyGoal.STATUS_CHOICES}
    if status and status in valid_statuses and goal.status != status:
        goal.status = status
        update_fields.add("status")

    progress = data.get("progress")
    if progress is not None:
        try:
            progress_val = int(progress)
        except (TypeError, ValueError):
            progress_val = goal.progress
        progress_val = max(0, min(100, progress_val))
        if goal.progress != progress_val:
            goal.progress = progress_val
            update_fields.add("progress")

    due_str = data.get("due_date")
    if "due_date" in data:
        if due_str:
            try:
                goal.due_date = date.fromisoformat(due_str)
            except ValueError:
                return _json_error("Invalid due date format.", status=422)
        else:
            goal.due_date = None
        update_fields.add("due_date")

    if "title" in data:
        title = (data.get("title") or "").strip()
        if title:
            goal.title = title[:160]
            update_fields.add("title")

    if "description" in data:
        goal.description = (data.get("description") or "").strip()
        update_fields.add("description")

    if "module_name" in data:
        goal.module_name = (data.get("module_name") or "").strip()[:120]
        update_fields.add("module_name")

    if "category" in data:
        category = (data.get("category") or "").strip()
        valid_categories = {choice[0] for choice in StudyGoal.CATEGORY_CHOICES}
        if category in valid_categories:
            goal.category = category
            update_fields.add("category")

    if "target_percent" in data:
        target_percent = data.get("target_percent")
        try:
            goal.target_percent = float(target_percent) if target_percent not in (None, "") else None
        except (TypeError, ValueError):
            goal.target_percent = None
        update_fields.add("target_percent")

    completed_now = False
    if goal.status == "completed" or goal.progress >= 100:
        if goal.status != "completed":
            goal.status = "completed"
            update_fields.add("status")
        if goal.progress != 100:
            goal.progress = 100
            update_fields.add("progress")
        if not goal.completed_at:
            goal.completed_at = timezone.now()
            update_fields.add("completed_at")
        if not was_completed:
            completed_now = True
    else:
        if goal.completed_at:
            goal.completed_at = None
            update_fields.add("completed_at")

    if update_fields:
        update_fields.add("updated_at")
        goal.save(update_fields=list(update_fields))
        if completed_now:
            goal.refresh_from_db()
            module = sync_module_progress_for_goal(goal)
            if module:
                message = (
                    f"Completed goal \"{goal.title}\" — {module.name} now "
                    f"{module.completion_percent:.0f}% complete."
                )
            else:
                message = f"Completed goal \"{goal.title}\"."
            _record_timeline_event(request.user, "goal_completed", message)
        elif goal.status == "completed":
            sync_module_progress_for_goal(goal)

    goals = StudyGoal.objects.filter(user=request.user)
    return JsonResponse(
        {
            "ok": True,
            "goal": _serialize_goal(goal),
            "summary": _goal_summary(goals),
        }
    )


@login_required
def snapshot_comparison(request):
    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:10]
    )
    comparisons: list[dict[str, object]] = []
    overall_change = 0.0
    if snapshots:
        newest = snapshots[0]
        oldest = snapshots[-1]
        overall_change = round(
            (newest.average_percent or 0) - (oldest.average_percent or 0), 2
        )

    for idx, snap in enumerate(snapshots):
        previous = snapshots[idx + 1] if idx + 1 < len(snapshots) else None
        current_avg = round(snap.average_percent or 0, 2)
        previous_avg = round(previous.average_percent or 0, 2) if previous else None
        change_value = None
        direction = "flat"
        if previous:
            change_value = round(current_avg - (previous.average_percent or 0), 2)
            if change_value > 0.1:
                direction = "up"
            elif change_value < -0.1:
                direction = "down"
            else:
                direction = "flat"

        comparisons.append(
            {
                "id": snap.pk,
                "average": current_avg,
                "classification": snap.classification or classify_percent(current_avg),
                "label": snap.label or f"Snapshot {snap.created_at.strftime('%d %b %Y')}",
                "created_at": timezone.localtime(snap.created_at).strftime("%d %b %Y %H:%M"),
                "change": change_value,
                "direction": direction,
                "previous_average": previous_avg,
            }
        )

    comparison_list = _comparison_payload(request.user)
    wants_json = _is_ajax(request) or "json" in (request.headers.get("accept") or "").lower()
    fetch_dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if fetch_dest and fetch_dest != "document":
        wants_json = True
    if wants_json:
        return JsonResponse({"comparison": comparison_list})

    context = {
        "snapshots": comparisons,
        "has_snapshots": bool(comparisons),
        "overall_change": overall_change,
        "comparison_data": comparison_list,
    }
    return render(request, "core/snapshot_comparison.html", context)


@login_required
@require_POST
def create_snapshot(request):
    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    snapshot = PredictionSnapshot.objects.create(
        user=request.user,
        average_percent=avg,
        classification=classify_percent(avg),
    )
    _record_timeline_event(
        request.user,
        "snapshot_taken",
        f"Snapshot captured at {round(avg, 1)}%.",
    )
    generate_timeline_comparison(request.user)
    generate_smart_insight_from_comparisons(request.user)
    return JsonResponse(
        {
            "ok": True,
            "average_percent": round(avg, 2),
            "classification": snapshot.classification,
            "saved_at": timezone.localtime(snapshot.created_at).isoformat(),
            "comparison": _comparison_payload(request.user),
        }
    )


# ---------------------------------------------------------------------------
# AI helpers and live insights
# ---------------------------------------------------------------------------


def _assistant_chat_payload(
    request,
    query: str,
    *,
    persona_override: str | None = None,
) -> tuple[dict, int]:
    message = (query or "").strip()
    if not message:
        return {"ok": False, "error": "Ask a question about your study plan."}, 422

    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    has_access = premium_status["has_access"]
    free_limit = max(1, int(getattr(settings, "FREE_AI_CHAT_LIMIT", 2) or 2))
    daily_limit = 5 if has_access else free_limit

    allowed, note = _feature_guard(request, "ai.chat", daily_limit=daily_limit)
    if not allowed:
        return {
            "ok": False,
            "error": note or "Daily limit reached for this AI feature. Upgrade to Premium.",
            "requires_upgrade": True,
        }, 429

    persona_candidate = persona_override if persona_override is not None else None
    if has_access:
        persona = normalise_persona(persona_candidate or profile.ai_persona)
    else:
        persona = AI_PERSONA_DEFAULT

    modules = list(Module.objects.filter(user=request.user, level="UNI"))
    avg = _weighted_average(modules)
    completed = _completed_credits(modules)

    graded_modules = [m for m in modules if m.grade_percent is not None]
    top_module = ""
    struggling_module = ""
    if graded_modules:
        top_mod = max(graded_modules, key=lambda m: m.grade_percent or 0)
        bottom_mod = min(graded_modules, key=lambda m: m.grade_percent or 0)
        top_module = f"{top_mod.name} ({top_mod.grade_percent:.1f}%)"
        struggling_module = f"{bottom_mod.name} ({bottom_mod.grade_percent:.1f}%)"

    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:2]
    )
    trend = "steady"
    if len(snapshots) >= 2:
        delta = (snapshots[0].average_percent or 0) - (snapshots[1].average_percent or 0)
        if delta > 0.5:
            trend = "improving"
        elif delta < -0.5:
            trend = "dropping"

    upcoming_items = _upcoming_deadlines(request.user, limit=3)
    upcoming_payload = [
        {
            "title": item.get("title", "Deadline"),
            "due_in_days": int(item.get("days", 0)),
        }
        for item in upcoming_items
    ]

    session = (
        AIChatSession.objects.filter(user=request.user, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if not session:
        session = AIChatSession.objects.create(user=request.user, persona=persona)
    else:
        session_persona = normalise_persona(session.persona)
        target_persona = persona if has_access else AI_PERSONA_DEFAULT
        if session_persona != target_persona:
            session.persona = target_persona
            session.save(update_fields=["persona", "updated_at"])
        persona = target_persona

    if has_access and profile.ai_persona != persona:
        profile.set_persona(persona)

    history_window = 12 if has_access else 6
    history_messages = list(session.messages.order_by("-created_at")[:history_window])
    history_messages.reverse()

    stats = {
        "average": avg,
        "credits": completed,
        "target_class": "Upper Second (2:1)" if has_access else "First",
        "trend": trend,
        "top_module": top_module,
        "struggling_module": struggling_module,
        "upcoming_deadlines": upcoming_payload,
    }

    ai_response = fetch_chat_completion(
        messages=build_chat_messages(persona, message, history_messages, stats)
    )
    answer = ai_response.message.strip() if ai_response and ai_response.message else ""

    fallback_lines = [
        generate_ai_mentor_message(avg or 0, trend, "motivational"),
        generate_ai_study_tip(request.user, avg or 0, trend),
    ]
    fallback = " ".join(line for line in fallback_lines if line).strip()
    answer = answer or fallback or "Log a new snapshot and we will explore improvement ideas together."

    if upcoming_payload:
        nearest = upcoming_payload[0]
        days = nearest["due_in_days"]
        if days < 0:
            suffix = "days" if abs(days) != 1 else "day"
            deadline_hint = (
                f"The deadline '{nearest['title']}' passed {abs(days)} {suffix} ago. Capture a recovery plan."
            )
        else:
            suffix = "days" if days != 1 else "day"
            deadline_hint = (
                f"Next deadline '{nearest['title']}' is in {days} {suffix}. Schedule focused revision."
            )
        if "deadline" in message.lower() or days <= 7:
            if answer:
                answer = f"{answer}\n\n{deadline_hint}"
            else:
                answer = deadline_hint

    session.messages.create(role="user", content=message)
    session.messages.create(role="assistant", content=answer)
    session.save(update_fields=["updated_at"])

    history_limit = 20 if has_access else 10
    history_qs = list(session.messages.order_by("-created_at")[:history_limit])
    history_qs.reverse()
    history_serialized = serialize_history(history_qs)

    payload = {
        "ok": True,
        "answer": answer.strip(),
        "persona": persona,
        "history": history_serialized,
    }
    if not has_access:
        payload.update(
            {
                "requires_upgrade": True,
                "upgrade_hint": (
                    f"Enjoy {daily_limit} mentor replies per day on the free plan. "
                    "Upgrade to unlock unlimited chat, persona switching, and deeper progress insights."
                ),
                "free_preview": True,
            }
        )
    else:
        payload["requires_upgrade"] = False
        payload["free_preview"] = False
        payload["upgrade_hint"] = ""
    if note:
        payload["limit_note"] = note
    return payload, 200


def _study_suggestion_items(user) -> list[str]:
    modules = list(
        Module.objects.filter(user=user, level="UNI").order_by("grade_percent", "name")
    )
    graded = [module for module in modules if module.grade_percent is not None]
    suggestions: list[str] = []

    if not modules:
        return [
            "Add your first university module so PredictMyGrade can tailor study suggestions to your results.",
            "Create a study plan for your next assessment and keep the session short enough to complete consistently.",
            "Take a prediction snapshot after entering results so you can compare progress over time.",
        ]

    if graded:
        lowest = graded[0]
        average = round(_weighted_average(graded), 1)
        suggestions.append(
            f"Prioritise {lowest.name}: it is currently your lowest recorded module at {lowest.grade_percent:.1f}%."
        )
        if average < 60:
            suggestions.append(
                "Use two focused revision blocks this week on the topics costing you the most marks, then log the next result."
            )
        elif average < 70:
            suggestions.append(
                "You are close to a First-class average; target the highest-credit assessments where a small improvement has the biggest effect."
            )
        else:
            suggestions.append(
                "Your current average is strong; protect it with spaced review and timed practice instead of increasing study volume blindly."
            )
    else:
        suggestions.append(
            "Add a grade to one of your modules so the suggestions can identify your strongest and weakest areas."
        )

    next_deadline = (
        UpcomingDeadline.objects.filter(user=user, completed=False)
        .order_by("due_date")
        .first()
    )
    if next_deadline:
        suggestions.append(
            f"Plan backwards from {next_deadline.title} on {next_deadline.due_date:%d %b}: schedule the next revision block before the final 48 hours."
        )
    else:
        suggestions.append(
            "Add your next assessment deadline so the dashboard can turn these suggestions into a time-based study plan."
        )

    return suggestions[:3]


@login_required
def study_suggestions_page(request):
    return render(
        request,
        "core/study_suggestions.html",
        {"tips": _study_suggestion_items(request.user)},
    )


@login_required
def study_suggestions_data(request):
    return JsonResponse({"tips": _study_suggestion_items(request.user)})


@login_required
def ai_mentor_tip(request):
    allowed, note = _feature_guard(request, "ai.tip", daily_limit=5)
    if not allowed:
        return _json_error(note, status=429)

    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:2]
    )
    trend = "steady"
    if len(snapshots) == 2:
        delta = (snapshots[0].average_percent or 0) - (snapshots[1].average_percent or 0)
        if delta > 0.5:
            trend = "improving"
        elif delta < -0.5:
            trend = "dropping"

    tip = generate_ai_study_tip(request.user, avg, trend)

    focus_deadline = (
        UpcomingDeadline.objects.filter(user=request.user, completed=False)
        .order_by("due_date")
        .first()
    )
    today = date.today()
    if focus_deadline:
        plan_title = f"Mentor focus: {focus_deadline.title}"
        plan_date = max(today, focus_deadline.due_date - timedelta(days=1))
    else:
        plan_title = "Mentor focus session"
        plan_date = today

    plan_obj, _ = StudyPlan.objects.update_or_create(
        user=request.user,
        notes="AUTO_MENTOR",
        defaults={
            "module": None,
            "title": plan_title,
            "date": plan_date,
            "duration_hours": Decimal("1.5"),
            "notes": "AUTO_MENTOR",
        },
    )

    plan_item = {
        "title": plan_obj.title,
        "date": plan_obj.date.isoformat(),
        "hours": float(plan_obj.duration_hours),
    }

    response = {"ai_tip": tip, "plan_item": plan_item}
    if note:
        response["limit_note"] = note
    return JsonResponse(response)




@login_required
@require_POST
def ai_forecast_chat(request):
    query = request.POST.get('q', '')
    persona = request.POST.get('persona')
    payload, status_code = _assistant_chat_payload(
        request, query, persona_override=persona
    )
    return JsonResponse(payload, status=status_code)


@login_required
@require_POST
def ai_assistant(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    has_access = premium_status["has_access"]
    persona = data.get("persona") if has_access else None
    if data.get("reset"):
        session = (
            AIChatSession.objects.filter(user=request.user, is_active=True)
            .order_by("-updated_at")
            .first()
        )
        target_persona = (
            normalise_persona(persona or profile.ai_persona)
            if has_access
            else AI_PERSONA_DEFAULT
        )
        if session:
            session.messages.all().delete()
            if session.persona != target_persona:
                session.persona = target_persona
                session.save(update_fields=["persona", "updated_at"])
            else:
                session.save(update_fields=["updated_at"])
        elif has_access:
            AIChatSession.objects.create(user=request.user, persona=target_persona)
        response = {
            "ok": True,
            "history": [],
            "persona": target_persona,
            "requires_upgrade": not has_access,
            "free_preview": not has_access,
        }
        if not has_access:
            response["upgrade_hint"] = (
                "Upgrade to unlock unlimited mentor chat, persona switching, and deeper analytics."
            )
        return JsonResponse(response)

    message = (data.get("message") or "").strip()
    if not message:
        return _json_error("Type a message to chat with the assistant.", status=422)

    payload, status_code = _assistant_chat_payload(
        request,
        message,
        persona_override=persona,
    )
    return JsonResponse(payload, status=status_code)





@login_required
def ai_forecast_state(request):
    profile = get_profile(request.user)
    personas = available_personas()
    premium_status = resolve_premium_status(request.user, profile=profile)
    has_access = premium_status["has_access"]
    incoming_persona = request.POST.get("persona") if request.method == "POST" else None
    persona = normalise_persona(incoming_persona or profile.ai_persona)

    if not has_access:
        return JsonResponse(
            {
                "ok": False,
                "error": "Premium feature",
                "persona": persona,
                "personas": personas,
                "history": [],
                "trial": {
                    "active": premium_status["is_trial_active"],
                    "ends_at": profile.trial_ends_at.isoformat()
                    if profile.trial_ends_at
                    else None,
                },
            },
            status=403,
        )

    session = (
        AIChatSession.objects.filter(user=request.user, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if not session:
        session = AIChatSession.objects.create(user=request.user, persona=persona)
    else:
        session_persona = normalise_persona(session.persona)
        if request.method == "POST" and persona != session_persona:
            session.persona = persona
            session.save(update_fields=["persona", "updated_at"])
        else:
            persona = session_persona

    reset_flag = False
    if request.method == "POST":
        reset_value = (request.POST.get("reset") or "").lower()
        reset_flag = reset_value in {"1", "true", "yes", "reset"}
        if reset_flag:
            session.messages.all().delete()
            session.save(update_fields=["updated_at"])

    if profile.ai_persona != persona:
        profile.set_persona(persona)

    history_qs = list(session.messages.order_by("-created_at")[:20])
    history_qs.reverse()

    return JsonResponse(
        {
            "ok": True,
            "persona": persona,
            "personas": personas,
            "history": serialize_history(history_qs),
            "trial": {
                "active": profile.is_trial_active,
                "ends_at": profile.trial_ends_at.isoformat()
                if profile.trial_ends_at
                else None,
            },
        }
    )




@login_required
def ai_voice_mentor(request):
    allowed, note = _feature_guard(request, "ai.voice", daily_limit=2)
    if not allowed:
        return _json_error(note, status=429)

    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    msg = (
        f"Hi {request.user.first_name or request.user.username}, your current average is {avg:.1f} percent. "
        "Keep a steady rhythm this week and log a new snapshot after your next revision session."
    )
    payload = {"msg": msg}
    if note:
        payload["limit_note"] = note
    return JsonResponse(payload)



@login_required
def ai_deadline_react(request, deadline_id: int):
    deadline = get_object_or_404(UpcomingDeadline, pk=deadline_id, user=request.user)
    days = (deadline.due_date - date.today()).days
    urgency = "High" if days <= 3 else "Medium" if days <= 7 else "Low"
    return JsonResponse(
        {
            "deadline": deadline.title,
            "urgency": urgency,
            "recommendation": "Set aside a 2 hour revision block." if urgency != "Low" else "Keep it on the radar.",
        }
    )


@login_required
def ai_study_schedule(request):
    today = date.today()
    deadlines = UpcomingDeadline.objects.filter(user=request.user, completed=False)
    schedule = []
    for offset in range(5):
        day = today + timedelta(days=offset)
        slots = 2.5
        if deadlines.filter(due_date__lte=day + timedelta(days=2)).exists():
            slots += 1.5
        schedule.append({"date": day.isoformat(), "hours": round(slots, 1)})
    return JsonResponse({"days": schedule})


@login_required
def ai_revision_scheduler(request):
    modules = Module.objects.filter(user=request.user, level="UNI").order_by("-grade_percent")[:5]
    recommendations = [
        {"module": module.name, "focus": "Consolidate lecture notes", "duration": "90 min"}
        for module in modules
    ]
    return JsonResponse({"recommendations": recommendations})


@login_required
def ai_cross_level_forecast(request):
    gcse_avg = (
        Module.objects.filter(user=request.user, level="GCSE")
        .aggregate(avg=Avg("grade_percent"))
        .get("avg")
        or 0
    )
    uni_avg = _weighted_average(Module.objects.filter(user=request.user, level="UNI"))
    delta = uni_avg - gcse_avg
    return JsonResponse(
        {
            "gcse_average": round(gcse_avg, 1),
            "uni_average": round(uni_avg, 1),
            "delta": round(delta, 1),
        }
    )


@login_required
def ai_subject_radar(request):
    modules = Module.objects.filter(user=request.user, level="UNI").order_by("name")[:6]
    labels = [module.name for module in modules]
    scores = [module.grade_percent or 0 for module in modules]
    return JsonResponse({"labels": labels, "values": scores})



@login_required
def ai_forecast_hub(request):
    allowed, note = _feature_guard(request, "ai.forecast", daily_limit=2)
    if not allowed:
        return _json_error(note, status=429)

    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    completed = _completed_credits(modules)
    prediction = personalised_prediction(
        request.user,
        avg_so_far=avg or 0,
        credits_done=completed,
        difficulty=0.4,
        variance=0.2,
        engagement=0.7,
    )
    payload = {
        "current_average": round(avg, 2),
        "predicted_average": prediction.average,
        "confidence": prediction.confidence,
        "model": prediction.model_label or "Adaptive Ridge",
        "notes": "Forecast blended from historical performance, deadlines, and plan density.",
    }
    if note:
        payload["limit_note"] = note
    return JsonResponse(payload)



@login_required
def ai_study_load_dashboard(request):
    plans = StudyPlan.objects.filter(user=request.user, date__gte=date.today()).order_by("date")
    data = [
        {
            "date": plan.date.isoformat(),
            "title": plan.title,
            "hours": float(plan.duration_hours),
        }
        for plan in plans
    ]
    return JsonResponse({"plans": data})


@login_required
@require_POST
def add_study_plan(request):
    title = request.POST.get("title") or "Study Session"
    duration = float(request.POST.get("duration_hours") or 1.5)
    date_str = request.POST.get("date") or date.today().isoformat()
    plan_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    StudyPlan.objects.create(
        user=request.user,
        title=title[:100],
        duration_hours=duration,
        date=plan_date,
    )
    return redirect(request.META.get("HTTP_REFERER", reverse("core:dashboard")))



@login_required
def ai_generate_study_plan(request):
    allowed, note = _feature_guard(request, "assistant.plan", daily_limit=2)
    if not allowed:
        return _json_error(note, status=429)

    deadlines = UpcomingDeadline.objects.filter(user=request.user, completed=False).order_by("due_date")
    tips = []
    for deadline in deadlines[:4]:
        days = (deadline.due_date - date.today()).days
        tips.append(f"Focus on '{deadline.title}' within the next {days} days.")
    if not tips:
        tips.append("Use this week to revise core concepts and schedule mock assessments.")

    StudyPlan.objects.filter(user=request.user, notes="AUTO_ASSISTANT").delete()

    today = date.today()
    plan_items = []
    planned_modules = list(
        PlannedModule.objects.filter(user=request.user).order_by("-created_at")
    )

    for offset in range(7):
        day = today + timedelta(days=offset)
        nearby_deadlines = [
            d for d in deadlines if 0 <= (d.due_date - day).days <= 2
        ]
        module_ref = planned_modules[offset % len(planned_modules)] if planned_modules else None
        if nearby_deadlines:
            focus = nearby_deadlines[0]
            title = f"Sprint: {focus.title}"
            duration = Decimal("2.0")
        elif module_ref:
            title = f"Progress {module_ref.name}"
            duration = Decimal("1.5")
        else:
            title = "General revision"
            duration = Decimal("1.0")

        plan = StudyPlan.objects.create(
            user=request.user,
            module=None,
            title=title,
            date=day,
            duration_hours=duration,
            notes="AUTO_ASSISTANT",
        )
        plan_items.append(
            {
                "title": plan.title,
                "date": plan.date.isoformat(),
                "hours": float(plan.duration_hours),
            }
        )

    payload = {"msg": "Plan generated", "tips": tips, "plan_items": plan_items}
    if note:
        payload["limit_note"] = note
    return JsonResponse(payload)



@login_required
def weekly_goals_data(request):
    today = date.today()
    labels: list[str] = []
    values: list[float] = []
    totals: list[int] = []
    completed: list[int] = []
    goals = StudyGoal.objects.filter(user=request.user)
    for offset in range(4):
        week_start = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        week_end = week_start + timedelta(days=6)
        week_goals = goals.filter(
            Q(due_date__range=(week_start, week_end))
            | Q(due_date__isnull=True, created_at__date__range=(week_start, week_end))
        )
        week_total = week_goals.count()
        week_completed = week_goals.filter(status="completed").count()
        percent = round((week_completed / week_total) * 100, 1) if week_total else 0.0
        labels.append(f"Wk {week_start.strftime('%W')}")
        values.append(percent)
        totals.append(week_total)
        completed.append(week_completed)
    labels.reverse()
    values.reverse()
    totals.reverse()
    completed.reverse()
    return JsonResponse(
        {"labels": labels, "values": values, "totals": totals, "completed": completed}
    )


@login_required
def study_habits_data(request):
    today = date.today()
    labels = []
    hours = []
    plans_map = {plan.date: 0.0 for plan in StudyPlan.objects.filter(user=request.user, date__gte=today - timedelta(days=6))}
    for plan in StudyPlan.objects.filter(user=request.user, date__gte=today - timedelta(days=6)):
        plans_map[plan.date] = plans_map.get(plan.date, 0.0) + float(plan.duration_hours)
    for offset in range(7):
        day = today - timedelta(days=6 - offset)
        labels.append(day.strftime("%a"))
        hours.append(round(plans_map.get(day, random.uniform(1, 2.5)), 1))
    return JsonResponse({"labels": labels, "hours": hours})


@login_required
def ai_weekly_reflection(request):
    snapshots = list(
        PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")[:2]
    )
    if len(snapshots) == 2:
        current = snapshots[0].average_percent or 0
        previous = snapshots[1].average_percent or 0
    else:
        current = (
            PredictionSnapshot.objects.filter(user=request.user)
            .aggregate(avg=Avg("average_percent"))
            .get("avg")
            or 0
        )
        previous = current
    change = round(current - previous, 1)
    tone = "steady"
    if change > 0.5:
        tone = "rising"
    elif change < -0.5:
        tone = "dropping"
    summary = (
        f"This week your trend is {tone}. Average stands at {current:.1f}% "
        f"({change:+.1f}% vs last week). Keep refining high impact modules."
    )
    return JsonResponse({"summary": summary})


@login_required
def ai_study_schedule_week(request):
    today = date.today()
    deadlines = UpcomingDeadline.objects.filter(user=request.user, completed=False)
    payload = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        nearby = deadlines.filter(due_date__range=(day - timedelta(days=1), day + timedelta(days=1)))
        study_hours = 2.0 + (1.0 if nearby.exists() else 0.0)
        payload.append(
            {
                "date": day.isoformat(),
                "study_hours": round(study_hours, 1),
                "deadlines": [
                    {"title": d.title, "module": d.module.name if d.module else ""}
                    for d in nearby
                ],
            }
        )
    return JsonResponse({"days": payload})



@login_required
def ai_daily_motivation(request):
    quotes = [
        "Keep the tempo - small wins compound fast.",
        "Consistency beats intensity. Log one focused session today.",
        "You are one assignment closer to the results you want.",
        "Sharpen your plan, then execute with calm confidence.",
        "Balance deep work with deliberate downtime.",
        "Progress happens when preparation meets action.",
    ]
    return JsonResponse({"quote": random.choice(quotes)})


@login_required
def study_energy_data(request):
    recent = PredictionSnapshot.objects.filter(
        user=request.user,
        created_at__gte=timezone.now() - timedelta(days=7),
    ).count()
    energy = min(100, recent * 12 + random.randint(10, 25))
    return JsonResponse({"energy": energy})


@login_required
def weekly_digest(request):
    modules = list(Module.objects.filter(user=request.user, level="UNI")[:5])
    if not modules:
        return JsonResponse({"ok": False, "summary": "Add modules to generate a digest."}, status=400)
    top_module = max(modules, key=lambda m: m.grade_percent or 0)
    bottom_module = min(modules, key=lambda m: m.grade_percent or 100)
    summary = (
        f"Headline: {request.user.first_name or request.user.username}, your average is "
        f"{_weighted_average(modules):.1f}%. Top module '{top_module.name}' continues to shine, "
        f"while '{bottom_module.name}' needs fresh momentum."
    )
    tips = [
        "Log a new prediction snapshot after your next revision block.",
        "Share your plan with a mentor to keep accountability high.",
        "Schedule an early-week focus block for your lowest scoring module.",
    ]
    return JsonResponse({"ok": True, "summary": summary, "tips": tips})


@login_required
def generate_mock_data(request):
    if Module.objects.filter(user=request.user).exists():
        messages.info(request, "You already have modules. Mock data was not added.")
        return redirect("core:dashboard")

    sample_modules = [
        ("Computer Science Fundamentals", 20, 68),
        ("Data Structures", 20, 72),
        ("AI and Machine Learning", 20, 75),
        ("Database Systems", 20, 65),
    ]
    for name, credits, grade in sample_modules:
        Module.objects.create(
            user=request.user,
            name=name,
            credits=credits,
            grade_percent=grade,
            level="UNI",
        )
    messages.success(request, "Sample modules created. Explore the dashboard with demo data!")
    return redirect("core:dashboard")

# ---------------------------------------------------------------------------
# Settings and preferences
# ---------------------------------------------------------------------------
@login_required
def settings_view(request):
    profile = get_profile(request.user)
    personas = available_personas()
    current_persona = normalise_persona(getattr(profile, "ai_persona", None))

    trial_info = None

    context = {
        "profile": profile,
        "personas": personas,
        "current_persona": current_persona,
        "trial": trial_info,
        "theme": request.session.get("theme") or profile.theme or "dark",
        "user_role": _get_user_role(request.user),
        "current_page_url": request.build_absolute_uri(),
    }
    return render(request, "core/settings.html", context)


@login_required
def privacy_dashboard(request):
    exports = DataExportLog.objects.filter(user=request.user).order_by("-created_at")[:20]
    deletions = AccountDeletionLog.objects.filter(user_id=request.user.id).order_by("-deleted_at")[:5]
    context = {
        "profile": get_profile(request.user),
        "export_logs": exports,
        "deletion_logs": deletions,
    }
    return render(request, "core/privacy_dashboard.html", context)


def _collect_personal_data(user):
    payload = {
        "export_generated_at": timezone.now().isoformat(),
        "user": {
            "id": user.id,
            "username": user.get_username(),
            "email": user.email,
            "date_joined": user.date_joined,
            "is_premium": user.profile.is_premium,
        },
        "modules": list(
            Module.objects.filter(user=user).values(
                "id",
                "name",
                "level",
                "credits",
                "grade_percent",
                "completion_percent",
                "created_at",
                "updated_at",
            )
        ),
        "planned_modules": list(
            PlannedModule.objects.filter(user=user).values(
                "id",
                "name",
                "credits",
                "expected_grade",
                "term",
                "category",
                "status",
                "created_at",
                workload=F("workload_hours"),
            )
        ),
        "deadlines": list(
            UpcomingDeadline.objects.filter(user=user).values(
                "id",
                "module_id",
                "title",
                "due_date",
                "notes",
                "weight",
                "completed",
                "created_at",
            )
        ),
        "study_plans": list(
            StudyPlan.objects.filter(user=user).values(
                "id", "title", "date", "duration_hours", "notes", "created_at"
            )
        ),
        "study_goals": list(
            StudyGoal.objects.filter(user=user).values(
                "id", "title", "status", "category", "due_date", "progress", "created_at"
            )
        ),
        "snapshots": list(
            PredictionSnapshot.objects.filter(user=user).values(
                "id", "average_percent", "label", "classification", "created_at"
            )
        ),
        "timeline_events": list(
            TimelineEvent.objects.filter(user=user).values(
                "id", "event_type", "message", "created_at"
            )
        ),
        "smart_insights": list(
            SmartInsight.objects.filter(user=user).values(
                "id", "title", "summary", "impact_score", "metadata", "created_at"
            )
        ),
        "achievements": list(
            UserAchievement.objects.filter(user=user).values(
                "id", "code", "title", "description", "category", "unlocked_at"
            )
        ),
        "ai_sessions": list(
            AIChatSession.objects.filter(user=user).values(
                "id", "persona", "title", "is_active", "created_at", "updated_at"
            )
        ),
        "ai_messages": list(
            AIChatMessage.objects.filter(session__user=user)
            .order_by("created_at")
            .values("session_id", "role", "content", "created_at")
        ),
    }
    return payload


@login_required
def download_personal_data(request):
    payload = _collect_personal_data(request.user)
    record_count = sum(len(value) for value in payload.values() if isinstance(value, list))
    data = json.dumps(payload, default=str, indent=2)
    DataExportLog.objects.create(
        user=request.user,
        format="json",
        record_count=record_count,
        notes="privacy_dashboard",
    )
    filename = f"predictmygrade-data-{timezone.now():%Y%m%d}.json"
    response = HttpResponse(data, content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename=\"{filename}\"'
    return response


@login_required
@require_POST
def toggle_theme(request):
    profile = get_profile(request.user)
    current = request.session.get("theme") or profile.theme or "dark"
    new_theme = "light" if current == "dark" else "dark"
    request.session["theme"] = new_theme
    request.session.modified = True
    request.session["theme_override"] = new_theme
    if profile.theme != new_theme:
        profile.theme = new_theme
        profile.save(update_fields=["theme"])
    return redirect("core:settings")


@login_required
@require_POST
def update_settings(request):
    profile = get_profile(request.user)
    action = request.POST.get("action")

    if action == "theme":
        desired = (request.POST.get("theme") or "").strip().lower()
        if desired not in {"light", "dark"}:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": "Choose a valid theme option."}, status=400)
            messages.error(request, "Choose a valid theme option.")
        else:
            request.session["theme"] = desired
            request.session.modified = True
            if profile.theme != desired:
                profile.theme = desired
                profile.save(update_fields=["theme"])
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "theme": desired})
            request.session["theme_override"] = desired
            messages.success(request, f"Theme set to {desired.title()} mode.")
        return redirect("core:settings")

    if action == "persona":
        personas = {item["id"]: item["label"] for item in available_personas()}
        persona_id = request.POST.get("persona")
        if persona_id not in personas:
            messages.error(request, "Choose a valid assistant persona.")
            return redirect("core:settings")
        label = personas.get(persona_id, persona_id.title())
        profile.set_persona(persona_id)
        messages.success(request, f"AI assistant persona updated to {label}.")
        return redirect("core:settings")

    if action == "milestones":
        enabled = request.POST.get("milestone_effects") == "on"
        if profile.milestone_effects_enabled != enabled:
            profile.milestone_effects_enabled = enabled
            profile.save(update_fields=["milestone_effects_enabled"])
            state = "enabled" if enabled else "disabled"
            messages.success(request, f"Milestone celebrations {state}.")
        else:
            messages.info(request, "Milestone preference already set.")
        return redirect("core:settings")

    messages.error(request, "Unknown settings action.")
    return redirect("core:settings")


@login_required
@require_POST
def submit_support_request(request):
    category = request.POST.get("category", "feedback").strip().lower()
    if category not in {"bug", "feedback"}:
        category = "feedback"

    subject = (request.POST.get("subject") or "").strip()
    message_body = (request.POST.get("message") or "").strip()
    meta_value = (request.POST.get("severity") or request.POST.get("topic") or "").strip()
    page_url = (request.POST.get("page_url") or request.build_absolute_uri()).strip()
    screenshot = request.FILES.get("screenshot") if category == "bug" else None

    if not subject or not message_body:
        messages.error(request, "Please provide both a subject and details.")
        return redirect("core:settings")

    if category == "bug" and not screenshot:
        messages.error(request, "Please attach a screenshot so we can reproduce the bug.")
        return redirect("core:settings")

    support_email = getattr(settings, "SUPPORT_EMAIL", "no-reply@example.com")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", support_email)
    category_label = "Bug report" if category == "bug" else "Feedback"

    body_lines = [
        f"Category: {category_label}",
        f"Submitted by: {request.user.get_username()} (ID {request.user.id})",
        f"Email: {request.user.email or 'not provided'}",
        f"{'Severity' if category == 'bug' else 'Topic'}: {meta_value or 'not specified'}",
        f"Page: {page_url}",
        "",
        message_body,
    ]

    email_subject = f"[PredictMyGrade] {category_label}: {subject}"

    email = EmailMessage(email_subject, "\n".join(body_lines), from_email, [support_email])
    if screenshot:
        email.attach(
            screenshot.name,
            screenshot.read(),
            getattr(screenshot, "content_type", None) or "application/octet-stream",
        )

    try:
        email.send(fail_silently=False)
    except Exception:  # noqa: B902
        logger.exception("Failed to send support email for user %s", request.user.id)
        messages.error(
            request,
            "We couldn't send your message. Please try again in a moment.",
        )
    else:
        messages.success(request, "Thanks! Your message has been submitted.")

    return redirect("core:settings")


@require_http_methods(["GET", "POST"])
def contact_support(request):
    initial_email = ""
    if request.user.is_authenticated:
        initial_email = request.user.email or ""

    context: dict[str, object] = {
        "initial_email": initial_email,
    }

    if request.method == "POST":
        subject = (request.POST.get("subject") or "").strip()
        message_body = (request.POST.get("message") or "").strip()
        reply_email = (request.POST.get("email") or "").strip() or initial_email
        context["initial_email"] = reply_email

        context.update({"subject": subject, "message_body": message_body})

        if not subject or not message_body:
            messages.error(request, "Please provide both a subject and a message.")
            return render(request, "core/contact_support.html", context, status=400)

        support_email = getattr(settings, "SUPPORT_EMAIL", "no-reply@example.com")
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", support_email)

        identifier = (
            f"{request.user.get_username()} (ID {request.user.id})"
            if request.user.is_authenticated
            else "Guest user"
        )

        body_lines = [
            "Contact Support submission",
            f"Submitted by: {identifier}",
            f"Reply email: {reply_email or 'not provided'}",
            "",
            message_body,
        ]

        email = EmailMessage(
            f"[PredictMyGrade] Contact: {subject}",
            "\n".join(body_lines),
            from_email,
            [support_email],
        )
        if reply_email:
            email.reply_to = [reply_email]

        try:
            email.send(fail_silently=False)
        except Exception:  # noqa: B902
            logger.exception("Failed to send contact support email")
            messages.error(
                request,
                "We couldn't send your message right now. Please try again in a moment.",
            )
            return render(request, "core/contact_support.html", context, status=502)

        messages.success(request, "Thanks! Your message has been sent to the support team.")
        return redirect("core:contact_support")

    return render(request, "core/contact_support.html", context)


@login_required
def export_data(request):
    modules = Module.objects.filter(user=request.user).order_by("level", "name")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Level", "Credits", "Grade %"])
    for module in modules:
        writer.writerow([module.name, module.level, module.credits, module.grade_percent if module.grade_percent is not None else ""])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="predictmygrade_export.csv"'
    DataExportLog.objects.create(
        user=request.user,
        format="csv",
        record_count=modules.count(),
        notes="settings_quick_export",
    )
    return response


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------
@login_required
def delete_account(request):
    profile = get_profile(request.user)
    if request.method == "POST":
        success, updated_data, error_msg, _ = _cancel_active_subscription(profile, fail_on_missing=False)
        if not success:
            messages.error(
                request,
                error_msg or "Please cancel any premium subscription before deleting your account.",
            )
            return render(request, "core/delete_account.html")
        user = request.user
        AccountDeletionLog.objects.create(
            user_id=user.id,
            username=user.get_username(),
            email=user.email or "",
        )
        logout(request)
        user.delete()
        final_message = "Your account has been deleted. We're sorry to see you go."
        if updated_data:
            final_message += " Your premium membership will end at the close of the current billing period."
        messages.success(request, final_message)
        return redirect("account_login")

    return render(request, "core/delete_account.html")

# ---------------------------------------------------------------------------
# Module management
# ---------------------------------------------------------------------------
@login_required
def modules_list(request):
    modules = Module.objects.filter(user=request.user).order_by("-grade_percent", "-credits", "name")
    return render(request, "core/modules_list.html", {"modules": modules})


@login_required
@require_POST
def module_add(request):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    name = (request.POST.get("name") or "").strip()
    level = request.POST.get("level") or "UNI"
    valid_levels = {choice[0] for choice in Module.LEVEL_CHOICES}
    if level not in valid_levels:
        if not is_ajax:
            messages.error(request, "Choose a valid study level.")
            return redirect("core:modules_list")
        return JsonResponse({"ok": False, "error": "Choose a valid study level."}, status=400)
    credits_raw = request.POST.get("credits")
    try:
        numeric_credits = float(credits_raw or 0)
        if not math.isfinite(numeric_credits) or not numeric_credits.is_integer() or not 0 <= numeric_credits <= 100:
            raise ValueError
        credits = int(numeric_credits)
    except (TypeError, ValueError):
        if not is_ajax:
            messages.error(request, "Credits must be a whole number between 0 and 100.")
            return redirect("core:modules_list")
        return JsonResponse(
            {"ok": False, "error": "Credits must be a whole number between 0 and 100."},
            status=400,
        )
    if not name or len(name) > 128:
        error = "Enter a module name between 1 and 128 characters."
        if not is_ajax:
            messages.error(request, error)
            return redirect("core:modules_list")
        return JsonResponse({"ok": False, "error": error}, status=400)
    grade_percent_raw = request.POST.get("grade_percent")
    grade_percent = None
    if grade_percent_raw not in (None, "", "null"):
        try:
            grade_percent = float(grade_percent_raw)
            if not math.isfinite(grade_percent) or not 0 <= grade_percent <= 100:
                raise ValueError
        except (TypeError, ValueError):
            if not is_ajax:
                messages.error(request, "Grade must be a number between 0 and 100.")
                return redirect("core:modules_list")
            return JsonResponse(
                {"ok": False, "error": "Grade must be a number between 0 and 100."},
                status=400,
            )
    try:
        with transaction.atomic():
            module = Module.objects.create(
                user=request.user,
                name=name,
                level=level,
                credits=credits,
                grade_percent=grade_percent,
            )
    except IntegrityError:
        if not is_ajax:
            messages.error(request, "You already have a module with this name at this level.")
            return redirect("core:modules_list")
        return JsonResponse(
            {
                "ok": False,
                "error": "You already have a module with this name at this level.",
            },
            status=400,
        )
    _record_timeline_event(request.user, "module_added", f"Added module {module.name}.")
    messages.success(request, "Module added.")
    if not is_ajax:
        return redirect("core:modules_list")
    return JsonResponse(
        {
            "ok": True,
            "module": {
                "id": module.pk,
                "name": module.name,
                "level": module.level,
                "credits": module.credits,
                "grade_percent": module.grade_percent,
            },
        }
    )


@login_required
@require_POST
def module_update(request, pk: int):
    module = get_object_or_404(Module, pk=pk, user=request.user)
    field = request.POST.get("field")
    if field:
        if field not in {"name", "level", "credits", "grade_percent"}:
            return JsonResponse({"ok": False, "error": "Unsupported module field."}, status=400)
        request.POST = request.POST.copy()
        request.POST[field] = request.POST.get("value", "")
    changed = False
    if "name" in request.POST:
        new_name = request.POST.get("name", "").strip()
        if not new_name or len(new_name) > 128:
            return JsonResponse({"ok": False, "error": "Enter a module name between 1 and 128 characters."}, status=400)
        module.name = new_name
        changed = True
    if "credits" in request.POST:
        try:
            c = float(request.POST.get("credits"))
            if not math.isfinite(c) or not c.is_integer() or not 0 <= c <= 100:
                raise ValueError
            module.credits = int(c)
            changed = True
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": "Credits must be a whole number between 0 and 100."}, status=400
            )
    if "level" in request.POST:
        level = request.POST.get("level")
        valid_levels = {choice[0] for choice in Module.LEVEL_CHOICES}
        if level not in valid_levels:
            return JsonResponse({"ok": False, "error": "Choose a valid study level."}, status=400)
        module.level = level
        changed = True
    if "grade_percent" in request.POST:
        raw = request.POST.get("grade_percent")
        try:
            g = None if raw in (None, "", "null") else float(raw)
            if g is not None and (not math.isfinite(g) or not 0 <= g <= 100):
                raise ValueError
            module.grade_percent = g
            changed = True
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": "grade_percent must be a number 0–100"}, status=400
            )
    if changed:
        try:
            with transaction.atomic():
                module.save()
        except IntegrityError:
            return JsonResponse({"ok": False, "error": "You already have a module with this name at this level."}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "name": module.name,
            "level": module.level,
            "credits": module.credits,
            "grade_percent": module.grade_percent,
        }
    )


@login_required
def snapshot_history(request):
    snapshots = list(PredictionSnapshot.objects.filter(user=request.user).order_by("created_at"))
    return render(
        request,
        "core/snapshot_history.html",
        {
            "snapshots": list(reversed(snapshots)),
            "timeline_labels": json.dumps([timezone.localtime(item.created_at).strftime("%d %b") for item in snapshots]),
            "timeline_values": json.dumps([round(item.average_percent or 0, 2) for item in snapshots]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def what_if_basic(request):
    outcomes = []
    error = None
    if request.method == "POST":
        weighted_total = 0.0
        credit_total = 0
        for mark_raw, credit_raw in zip(request.POST.getlist("sim_mark"), request.POST.getlist("sim_credits")):
            if not mark_raw and not credit_raw:
                continue
            try:
                mark = float(mark_raw)
                credit = int(credit_raw)
                if not 0 <= mark <= 100 or not 1 <= credit <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                error = "Enter marks between 0 and 100 and credits between 1 and 100."
                outcomes = []
                break
            weighted_total += mark * credit
            credit_total += credit
            average = weighted_total / credit_total
            outcomes.append({"average": round(average, 1), "classification": classify_percent(average)})
        if not error and not outcomes:
            error = "Add at least one simulated module."
    return render(request, "core/what_if_basic.html", {"outcomes": outcomes, "error": error})


@login_required
@require_POST
def module_delete(request, pk: int):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    module = get_object_or_404(Module, pk=pk, user=request.user)
    module_name = module.name
    module.delete()
    _record_timeline_event(request.user, "module_removed", f"Removed module {module_name}.")
    messages.success(request, f"Removed module {module_name}.")
    if not is_ajax:
        return redirect("core:modules_list")
    return JsonResponse({"ok": True, "removed": pk, "name": module_name})


@login_required
def modules_stats(request):
    modules = Module.objects.filter(user=request.user, level="UNI")
    payload = {
        "count": modules.count(),
        "average": round(_weighted_average(modules), 2),
        "credits": _completed_credits(modules),
    }
    return JsonResponse(payload)

# ---------------------------------------------------------------------------
# Export & backup
# ---------------------------------------------------------------------------
@login_required
def export_user_data(request):
    payload, total_rows = build_user_data_export(request.user)
    response = HttpResponse(payload, content_type="text/csv")
    filename = f"predictmygrade_export_{timezone.now().date().isoformat()}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    DataExportLog.objects.create(
        user=request.user,
        format="csv",
        record_count=total_rows,
        notes="export_user_data",
    )
    return response


@login_required
@require_POST
def import_user_data(request):
    uploaded = (
        request.FILES.get("file")
        or request.FILES.get("data")
        or request.FILES.get("data_file")
    )
    if not uploaded:
        return _json_error("Upload a CSV file to import.")

    try:
        content = uploaded.read().decode("utf-8")
    except UnicodeDecodeError:
        return _json_error("CSV must be UTF-8 encoded.")

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return _json_error("CSV is empty.")

    created = {"modules": 0, "goals": 0, "timeline": 0}
    allowed_events = {choice[0] for choice in TimelineEvent.EVENT_CHOICES}

    def _parse_float(value):
        if value in (None, "",):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_int(value):
        if value in (None, "",):
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _parse_timestamp(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if timezone.is_naive(dt):
            try:
                return timezone.make_aware(dt)
            except Exception:
                return timezone.now()
        return dt

    for row in reader:
        section = (row.get("section") or row.get("SECTION") or "").strip().lower()
        title = (row.get("title") or row.get("TITLE") or "").strip()
        category = (row.get("category") or row.get("CATEGORY") or "").strip()
        credits_raw = row.get("credits") or row.get("CREDITS")
        grade_raw = row.get("grade") or row.get("GRADE")
        completion_raw = row.get("completion") or row.get("COMPLETION")
        notes = row.get("notes") or row.get("NOTES") or ""
        timestamp_raw = row.get("timestamp") or row.get("TIMESTAMP")

        if section == "module" and title:
            credits = max(0, _parse_int(credits_raw))
            grade = _parse_float(grade_raw)
            completion = _parse_float(completion_raw) or 0.0
            Module.objects.update_or_create(
                user=request.user,
                name=title[:128],
                level=category[:12] or "UNI",
                defaults={
                    "credits": credits,
                    "grade_percent": grade,
                    "completion_percent": max(0.0, min(100.0, completion)),
                },
            )
            created["modules"] += 1
            continue

        if section == "goal" and title:
            target = _parse_float(grade_raw)
            progress_val = max(0, min(100, _parse_int(completion_raw)))
            status = "completed" if progress_val >= 100 else "active"
            completed_at = _parse_timestamp(timestamp_raw) if status == "completed" else None
            goal, _ = StudyGoal.objects.update_or_create(
                user=request.user,
                title=title[:160],
                defaults={
                    "category": category or "academic",
                    "target_percent": target,
                    "progress": progress_val,
                    "status": status,
                    "module_name": notes[:120],
                    "completed_at": completed_at,
                },
            )
            if status == "completed":
                if not goal.completed_at:
                    goal.completed_at = timezone.now()
                    goal.save(update_fields=["completed_at"])
                sync_module_progress_for_goal(goal)
            created["goals"] += 1
            continue

        if section == "timeline":
            event_type = category or "snapshot_taken"
            if event_type not in allowed_events:
                event_type = "snapshot_taken"
            message = title or notes or "Timeline update"
            event = TimelineEvent.objects.create(
                user=request.user,
                event_type=event_type,
                message=message[:255],
            )
            timestamp_value = _parse_timestamp(timestamp_raw)
            if timestamp_value:
                TimelineEvent.objects.filter(pk=event.pk).update(created_at=timestamp_value)
            created["timeline"] += 1

    return JsonResponse({"ok": True, "created": created})


@login_required
def export_study_plan_calendar(request):
    profile = get_profile(request.user)
    if not resolve_premium_status(request.user, profile=profile)["has_access"]:
        return HttpResponse("Upgrade to Premium to unlock this feature.", status=403)

    base_path = reverse("core:study_plan_calendar")
    absolute_url = request.build_absolute_uri(base_path)

    target = (request.GET.get("target") or "").lower()
    if target == "google":
        google_url = f"https://calendar.google.com/calendar/render?cid={quote(absolute_url, safe='')}"
        return redirect(google_url)

    try:
        weeks = int(request.GET.get("weeks", 6))
    except (TypeError, ValueError):
        weeks = 6
    weeks = max(1, min(12, weeks))

    start_date = date.today()
    end_date = start_date + timedelta(weeks=weeks)
    plans = (
        StudyPlan.objects.filter(user=request.user, date__range=(start_date, end_date))
        .order_by("date", "title")
    )

    now = timezone.now()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PredictMyGrade//StudyPlan//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:PredictMyGrade Study Plan",
        "X-WR-TIMEZONE:UTC",
    ]

    for plan in plans:
        start_str = plan.date.strftime("%Y%m%d")
        end_str = (plan.date + timedelta(days=1)).strftime("%Y%m%d")
        uid_source = plan.pk or secrets.token_hex(8)
        summary = _ics_escape(plan.title or "Study session")
        duration_hours = float(plan.duration_hours) if plan.duration_hours is not None else None
        duration_text = f"{duration_hours:.1f}h focus block" if duration_hours is not None else "Focus block"
        description_parts = [duration_text]
        if plan.notes and plan.notes != "AUTO_ASSISTANT":
            description_parts.append(plan.notes)
        elif plan.notes == "AUTO_ASSISTANT":
            description_parts.append("Generated by the PredictMyGrade assistant.")
        description = _ics_escape(" | ".join(description_parts))
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid_source}@predictmygrade.ai",
                f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART;VALUE=DATE:{start_str}",
                f"DTEND;VALUE=DATE:{end_str}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                "CATEGORIES:PREDICTMYGRADE,STUDY",
                "END:VEVENT",
            ]
        )

    if not plans:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:placeholder-{secrets.token_hex(6)}@predictmygrade.ai",
                f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART;VALUE=DATE:{start_date.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(start_date + timedelta(days=1)).strftime('%Y%m%d')}",
                "SUMMARY:Plan your next study session",
                "DESCRIPTION:Use PredictMyGrade to generate a study plan and re-export your calendar.",
                "CATEGORIES:PREDICTMYGRADE,STUDY",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    ics_body = "\r\n".join(lines) + "\r\n"
    response = HttpResponse(ics_body, content_type="text/calendar")
    response["Content-Disposition"] = 'attachment; filename="predictmygrade-study-plan.ics"'
    return response


@login_required
def export_modules_csv(request):
    modules = Module.objects.filter(user=request.user).order_by("name")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Level", "Credits", "Grade Percent"])
    for module in modules:
        writer.writerow([module.name, module.level, module.credits, module.grade_percent if module.grade_percent is not None else ""])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="modules.csv"'
    return response


@login_required
def backup_json(request):
    modules = list(
        Module.objects.filter(user=request.user).values("name", "level", "credits", "grade_percent", "completion_percent")
    )
    data = {"modules": modules}
    response = HttpResponse(json.dumps(data, indent=2), content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="predictmygrade_backup.json"'
    return response


@login_required
def backup_history(request):
    now = timezone.now()

    def _age_label(dt):
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"

    def _size_label(size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / 1024:.1f} KB"

    modules_export = Module.objects.filter(user=request.user).values(
        "name",
        "level",
        "credits",
        "grade_percent",
    )
    serialized_modules = []
    for module in modules_export:
        credits_value = module["credits"]
        if isinstance(credits_value, Decimal):
            credits_value = float(credits_value)
        grade_value = module["grade_percent"]
        if isinstance(grade_value, Decimal):
            grade_value = float(grade_value)
        serialized_modules.append(
            {
                "name": module["name"],
                "level": module["level"],
                "credits": credits_value,
                "grade_percent": grade_value,
            }
        )

    backup_payload = json.dumps({"modules": serialized_modules})
    payload_bytes = backup_payload.encode("utf-8")
    base_size = max(len(payload_bytes), 180_000)

    backups_raw = [
        {
            "name": f"predictmygrade-backup-{i+1}.json",
            "size_bytes": base_size + (i * 42_000),
            "created_at": now - timedelta(hours=6 * (i + 1)),
            "url": reverse("core:backup_json"),
            "payload": backup_payload,
        }
        for i in range(3)
    ]
    backups = [
        {
            **entry,
            "size_label": _size_label(entry["size_bytes"]),
            "age_label": _age_label(entry["created_at"]),
            "created_display": timezone.localtime(entry["created_at"]).strftime("%b %d, %Y %H:%M"),
        }
        for entry in backups_raw
    ]
    context = {"backups": backups, "backups_count": len(backups)}
    return render(request, "core/backup_history.html", context)


@login_required
@require_POST
def restore_backup(request):
    uploaded = request.FILES.get("file")
    payload = None
    if uploaded:
        try:
            payload = json.load(uploaded)
        except (json.JSONDecodeError, UnicodeDecodeError):
            messages.error(request, "The uploaded file is not valid JSON.")
            return redirect("core:dashboard")
    else:
        backup_json = request.POST.get("backup_json", "").strip()
        if backup_json:
            try:
                payload = json.loads(backup_json)
            except json.JSONDecodeError:
                messages.error(request, "The selected backup data is corrupt.")
                return redirect("core:dashboard")
    if payload is None:
        messages.error(request, "Upload a valid JSON backup to restore.")
        return redirect("core:dashboard")
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("modules"), list):
            raise ValueError
        staged = []
        keys = set()
        for row in payload["modules"]:
            if not isinstance(row, dict):
                raise ValueError
            credits = float(row.get("credits", 0))
            grade = row.get("grade_percent")
            grade = None if grade in (None, "") else float(grade)
            completion = float(row.get("completion_percent", 0))
            if not math.isfinite(credits) or not credits.is_integer():
                raise ValueError
            if (grade is not None and not math.isfinite(grade)) or not math.isfinite(completion):
                raise ValueError
            module = Module(user=request.user, name=row.get("name"), level=row.get("level", "UNI"), credits=int(credits), grade_percent=grade, completion_percent=completion)
            module.full_clean(validate_unique=False, validate_constraints=False)
            key = (module.name, module.level)
            if key in keys:
                raise ValueError
            keys.add(key)
            staged.append(module)
        with transaction.atomic():
            Module.objects.filter(user=request.user).delete()
            Module.objects.bulk_create(staged)
    except (ValueError, TypeError, ValidationError, IntegrityError):
        messages.error(request, "Invalid backup. No modules were changed.")
        return redirect("core:dashboard")
    messages.success(request, "Backup restored.")
    return redirect("core:dashboard")


@login_required
def export_prediction_csv(request):
    snapshots = PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Created At", "Average %", "Classification"])
    for snap in snapshots:
        writer.writerow(
            [
                timezone.localtime(snap.created_at).isoformat(),
                snap.average_percent,
                snap.classification,
            ]
        )
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="predictions.csv"'
    return response
# ---------------------------------------------------------------------------
# Predictions & analytics
# ---------------------------------------------------------------------------

@login_required
def ai_prediction(request):
    allowed, note = _feature_guard(request, "ai.prediction", daily_limit=3)
    if not allowed:
        return _json_error(note, status=429)

    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    completed = _completed_credits(modules)
    predicted = personalised_prediction(
        request.user,
        avg_so_far=avg or 0,
        credits_done=completed,
        difficulty=0.5,
        variance=0.2,
        engagement=0.7,
    )
    classification_value = classify_percent(predicted.average)
    payload = {
        "predicted_average": predicted.average,
        "classification": classification_value,
        "predicted_classification": classification_value,
        "confidence": predicted.confidence,
        "model": predicted.model_label or "Adaptive Ridge",
    }
    if note:
        payload["limit_note"] = note
    return JsonResponse(payload)



@login_required
def predict_final_average(request):
    return ai_prediction(request)


@login_required
def target_grade_calculator_page(request):
    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    result = None
    form_values = {
        "current_avg": request.POST.get("current_avg", ""),
        "completed_credits": request.POST.get("completed_credits", ""),
        "target_class": request.POST.get("target_class", "First"),
    }

    if request.method == "POST":
        try:
            current_avg = float(form_values["current_avg"] or 0)
            completed_credits = float(form_values["completed_credits"] or 0)
        except (TypeError, ValueError):
            result = {"error": "Enter valid numeric values for average and credits."}
        else:
            total_goal = _total_credits_target(profile)
            plan = calculate_future_target(
                current_avg=current_avg,
                completed_credits=completed_credits,
                target_class=form_values["target_class"],
                total_credits=total_goal,
                is_premium=premium_status["has_access"],
            )
            if plan.get("error"):
                result = {"error": plan["error"]}
            else:
                result = plan

    context = {
        "result": result,
        "form_values": form_values,
    }
    return render(request, "core/target_grade_calculator.html", context)


@login_required
@require_POST
def target_calculator(request):
    profile = get_profile(request.user)
    premium_status = resolve_premium_status(request.user, profile=profile)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    desired = payload.get("desired_grade")
    if desired is None:
        return _json_error("Provide a desired grade percentage.", status=422)
    try:
        desired = float(desired)
    except (TypeError, ValueError):
        return _json_error("Desired grade must be numeric.", status=422)
    desired = max(0.0, min(100.0, desired))

    modules = Module.objects.filter(user=request.user, level="UNI")
    current_avg = _weighted_average(modules)
    completed_credits = _completed_credits(modules)

    total_credits = payload.get("total_credits")
    if total_credits is None:
        total_credits = request.session.get(
            "dashboard_total_credits", _total_credits_target(profile)
        )
    try:
        total_credits = float(total_credits)
    except (TypeError, ValueError):
        total_credits = _total_credits_target(profile)
    total_credits = max(total_credits, completed_credits if completed_credits else 0)

    target_class = classify_percent(desired)
    plan = calculate_future_target(
        current_avg=current_avg,
        completed_credits=completed_credits,
        target_class=target_class,
        total_credits=total_credits,
        is_premium=premium_status["has_access"],
    )
    plan["completed_credits"] = round(completed_credits, 2)
    plan["total_credits"] = round(total_credits, 2)
    plan["remaining_credits"] = round(plan.get("remaining_credits", 0), 2)
    plan["target_avg"] = round(desired, 2)
    plan["gap_to_target"] = round(max(0.0, desired - current_avg), 2)

    improve_by = payload.get("improve_by")
    boost_preview = None
    if improve_by is not None:
        try:
            improve_by = float(improve_by)
        except (TypeError, ValueError):
            improve_by = None
        if improve_by is not None:
            boosted = max(0.0, min(100.0, current_avg + improve_by))
            boost_preview = {
                "amount": round(improve_by, 2),
                "projected_average": round(boosted, 2),
                "gap_to_target": round(max(0.0, desired - boosted), 2),
            }

    suggestions: list[str] = []
    required = plan.get("required_avg_remaining")
    if required is not None:
        if required >= 80:
            suggestions.append(
                "Aim for 80%+ in your upcoming assessments; prioritise the modules with the largest credit weight."
            )
        elif required >= 70:
            suggestions.append(
                "Focus on lifting your two lowest modules by 8-10% to close the remaining gap."
            )
        elif required >= 60:
            suggestions.append(
                "Maintain consistent 65%+ performance across the remaining assessments to stay on track."
            )
        else:
            suggestions.append(
                "You are ahead of target—log regular study sessions to lock in the grade."
            )
    remaining = plan.get("remaining_credits")
    if remaining:
        suggestions.append(
            f"Remaining credits counted: {round(remaining, 1)}. Schedule revision blocks for the next {min(remaining, 40):.0f} credits."
        )

    response = {
        "ok": True,
        "current_avg": round(current_avg, 2),
        "desired_avg": round(desired, 2),
        "remaining_credits": round(plan.get("remaining_credits", 0), 2),
        "required_avg_remaining": plan.get("required_avg_remaining"),
        "plan": plan,
        "boost_preview": boost_preview,
        "suggestions": suggestions,
    }
    return JsonResponse(response)



@login_required
def predict_targets(request):
    allowed, note = _feature_guard(request, "ai.targets", daily_limit=2)
    if not allowed:
        return _json_error(note, status=429)

    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    completed = _completed_credits(modules)
    plan = calculate_future_target(avg, completed)
    if note:
        plan["limit_note"] = note
    return JsonResponse(plan)



@login_required
@require_POST
def save_prediction(request):
    modules = Module.objects.filter(user=request.user, level="UNI")
    avg = _weighted_average(modules)
    prediction = personalised_prediction(
        request.user,
        avg_so_far=avg or 0,
        credits_done=_completed_credits(modules),
        difficulty=0.5,
        variance=0.2,
        engagement=0.7,
    )
    PredictionSnapshot.objects.create(
        user=request.user,
        average_percent=prediction.average,
        classification=classify_percent(prediction.average),
        label=request.POST.get("label") or "Saved prediction",
    )
    messages.success(request, "Prediction saved.")
    return redirect("core:dashboard")


@login_required
def prediction_history(request):
    snapshots = PredictionSnapshot.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "core/prediction_history.html", {"snapshots": snapshots})


@premium_required
@login_required
@require_POST
def predict_what_if(request):

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return _json_error("Invalid payload.")

    sims = payload.get("sims") or []
    if not isinstance(sims, list) or not sims:
        return _json_error("Provide at least one scenario.", status=422)

    modules = Module.objects.filter(user=request.user, level="UNI")
    current_avg = _weighted_average(modules)
    completed_credits = _completed_credits(modules)
    current_total = current_avg * completed_credits

    predicted_points: list[float] = []
    classifications: list[str] = []
    adjusted_points: list[float] = []
    adjusted_classes: list[str] = []
    recommendations: list[str] = []
    scenario_summaries: list[dict[str, object]] = []

    try:
        target_avg = float(payload.get("target_avg", current_avg or 0))
    except (TypeError, ValueError):
        return _json_error("Target average is invalid.", status=422)
    target_avg = max(40.0, min(95.0, target_avg))

    try:
        study_hours = float(payload.get("study_hours", 0) or 0)
    except (TypeError, ValueError):
        return _json_error("Study hours value is invalid.", status=422)
    study_hours = max(0.0, min(40.0, study_hours))

    plan_weeks_raw = payload.get("plan_weeks", 4)
    try:
        plan_weeks = int(plan_weeks_raw)
    except (TypeError, ValueError):
        return _json_error("Plan weeks value is invalid.", status=422)
    plan_weeks = max(1, min(12, plan_weeks))

    start_date_str = payload.get("study_start_date")
    start_date = None
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            return _json_error("Study start date is invalid.", status=422)

    HOURS_PER_PERCENT = max(0.1, float(getattr(settings, "WHAT_IF_HOUR_BOOST", 0.65)))
    hours_effect = study_hours * HOURS_PER_PERCENT

    focus_module = (
        modules.filter(grade_percent__isnull=False).order_by("grade_percent").first()
    )
    focus_label = focus_module.name if focus_module else "your most challenging module"

    for index, sim in enumerate(sims, start=1):
        try:
            mark = float(sim.get("mark", 0))
            credits = float(sim.get("credits", 0))
        except (TypeError, ValueError):
            return _json_error(f"Scenario {index} is invalid.", status=422)

        mark = max(0.0, min(100.0, mark))
        credits = max(0.0, credits)

        denominator = completed_credits + credits
        if denominator == 0:
            projected = mark
        else:
            projected = (current_total + (mark * credits)) / denominator

        projected = max(0.0, min(100.0, projected))
        projected = round(projected, 2)
        predicted_points.append(projected)
        classification = classify_percent(projected)
        classifications.append(classification)

        adjusted = max(0.0, min(100.0, projected + hours_effect))
        adjusted = round(adjusted, 2)
        adjusted_points.append(adjusted)
        adjusted_classification = classify_percent(adjusted)
        adjusted_classes.append(adjusted_classification)

        if projected >= target_avg:
            message = (
                f"Scenario {index} already meets your target ({projected:.1f}% >= {target_avg:.1f}%)."
            )
            hours_needed = 0
        elif adjusted >= target_avg and study_hours > 0:
            hours_needed = max(0, math.ceil(max(0.0, target_avg - projected) / HOURS_PER_PERCENT))
            message = (
                f"Scenario {index} hits {adjusted:.1f}% with {study_hours:.0f} extra hour(s);"
                f" concentrate on {focus_label}."
            )
        else:
            if HOURS_PER_PERCENT:
                hours_needed = math.ceil(max(0.0, target_avg - projected) / HOURS_PER_PERCENT)
            else:
                hours_needed = 0
            if hours_needed <= 0:
                hours_needed = 1
            message = (
                f"Scenario {index} needs about {hours_needed} extra hour(s) per week to reach"
                f" {target_avg:.1f}%. Prioritise {focus_label}."
            )
        recommendations.append(message)

        raw_name = (sim.get("name") or "").strip()
        name = raw_name or f"Scenario {index}"
        improvement = projected - (current_avg or 0)
        adjusted_improvement = adjusted - (current_avg or 0)
        scenario_summaries.append(
            {
                "index": index,
                "name": name,
                "projected": projected,
                "adjusted": adjusted,
                "classification": classification,
                "adjusted_classification": adjusted_classification,
                "recommendation": message,
                "improvement": round(improvement, 2),
                "adjusted_improvement": round(adjusted_improvement, 2),
                "hours_needed": hours_needed,
            }
        )

    best_scenario_summary = None
    plan_headline = None
    plan_detail = None
    plan_timeline: list[dict[str, object]] = []
    if scenario_summaries:
        best_index = max(
            range(len(scenario_summaries)),
            key=lambda idx: (
                scenario_summaries[idx]["adjusted"],
                scenario_summaries[idx]["improvement"],
            ),
        )
        best_scenario_summary = scenario_summaries[best_index]
        plan_headline = (
            f"{best_scenario_summary['name']} is the strongest option at "
            f"{best_scenario_summary['adjusted']:.1f}% with +{study_hours:.0f}h/week."
        )
        plan_detail = best_scenario_summary["recommendation"]
        base_date = start_date or timezone.now().date()
        base_average = current_avg or 0
        target_average = max(base_average, best_scenario_summary["adjusted"])
        total_gain = target_average - base_average
        weekly_increment = total_gain / plan_weeks if plan_weeks else 0
        for offset in range(plan_weeks):
            entry_date = base_date + timedelta(weeks=offset)
            plan_timeline.append(
                {
                    "week": offset + 1,
                    "date": entry_date.isoformat(),
                    "target": round(base_average + weekly_increment * (offset + 1), 2),
                    "hours": round(study_hours, 1) if study_hours else 0,
                    "focus": focus_label,
                }
            )

    return JsonResponse(
        {
            "ok": True,
            "current": round(current_avg, 2),
            "predicted_points": predicted_points,
            "classifications": classifications,
            "adjusted_points": adjusted_points,
            "adjusted_classifications": adjusted_classes,
            "target_average": round(target_avg, 2),
            "study_hours": round(study_hours, 1),
            "hours_gain_per_week": round(hours_effect, 2),
            "recommendations": recommendations,
            "scenario_summaries": scenario_summaries,
            "best_scenario": best_scenario_summary,
            "study_start_date": start_date.isoformat() if start_date else None,
            "plan_weeks": plan_weeks,
            "plan_summary": {
                "headline": plan_headline,
                "detail": plan_detail,
                "scenarios_tested": len(scenario_summaries),
                "timeline": plan_timeline,
            },
        }
    )


@login_required
def what_if_simulation(request):
    module_limit = 20
    module_qs = Module.objects.filter(user=request.user, level="UNI").order_by("-created_at")
    module_slice = list(module_qs[: module_limit + 1])
    has_more_modules = len(module_slice) > module_limit
    if has_more_modules:
        module_slice = module_slice[:module_limit]

    hour_gain = max(0.1, float(getattr(settings, "WHAT_IF_HOUR_BOOST", 0.65)))
    return render(
        request,
        "core/what_if.html",
        {
            "modules": module_slice,
            "hour_gain": hour_gain,
            "module_limit": module_limit,
            "has_more_modules": has_more_modules,
        },
    )


@login_required
def what_if_history(request):
    return render(request, "core/what_if_history.html")


@login_required
def achievements_center(request):
    profile = get_profile(request.user)
    achievements_qs = list(
        UserAchievement.objects.filter(user=request.user).order_by("-unlocked_at")
    )
    overview = achievement_status(request.user, achievements=achievements_qs)

    achievements_for_view = []
    recently_unlocked = []
    for item in overview:
        achievement_obj = next(
            (ach for ach in achievements_qs if ach.code == item["code"]), None
        )
        share_url = (
            request.build_absolute_uri(
                reverse("core:achievement_share", args=[achievement_obj.share_token])
            )
            if achievement_obj
            else None
        )
        achievements_for_view.append(
            {
                **item,
                "share_url": share_url,
                "unlocked_at": achievement_obj.unlocked_at if achievement_obj else None,
            }
        )

    for achievement in achievements_qs[:5]:
        recently_unlocked.append(
            {
                "code": achievement.code,
                "title": achievement.title,
                "description": achievement.description,
                "emoji": achievement.metadata.get("emoji")
                if isinstance(achievement.metadata, dict)
                else None,
                "share_url": request.build_absolute_uri(
                    reverse("core:achievement_share", args=[achievement.share_token])
                ),
                "unlocked_at": achievement.unlocked_at,
            }
        )

    unlocked_count = sum(1 for item in achievements_for_view if item["unlocked"])
    next_focus = next((item for item in achievements_for_view if not item["unlocked"]), None)

    context = {
        "profile": profile,
        "achievements": achievements_for_view,
        "unlocked_count": unlocked_count,
        "total_count": len(achievements_for_view),
        "recent_achievements": recently_unlocked,
        "next_focus": next_focus,
    }
    return render(request, "core/achievements.html", context)


def achievement_share(request, token: str):
    achievement = get_object_or_404(UserAchievement, share_token=token)
    metadata = achievement.metadata if isinstance(achievement.metadata, dict) else {}
    emoji = metadata.get("emoji") or "🌟"
    display_name = achievement.user.first_name or achievement.user.username
    context = {
        "achievement": achievement,
        "display_name": display_name,
        "emoji": emoji,
        "unlocked_at": timezone.localtime(achievement.unlocked_at),
    }
    return render(request, "core/achievement_share.html", context)


def whats_new(request):
    """
    Render the public What's New page with admin-managed entries.
    Superusers can preview unpublished items to verify copy before going live.
    """

    ordering = ("display_order", "-published_at", "-created_at")
    if request.user.is_authenticated and request.user.is_superuser:
        entries = WhatsNewEntry.objects.all().order_by(*ordering)
    else:
        entries = WhatsNewEntry.objects.filter(is_published=True).order_by(*ordering)
    return render(request, "core/whats_new.html", {"entries": entries})


@login_required
def sync_dashboard(request):
    return dashboard_live_data(request)

@login_required
def dashboard_data(request):
    return dashboard_live_data(request)

# ---------------------------------------------------------------------------
# Billing / Premium
# ---------------------------------------------------------------------------
@login_required
def pricing(request):
    context = {
        "billing_checkout_available": _billing_checkout_enabled(),
        "billing_is_mock": getattr(settings, "BILLING_MOCK_MODE", True),
    }
    return render(request, "core/pricing.html", context)


@login_required
def upgrade_page(request):
    profile = get_profile(request.user)

    def _price_snapshot(plan_type: str) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "id": plan_type,
            "unit_amount": None,
            "currency": None,
            "interval": None,
            "display": None,
            "nickname": None,
        }
        if plan_type == "yearly":
            snapshot.update(
                {
                    "currency": "GBP",
                    "interval": "year",
                    "display": "99.00 GBP/year",
                    "nickname": "Yearly Plan",
                }
            )
        else:
            snapshot.update(
                {
                    "currency": "GBP",
                    "interval": "month",
                    "display": "9.99 GBP/month",
                    "nickname": "Monthly Plan",
                }
            )
        return snapshot

    context = {
        "monthly_snapshot": _price_snapshot("monthly"),
        "yearly_snapshot": _price_snapshot("yearly"),
        "promotion_code": settings.UPGRADE_PROMO_CODE,
        "billing_is_mock": getattr(settings, "BILLING_MOCK_MODE", True),
        "billing_checkout_available": _billing_checkout_enabled(),
    }

    subscription_summary = None
    if profile.is_premium and getattr(settings, "BILLING_MOCK_MODE", True):
        subscription_summary = _mock_subscription_summary(_mock_plan_type_for_profile(profile))

    context.update(
        {
            "profile": profile,
            "subscription_summary": subscription_summary,
            "manage_subscription_url": reverse("core:manage_subscription"),
            "portal_available": bool(profile.is_premium and _billing_checkout_enabled()),
        }
    )
    return render(request, "core/upgrade.html", context)


@login_required
def payment_success(request):
    plan_type = request.GET.get("plan_type")
    profile = get_profile(request.user)
    billing_is_mock = getattr(settings, "BILLING_MOCK_MODE", True)

    if billing_is_mock:
        if not profile.stripe_customer_id:
            profile.stripe_customer_id = _mock_customer_id(request.user)
            profile.save(update_fields=["stripe_customer_id"])
        profile.set_premium(True)
        _log_billing_event(
            profile,
            "upgrade",
            "mock_checkout_completed",
            {"plan_type": plan_type or "monthly", "mock": True},
        )
    return render(
        request,
        "core/payment_success.html",
        {
            "plan_type": plan_type,
            "billing_is_mock": billing_is_mock,
        },
    )


@login_required
def payment_cancel(request):
    return render(
        request,
        "core/payment_cancel.html",
        {"billing_is_mock": getattr(settings, "BILLING_MOCK_MODE", True)},
    )


@login_required
def manage_subscription(request):
    profile = get_profile(request.user)
    subscription_info = None
    if getattr(settings, "BILLING_MOCK_MODE", True) and profile.is_premium:
        summary = _mock_subscription_summary(_mock_plan_type_for_profile(profile))
        subscription_info = {
            "id": "mock_subscription",
            "status": summary["status"],
            "cancel_at_period_end": False,
            "cancel_at": None,
            "current_period_end": timezone.localtime(timezone.now() + timedelta(days=summary["days_remaining"])),
            "current_period_start": timezone.localtime(timezone.now()),
            "days_remaining": summary["days_remaining"],
            "plan": {
                "nickname": summary["plan_display"],
                "amount": None,
                "amount_display": summary["amount_display"],
                "currency": "GBP",
                "interval": summary["plan_interval"],
            },
        }
    context = {
        "profile": profile,
        "subscription": subscription_info,
        "billing_configured": _billing_checkout_enabled(),
        "portal_available": bool(profile.is_premium and _billing_checkout_enabled()),
        "billing_is_mock": getattr(settings, "BILLING_MOCK_MODE", True),
    }
    return render(request, "core/manage_subscription.html", context)


def _log_billing_event(profile, event: str, reason: str = "", metadata: dict | None = None) -> None:
    try:
        BillingEventLog.objects.create(
            user=profile.user,
            event=event,
            reason=reason or "",
            metadata=metadata or {},
        )
    except Exception:  # noqa: B902
        logger.warning("Unable to persist billing event for user %s", profile.user_id)


def _cancel_active_subscription(profile, *, fail_on_missing=True):
    if getattr(settings, "BILLING_MOCK_MODE", True):
        if not profile.is_premium:
            if fail_on_missing:
                return False, None, "No active subscription found.", 404
            return True, None, None, None
        profile.set_premium(False)
        payload = {
            "status": "canceled",
            "cancel_at_period_end": False,
            "current_period_end": None,
            "cancel_at": None,
            "mock": True,
        }
        _log_billing_event(profile, "cancel", "mock_subscription_cancelled", payload)
        return True, payload, None, None

    return False, None, "Only demo billing is supported in this portfolio build.", 503


@login_required
@require_POST
def cancel_subscription(request):
    profile = get_profile(request.user)
    success, updated_data, error_msg, status_code = _cancel_active_subscription(profile)
    if not success:
        return _json_error(error_msg or "Unable to cancel subscription.", status=status_code or 502)

    effective_epoch = updated_data.get("cancel_at") or updated_data.get("current_period_end")
    if updated_data.get("mock"):
        notice_message = "Your demo subscription has been ended immediately. No real billing provider was contacted."
    else:
        notice_message = "Your subscription will remain active until the end of the current billing period."
    if effective_epoch:
        try:
            end_dt = datetime.utcfromtimestamp(effective_epoch)
            aware_end_dt = timezone.make_aware(end_dt, timezone.utc)
            local_end_dt = timezone.localtime(aware_end_dt)
            notice_message = f"Your subscription will remain active until {local_end_dt.strftime('%d %b %Y %H:%M')}."
        except (OSError, ValueError, TypeError):
            pass
    if updated_data.get("mock"):
        notice_message += " You can re-enable it any time from the upgrade page."
    else:
        notice_message += " You can re-activate anytime via the billing portal."
    request.session[CANCELLATION_NOTICE_SESSION_KEY] = notice_message
    _log_billing_event(profile, "cancel", "user_requested_cancel")

    return JsonResponse(
        {
            "ok": True,
            "status": updated_data.get("status"),
            "cancel_at_period_end": updated_data.get("cancel_at_period_end", True),
            "effective_period_end": effective_epoch,
            "message": notice_message,
        }
    )


@login_required
@require_POST
def create_checkout_session(request, plan_type="monthly"):
    selected_plan = request.POST.get("plan_type", plan_type) or plan_type
    if selected_plan not in {"monthly", "yearly"}:
        selected_plan = "monthly"

    profile = get_profile(request.user)

    if not profile.stripe_customer_id:
        profile.stripe_customer_id = _mock_customer_id(request.user)
        profile.save(update_fields=["stripe_customer_id"])
    checkout_url = (
        request.build_absolute_uri(reverse("core:payment_success"))
        + "?"
        + urlencode({"plan_type": selected_plan, "mock_checkout": "1"})
    )
    _log_billing_event(
        profile,
        "upgrade",
        "mock_checkout_started",
        {"plan_type": selected_plan, "mock": True},
    )
    return JsonResponse({"ok": True, "checkout_url": checkout_url, "mock": True})


@login_required
@require_POST
def create_portal_session(request):
    profile = get_profile(request.user)
    if not profile.is_premium:
        return _json_error("Activate the demo premium plan before opening the mock billing portal.", status=400)
    portal_url = request.build_absolute_uri(reverse("core:manage_subscription")) + "?mock_portal=1"
    _log_billing_event(profile, "upgrade", "mock_portal_opened", {"mock": True})
    return JsonResponse({"ok": True, "portal_url": portal_url, "mock": True})


# Legacy alias
take_snapshot_now = create_snapshot
