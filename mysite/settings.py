"""
Django settings for mysite (matesla / MyRobotCar).

Modernized for Django 5.2 LTS + Python 3.12. Local default: SQLite + HTTP.
"""

import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "eta1h#d8$plq6gy!2l96lbxe5k9gu7we#q*0w=gxe+=(szicm4",
)
# Used by legacy VIN hashing; keep stable for existing data compatibility.
saltSeed = SECRET_KEY + "LL2SV-4tghzsrgsdgvsdgqdgvqd[_zCRxUwXYC=wsdgqdsgqdgqdgvghjjkjfCC5GCTNdE-Dsw>}bBp."

# Off by default (no debug toolbar on phone / Tailscale). Opt in with DJANGO_DEBUG=1.
DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "afternoon-scrubland-61531.herokuapp.com",
    "matesla.herokuapp.com",
    # Tailscale Serve (phone / other devices on the tailnet)
    "mehdi-thinkbook-13s-g2-itl.taila97662.ts.net",
    "100.70.189.84",
]
# Extra hosts via env, comma-separated (e.g. another Tailscale hostname)
ALLOWED_HOSTS += [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()
]

# ---------------------------------------------------------------------------
# Security — relaxed in DEBUG so http://localhost:8001 works
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Forms / login over Tailscale HTTPS (TLS terminated by `tailscale serve`)
CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "https://mehdi-thinkbook-13s-g2-itl.taila97662.ts.net",
    "https://mehdi-thinkbook-13s-g2-itl.taila97662.ts.net:8443",
]
CSRF_TRUSTED_ORIGINS += [
    o.strip()
    for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]
# Tailscale Serve proxies HTTPS → plain HTTP to runserver
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Local runserver + Tailscale Serve: do not force HTTPS redirects (cron hits
# http://127.0.0.1/.../internal/capture). Opt in with DJANGO_SECURE_SSL_REDIRECT=1.
_secure_ssl = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "False").lower() in (
    "1",
    "true",
    "yes",
)
if DEBUG:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    # OAuth returns via top-level GET from auth.tesla.com — Lax keeps the session.
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
else:
    SECURE_SSL_REDIRECT = _secure_ssl
    # Secure cookies OK behind Tailscale HTTPS; local http://127.0.0.1 may need DJANGO_DEBUG=1
    SESSION_COOKIE_SECURE = os.environ.get("DJANGO_COOKIE_SECURE", "False").lower() in (
        "1",
        "true",
        "yes",
    )
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "matesla.apps.MateslaConfig",
    "accounts.apps.AccountsConfig",
    "carimage.apps.CarimageConfig",
    "personalstats.apps.PersonalstatsConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tables2",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # After LocaleMiddleware so resolve() sees language-prefixed paths.
    "mysite.middleware.ReadOnlyRemoteMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Cookie preference for km/mi display (safe after CommonMiddleware).
    "mysite.middleware.DistanceUnitMiddleware",
]

# Read-only for Tailscale / remote Host (same pattern as PicturesDjango).
# Full write (Tesla OAuth, admin, setup) only on these Host headers.
WRITABLE_HOSTS = [
    h.strip()
    for h in os.environ.get("MATESLA_WRITABLE_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]
# Optional: Django user that owns the Tesla token (anonymous Tailscale viewers).
# Default: the only user with a TeslaToken, else first superuser.
_owner_id = os.environ.get("MATESLA_OWNER_USER_ID", "").strip()
MATESLA_OWNER_USER_ID = int(_owner_id) if _owner_id.isdigit() else None
MATESLA_OWNER_USERNAME = os.environ.get("MATESLA_OWNER_USERNAME", "").strip() or None
# POST allowed remotely (session / auth only — not Tesla setup).
READONLY_SAFE_POST_URL_NAMES = [
    "login",
    "logout",
    "select_vehicle",
    "set_distance_unit",
    "password_reset",
    "password_reset_confirm",
    "password_change",
    "password_change_done",
]
# GET (and safe POST above) allowed on remote hosts; everything else → 404.
READONLY_ALLOWED_URL_NAMES = [
    # Auth
    "login",
    "logout",
    "password_reset",
    "password_reset_done",
    "password_reset_confirm",
    "password_reset_complete",
    "password_change",
    "password_change_done",
    # Status / browse (no commands)
    "home",
    "tesla_status",
    "teslastatusJson",
    "teslaasleep",
    "TeslaServerError",
    "NoTeslaVehicules",
    "ConnectionError",
    "select_vehicle",
    "CarImageFromTesla",
    "set_distance_unit",

    # Personal stats / maps / graphs
    "PersoStats",
    "PersoDayMap",
    "PersoDayMapDay",
    "PersoDayChargeSessionGraph",
    "PersoDrives",
    "PersoDCCharge",
    "PersoDCChargeGraph",
    "PersoPollDetails",
    "PersoLifetimeMapData",
    "PersoResolveAddress",
    "PersoMatchSupercharger",
    "PersoStatsBatteryDegradationGraph",
    "PersoStatsFirmwareHistory",
    "PersoStatsFirmwareHistoryCSV",
    "StatsOnCarGraph",
    "AllMyDataAsCSV",
]

# Debug toolbar only when DEBUG (never on phone / release-like local)
if DEBUG:
    INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]
    MIDDLEWARE = [
        "debug_toolbar.middleware.DebugToolbarMiddleware",
        *MIDDLEWARE,
    ]

ROOT_URLCONF = "mysite.urls"

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
                "mysite.context_processors.writable_access",
                "mysite.context_processors.distance_unit",
            ],
        },
    },
]

WSGI_APPLICATION = "mysite.wsgi.application"
# Day map landing (no Fleet cost); status is opt-in via nav.
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Database — SQLite local by default; Postgres if DATABASE_URL is set
# ---------------------------------------------------------------------------
# SQLite + multi-process (cron manage.py + web) is fragile. Prefer a single web
# process (gunicorn --workers 1) and trigger capture via HTTP on that process.
# WAL is enabled in matesla.apps (connection_created signal).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 30,  # seconds to wait on locks instead of immediate OperationalError
        },
        # Django TestCase always uses this separate file (never db.sqlite3).
        # Destroyed after the test run. *.sqlite3 is gitignored.
        "TEST": {
            "NAME": BASE_DIR / "test_matesla.sqlite3",
        },
    }
}

if os.environ.get("DATABASE_URL"):
    import dj_database_url

    DATABASES["default"] = dj_database_url.config(
        conn_max_age=600,
        conn_health_checks=True,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
LANGUAGES = (
    ("en", _("English")),
    ("fr", _("Français")),
    ("es", _("Espanol")),
    ("de", _("Deutsch")),
    ("nl", _("Nederlands")),
    ("nb", _("Norsk")),
)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

LOCALE_PATHS = (BASE_DIR / "locale",)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        # Compressed (not Manifest): Manifest breaks on leaflet.js sourceMappingURL
        # and requires a full collectstatic rebuild whenever DEBUG is off.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedStaticFilesStorage"
        ),
    },
}

# ---------------------------------------------------------------------------
# Cache / toolbar
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-snowflake",
    }
}

INTERNAL_IPS = ["127.0.0.1"]

# ---------------------------------------------------------------------------
# Email (password reset) — console backend if no SendGrid key
# ---------------------------------------------------------------------------
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply.matesla@gmail.com")

if SENDGRID_API_KEY:
    EMAIL_BACKEND = "sendgrid_backend.SendgridBackend"
    SENDGRID_SANDBOX_MODE_IN_DEBUG = False
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ---------------------------------------------------------------------------
# Tesla Fleet API (MyRobotCar) — set in .env, never commit secrets
# ---------------------------------------------------------------------------
TESLA_CLIENT_ID = os.getenv("TESLA_CLIENT_ID", "")
TESLA_CLIENT_SECRET = os.getenv("TESLA_CLIENT_SECRET", "")
TESLA_REDIRECT_URI = os.getenv(
    "TESLA_REDIRECT_URI", "http://localhost:8001/oauth/callback"
)
# EU region for Belgium; override if needed (na, eu, cn)
TESLA_FLEET_API_BASE = os.getenv(
    "TESLA_FLEET_API_BASE", "https://fleet-api.prd.eu.vn.cloud.tesla.com"
)
TESLA_AUTH_BASE = os.getenv("TESLA_AUTH_BASE", "https://auth.tesla.com/oauth2/v3")
