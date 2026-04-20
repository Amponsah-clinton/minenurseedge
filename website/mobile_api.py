"""
JSON API for the React Native app. Authenticates with Supabase JWT (Authorization: Bearer <access_token>).
Uses the same Supabase service-role access as server-rendered views — no secrets in the mobile bundle.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Optional

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from website import views as V

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _parse_json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}


def _bearer_token(request) -> Optional[str]:
    h = request.headers.get("Authorization") or ""
    if h.startswith("Bearer "):
        return h[7:].strip()
    return None


def _user_id_from_jwt(request) -> tuple[Optional[str], Optional[JsonResponse]]:
    token = _bearer_token(request)
    if not token:
        return None, JsonResponse({"ok": False, "error": "missing_token"}, status=401)
    try:
        sb = V._supabase()
        resp = sb.auth.get_user(token)
        if not resp or not resp.user:
            return None, JsonResponse({"ok": False, "error": "invalid_token"}, status=401)
        return str(resp.user.id), None
    except Exception:
        logger.exception("JWT validation failed")
        return None, JsonResponse({"ok": False, "error": "invalid_token"}, status=401)


def _profile_row(user_id: str) -> Optional[dict]:
    try:
        rows = (
            V._supabase_admin()
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _subscription_block(user_id: str, profile: Optional[dict]) -> Optional[JsonResponse]:
    if profile and profile.get("role") == "admin":
        return None
    allowed, reason = V._subscription_access_state(user_id)
    if allowed:
        return None
    return JsonResponse(
        {"ok": False, "error": "subscription_required", "reason": reason},
        status=402,
    )


def _required_fields(body: dict, fields: list[str]) -> Optional[JsonResponse]:
    missing = [f for f in fields if not str(body.get(f) or "").strip()]
    if missing:
        return JsonResponse({"ok": False, "error": "missing_fields", "fields": missing}, status=400)
    return None


@csrf_exempt
@require_GET
def mobile_health(_request):
    return JsonResponse({"ok": True, "service": "namari-mobile-api"})


@csrf_exempt
@require_POST
def mobile_auth_signup(request):
    body = _parse_json_body(request)
    needed = _required_fields(body, ["full_name", "email", "password", "confirm_password", "programme", "year_of_study", "school"])
    if needed:
        return needed
    full_name = str(body.get("full_name")).strip()
    email = str(body.get("email")).strip().lower()
    password = str(body.get("password"))
    confirm_password = str(body.get("confirm_password"))
    phone = str(body.get("phone") or "").strip()
    programme = str(body.get("programme")).strip()
    year_of_study = str(body.get("year_of_study")).strip()
    school = str(body.get("school")).strip()
    if password != confirm_password:
        return JsonResponse({"ok": False, "error": "password_mismatch"}, status=400)
    errors = V._validate_password(password)
    if errors:
        return JsonResponse({"ok": False, "error": "weak_password", "details": errors}, status=400)
    if programme not in V.PROGRAMME_CHOICES:
        return JsonResponse({"ok": False, "error": "invalid_programme"}, status=400)
    if year_of_study not in V.YEAR_OF_STUDY_CODES:
        return JsonResponse({"ok": False, "error": "invalid_year_of_study"}, status=400)

    try:
        existing = V._find_auth_user_by_email(email)
        if existing:
            return JsonResponse({"ok": False, "error": "email_already_exists"}, status=409)
    except Exception:
        pass

    try:
        auth = V._supabase().auth.sign_up(
            {
                "email": email,
                "password": password,
                "options": {"data": {"full_name": full_name}},
            }
        )
        if not auth or not auth.user:
            return JsonResponse({"ok": False, "error": "signup_failed"}, status=500)
        user_id = str(auth.user.id)
        plans = V._get_plans()
        plan_slug = "standard"
        plan = plans.get(plan_slug, {})
        admin = V._supabase_admin()
        admin.table("profiles").upsert(
            {
                "id": user_id,
                "email": email,
                "full_name": full_name,
                "phone_number": phone,
                "year_of_study": year_of_study,
                "school": school,
                "programme": programme,
                "role": "student",
                "is_active": True,
                "plan_slug": plan_slug,
                "subscription_status": "pending_payment",
            }
        ).execute()
        admin.table("subscriptions").insert(
            {
                "user_id": user_id,
                "user_email": email,
                "user_name": full_name,
                "plan_slug": plan_slug,
                "amount_due": plan.get("price", 0),
                "currency": plan.get("currency", "GHS"),
                "status": "pending_payment",
            }
        ).execute()
        return JsonResponse({"ok": True, "user_id": user_id, "requires_subscription": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": "signup_failed", "detail": str(exc)}, status=500)


@csrf_exempt
@require_POST
def mobile_auth_forgot_password(request):
    body = _parse_json_body(request)
    email = str(body.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "email_required"}, status=400)
    try:
        V._supabase().auth.reset_password_for_email(email)
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": "reset_email_failed", "detail": str(exc)}, status=500)


@csrf_exempt
@require_GET
def mobile_me(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not profile:
        return JsonResponse({"ok": False, "error": "profile_not_found"}, status=404)
    admin = V._supabase_admin()
    sub = V._get_active_subscription(user_id)
    pending_academic = False
    if profile.get("role") == "student":
        pending_academic = not V._academic_profile_is_complete(profile)
    return JsonResponse(
        {
            "ok": True,
            "user_id": user_id,
            "email": profile.get("email"),
            "full_name": profile.get("full_name"),
            "role": profile.get("role"),
            "programme": profile.get("programme"),
            "year_of_study": profile.get("year_of_study"),
            "school": profile.get("school"),
            "is_active": profile.get("is_active"),
            "is_free_access": V._user_has_free_access(admin, user_id),
            "pending_academic_profile": pending_academic,
            "subscription": sub,
        }
    )


@csrf_exempt
@require_GET
def mobile_dashboard(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not profile:
        return JsonResponse({"ok": False, "error": "profile_not_found"}, status=404)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err

    unread_count = V._student_unread_count(user_id)
    mock_exam_count = 0
    best_mock_percentage = 0
    total_questions_available = 0
    recent_messages = []
    peer_comparison = None
    admin = V._supabase_admin()
    try:
        programme = (profile.get("programme") or "").strip()
        if programme:
            mock_exam_count = (
                admin.table("mock_exams")
                .select("id", count="exact", head=True)
                .eq("programme", programme)
                .eq("is_published", True)
                .execute()
                .count
                or 0
            )
            total_questions_available = (
                admin.table("question_bank")
                .select("id", count="exact", head=True)
                .eq("programme", programme)
                .execute()
                .count
                or 0
            )
            recent_messages = (
                admin.table("student_notifications")
                .select("id, title, message_body, is_read, created_at")
                .eq("student_id", user_id)
                .order("created_at", desc=True)
                .limit(3)
                .execute()
                .data
                or []
            )
        else:
            total_questions_available = (
                admin.table("question_bank")
                .select("id", count="exact", head=True)
                .execute()
                .count
                or 0
            )
            recent_messages = (
                admin.table("student_notifications")
                .select("id, title, message_body, is_read, created_at")
                .eq("student_id", user_id)
                .order("created_at", desc=True)
                .limit(3)
                .execute()
                .data
                or []
            )
        best_attempt_rows = (
            admin.table("mock_attempts")
            .select("percentage")
            .eq("student_id", user_id)
            .not_.is_("submitted_at", "null")
            .order("percentage", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if best_attempt_rows:
            best_mock_percentage = int(float(best_attempt_rows[0].get("percentage") or 0))
        peer_comparison = V._build_weekly_peer_comparison(admin, user_id)
    except Exception:
        pass

    subscription = V._get_active_subscription(user_id)
    plan_name = "NursesEdge Access"
    plan_slug = "standard"
    sub_status = "pending_payment"
    sub_expires = None
    try:
        if subscription:
            plan_slug = subscription.get("plan_slug", "standard")
            sub_status = subscription.get("status", "pending_payment")
            sub_expires = subscription.get("expires_at", None)
        if sub_status == "active":
            plan_name = "Subscribed"
        elif sub_status == "pending_payment":
            plan_name = "Pending Activation"
        else:
            plan_name = "Inactive"
    except Exception:
        pass

    joined_communities = []
    community_unread = 0
    try:
        memberships = (
            admin.table("community_members")
            .select("community_id")
            .eq("user_id", str(user_id))
            .execute()
            .data
            or []
        )
        if memberships:
            cids = [m["community_id"] for m in memberships]
            joined_communities = (
                admin.table("communities")
                .select("id, name, slug, icon, color_hex, member_count")
                .in_("id", cids)
                .eq("is_active", True)
                .order("name")
                .execute()
                .data
                or []
            )
        community_unread = V._community_unread_count(user_id)
    except Exception:
        pass

    show_nmc_disclaimer = False
    if profile.get("role") != "admin":
        show_nmc_disclaimer = V._student_needs_dashboard_nmc_disclaimer(request, user_id)

    days_remaining = None
    plan_progress_pct = 100
    sub_expires_display = ""
    _sub_expires_str = (str(sub_expires) if sub_expires else "")[:10]
    if _sub_expires_str and sub_status == "active":
        try:
            expiry_date = date.fromisoformat(_sub_expires_str)
            days_remaining = (expiry_date - date.today()).days
            plan_progress_pct = max(0, min(100, round(max(days_remaining or 0, 0) / 365 * 100)))
            sub_expires_display = f"{expiry_date.day} {expiry_date.strftime('%b')} {expiry_date.year}"
        except Exception:
            pass

    payload = {
        "ok": True,
        "full_name": profile.get("full_name") or "Student",
        "email": profile.get("email"),
        "role": profile.get("role"),
        "student_unread_notifications": unread_count,
        "mock_exam_count": mock_exam_count,
        "best_mock_percentage": best_mock_percentage,
        "total_questions_available": total_questions_available,
        "recent_messages": recent_messages,
        "joined_communities": joined_communities,
        "community_unread": community_unread,
        "plan_name": plan_name,
        "plan_slug": plan_slug,
        "sub_status": sub_status,
        "sub_expires": _sub_expires_str,
        "sub_expires_display": sub_expires_display,
        "days_remaining": days_remaining,
        "plan_progress_pct": plan_progress_pct,
        "peer_comparison": peer_comparison,
        "show_nmc_disclaimer": show_nmc_disclaimer,
    }
    return JsonResponse(_json_safe(payload))


@csrf_exempt
@require_GET
def mobile_messages(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    try:
        rows = (
            V._supabase_admin()
            .table("student_notifications")
            .select("*")
            .eq("student_id", user_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return JsonResponse({"ok": True, "messages": _json_safe(rows)})


@csrf_exempt
@require_POST
def mobile_message_read(request, message_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    try:
        (
            V._supabase_admin()
            .table("student_notifications")
            .update({"is_read": True})
            .eq("id", str(message_id))
            .eq("student_id", user_id)
            .execute()
        )
    except Exception:
        pass
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_messages_read_all(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    try:
        (
            V._supabase_admin()
            .table("student_notifications")
            .update({"is_read": True})
            .eq("student_id", user_id)
            .eq("is_read", False)
            .execute()
        )
    except Exception:
        pass
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_drug_cards(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    drugs = []
    try:
        query = V._supabase_admin().table("drug_cards").select("*").eq("is_active", True)
        if category:
            query = query.eq("category", category)
        if q:
            query = query.ilike("drug_name", f"%{q}%")
        drugs = query.order("drug_name").execute().data or []
    except Exception:
        pass
    return JsonResponse(
        {
            "ok": True,
            "drugs": _json_safe(drugs),
            "categories": V.DRUG_CATEGORIES,
            "query": q,
            "selected_category": category,
        }
    )


@csrf_exempt
def mobile_dosage(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    if request.method == "POST":
        body = _parse_json_body(request)
        if body.get("action") == "save":
            try:
                admin.table("dosage_calculations").insert(
                    {
                        "user_id": user_id,
                        "calc_type": body.get("calc_type", ""),
                        "inputs_summary": body.get("inputs_summary", ""),
                        "result_text": body.get("result_text", ""),
                    }
                ).execute()
                return JsonResponse({"ok": True, "saved": True})
            except Exception:
                return JsonResponse({"ok": False, "error": "save_failed"}, status=400)
    calc_history = []
    try:
        calc_history = (
            admin.table("dosage_calculations")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
    except Exception:
        pass
    return JsonResponse({"ok": True, "calc_history": _json_safe(calc_history)})


@csrf_exempt
@require_GET
def mobile_mock_exams(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    programme = V.GLOBAL_MOCK_PROGRAMME
    exams = []
    try:
        exams = (
            admin.table("mock_exams")
            .select("*")
            .eq("programme", programme)
            .eq("is_published", True)
            .order("mock_number", desc=False)
            .execute()
            .data
            or []
        )
    except Exception:
        exams = []
    try:
        pool_cnt = (
            admin.table("question_bank")
            .select("id", count="exact", head=True)
            .eq("programme", programme)
            .execute()
            .count
            or 0
        )
    except Exception:
        pool_cnt = 0
    V._enrich_mock_exams_for_admin(exams, pool_cnt)
    try:
        attempts = (
            admin.table("mock_attempts")
            .select("id, mock_exam_id, percentage, submitted_at")
            .eq("student_id", user_id)
            .not_.is_("submitted_at", "null")
            .order("submitted_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        attempts = []
    best_by_exam = {}
    for attempt in attempts:
        eid = attempt.get("mock_exam_id")
        pct = float(attempt.get("percentage") or 0)
        existing = best_by_exam.get(eid)
        if existing is None or pct > existing["pct"]:
            best_by_exam[eid] = {"pct": pct, "attempt_id": attempt.get("id")}
    for exam in exams:
        best = best_by_exam.get(exam["id"])
        exam["my_best"] = best["pct"] if best else None
        exam["best_attempt_id"] = best["attempt_id"] if best else None
        exam["leaderboard_rows"] = []
        exam["my_rank"] = None
        exam["my_row"] = None
    for exam in exams:
        rows, my_rank, my_row = V._build_mock_leaderboard(admin, exam["id"], user_id)
        exam["leaderboard_rows"] = rows
        exam["my_rank"] = my_rank
        exam["my_row"] = my_row
    return JsonResponse(_json_safe({"ok": True, "programme": programme, "mock_exams": exams}))


@csrf_exempt
@require_GET
def mobile_general_tests(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    programme = (profile.get("programme") or "").strip() if profile else ""
    active_attempts_map = {}
    try:
        active_rows = (
            admin.table("general_test_attempts")
            .select("id, paper_title, status, paused_remaining_seconds, resumed_at, started_at, time_limit_minutes")
            .eq("student_id", user_id)
            .is_("submitted_at", "null")
            .execute()
            .data
            or []
        )
        for row in active_rows:
            active_attempts_map[row["paper_title"]] = row
    except Exception:
        pass
    tests = []
    try:
        if programme in V.PROGRAMME_PAPERS:
            grouped = V._question_bank_counts_by_paper(admin, programme)
        else:
            grouped = V._question_bank_counts_by_paper_chunked(admin, programme)
        for paper, count in grouped.items():
            if count <= 0:
                continue
            batch_size = V._general_test_question_count(paper)
            num_batches = V._general_test_num_batches(count, batch_size)
            for test_number in range(1, num_batches + 1):
                actual_q = V._general_test_actual_batch_size(count, batch_size, test_number)
                if actual_q <= 0:
                    continue
                full_title = f"{paper} — General Test {test_number}"
                active = active_attempts_map.get(full_title)
                tests.append(
                    {
                        "paper_title": paper,
                        "test_number": test_number,
                        "question_count": actual_q,
                        "duration_minutes": V._general_test_batch_duration_minutes(
                            paper, batch_size, actual_q
                        ),
                        "attempt_id": active["id"] if active else None,
                        "attempt_status": active.get("status", "in_progress") if active else None,
                    }
                )
        tests.sort(key=lambda x: (x["paper_title"], x["test_number"]))
    except Exception:
        tests = []
    return JsonResponse({"ok": True, "tests": tests, "programme": programme})


@csrf_exempt
@require_GET
def mobile_quizzes(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    quizzes = []
    try:
        quizzes = (
            admin.table("practice_quizzes")
            .select("id, title, sort_index, created_at")
            .eq("is_published", True)
            .order("sort_index", desc=False)
            .execute()
            .data
            or []
        )
        for qz in quizzes:
            q_cnt = (
                admin.table("practice_quiz_questions")
                .select("id", count="exact", head=True)
                .eq("quiz_id", qz["id"])
                .execute()
            )
            qz["question_count"] = q_cnt.count or 0
            best_rows = (
                admin.table("practice_quiz_attempts")
                .select("percentage")
                .eq("student_id", user_id)
                .eq("quiz_id", qz["id"])
                .order("percentage", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
            qz["best_percentage"] = float(best_rows[0]["percentage"]) if best_rows else None
    except Exception:
        quizzes = []
    return JsonResponse(_json_safe({"ok": True, "quizzes": quizzes}))


@csrf_exempt
@require_GET
def mobile_communities(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    db = V._supabase_admin()
    try:
        communities = db.table("communities").select("*").eq("is_active", True).order("name").execute().data or []
    except Exception:
        communities = []
    try:
        memberships = (
            db.table("community_members")
            .select("community_id")
            .eq("user_id", str(user_id))
            .execute()
            .data
            or []
        )
        joined_ids = {m["community_id"] for m in memberships}
    except Exception:
        joined_ids = set()
    for c in communities:
        c["is_member"] = c["id"] in joined_ids
    return JsonResponse(
        _json_safe(
            {
                "ok": True,
                "communities": communities,
                "joined_count": len(joined_ids),
            }
        )
    )


@csrf_exempt
@require_POST
def mobile_community_join(request, slug):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    community = V._get_community_by_slug(slug)
    if not community:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    try:
        V._supabase_admin().table("community_members").insert(
            {"community_id": community["id"], "user_id": str(user_id)}
        ).execute()
    except Exception:
        pass
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_community_leave(request, slug):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    community = V._get_community_by_slug(slug)
    if not community:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    try:
        V._supabase_admin().table("community_members").delete().eq("community_id", community["id"]).eq(
            "user_id", str(user_id)
        ).execute()
    except Exception:
        pass
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_performance(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    mock_attempts = (
        admin.table("mock_attempts")
        .select("id, mock_exam_id, percentage, score, total_questions, submitted_at")
        .eq("student_id", user_id)
        .not_.is_("submitted_at", "null")
        .order("submitted_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )
    mock_ids = [m.get("mock_exam_id") for m in mock_attempts if m.get("mock_exam_id")]
    mock_names = {}
    if mock_ids:
        mock_rows = admin.table("mock_exams").select("id, title").in_("id", mock_ids).execute().data or []
        mock_names = {m["id"]: m.get("title") or "Mock Exam" for m in mock_rows}
    for m in mock_attempts:
        m["test_name"] = mock_names.get(m.get("mock_exam_id"), "Mock Exam")
        m["test_type"] = "Mock"
    general_attempts = (
        admin.table("general_test_attempts")
        .select("id, paper_title, percentage, score, total_questions, submitted_at")
        .eq("student_id", user_id)
        .not_.is_("submitted_at", "null")
        .order("submitted_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    for g in general_attempts:
        g["test_name"] = g.get("paper_title") or "General Test"
        g["test_type"] = "General Test"
    quiz_attempts = []
    try:
        quiz_attempts = (
            admin.table("practice_quiz_attempts")
            .select("id, quiz_id, percentage, score, total_questions, submitted_at")
            .eq("student_id", user_id)
            .order("submitted_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        quiz_attempts = []
    quiz_ids = [q.get("quiz_id") for q in quiz_attempts if q.get("quiz_id")]
    quiz_titles = {}
    if quiz_ids:
        try:
            qz_rows = admin.table("practice_quizzes").select("id, title").in_("id", quiz_ids).execute().data or []
            quiz_titles = {r["id"]: r.get("title") or "Quiz" for r in qz_rows}
        except Exception:
            quiz_titles = {}
    for q in quiz_attempts:
        q["test_name"] = quiz_titles.get(q.get("quiz_id"), "Quiz")
        q["test_type"] = "Quiz"
    all_attempts = mock_attempts + general_attempts + quiz_attempts
    all_attempts.sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    avg_score = (
        round(sum(float(a.get("percentage") or 0) for a in all_attempts) / len(all_attempts), 1) if all_attempts else 0
    )
    return JsonResponse(
        _json_safe({"ok": True, "all_attempts": all_attempts, "avg_score": avg_score})
    )


@csrf_exempt
@require_GET
def mobile_lecture_notes(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    notes = []
    try:
        raw_notes = (
            V._supabase_admin()
            .table("lecture_notes")
            .select("*")
            .eq("is_published", True)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        notes = [
            {
                **n,
                "render_html": V._lecture_body_for_render(n),
                "render_font_px": V._lecture_display_font_px(n),
            }
            for n in raw_notes
        ]
    except Exception:
        notes = []
    groups = V._group_student_lecture_notes(notes)
    return JsonResponse(_json_safe({"ok": True, "notes": notes, "note_groups": groups}))


@csrf_exempt
def mobile_flashcards(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    today = datetime.now(dt_timezone.utc).date().isoformat()
    question_pool = []
    try:
        question_pool = (
            admin.table("question_bank")
            .select("id, question_text, options, correct_option, explanation")
            .eq("programme", V.GLOBAL_MOCK_PROGRAMME)
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )
    except Exception:
        question_pool = []
    if not question_pool:
        return JsonResponse({"ok": True, "empty_state": True, "daily_cards": [], "done_for_today": True})
    rng = random.Random(f"{user_id}:{today}")
    shuffled = list(question_pool)
    rng.shuffle(shuffled)
    daily_cards = shuffled[:10]
    daily_ids = [str(item.get("id")) for item in daily_cards if item.get("id") is not None]
    daily_id_set = set(daily_ids)
    reviewed_rows = []
    if daily_ids:
        try:
            reviewed_rows = (
                admin.table("flashcard_daily_reviews")
                .select("question_id")
                .eq("student_id", user_id)
                .eq("review_date", today)
                .in_("question_id", daily_ids)
                .execute()
                .data
                or []
            )
        except Exception:
            reviewed_rows = []
    reviewed_ids = {str(row.get("question_id")) for row in reviewed_rows if row.get("question_id") is not None}
    if request.method == "POST":
        body = _parse_json_body(request)
        question_id = str(body.get("question_id", "") or "").strip()
        if question_id and question_id in daily_id_set:
            V._save_flashcard_daily_review(admin, user_id, today, question_id)
        return JsonResponse({"ok": True})
    pending_cards = [card for card in daily_cards if str(card.get("id")) not in reviewed_ids]
    reviewed_count = len(reviewed_ids.intersection(daily_id_set))
    done_for_today = reviewed_count >= len(daily_cards)
    current_card = pending_cards[0] if pending_cards else None
    return JsonResponse(
        _json_safe(
            {
                "ok": True,
                "review_date": today,
                "daily_cards": daily_cards,
                "pending_count": len(pending_cards),
                "done_for_today": done_for_today,
                "current_card": current_card,
            }
        )
    )


@csrf_exempt
@require_POST
def mobile_ack_disclaimer(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if profile and profile.get("role") == "admin":
        return JsonResponse({"ok": True})
    try:
        V._supabase_admin().table("profiles").update({"dashboard_nmc_disclaimer_ack_at": datetime.now(dt_timezone.utc).isoformat()}).eq(
            "id", user_id
        ).execute()
    except Exception:
        pass
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_academic_profile(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not profile or profile.get("role") != "student":
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    body = _parse_json_body(request)
    programme = (body.get("programme") or "").strip()
    year_of_study = (body.get("yearOfStudy") or body.get("year_of_study") or "").strip()
    institution = (body.get("institution") or "").strip()
    errors = []
    if programme not in V.PROGRAMME_CHOICES:
        errors.append("Invalid programme.")
    if year_of_study not in V.YEAR_OF_STUDY_CODES:
        errors.append("Invalid year of study.")
    if not institution:
        errors.append("School / Institution is required.")
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    try:
        V._supabase_admin().table("profiles").update(
            {"programme": programme, "year_of_study": year_of_study, "school": institution}
        ).eq("id", user_id).execute()
    except Exception:
        return JsonResponse({"ok": False, "error": "save_failed"}, status=500)
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_contact(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    body = _parse_json_body(request)
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    phone = (body.get("phone") or "").strip()
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    if not all([name, email, phone, message]):
        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)
    try:
        V._supabase_admin().table("contact_messages").insert(
            {"name": name, "email": email, "phone": phone, "subject": subject, "message": message}
        ).execute()
    except Exception:
        return JsonResponse({"ok": False, "error": "insert_failed"}, status=500)
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_bookmarks(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    mock_rows = []
    general_rows = []
    try:
        mock_rows = (
            admin.table("mock_attempt_answers")
            .select(
                "question_id, selected_option, created_at, "
                "mock_attempts!inner(student_id, mock_exam_id, submitted_at), "
                "question_bank(question_text, options, explanation), "
                "mock_exams:mock_attempts(mock_exams(title))"
            )
            .eq("is_bookmarked", True)
            .eq("mock_attempts.student_id", user_id)
            .execute()
            .data
            or []
        )
    except Exception:
        mock_rows = []
    try:
        general_rows = (
            admin.table("general_test_attempt_answers")
            .select(
                "question_id, selected_option, created_at, "
                "general_test_attempts!inner(student_id, paper_title, submitted_at), "
                "question_bank(question_text, options, explanation)"
            )
            .eq("is_bookmarked", True)
            .eq("general_test_attempts.student_id", user_id)
            .execute()
            .data
            or []
        )
    except Exception:
        general_rows = []
    bookmarks = []
    for row in mock_rows:
        q = row.get("question_bank") or {}
        mock_attempt = row.get("mock_attempts") or {}
        title = "Mock Exam"
        nested = row.get("mock_exams")
        if isinstance(nested, dict):
            title = nested.get("title") or title
        bookmarks.append(
            {
                "source": "Mock",
                "source_name": title,
                "question_text": q.get("question_text") or "Question",
                "options": q.get("options") or {},
                "selected_option": row.get("selected_option") or "",
                "explanation": q.get("explanation") or "",
                "created_at": row.get("created_at") or "",
                "submitted_at": mock_attempt.get("submitted_at") or "",
            }
        )
    for row in general_rows:
        q = row.get("question_bank") or {}
        attempt = row.get("general_test_attempts") or {}
        bookmarks.append(
            {
                "source": "General Test",
                "source_name": attempt.get("paper_title") or "General Test",
                "question_text": q.get("question_text") or "Question",
                "options": q.get("options") or {},
                "selected_option": row.get("selected_option") or "",
                "explanation": q.get("explanation") or "",
                "created_at": row.get("created_at") or "",
                "submitted_at": attempt.get("submitted_at") or "",
            }
        )
    bookmarks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return JsonResponse({"ok": True, "bookmarks": _json_safe(bookmarks)})


def _is_admin_profile(profile: Optional[dict]) -> bool:
    return bool(profile and profile.get("role") == "admin")


@csrf_exempt
@require_GET
def mobile_quiz_detail(request, quiz_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    quiz_rows = (
        admin.table("practice_quizzes")
        .select("id, title")
        .eq("id", str(quiz_id))
        .eq("is_published", True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not quiz_rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    questions = (
        admin.table("practice_quiz_questions")
        .select("id, question_order, question_text, option_a, option_b, option_c")
        .eq("quiz_id", str(quiz_id))
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    for q in questions:
        q["options"] = {"A": q.get("option_a") or "", "B": q.get("option_b") or "", "C": q.get("option_c") or ""}
    return JsonResponse(_json_safe({"ok": True, "quiz": quiz_rows[0], "questions": questions}))


@csrf_exempt
@require_POST
def mobile_quiz_submit(request, quiz_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    body = _parse_json_body(request)
    answers = body.get("answers") or {}
    rows = (
        admin.table("practice_quiz_questions")
        .select("id, correct_option")
        .eq("quiz_id", str(quiz_id))
        .execute()
        .data
        or []
    )
    if not rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    correct_count = 0
    answer_rows = []
    for row in rows:
        qid = str(row.get("id"))
        sel = str(answers.get(qid) or "").strip().upper()
        if sel not in ("A", "B", "C"):
            sel = ""
        corr = str(row.get("correct_option") or "").upper()
        is_ok = bool(sel and sel == corr)
        if is_ok:
            correct_count += 1
        answer_rows.append({"question_id": qid, "selected_option": sel or None, "is_correct": is_ok})
    total = len(rows)
    pct = round((correct_count / total) * 100, 2) if total else 0.0
    attempt = (
        admin.table("practice_quiz_attempts")
        .insert(
            {
                "student_id": user_id,
                "quiz_id": str(quiz_id),
                "total_questions": total,
                "score": correct_count,
                "correct_answers": correct_count,
                "percentage": pct,
            }
        )
        .execute()
        .data
    )[0]
    for row in answer_rows:
        row["attempt_id"] = attempt["id"]
    if answer_rows:
        admin.table("practice_quiz_attempt_answers").insert(answer_rows).execute()
    return JsonResponse({"ok": True, "attempt_id": attempt["id"], "score": correct_count, "total": total, "percentage": pct})


@csrf_exempt
@require_POST
def mobile_general_test_start(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    paper_title = str(body.get("paper_title") or "").strip()
    test_number = int(body.get("test_number") or 1)
    if not paper_title:
        return JsonResponse({"ok": False, "error": "paper_title_required"}, status=400)
    full_title = f"{paper_title} — General Test {test_number}"
    admin = V._supabase_admin()
    existing = (
        admin.table("general_test_attempts")
        .select("id")
        .eq("student_id", user_id)
        .eq("paper_title", full_title)
        .is_("submitted_at", "null")
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return JsonResponse({"ok": True, "attempt_id": existing[0]["id"], "existing": True})
    batch_size = V._general_test_question_count(paper_title)
    offset = (max(1, test_number) - 1) * batch_size
    profile_prog = (profile or {}).get("programme") or ""
    all_ids = (
        admin.table("question_bank")
        .select("id")
        .eq("programme", profile_prog)
        .eq("paper_title", paper_title)
        .order("created_at", desc=False)
        .range(offset, offset + batch_size - 1)
        .execute()
        .data
        or []
    )
    actual_count = len(all_ids)
    if actual_count < 1:
        return JsonResponse({"ok": False, "error": "insufficient_questions"}, status=400)
    attempt = (
        admin.table("general_test_attempts")
        .insert(
            {
                "student_id": user_id,
                "paper_title": full_title,
                "time_limit_minutes": V._general_test_batch_duration_minutes(
                    paper_title, batch_size, actual_count
                ),
                "total_questions": actual_count,
                "status": "in_progress",
            }
        )
        .execute()
        .data
    )[0]
    links = [{"attempt_id": attempt["id"], "question_id": row["id"], "question_order": idx} for idx, row in enumerate(all_ids, start=1)]
    admin.table("general_test_attempt_questions").insert(links).execute()
    return JsonResponse({"ok": True, "attempt_id": attempt["id"], "existing": False})


@csrf_exempt
def mobile_general_test_attempt(request, attempt_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    rows = (
        admin.table("general_test_attempts").select("*").eq("id", str(attempt_id)).eq("student_id", user_id).limit(1).execute().data
        or []
    )
    if not rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    attempt = rows[0]
    if request.method == "POST":
        body = _parse_json_body(request)
        action = str(body.get("action") or "").strip()
        qid = str(body.get("question_id") or "").strip()
        selected_option = str(body.get("selected_option") or "").strip().upper()
        if selected_option not in ("A", "B", "C"):
            selected_option = ""
        is_bookmarked = bool(body.get("is_bookmarked"))
        is_flagged = bool(body.get("is_flagged"))
        if action == "resume":
            admin.table("general_test_attempts").update({"status": "in_progress", "resumed_at": datetime.now(dt_timezone.utc).isoformat()}).eq("id", str(attempt_id)).execute()
        else:
            links = (
                admin.table("general_test_attempt_questions")
                .select("question_id, question_bank(correct_option)")
                .eq("attempt_id", str(attempt_id))
                .eq("question_id", qid)
                .limit(1)
                .execute()
                .data
                or []
            )
            if links:
                correct = ((links[0].get("question_bank") or {}).get("correct_option") or "").upper()
                payload = {
                    "attempt_id": str(attempt_id),
                    "question_id": qid,
                    "selected_option": selected_option or None,
                    "is_correct": bool(selected_option and selected_option == correct),
                    "is_bookmarked": is_bookmarked,
                    "is_flagged": is_flagged,
                    "answered_at": datetime.now(dt_timezone.utc).isoformat(),
                }
                existing = (
                    admin.table("general_test_attempt_answers").select("id").eq("attempt_id", str(attempt_id)).eq("question_id", qid).limit(1).execute().data
                    or []
                )
                if existing:
                    admin.table("general_test_attempt_answers").update(payload).eq("id", existing[0]["id"]).execute()
                else:
                    admin.table("general_test_attempt_answers").insert(payload).execute()
            if action == "pause":
                remaining_now = int(body.get("remaining_seconds") or 0)
                admin.table("general_test_attempts").update({"status": "paused", "paused_remaining_seconds": remaining_now, "paused_at_index": int(body.get("current_index") or 1)}).eq("id", str(attempt_id)).execute()
            if action == "submit":
                final_answers = admin.table("general_test_attempt_answers").select("*").eq("attempt_id", str(attempt_id)).execute().data or []
                total_questions = int(attempt.get("total_questions") or 0)
                correct_answers = sum(1 for x in final_answers if x.get("is_correct"))
                percentage = round((correct_answers / total_questions) * 100, 2) if total_questions else 0.0
                admin.table("general_test_attempts").update({"submitted_at": datetime.now(dt_timezone.utc).isoformat(), "score": correct_answers, "correct_answers": correct_answers, "percentage": percentage, "status": "submitted"}).eq("id", str(attempt_id)).execute()
        attempt = (admin.table("general_test_attempts").select("*").eq("id", str(attempt_id)).limit(1).execute().data or [attempt])[0]
    links = (
        admin.table("general_test_attempt_questions")
        .select("question_order, question_id, question_bank(id, question_text, options, correct_option, explanation)")
        .eq("attempt_id", str(attempt_id))
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    answers = admin.table("general_test_attempt_answers").select("*").eq("attempt_id", str(attempt_id)).execute().data or []
    now_utc = datetime.now(dt_timezone.utc)
    if attempt.get("submitted_at"):
        remaining = 0
    elif attempt.get("resumed_at"):
        resumed_at = datetime.fromisoformat(str(attempt["resumed_at"]).replace("Z", "+00:00"))
        end_time = resumed_at + timedelta(seconds=int(attempt.get("paused_remaining_seconds") or 0))
        remaining = max(0, int((end_time - now_utc).total_seconds()))
    else:
        started_at = datetime.fromisoformat(str(attempt["started_at"]).replace("Z", "+00:00"))
        end_time = started_at + timedelta(minutes=int(attempt.get("time_limit_minutes") or 90))
        remaining = max(0, int((end_time - now_utc).total_seconds()))
    return JsonResponse(_json_safe({"ok": True, "attempt": attempt, "questions": links, "answers": answers, "remaining_seconds": remaining}))


@csrf_exempt
@require_GET
def mobile_general_test_result(request, attempt_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    rows = admin.table("general_test_attempts").select("*").eq("id", str(attempt_id)).eq("student_id", user_id).limit(1).execute().data or []
    if not rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    return JsonResponse({"ok": True, "attempt": _json_safe(rows[0]), "encouragement": V._score_message(float(rows[0].get("percentage") or 0))})


@csrf_exempt
def mobile_mock_exam_attempt(request, exam_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    exam_rows = admin.table("mock_exams").select("*").eq("id", str(exam_id)).limit(1).execute().data or []
    if not exam_rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    exam = exam_rows[0]
    attempt_rows = (
        admin.table("mock_attempts")
        .select("*")
        .eq("mock_exam_id", str(exam_id))
        .eq("student_id", user_id)
        .is_("submitted_at", "null")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if request.method == "POST":
        body = _parse_json_body(request)
        action = str(body.get("action") or "").strip()
        if action == "begin" and not attempt_rows:
            admin.table("mock_attempts").insert({"mock_exam_id": str(exam_id), "student_id": user_id, "time_limit_minutes": int(exam.get("duration_minutes") or V.MOCK_DURATION_MINUTES), "total_questions": int(exam.get("question_count") or V.MOCK_QUESTION_BATCH_SIZE)}).execute()
            attempt_rows = (
                admin.table("mock_attempts")
                .select("*")
                .eq("mock_exam_id", str(exam_id))
                .eq("student_id", user_id)
                .is_("submitted_at", "null")
                .order("started_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
        if not attempt_rows:
            return JsonResponse({"ok": False, "error": "attempt_not_started"}, status=400)
        attempt = attempt_rows[0]
        qid = str(body.get("question_id") or "").strip()
        selected_option = str(body.get("selected_option") or "").strip().upper()
        if selected_option not in ("A", "B", "C"):
            selected_option = ""
        is_bookmarked = bool(body.get("is_bookmarked"))
        is_flagged = bool(body.get("is_flagged"))
        if action in ("save", "next", "prev", "skip", "submit"):
            links = (
                admin.table("mock_attempt_questions")
                .select("question_id, question_bank(correct_option)")
                .eq("attempt_id", attempt["id"])
                .eq("question_id", qid)
                .limit(1)
                .execute()
                .data
                or []
            )
            if links:
                correct = ((links[0].get("question_bank") or {}).get("correct_option") or "").upper()
                payload = {
                    "attempt_id": attempt["id"],
                    "question_id": qid,
                    "selected_option": selected_option or None,
                    "is_correct": bool(selected_option and selected_option == correct),
                    "is_bookmarked": is_bookmarked,
                    "is_flagged": is_flagged,
                    "answered_at": datetime.now(dt_timezone.utc).isoformat(),
                }
                existing = admin.table("mock_attempt_answers").select("id").eq("attempt_id", attempt["id"]).eq("question_id", qid).limit(1).execute().data or []
                if existing:
                    admin.table("mock_attempt_answers").update(payload).eq("id", existing[0]["id"]).execute()
                else:
                    admin.table("mock_attempt_answers").insert(payload).execute()
            if action == "submit":
                final_answers = admin.table("mock_attempt_answers").select("*").eq("attempt_id", attempt["id"]).execute().data or []
                links_full = admin.table("mock_attempt_questions").select("question_id").eq("attempt_id", attempt["id"]).execute().data or []
                total_questions = len(links_full)
                correct_answers = sum(1 for x in final_answers if x.get("is_correct"))
                percentage = round((correct_answers / total_questions) * 100, 2) if total_questions else 0.0
                admin.table("mock_attempts").update({"submitted_at": datetime.now(dt_timezone.utc).isoformat(), "score": correct_answers, "correct_answers": correct_answers, "total_questions": total_questions, "percentage": percentage}).eq("id", attempt["id"]).execute()
        attempt_rows = (
            admin.table("mock_attempts")
            .select("*")
            .eq("mock_exam_id", str(exam_id))
            .eq("student_id", user_id)
            .is_("submitted_at", "null")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    if not attempt_rows:
        leaderboard_rows, my_rank, my_row = V._build_mock_leaderboard(admin, str(exam_id), user_id)
        return JsonResponse(_json_safe({"ok": True, "exam": exam, "lobby": True, "leaderboard_rows": leaderboard_rows, "my_rank": my_rank, "my_row": my_row}))
    attempt = attempt_rows[0]
    links = (
        admin.table("mock_attempt_questions")
        .select("question_order, question_id, question_bank(id, question_text, options, correct_option, explanation)")
        .eq("attempt_id", attempt["id"])
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    if not links:
        needed = int(exam.get("question_count") or V.MOCK_QUESTION_BATCH_SIZE)
        mock_number = int(exam.get("mock_number") or 1)
        student_programme = ((profile or {}).get("programme") or "").strip()
        pool_ids = V._build_mock_exam_question_pool(
            admin,
            student_programme=student_programme,
            needed=needed,
            mock_number=mock_number,
        )
        if not pool_ids or len(pool_ids) < needed:
            return JsonResponse({"ok": False, "error": "insufficient_mock_questions"}, status=400)
        pool = [{"id": qid} for qid in pool_ids]
        attempt_questions = [{"attempt_id": attempt["id"], "question_id": item["id"], "question_order": idx} for idx, item in enumerate(pool, start=1)]
        if attempt_questions:
            admin.table("mock_attempt_questions").insert(attempt_questions).execute()
        links = (
            admin.table("mock_attempt_questions")
            .select("question_order, question_id, question_bank(id, question_text, options, correct_option, explanation)")
            .eq("attempt_id", attempt["id"])
            .order("question_order", desc=False)
            .execute()
            .data
            or []
        )
    answers = admin.table("mock_attempt_answers").select("*").eq("attempt_id", attempt["id"]).execute().data or []
    started_at = datetime.fromisoformat(str(attempt["started_at"]).replace("Z", "+00:00"))
    end_time = started_at + timedelta(minutes=int(attempt.get("time_limit_minutes") or V.MOCK_DURATION_MINUTES))
    remaining = max(0, int((end_time - datetime.now(dt_timezone.utc)).total_seconds()))
    return JsonResponse(_json_safe({"ok": True, "exam": exam, "attempt": attempt, "questions": links, "answers": answers, "remaining_seconds": remaining}))


@csrf_exempt
@require_GET
def mobile_mock_exam_result(request, exam_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    rows = (
        admin.table("mock_attempts")
        .select("*")
        .eq("mock_exam_id", str(exam_id))
        .eq("student_id", user_id)
        .not_.is_("submitted_at", "null")
        .order("submitted_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    return JsonResponse({"ok": True, "attempt": _json_safe(rows[0]), "encouragement": V._score_message(float(rows[0].get("percentage") or 0))})


@csrf_exempt
@require_GET
def mobile_community_detail(request, slug):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    db = V._supabase_admin()
    community = V._get_community_by_slug(slug)
    if not community:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    posts = db.table("community_posts").select("*").eq("community_id", community["id"]).order("created_at", desc=True).limit(50).execute().data or []
    return JsonResponse(_json_safe({"ok": True, "community": community, "posts": posts}))


@csrf_exempt
@require_POST
def mobile_community_create_post(request, slug):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    db = V._supabase_admin()
    community = V._get_community_by_slug(slug)
    if not community:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    body = _parse_json_body(request)
    content = str(body.get("content") or body.get("body") or "").strip()
    title = str(body.get("title") or "").strip()
    if not content:
        return JsonResponse({"ok": False, "error": "content_required"}, status=400)
    row = (
        db.table("community_posts")
        .insert({"community_id": community["id"], "author_id": user_id, "title": title or "Post", "body": content})
        .execute()
        .data
        or []
    )
    return JsonResponse(_json_safe({"ok": True, "post": row[0] if row else None}))


@csrf_exempt
@require_POST
def mobile_community_add_comment(request, post_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    content = str(body.get("content") or body.get("body") or "").strip()
    if not content:
        return JsonResponse({"ok": False, "error": "content_required"}, status=400)
    db = V._supabase_admin()
    row = (
        db.table("community_comments")
        .insert({"post_id": str(post_id), "author_id": user_id, "body": content})
        .execute()
        .data
        or []
    )
    return JsonResponse(_json_safe({"ok": True, "comment": row[0] if row else None}))


@csrf_exempt
@require_POST
def mobile_community_react_post(request, post_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    reaction = str(body.get("reaction") or "like").strip().lower()[:20]
    db = V._supabase_admin()
    existing = (
        db.table("post_reactions")
        .select("id")
        .eq("post_id", str(post_id))
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        db.table("post_reactions").update({"reaction": reaction}).eq("id", existing[0]["id"]).execute()
    else:
        db.table("post_reactions").insert({"post_id": str(post_id), "user_id": user_id, "reaction": reaction}).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_community_react_comment(request, comment_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    reaction = str(body.get("reaction") or "like").strip().lower()[:20]
    db = V._supabase_admin()
    existing = (
        db.table("comment_reactions")
        .select("id")
        .eq("comment_id", str(comment_id))
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        db.table("comment_reactions").update({"reaction": reaction}).eq("id", existing[0]["id"]).execute()
    else:
        db.table("comment_reactions").insert({"comment_id": str(comment_id), "user_id": user_id, "reaction": reaction}).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_admin_summary(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not profile or profile.get("role") != "admin":
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    admin = V._supabase_admin()
    try:
        total_users = admin.table("profiles").select("id", count="exact", head=True).execute().count or 0
        total_students = (
            admin.table("profiles").select("id", count="exact", head=True).eq("role", "student").execute().count or 0
        )
        unread_messages = (
            admin.table("contact_messages").select("id", count="exact", head=True).eq("is_read", False).execute().count
            or 0
        )
    except Exception:
        total_users = total_students = unread_messages = 0
    return JsonResponse(
        {
            "ok": True,
            "total_users": total_users,
            "total_students": total_students,
            "unread_contact_messages": unread_messages,
        }
    )


@csrf_exempt
@require_GET
def mobile_admin_users(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    users = V._supabase_admin().table("profiles").select("*").order("created_at", desc=True).limit(200).execute().data or []
    return JsonResponse({"ok": True, "users": _json_safe(users)})


@csrf_exempt
@require_POST
def mobile_admin_toggle_user(request, target_user_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    body = _parse_json_body(request)
    is_active = bool(body.get("is_active"))
    admin = V._supabase_admin()
    admin.table("profiles").update({"is_active": is_active}).eq("id", str(target_user_id)).execute()
    if not is_active:
        admin.table("active_sessions").delete().eq("user_id", str(target_user_id)).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def mobile_admin_broadcast(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    body = _parse_json_body(request)
    title = str(body.get("title") or "").strip()
    message_body = str(body.get("message_body") or body.get("message") or "").strip()
    if not title or not message_body:
        return JsonResponse({"ok": False, "error": "title_and_message_required"}, status=400)
    admin = V._supabase_admin()
    students = admin.table("profiles").select("id").eq("role", "student").execute().data or []
    rows = [{"student_id": s.get("id"), "title": title, "message_body": message_body, "sent_by": user_id, "is_read": False} for s in students if s.get("id")]
    if rows:
        admin.table("student_notifications").insert(rows).execute()
    return JsonResponse({"ok": True, "sent_count": len(rows)})


@csrf_exempt
@require_GET
def mobile_nmc_mastery(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    return JsonResponse({"ok": True, "title": "NMC Mastery", "message": "NMC mastery content available in mobile learning module."})


@csrf_exempt
@require_POST
def mobile_report_question(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    question_id = str(body.get("question_id") or "").strip()
    reason = str(body.get("reason") or "").strip()
    notes = str(body.get("notes") or "").strip()
    if not question_id or not reason:
        return JsonResponse({"ok": False, "error": "question_id_and_reason_required"}, status=400)
    admin = V._supabase_admin()
    existing = (
        admin.table("question_reports")
        .select("id, status")
        .eq("question_id", question_id)
        .eq("reported_by", user_id)
        .eq("status", "pending")
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return JsonResponse({"ok": True, "already_reported": True, "report_id": existing[0]["id"]})
    row = (
        admin.table("question_reports")
        .insert({"question_id": question_id, "reported_by": user_id, "reason": reason, "notes": notes, "status": "pending"})
        .execute()
        .data
        or []
    )
    return JsonResponse({"ok": True, "report": _json_safe(row[0] if row else None)})


@csrf_exempt
@require_GET
def mobile_attempt_review(request, test_type, attempt_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    admin = V._supabase_admin()
    t = (test_type or "").strip().lower()
    payload = {"ok": True, "test_type": t, "attempt_id": str(attempt_id), "header": {}, "items": []}
    if t == "mock":
        attempts = admin.table("mock_attempts").select("*").eq("id", str(attempt_id)).eq("student_id", user_id).limit(1).execute().data or []
        if not attempts:
            return JsonResponse({"ok": False, "error": "not_found"}, status=404)
        links = admin.table("mock_attempt_answers").select("selected_option, is_correct, is_bookmarked, is_flagged, question_bank(question_text, options, correct_option, explanation)").eq("attempt_id", str(attempt_id)).execute().data or []
        payload["header"] = attempts[0]
        payload["items"] = links
    elif t == "general-test":
        attempts = admin.table("general_test_attempts").select("*").eq("id", str(attempt_id)).eq("student_id", user_id).limit(1).execute().data or []
        if not attempts:
            return JsonResponse({"ok": False, "error": "not_found"}, status=404)
        links = admin.table("general_test_attempt_answers").select("selected_option, is_correct, is_bookmarked, is_flagged, question_bank(question_text, options, correct_option, explanation)").eq("attempt_id", str(attempt_id)).execute().data or []
        payload["header"] = attempts[0]
        payload["items"] = links
    elif t == "quiz":
        attempts = admin.table("practice_quiz_attempts").select("*").eq("id", str(attempt_id)).eq("student_id", user_id).limit(1).execute().data or []
        if not attempts:
            return JsonResponse({"ok": False, "error": "not_found"}, status=404)
        links = admin.table("practice_quiz_attempt_answers").select("selected_option, is_correct, question_bank:practice_quiz_questions(question_text, option_a, option_b, option_c, correct_option, explanation)").eq("attempt_id", str(attempt_id)).execute().data or []
        payload["header"] = attempts[0]
        payload["items"] = links
    else:
        return JsonResponse({"ok": False, "error": "invalid_test_type"}, status=400)
    return JsonResponse(_json_safe(payload))


@csrf_exempt
@require_GET
def mobile_community_post_detail(request, post_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    db = V._supabase_admin()
    posts = db.table("community_posts").select("*").eq("id", str(post_id)).limit(1).execute().data or []
    if not posts:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    comments = db.table("community_comments").select("*").eq("post_id", str(post_id)).order("created_at", desc=False).execute().data or []
    return JsonResponse(_json_safe({"ok": True, "post": posts[0], "comments": comments}))


@csrf_exempt
@require_POST
def mobile_community_report_post(request, post_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    body = _parse_json_body(request)
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return JsonResponse({"ok": False, "error": "reason_required"}, status=400)
    row = (
        V._supabase_admin().table("post_reports").insert({"post_id": str(post_id), "reported_by": user_id, "reason": reason, "status": "pending"}).execute().data
        or []
    )
    return JsonResponse({"ok": True, "report": _json_safe(row[0] if row else None)})


@csrf_exempt
@require_POST
def mobile_community_notifications_read(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    sub_err = _subscription_block(user_id, profile)
    if sub_err:
        return sub_err
    V._supabase_admin().table("community_notifications").update({"is_read": True}).eq("user_id", user_id).eq("is_read", False).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_subscription_plans(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    plans = V._get_plans()
    sub = V._get_active_subscription(user_id)
    return JsonResponse({"ok": True, "plans": _json_safe(plans), "current_subscription": _json_safe(sub)})


@csrf_exempt
@require_GET
def mobile_payment_page(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id) or {}
    sub = V._get_active_subscription(user_id)
    return JsonResponse({"ok": True, "profile": _json_safe(profile), "subscription": _json_safe(sub), "plans": _json_safe(V._get_plans())})


@csrf_exempt
@require_GET
def mobile_admin_locked_accounts(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    rows = V._supabase_admin().table("profiles").select("*").eq("role", "student").eq("is_active", False).order("created_at", desc=True).execute().data or []
    return JsonResponse({"ok": True, "locked_accounts": _json_safe(rows)})


@csrf_exempt
@require_GET
def mobile_admin_messages(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    rows = V._supabase_admin().table("contact_messages").select("*").order("created_at", desc=True).limit(300).execute().data or []
    return JsonResponse({"ok": True, "messages": _json_safe(rows)})


@csrf_exempt
@require_POST
def mobile_admin_message_mark_read(request, message_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    V._supabase_admin().table("contact_messages").update({"is_read": True}).eq("id", str(message_id)).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_admin_manage_questions(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    q = request.GET.get("q", "").strip()
    programme = request.GET.get("programme", "").strip()
    page = max(1, int(request.GET.get("page", "1") or "1"))
    page_size = min(100, max(10, int(request.GET.get("page_size", "30") or "30")))
    query = V._supabase_admin().table("question_bank").select("*").order("created_at", desc=True)
    if programme:
        query = query.eq("programme", programme)
    if q:
        query = query.ilike("question_text", f"%{q}%")
    rows = query.range((page - 1) * page_size, (page * page_size) - 1).execute().data or []
    return JsonResponse({"ok": True, "items": _json_safe(rows), "page": page, "page_size": page_size})


@csrf_exempt
@require_POST
def mobile_admin_question_upsert(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    body = _parse_json_body(request)
    qid = str(body.get("id") or "").strip()
    payload = {
        "programme": body.get("programme"),
        "paper_title": body.get("paper_title"),
        "question_text": body.get("question_text"),
        "options": body.get("options") or {},
        "correct_option": body.get("correct_option"),
        "explanation": body.get("explanation") or "",
    }
    admin = V._supabase_admin()
    if qid:
        row = admin.table("question_bank").update(payload).eq("id", qid).execute().data or []
    else:
        row = admin.table("question_bank").insert(payload).execute().data or []
    return JsonResponse({"ok": True, "item": _json_safe(row[0] if row else None)})


@csrf_exempt
@require_POST
def mobile_admin_question_delete(request, question_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    V._supabase_admin().table("question_bank").delete().eq("id", str(question_id)).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_admin_payments(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    subs = V._supabase_admin().table("subscriptions").select("*").order("created_at", desc=True).limit(500).execute().data or []
    plans = V._supabase_admin().table("subscription_plans").select("*").eq("is_active", True).order("sort_order").execute().data or []
    return JsonResponse({"ok": True, "subscriptions": _json_safe(subs), "plans": _json_safe(plans)})


@csrf_exempt
@require_POST
def mobile_admin_payment_update(request, subscription_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    body = _parse_json_body(request)
    status = str(body.get("status") or "").strip()
    if status not in ("active", "pending_payment", "cancelled", "expired"):
        return JsonResponse({"ok": False, "error": "invalid_status"}, status=400)
    update = {"status": status}
    if status == "active":
        update["activated_at"] = datetime.now(dt_timezone.utc).isoformat()
        update["activated_by"] = user_id
    V._supabase_admin().table("subscriptions").update(update).eq("id", str(subscription_id)).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_admin_community(request):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    db = V._supabase_admin()
    communities = db.table("communities").select("*").order("name").execute().data or []
    posts = db.table("community_posts").select("*").order("created_at", desc=True).limit(100).execute().data or []
    reports = db.table("post_reports").select("*").order("created_at", desc=True).limit(100).execute().data or []
    warnings = db.table("community_warnings").select("*").order("created_at", desc=True).limit(100).execute().data or []
    return JsonResponse(_json_safe({"ok": True, "communities": communities, "posts": posts, "reports": reports, "warnings": warnings}))


@csrf_exempt
@require_POST
def mobile_admin_community_resolve_report(request, report_id):
    user_id, err = _user_id_from_jwt(request)
    if err:
        return err
    profile = _profile_row(user_id)
    if not _is_admin_profile(profile):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    V._supabase_admin().table("post_reports").update({"status": "resolved", "resolved_by": user_id, "resolved_at": datetime.now(dt_timezone.utc).isoformat()}).eq("id", str(report_id)).execute()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def mobile_meta(request):
    """Programme / year choices for academic profile forms."""
    _, err = _user_id_from_jwt(request)
    if err:
        return err
    return JsonResponse(
        {
            "ok": True,
            "programmes": V.PROGRAMME_CHOICES,
            "years": [{"code": c[0], "label": c[1]} for c in V.ACADEMIC_YEAR_CHOICES],
        }
    )
