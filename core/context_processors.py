from django.conf import settings

from .utils import resolve_premium_status


def marketing_settings(request):
    return {
        "saved_account_theme": request.user.profile.theme if request.user.is_authenticated else "",
        "theme_override": request.session.pop("theme_override", ""),
        "ANALYTICS_SCRIPT_URL": getattr(settings, "ANALYTICS_SCRIPT_URL", ""),
        "ANALYTICS_DATA_LAYER": getattr(settings, "ANALYTICS_DATA_LAYER", "dataLayer"),
        "BILLING_MOCK_MODE": getattr(settings, "BILLING_MOCK_MODE", True),
    }


def premium_status(request):
    status = {
        "has_access": False,
        "plan_type": "free",
        "plan_label": "Free",
        "plan_days_remaining": None,
        "plan_cancel_at_end": False,
    }
    if request.user.is_authenticated:
        status = resolve_premium_status(request.user)

    return {
        "has_premium_access": status["has_access"],
        "plan_type": status["plan_type"],
        "plan_label": status["plan_label"],
        "plan_days_remaining": status["plan_days_remaining"],
        "plan_cancel_at_end": status["plan_cancel_at_end"],
    }
