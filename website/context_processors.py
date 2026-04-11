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
