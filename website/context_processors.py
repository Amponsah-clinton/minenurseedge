from django.conf import settings
from supabase import create_client


def support_email(request):
    return {"support_email": getattr(settings, "DEFAULT_FROM_EMAIL", "")}


def contact_info(request):
    return {"contact_info": {}}


def identifier_resolver_domain(request):
    return {"identifier_resolver_domain": getattr(settings, "IDENTIFIER_RESOLVER_DOMAIN", "")}


def default_app_font_size(request):
    return {"default_app_font_size": "16px"}


def user_permissions(request):
    return {"user_permissions": {}}


def page_states(request):
    return {"page_states": {}}


def bytez_key(request):
    # Exposed to the browser so the assistant can call Bytez directly.
    # Make sure you use the correct Bytez key for your environment.
    return {"bytez_api_key": getattr(settings, "BYTEZ_API_KEY", "")}


def academic_profile_gate(request):
    """
    Students who have not set programme / year / school (e.g. after Google sign-up)
    see a blocking modal only after their annual fee is paid (same rule as email signup:
    pay on /subscribe/, then dashboard). Programme drives question_bank filters in views.
    """
    from website.views import (
        ACADEMIC_YEAR_CHOICES,
        PROGRAMME_CHOICES,
        subscription_allows_dashboard,
    )

    base = {
        "academic_programme_choices": PROGRAMME_CHOICES,
        "academic_year_choices": ACADEMIC_YEAR_CHOICES,
    }

    if request.session.get("role") != "student":
        return {
            "pending_academic_profile": False,
            "show_academic_profile_modal": False,
            **base,
        }

    uid = request.session.get("user_id")
    pending = bool(request.session.get("pending_academic_profile"))
    paid = False
    if pending and uid:
        paid = subscription_allows_dashboard(uid)
    # Do not block /subscribe/ or payment: modal only when they can use the dashboard
    # but academic fields are still empty.
    show_modal = pending and paid

    return {
        "pending_academic_profile": pending,
        "show_academic_profile_modal": show_modal,
        **base,
    }


def subscription_status_ctx(request):
    """
    Adds `is_subscribed` to every template context so the sidebar can show
    premium lock badges on gated items without each view needing to pass it.
    Only queries Supabase for logged-in students; result is cached in the
    session for 60 seconds to avoid a DB hit on every request.
    """
    import time

    if request.session.get("role") != "student":
        return {"is_subscribed": True}  # admins see no lock badges

    uid = request.session.get("user_id")
    if not uid:
        return {"is_subscribed": False}

    now = time.time()
    cached_at = request.session.get("_sub_status_ts", 0)
    if now - cached_at < 60:
        return {"is_subscribed": bool(request.session.get("_sub_status_val", False))}

    try:
        from website.views import subscription_allows_dashboard
        val = subscription_allows_dashboard(uid)
    except Exception:
        val = True  # fail open — don't show badges on errors

    request.session["_sub_status_val"] = val
    request.session["_sub_status_ts"] = now
    return {"is_subscribed": val}


def referral_modal_ctx(request):
    """
    Passes two one-shot flags to every student dashboard template:
      show_referral_modal  – post-signup "do you have a referral code?" modal
      show_referral_prompt – post-login "refer a friend" invite modal
    Both are popped from the session immediately so they display only once per trigger.
    """
    if request.session.get("role") != "student":
        return {"show_referral_modal": False, "show_referral_prompt": False}
    show_modal  = bool(request.session.pop("show_referral_modal", False))
    show_prompt = bool(request.session.pop("show_referral_prompt", False))
    return {"show_referral_modal": show_modal, "show_referral_prompt": show_prompt}


def dashboard_search_nav(request):
    """Navigation index for the dashboard top search bar (client-side + API)."""
    path = request.path or ""
    if not (path.startswith("/dashboard/") or path.startswith("/admin-panel/")):
        return {"dashboard_search_nav": []}
    try:
        from website.views import _DASHBOARD_SEARCH_NAV_ADMIN, _DASHBOARD_SEARCH_NAV_STUDENT

        role = request.session.get("role", "student")
        nav = _DASHBOARD_SEARCH_NAV_ADMIN if role == "admin" else _DASHBOARD_SEARCH_NAV_STUDENT
        return {"dashboard_search_nav": nav}
    except Exception:
        return {"dashboard_search_nav": []}


def reported_questions_badge(request):
    """Provides pending report count for the admin sidebar badge.
    Only queries Supabase when the session role is 'admin'."""
    if request.session.get("role") != "admin":
        return {"reported_questions_pending": 0}
    try:
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        result = (
            client.table("question_reports")
            .select("id", count="exact", head=True)
            .eq("status", "pending")
            .execute()
        )
        count = result.count or 0
    except Exception:
        count = 0
    return {"reported_questions_pending": count}
