"""
Django settings for publishing_company project.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-this-in-production")
DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = ["*", ".academicdigital.space", "doi.ms", "www.doi.ms"]

CSRF_TRUSTED_ORIGINS = [
  
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]

CSRF_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_USE_SESSIONS = False

SESSION_COOKIE_SECURE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "website",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "website.middleware.VisitTrackerMiddleware",
    "website.middleware.MaintenanceModeMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "website.middleware.SingleSessionMiddleware",
    "website.middleware.StudentSubscriptionGateMiddleware",
    "website.middleware.LoginRequiredForFormsMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "publishing_company.urls"

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
                "website.context_processors.support_email",
                "website.context_processors.contact_info",
                "website.context_processors.identifier_resolver_domain",
                "website.context_processors.default_app_font_size",
                "website.context_processors.user_permissions",
                "website.context_processors.page_states",
                "website.context_processors.bytez_key",
            ],
        },
    },
]

WSGI_APPLICATION = "publishing_company.wsgi.application"
ASGI_APPLICATION = "publishing_company.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_PROJECT_ID = os.getenv("SUPABASE_PROJECT_ID", "")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "project-files")

BYTEZ_API_KEY = os.getenv("BYTEZ_API_KEY", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
# Used in Stripe Checkout success/cancel URLs if request.build_absolute_uri is wrong behind a proxy
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "").strip().rstrip("/")


def _normalize_paystack_secret(raw):
    """Strip whitespace, optional quotes, and accidental 'Bearer ' prefix from .env values."""
    if not raw:
        return ""
    s = str(raw).strip()
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        s = s[1:-1].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


# Paystack (preferred for /subscribe/ when secret is set)
PAYSTACK_SECRET_KEY = _normalize_paystack_secret(
    os.getenv("PAYSTACK_SECRET_KEY", "") or os.getenv("VITE_PAYSTACK_SECRET_KEY", "")
)
PAYSTACK_PUBLIC_KEY = (
    os.getenv("PAYSTACK_PUBLIC_KEY", "").strip()
    or os.getenv("VITE_PAYSTACK_PUBLIC_KEY", "").strip()
)

# Legacy names (still read if PAYSTACK_* not set)
VITE_PAYSTACK_PUBLIC_KEY = os.getenv("VITE_PAYSTACK_PUBLIC_KEY", "")
VITE_PAYSTACK_SECRET_KEY = os.getenv("VITE_PAYSTACK_SECRET_KEY", "")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "css", BASE_DIR / "js", BASE_DIR / "images", BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

if not DEBUG:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"
else:
    STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_USE_TLS = True
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = EMAIL_HOST_USER or "no-reply@example.com"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
IDENTIFIER_RESOLVER_DOMAIN = os.getenv("IDENTIFIER_RESOLVER_DOMAIN", "scholarindexing.academicdigital.space")
