import threading

from django.http import JsonResponse
from django.shortcuts import redirect

from website.site_maintenance import get_active_site_maintenance, maintenance_exempt_path

# Paths that require the session check (dashboard routes + paid-area account pages)
_PROTECTED_PREFIXES = ("/dashboard/", "/admin-panel/", "/payment/", "/subscribe/")

# Paths always allowed without a session
_SKIP_PREFIXES = ("/static/", "/css/", "/js/", "/images/", "/media/", "/sneat-assets/", "/admin/")

# Paths to skip for visit tracking (static assets, favicons, etc.)
_VISIT_SKIP_PREFIXES = ("/static/", "/css/", "/js/", "/images/", "/media/", "/sneat-assets/", "/favicon")


class SingleSessionMiddleware:
    """
    Enforces one active session per account across all devices.

    On every request to a protected path:
    - If no Django session exists  → redirect to /login/
    - If the stored session_token no longer matches the one in Supabase
      (meaning someone else logged in from another device) → flush & redirect
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Skip static assets and non-protected paths
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return self.get_response(request)

        if not any(path.startswith(p) for p in _PROTECTED_PREFIXES):
            return self.get_response(request)

        user_id = request.session.get("user_id")
        session_token = request.session.get("session_token")

        # Not logged in at all
        if not user_id or not session_token:
            return redirect("/login/")

        # Validate session token against Supabase
        try:
            from django.conf import settings
            from supabase import create_client

            admin = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
            resp = admin.table("active_sessions").select("session_token").eq("user_id", user_id).limit(1).execute()
            row = resp.data[0] if resp.data else None

            if row is None:
                # No active session in DB – account was logged out elsewhere
                request.session.flush()
                return redirect("/login/?reason=session_expired")

            if row["session_token"] != session_token:
                # A newer session exists (another device logged in)
                request.session.flush()
                return redirect("/login/?reason=session_expired")

        except Exception:
            # On any Supabase error, fail open (don't lock the user out)
            pass

        return self.get_response(request)


class StudentAcademicProfileSyncMiddleware:
    """
    Keeps session['pending_academic_profile'] aligned with Supabase profiles so the
    blocking modal appears for OAuth (or incomplete) students even after deploys or
    stale cookie sessions. Programme/year/school drive question_bank filters in views.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        # Avoid overwriting session with stale DB rows on the POST that saves the profile.
        if path.rstrip("/") == "/dashboard/complete-academic-profile" and request.method == "POST":
            return self.get_response(request)
        if (
            path.startswith("/dashboard/")
            and request.session.get("role") == "student"
            and request.session.get("user_id")
        ):
            try:
                from supabase import create_client
                from django.conf import settings

                from website.views import _sync_pending_academic_profile

                admin = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
                uid = str(request.session.get("user_id"))
                rows = (
                    admin.table("profiles")
                    .select("programme, year_of_study, school")
                    .eq("id", uid)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                _sync_pending_academic_profile(request, rows[0] if rows else {})
            except Exception:
                pass
        return self.get_response(request)


class StudentSubscriptionGateMiddleware:
    """
    Students must have an active, non-expired subscription to use most of /dashboard/.
    Free users (registered but unpaid) can access the main dashboard page, the free
    test, and initial setup pages. Everything else requires a paid subscription.
    """

    # Paths accessible to all logged-in students regardless of subscription.
    _FREE_PREFIXES = (
        "/dashboard/free-test/",
        "/dashboard/complete-academic-profile/",
        "/dashboard/ack-disclaimer/",
    )
    _FREE_EXACT = {"/dashboard/", "/dashboard"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if not path.startswith("/dashboard/"):
            return self.get_response(request)
        # Main dashboard landing page is free.
        if path.rstrip("/") + "/" in self._FREE_EXACT or path in self._FREE_EXACT:
            return self.get_response(request)
        # Free test and initial setup pages are always accessible.
        if any(path.startswith(p) for p in self._FREE_PREFIXES):
            return self.get_response(request)
        if request.session.get("role") != "student":
            return self.get_response(request)
        if not request.session.get("user_id"):
            return self.get_response(request)
        try:
            from website.views import (
                _reconcile_pending_subscription_from_paystack,
                _subscription_access_state,
            )

            uid = request.session.get("user_id")
            _reconcile_pending_subscription_from_paystack(uid)
            allowed, reason = _subscription_access_state(uid)
            if allowed:
                return self.get_response(request)
        except Exception:
            return self.get_response(request)
        return redirect(f"/subscribe/?reason={reason}")


class VisitTrackerMiddleware:
    """
    Records every page request (IP, user-agent, path) to the Supabase
    site_visits table.  The Supabase write runs in a background thread so
    it never adds latency to the response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        path = request.path
        # Skip static assets and internal Django admin
        if not any(path.startswith(p) for p in _VISIT_SKIP_PREFIXES):
            ip = (
                request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                or request.META.get("REMOTE_ADDR", "unknown")
            )[:45]
            ua = request.META.get("HTTP_USER_AGENT", "")[:500]
            t = threading.Thread(
                target=self._record_visit,
                args=(ip, ua, path),
                daemon=True,
            )
            t.start()

        return response

    @staticmethod
    def _record_visit(ip, user_agent, path):
        try:
            from django.conf import settings
            from supabase import create_client

            admin = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
            admin.table("site_visits").insert({
                "ip_address": ip,
                "user_agent": user_agent,
                "path": path,
            }).execute()
        except Exception:
            pass


class MaintenanceModeMiddleware:
    """
    When a maintenance window is active in Supabase, block the site for non-admins:
    - HTML: redirect to /maintenance/
    - /api/mobile/*: 503 JSON (except health + mobile admin routes for admin sessions)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        maint = get_active_site_maintenance()
        if not maint:
            return self.get_response(request)

        path = request.path or ""

        if maintenance_exempt_path(path):
            return self.get_response(request)

        # Mobile admin API: allow logged-in admins to keep operating the app
        if path.startswith("/api/mobile/admin/") and request.session.get("role") == "admin":
            return self.get_response(request)

        if path.startswith("/api/mobile/"):
            msg = (maint.get("message") or "").strip() or "The site is temporarily unavailable for maintenance."
            title = (maint.get("title") or "").strip() or "Maintenance"
            return JsonResponse(
                {
                    "error": "maintenance",
                    "title": title,
                    "message": msg,
                },
                status=503,
            )

        return redirect("/maintenance/")


class LoginRequiredForFormsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)
