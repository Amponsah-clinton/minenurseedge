import threading

from django.shortcuts import redirect

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


class StudentSubscriptionGateMiddleware:
    """
    Students must have an active, non-expired subscription to use /dashboard/.
    Redirects to Stripe checkout entry at /subscribe/ when payment is still required.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if not path.startswith("/dashboard/"):
            return self.get_response(request)
        if request.session.get("role") != "student":
            return self.get_response(request)
        if not request.session.get("user_id"):
            return self.get_response(request)
        try:
            from website.views import (
                _reconcile_pending_subscription_from_paystack,
                subscription_allows_dashboard,
            )

            uid = request.session.get("user_id")
            _reconcile_pending_subscription_from_paystack(uid)
            if subscription_allows_dashboard(uid):
                return self.get_response(request)
        except Exception:
            return self.get_response(request)
        return redirect("/subscribe/?reason=payment_required")


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
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class LoginRequiredForFormsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)
