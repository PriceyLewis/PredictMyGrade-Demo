"""
Django settings for PredictMyGrade.
"""

from datetime import timedelta
from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Core paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------
# Load local .env first (for developers) and then Render's mounted .env secret
# file when available. Existing process environment variables always win because
# `override=False` preserves anything already exported by the platform.
load_dotenv(BASE_DIR / ".env", override=False)
for secret_path in filter(
    None,
    [
        os.getenv("RENDER_ENV_FILE"),
        # Render secret files can be mounted anywhere; `/etc/secrets/.env`
        # matches the default path in their docs.
        "/etc/secrets/.env",
    ],
):
    load_dotenv(secret_path, override=False)


# ---------------------------------------------------------------------------
# Security & deployment
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver"
    ).split(",")
    if host.strip()
]

# Render injects the public hostname automatically for web services. Accepting it
# here keeps the demo deploy zero-config without broadening ALLOWED_HOSTS to "*".
_render_external_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _render_external_hostname and _render_external_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_external_hostname)



# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "django.contrib.sites",
    # "django_celery_beat",  # Celery is disabled on Render free tier (no workers).
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.microsoft",
    # Local apps
    "core",
]

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]


# ---------------------------------------------------------------------------
# Allauth configuration
# ---------------------------------------------------------------------------
ALLOWED_EMAIL_DOMAINS = [
    "edgehill.ac.uk",
    "student.edgehill.ac.uk",
    "gmail.com",
]

ACCOUNT_ADAPTER = "core.adapters.DomainRestrictedAccountAdapter"
SOCIALACCOUNT_ADAPTER = "core.adapters.ResilientSocialAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email", "username"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/post_login_redirect/"
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http" if DEBUG else "https"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Keep request.user available for allauth flows
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# URLs & WSGI / ASGI
# ---------------------------------------------------------------------------
ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.marketing_settings",
                "core.context_processors.premium_status",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_CONN_MAX_AGE = int(os.getenv("DATABASE_CONN_MAX_AGE", "600"))
_database_ssl_raw = os.getenv("DATABASE_SSL_REQUIRE")
if _database_ssl_raw is None:
    DATABASE_SSL_REQUIRE = not DEBUG
else:
    DATABASE_SSL_REQUIRE = _database_ssl_raw.lower() == "true"

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ImproperlyConfigured("DATABASE_URL must be provided.")

_database_config = dj_database_url.parse(
    DATABASE_URL,
    conn_max_age=DATABASE_CONN_MAX_AGE,
)
if DATABASE_SSL_REQUIRE and not _database_config.get("ENGINE", "").endswith(
    "sqlite3"
):
    _database_config.setdefault("OPTIONS", {})["sslmode"] = "require"

DATABASES = {"default": _database_config}


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

PREMIUM_BACKUP_DIR = Path(os.getenv("PREMIUM_BACKUP_DIR", BASE_DIR / "backups"))


# ---------------------------------------------------------------------------
# CSRF / sessions
# ---------------------------------------------------------------------------
_raw_csrf_origins = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "")
DEFAULT_CSRF_TRUSTED_ORIGINS = []
CSRF_TRUSTED_ORIGINS = [
    origin.rstrip("/")
    for origin in _raw_csrf_origins.split(",")
    if origin.strip()
]

# Render also injects the full public HTTPS URL. Trust only that exact origin so
# POST forms work on the generated onrender.com domain without manual setup.
_render_external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
if _render_external_url:
    CSRF_TRUSTED_ORIGINS.append(_render_external_url)

if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "http://localhost",
        "http://127.0.0.1",
        "https://localhost",
        "https://127.0.0.1",
    ]
CSRF_TRUSTED_ORIGINS += DEFAULT_CSRF_TRUSTED_ORIGINS
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(CSRF_TRUSTED_ORIGINS))

CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# OAuth providers (django-allauth)
# Keep credentials in environment; no secrets in code.
# If SocialApp entries exist in the DB they will be used; otherwise the
# below APP config will be used for Google/GitHub/Microsoft.
# ---------------------------------------------------------------------------
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["email", "profile"],
        "AUTH_PARAMS": {"access_type": "online"},
        "APP": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "key": "",
        },
    },
    "github": {
        "SCOPE": ["user:email"],
        "APP": {
            "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
            "secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
            "key": "",
        },
    },
    "microsoft": {
        # Tenant can be a GUID or "common"/"organizations"/"consumers"
        "tenant": os.getenv("MICROSOFT_TENANT_ID", "common"),
        "SCOPE": ["User.Read"],
        "APP": {
            "client_id": os.getenv("MICROSOFT_CLIENT_ID", ""),
            "secret": os.getenv("MICROSOFT_CLIENT_SECRET", ""),
            "key": "",
        },
    },
}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = os.getenv(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "PredictMyGrade Demo <no-reply@example.com>")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


# ---------------------------------------------------------------------------
# Background tasks (Celery)
# ---------------------------------------------------------------------------
# Disabled for Render free tier; keep placeholder values in case you
# introduce a worker later.
# CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "memory://")
# CELERY_RESULT_BACKEND = os.getenv(
#     "CELERY_RESULT_BACKEND", "cache+memory://"
# )
# CELERY_ACCEPT_CONTENT = ["json"]
# CELERY_TASK_SERIALIZER = "json"
# CELERY_RESULT_SERIALIZER = "json"
# CELERY_TIMEZONE = TIME_ZONE

# CELERY_TASK_ROUTES = {
#     "core.tasks.weekly_retrain_models": {"queue": "ai"},
#     "core.tasks.generate_weekly_ai_insights": {"queue": "ai"},
#     "core.tasks.dispatch_weekly_reports": {"queue": "ops"},
#     "core.tasks.run_premium_backup": {"queue": "ops"},
#     "core.tasks.capture_daily_progress_snapshot": {"queue": "ai"},
# }

# _weekly = timedelta(days=7)
# _daily = timedelta(days=1)
# CELERY_BEAT_SCHEDULE = {
#     "weekly-model-retrain": {
#         "task": "core.tasks.weekly_retrain_models",
#         "schedule": _weekly,
#     },
#     "weekly-ai-insights": {
#         "task": "core.tasks.generate_weekly_ai_insights",
#         "schedule": _weekly,
#     },
#     "weekly-admin-report": {
#         "task": "core.tasks.dispatch_weekly_reports",
#         "schedule": _weekly,
#     },
#     "weekly-premium-backup": {
#         "task": "core.tasks.run_premium_backup",
#         "schedule": _weekly,
#     },
#     "daily-progress-snapshot": {
#         "task": "core.tasks.capture_daily_progress_snapshot",
#         "schedule": _daily,
#     },
# }


# ---------------------------------------------------------------------------
# Billing demo mode
# ---------------------------------------------------------------------------
UPGRADE_PROMO_CODE = os.getenv("UPGRADE_PROMO_CODE", "EDUSTUDENT20")
BILLING_MOCK_MODE = True


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
ANALYTICS_SCRIPT_URL = os.getenv("ANALYTICS_SCRIPT_URL", "")
ANALYTICS_DATA_LAYER = os.getenv("ANALYTICS_DATA_LAYER", "dataLayer")


# ---------------------------------------------------------------------------
# AI / OpenAI
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
AI_CHAT_DAILY_LIMIT = int(os.getenv("AI_CHAT_DAILY_LIMIT", "20"))
TRIAL_PERIOD_DAYS = int(os.getenv("TRIAL_PERIOD_DAYS", "7"))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
WHAT_IF_HOUR_BOOST = float(os.getenv("WHAT_IF_HOUR_BOOST", "0.65"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ONBOARDING_SAMPLE_DATA_ENABLED = os.getenv("ONBOARDING_SAMPLE_DATA_ENABLED", "true").lower() == "true"
