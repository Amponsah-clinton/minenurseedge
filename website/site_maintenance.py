"""
Scheduled maintenance window (Supabase `site_maintenance` table).
Non-admin users are redirected to /maintenance/; mobile API returns 503.
"""

from datetime import datetime, timezone

SITE_MAINTENANCE_CACHE_KEY = "site_maintenance_active_v1"
SITE_MAINTENANCE_CACHE_TTL = 30

DEFAULT_MAINTENANCE_IMAGE_URL = (
    "https://img.freepik.com/free-vector/website-maintenance-abstract-concept-vector-illustration-"
    "website-service-webpage-seo-maintenance-web-design-corporate-site-professional-support-security-"
    "analysis-update-abstract-metaphor_335657-2295.jpg?semt=ais_hybrid&w=740&q=80"
)


def _parse_maintenance_ts(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _site_maintenance_row_is_active(row, now):
    if not row or not row.get("is_enabled", True):
        return False
    starts = _parse_maintenance_ts(row.get("starts_at"))
    if not starts or now < starts:
        return False
    ends = _parse_maintenance_ts(row.get("ends_at"))
    if ends is not None and now > ends:
        return False
    return True


def invalidate_site_maintenance_cache():
    try:
        from django.core.cache import cache

        cache.delete(SITE_MAINTENANCE_CACHE_KEY)
    except Exception:
        pass


def maintenance_row_is_live(row):
    """Whether this row is enabled and the current UTC time falls in [starts_at, ends_at]."""
    return _site_maintenance_row_is_active(row, datetime.now(timezone.utc))


def get_active_site_maintenance():
    """
    Returns the active maintenance row dict or None.
    """
    try:
        from django.core.cache import cache
    except Exception:
        cache = None

    if cache is not None:
        hit = cache.get(SITE_MAINTENANCE_CACHE_KEY)
        if hit is not None:
            return None if hit == "__none__" else hit

    try:
        from django.conf import settings
        from supabase import create_client

        admin = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        resp = (
            admin.table("site_maintenance")
            .select("*")
            .eq("is_enabled", True)
            .order("starts_at", desc=True)
            .limit(50)
            .execute()
        )
        rows = resp.data or []
    except Exception:
        rows = []

    now = datetime.now(timezone.utc)
    active = None
    for row in rows:
        if _site_maintenance_row_is_active(row, now):
            active = row
            break

    if cache is not None:
        cache.set(SITE_MAINTENANCE_CACHE_KEY, active if active else "__none__", SITE_MAINTENANCE_CACHE_TTL)

    return active


def maintenance_exempt_path(path):
    """Paths that stay reachable during maintenance (admin, auth, assets, health)."""
    if not path:
        return True
    p = path
    exempt = (
        "/login/",
        "/logout/",
        "/auth/",
        "/forgot-password/",
        "/reset-password/",
        "/admin-panel/",
        "/maintenance/",
        "/static/",
        "/css/",
        "/js/",
        "/images/",
        "/media/",
        "/sneat-assets/",
        "/favicon",
        "/admin/",
        "/api/mobile/health/",
    )
    for prefix in exempt:
        if p.startswith(prefix):
            return True
    if p.rstrip("/") == "/maintenance":
        return True
    return False
