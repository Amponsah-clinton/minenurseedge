import json
import logging
import math
import random
import re
import secrets
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.utils.html import escape
from django.views.decorators.http import require_POST

from supabase import create_client
from supabase.lib.client_options import SyncClientOptions
from supabase_auth import SyncSupportedStorage

from website.mnemonic_anatomy import ANATOMY_MNEMONICS_SECTION
from website.site_maintenance import get_active_site_maintenance, invalidate_site_maintenance_cache

EMPTY_QUESTION_FORM = {
    "programme": "",
    "paper_title": "",
    "question_text": "",
    "option_a": "",
    "option_b": "",
    "option_c": "",
    "correct_option": "",
    "explanation": "",
    "json_payload": "",
}

NCLEX_QUESTION_TYPES = {
    "mcq": "Multiple Choice (Single Best Answer)",
    "sata": "Select All That Apply (SATA)",
    "fill_blank": "Fill in the Blank",
    "ordered_response": "Ordered Response (Drag and Drop)",
}

NCLEX_JSON_SAMPLE = json.dumps(
    {
        "questions": [
            {
                "question_type": "mcq",
                "question_text": "What is the nurse's priority action for a patient with shortness of breath?",
                "options": [
                    "Administer oxygen as prescribed",
                    "Assess airway patency and breathing effort",
                    "Provide oral fluids",
                    "Prepare discharge paperwork",
                ],
                "correct_answers": ["Assess airway patency and breathing effort"],
                "rationale": "Apply ABC priority. Airway and breathing assessment comes first.",
                "difficulty": "medium",
            },
            {
                "question_type": "sata",
                "question_text": "Which findings indicate hypoglycemia? Select all that apply.",
                "options": [
                    "Sweating",
                    "Tremors",
                    "Bradycardia",
                    "Confusion",
                ],
                "correct_answers": ["Sweating", "Tremors", "Confusion"],
                "rationale": "Classic hypoglycemia signs include sweating, tremors, and confusion.",
                "difficulty": "easy",
            },
            {
                "question_type": "fill_blank",
                "question_text": "Calculate the IV flow rate: 120 mL over 2 hours = ____ mL/hr.",
                "correct_answers": ["60"],
                "rationale": "120 / 2 = 60 mL/hr.",
                "difficulty": "easy",
            },
            {
                "question_type": "ordered_response",
                "question_text": "Put the basic CPR actions in order for an unresponsive adult.",
                "options": [
                    "Call for help and activate emergency response",
                    "Check pulse and breathing quickly",
                    "Start chest compressions",
                    "Provide rescue breaths when indicated",
                ],
                "correct_answers": [
                    "Call for help and activate emergency response",
                    "Check pulse and breathing quickly",
                    "Start chest compressions",
                    "Provide rescue breaths when indicated",
                ],
                "rationale": "Correct sequence supports rapid, structured resuscitation.",
                "difficulty": "medium",
            },
        ]
    },
    ensure_ascii=False,
    indent=2,
)



# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supabase():
    """Anon/public key – used for auth sign-in."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


def _supabase_admin():
    """Service-role key – bypasses RLS, used for all DB writes."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


class _DjangoOAuthStorage(SyncSupportedStorage):
    """
    Persists Supabase GoTrue PKCE state in the Django session so OAuth works
    across the redirect to Google and back (in-memory Supabase clients cannot).
    """

    SESSION_BUCKET = "go_oauth_storage"

    def __init__(self, django_session):
        self._session = django_session

    def _bucket(self):
        return self._session.setdefault(self.SESSION_BUCKET, {})

    def get_item(self, key: str):
        return self._bucket().get(key)

    def set_item(self, key: str, value: str) -> None:
        b = self._bucket()
        b[key] = value
        self._session[self.SESSION_BUCKET] = b
        self._session.modified = True

    def remove_item(self, key: str) -> None:
        b = self._bucket()
        b.pop(key, None)
        self._session[self.SESSION_BUCKET] = b
        self._session.modified = True


def _supabase_oauth_client(request):
    """Anon Supabase client with PKCE verifier stored in the Django session."""
    storage = _DjangoOAuthStorage(request.session)
    opts = SyncClientOptions(
        storage=storage,
        flow_type="pkce",
        persist_session=False,
        auto_refresh_token=False,
    )
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY, options=opts)


def _public_site_origin(request):
    base = (getattr(settings, "PUBLIC_SITE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return request.build_absolute_uri("/").rstrip("/")


def _google_oauth_callback_url(request):
    return f"{_public_site_origin(request)}/auth/google/callback/"


def _filter_duplicate_questions(supabase_client, rows):
    """
    Remove rows that already exist in question_bank.

    Duplicate = same (programme, paper_title, question_text), compared
    case-insensitively after stripping whitespace.

    Also de-duplicates within the batch itself so the same question
    cannot appear twice in a single JSON upload.

    Returns (unique_rows, skipped_texts) where skipped_texts is a list
    of (question_text_snippet, reason) pairs for reporting.
    """
    from collections import defaultdict

    if not rows:
        return [], []

    # Group rows by (programme, paper_title) to minimise DB round-trips.
    groups = defaultdict(list)
    for row in rows:
        key = (row["programme"], row["paper_title"])
        groups[key].append(row)

    unique_rows = []
    skipped_texts = []

    for (programme, paper_title), group_rows in groups.items():
        # Fetch every existing question_text for this paper + programme.
        result = (
            supabase_client
            .table("question_bank")
            .select("question_text")
            .eq("programme", programme)
            .eq("paper_title", paper_title)
            .execute()
        )
        existing_texts = {
            " ".join((r.get("question_text") or "").split()).casefold()
            for r in (result.data or [])
        }

        for row in group_rows:
            key_text = " ".join((row.get("question_text") or "").split()).casefold()
            snippet = row["question_text"][:80]

            if key_text in existing_texts:
                skipped_texts.append((snippet, "already exists in the database"))
            else:
                unique_rows.append(row)
                # Prevent the same text from being inserted twice within
                # the same batch (e.g. duplicate entries in JSON upload).
                existing_texts.add(key_text)

    return unique_rows, skipped_texts


def _normalize_nclex_text(value):
    return " ".join(str(value or "").split()).casefold()


def _filter_duplicate_nclex_questions(supabase_client, rows, exclude_ids=None):
    """
    De-duplicate NCLEX rows by (question_type + normalized question_text).
    Checks both:
    - Existing rows in Supabase
    - Duplicate entries inside the incoming batch
    """
    if not rows:
        return [], []

    exclude_ids = {str(v) for v in (exclude_ids or []) if v}
    unique_rows = []
    skipped = []

    # Gather existing keys from DB by question_type in chunks to avoid row caps.
    existing_keys = set()
    type_groups = {}
    for row in rows:
        qtype = str(row.get("question_type") or "").strip().lower()
        type_groups[qtype] = True

    for qtype in type_groups.keys():
        offset = 0
        chunk_size = 1000
        while True:
            resp = (
                supabase_client
                .table("nclex_questions")
                .select("id, question_type, question_text")
                .eq("question_type", qtype)
                .range(offset, offset + chunk_size - 1)
                .execute()
            )
            data = resp.data or []
            if not data:
                break
            for item in data:
                row_id = str(item.get("id") or "")
                if row_id in exclude_ids:
                    continue
                key = (
                    str(item.get("question_type") or "").strip().lower(),
                    _normalize_nclex_text(item.get("question_text")),
                )
                existing_keys.add(key)
            if len(data) < chunk_size:
                break
            offset += chunk_size

    seen_in_batch = set()
    for row in rows:
        key = (
            str(row.get("question_type") or "").strip().lower(),
            _normalize_nclex_text(row.get("question_text")),
        )
        snippet = str(row.get("question_text") or "")[:80]
        if key in seen_in_batch:
            skipped.append((snippet, "duplicate in the uploaded batch"))
            continue
        if key in existing_keys:
            skipped.append((snippet, "already exists in NCLEX bank"))
            continue
        seen_in_batch.add(key)
        unique_rows.append(row)

    return unique_rows, skipped


def _build_mock_leaderboard(supabase_client, exam_id, current_user_id, top_n=10):
    """
    Build a ranked leaderboard for a mock exam.

    Returns (leaderboard_rows, my_rank, my_row):
      - leaderboard_rows: top_n entries as dicts with rank/student_name/percentage/is_me
      - my_rank: int rank of current_user_id (None if not on board)
      - my_row: the student's own row dict if outside top_n, else None
    """
    try:
        all_attempts = (
            supabase_client
            .table("mock_attempts")
            .select("student_id, percentage, submitted_at")
            .eq("mock_exam_id", exam_id)
            .not_.is_("submitted_at", "null")
            .order("percentage", desc=True)
            .order("submitted_at", desc=False)
            .execute()
            .data
            or []
        )
    except Exception:
        return [], None, None

    # Keep only each student's best score.
    best_by_student = {}
    for row in all_attempts:
        sid = row.get("student_id")
        pct = float(row.get("percentage") or 0)
        if sid not in best_by_student or pct > float(best_by_student[sid].get("percentage") or 0):
            best_by_student[sid] = row

    ranking = sorted(
        best_by_student.values(),
        key=lambda r: float(r.get("percentage") or 0),
        reverse=True,
    )

    # Fetch display names for everyone in the top_n + current user.
    top_entries = ranking[:top_n]
    ids_needed = {r.get("student_id") for r in top_entries if r.get("student_id")}
    if current_user_id:
        ids_needed.add(current_user_id)
    names_map = {}
    if ids_needed:
        try:
            profiles = (
                supabase_client
                .table("profiles")
                .select("id, full_name")
                .in_("id", list(ids_needed))
                .execute()
                .data
                or []
            )
            names_map = {p["id"]: (p.get("full_name") or "Student") for p in profiles}
        except Exception:
            pass

    my_rank = None
    for idx, row in enumerate(ranking, start=1):
        if row.get("student_id") == current_user_id:
            my_rank = idx
            break

    leaderboard_rows = [
        {
            "rank": idx,
            "student_name": names_map.get(row.get("student_id"), "Student"),
            "percentage": float(row.get("percentage") or 0),
            "is_me": row.get("student_id") == current_user_id,
        }
        for idx, row in enumerate(top_entries, start=1)
    ]

    # If the current user is outside the top_n, build a separate row for them.
    my_row = None
    if my_rank is not None and my_rank > top_n:
        user_entry = best_by_student.get(current_user_id)
        if user_entry:
            my_row = {
                "rank": my_rank,
                "student_name": names_map.get(current_user_id, "Student"),
                "percentage": float(user_entry.get("percentage") or 0),
                "is_me": True,
            }

    return leaderboard_rows, my_rank, my_row


def _escape_like_pattern_exact(value: str) -> str:
    """Escape %, _, \\ so ILIKE treats the string as a literal match (for emails)."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _profile_by_email_flexible(admin_client, email: str):
    """
    Resolve a profile row by email: exact lowercase match first, then case-insensitive ILIKE.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        rows = (
            admin_client.table("profiles")
            .select("id, full_name, email, is_active")
            .eq("email", email)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            return rows[0]
    except Exception:
        pass
    try:
        rows = (
            admin_client.table("profiles")
            .select("id, full_name, email, is_active")
            .ilike("email", _escape_like_pattern_exact(email))
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            return rows[0]
    except Exception:
        pass
    return None


def _find_auth_user_by_email(email):
    """
    Find a Supabase Auth user by email (case-insensitive).
    Uses the official client's admin list_users — raw REST ?filter=... is unreliable.
    Returns a dict with id, email, user_metadata for compatibility with callers.
    """
    email_l = (email or "").strip().lower()
    if not email_l:
        return None
    try:
        admin = _supabase_admin()
        page = 1
        max_pages = 50  # up to 10k users
        while page <= max_pages:
            users = admin.auth.admin.list_users(page=page, per_page=200)
            if not users:
                break
            for u in users:
                ue = (getattr(u, "email", None) or "").strip().lower()
                if ue == email_l:
                    if hasattr(u, "model_dump"):
                        data = u.model_dump()
                    elif hasattr(u, "dict"):
                        data = u.dict()
                    else:
                        data = {
                            "id": getattr(u, "id", ""),
                            "email": getattr(u, "email", ""),
                            "user_metadata": getattr(u, "user_metadata", None) or {},
                        }
                    uid = data.get("id")
                    return {
                        "id": str(uid) if uid is not None else "",
                        "email": data.get("email") or email_l,
                        "user_metadata": data.get("user_metadata") or {},
                    }
            if len(users) < 200:
                break
            page += 1
    except Exception:
        if settings.DEBUG:
            logging.getLogger(__name__).exception("Auth user lookup by email failed")
    return None


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

def _validate_password(password):
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"\d", password):
        errors.append("Password must contain at least one number.")
    if not re.search(r"[!@#$%^&*()\-_=+\[\]{};:'\",.<>?/\\|`~]", password):
        errors.append("Password must contain at least one special character.")
    return errors


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _create_session(request, user_id, email, full_name, role):
    """Delete any existing session for this user, create a new one, store in Django session."""
    session_token = secrets.token_hex(32)
    admin = _supabase_admin()

    # Single-session enforcement: remove any previous session for this user
    admin.table("active_sessions").delete().eq("user_id", user_id).execute()

    # Insert new session row
    admin.table("active_sessions").insert({
        "user_id": user_id,
        "session_token": session_token,
        "ip_address": request.META.get("REMOTE_ADDR", "")[:45],
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
    }).execute()

    # Store in Django session
    request.session.flush()
    request.session["user_id"] = user_id
    request.session["session_token"] = session_token
    request.session["email"] = email
    request.session["full_name"] = full_name
    request.session["role"] = role


# ── Plan-based feature access control ────────────────────────────────────────
# Features that require a Premium subscription. Basic users are redirected
# to /dashboard/upgrade/ when they try to reach any of these.
_PREMIUM_ONLY_FEATURES = frozenset({
    "lecture_notes",
    "flashcards",
    "community",
    "performance",
    "quizzes",
    "ai_assistant",
})

_FEATURE_LABELS = {
    "lecture_notes": ("Lecture Notes", "Access all lecture notes organised by year and topic."),
    "flashcards":    ("Flashcards & Spaced Repetition", "Review key concepts with daily flashcards and spaced repetition."),
    "community":     ("Community Forums", "Join study groups and discuss exam topics with peers."),
    "performance":   ("Detailed Performance Analytics", "Track your progress across every exam type with rich charts and insights."),
    "quizzes":       ("Competitive Quizzes", "Practice with timed competitive quizzes and compare your score with others."),
    "ai_assistant":  ("AI Study Assistant", "Get instant AI-powered answers to your nursing exam questions."),
}


def _is_premium(request):
    """
    Legacy name kept for compatibility.
    Product policy: any subscribed student has full feature access.
    """
    if request.session.get("role") == "admin":
        return True
    if request.session.get("user_id"):
        return True
    return False


def _plan_gate(request, feature):
    """
    Return an HttpResponseRedirect to the upgrade page if `feature` is
    premium-only and the current user is on the Basic plan.
    Returns None if access should be allowed.
    """
    if feature not in _PREMIUM_ONLY_FEATURES or _is_premium(request):
        return None
    return redirect(f"/dashboard/upgrade/?feature={feature}")
# ─────────────────────────────────────────────────────────────────────────────


def _destroy_session(request):
    user_id = request.session.get("user_id")
    if user_id:
        try:
            _supabase_admin().table("active_sessions").delete().eq("user_id", user_id).execute()
        except Exception:
            pass
    request.session.flush()


def _require_login(request):
    """Returns None if authenticated; otherwise returns a redirect response."""
    if not request.session.get("user_id"):
        return redirect("/login/")
    return None


# Supabase: add once so acknowledgements persist across devices —
#   alter table public.profiles add column if not exists dashboard_nmc_disclaimer_ack_at timestamptz;
def _student_needs_dashboard_nmc_disclaimer(request, user_id):
    """True until the student acknowledges the independent-platform disclaimer once."""
    try:
        admin = _supabase_admin()
        rows = (
            admin.table("profiles")
            .select("dashboard_nmc_disclaimer_ack_at")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("dashboard_nmc_disclaimer_ack_at"):
            return False
        if rows:
            return True
    except Exception:
        pass
    return bool(request.session.get("pending_dashboard_disclaimer"))


def _require_admin(request):
    """Returns None if admin; otherwise redirects appropriately."""
    check = _require_login(request)
    if check:
        return check
    if request.session.get("role") != "admin":
        return redirect("/dashboard/")
    return None


def _student_unread_count(user_id):
    try:
        resp = (
            _supabase_admin()
            .table("student_notifications")
            .select("id", count="exact", head=True)
            .eq("student_id", user_id)
            .eq("is_read", False)
            .execute()
        )
        return resp.count or 0
    except Exception:
        return 0


PROGRAMME_PAPERS = {
    "Registered General Nursing (RGN)": [
        "Medicine and Medical Nursing",
        "Surgery, Gynaecology, Urology & Orthopaedics",
        "General Paper",
    ],
    "Registered Midwifery (RM)": [
        "Paediatrics, Obstetric Anatomy & High-Risk Neonates",
        "Midwifery",
        "General Paper",
    ],
    "Nurse Assistant Clinical (NAC/NAP)": [
        "Basic Clinical Nursing",
        "Basic Practical & Preventive Nursing",
        "General Paper",
    ],
    "Registered Mental Health Nursing (RMHN)": [
        "Principles and Practice of Psychiatric Nursing",
        "Psychiatry, Psychopathology & Psychopharmacology",
        "General Paper",
    ],
    "Registered Public Health Nursing (RPHN)": [
        "Principles of Public Health Nursing",
        "Principles of Disease Management and Control",
        "General Paper",
    ],
}

PROGRAMME_NAMES = list(PROGRAMME_PAPERS.keys())
ALL_PAPERS = sorted({paper for papers in PROGRAMME_PAPERS.values() for paper in papers})


def _question_bank_counts_by_paper(admin_client, programme):
    """
    Exact row counts per curriculum paper for a programme (count=head per paper).
    Listing all question rows and grouping in Python hits PostgREST row caps (~1000),
    so later papers alphabetically (e.g. Surgery after Medicine) can disappear.
    """
    grouped = {}
    for paper in PROGRAMME_PAPERS.get(programme) or []:
        try:
            resp = (
                admin_client.table("question_bank")
                .select("id", count="exact", head=True)
                .eq("programme", programme)
                .eq("paper_title", paper)
                .execute()
            )
            grouped[paper] = int(resp.count or 0)
        except Exception:
            grouped[paper] = 0
    return grouped


def _question_bank_counts_by_paper_chunked(admin_client, programme):
    """Fallback when programme is not in PROGRAMME_PAPERS: aggregate with range chunks."""
    grouped = {}
    offset = 0
    chunk = 1000
    while True:
        try:
            rows = (
                admin_client.table("question_bank")
                .select("paper_title")
                .eq("programme", programme)
                .range(offset, offset + chunk - 1)
                .execute()
                .data
                or []
            )
        except Exception:
            break
        for row in rows:
            paper = (row.get("paper_title") or "Untitled").strip()
            grouped[paper] = grouped.get(paper, 0) + 1
        if len(rows) < chunk:
            break
        offset += chunk
    return grouped


GENERAL_PAPER_TITLE = "General Paper"


def _is_general_paper(paper_title):
    """True when this is the shared General Paper (all programmes); programme may be omitted."""
    return (paper_title or "").strip().casefold() == GENERAL_PAPER_TITLE.casefold()


def _general_paper_row_ids_for_same_question(admin_client, question_text):
    """IDs of all programme copies of one General Paper question (same question_text)."""
    qt = (question_text or "").strip()
    if not qt:
        return []
    rows = (
        admin_client.table("question_bank")
        .select("id, paper_title")
        .eq("question_text", qt)
        .execute()
        .data
        or []
    )
    return [r["id"] for r in rows if _is_general_paper(r.get("paper_title"))]


MANAGE_QUESTIONS_PER_PAGE = 25
MANAGE_QUESTIONS_MAX_FETCH = 8000


def _normalized_question_text(value):
    return " ".join((value or "").split()).casefold()


def _has_duplicate_question(admin_client, programme, paper_title, question_text, exclude_ids=None):
    """
    True when a logically-identical question already exists in the target programme+paper.
    Logical identity is case-insensitive and whitespace-normalized question text.
    """
    norm_text = _normalized_question_text(question_text)
    if not norm_text:
        return False
    excluded = {str(i) for i in (exclude_ids or []) if i}
    rows = (
        admin_client.table("question_bank")
        .select("id, question_text")
        .eq("programme", programme)
        .eq("paper_title", paper_title)
        .execute()
        .data
        or []
    )
    for row in rows:
        rid = str(row.get("id") or "")
        if rid and rid in excluded:
            continue
        if _normalized_question_text(row.get("question_text")) == norm_text:
            return True
    return False


def _dedupe_general_paper_rows_for_admin_list(rows):
    """Collapse duplicates in admin list; keep one row per logical question."""
    seen_exact = set()
    seen_general = set()
    out = []
    for r in rows:
        r = dict(r)
        r["general_paper_grouped"] = False
        paper_key = (r.get("paper_title") or "").strip().casefold()
        prog_key = (r.get("programme") or "").strip().casefold()
        text_key = _normalized_question_text(r.get("question_text"))
        if not text_key:
            continue
        if _is_general_paper(r.get("paper_title")):
            # General Paper is shared; display only one row across all programmes.
            key = (paper_key, text_key)
            if key in seen_general:
                continue
            seen_general.add(key)
            r["programme"] = "All programmes"
            r["general_paper_grouped"] = True
            seen_exact.add(("all programmes", paper_key, text_key))
        else:
            key = (prog_key, paper_key, text_key)
            if key in seen_exact:
                continue
            seen_exact.add(key)
        out.append(r)
    return out


def _programmes_for_paper(programme, paper_title):
    """General Paper should be available for all programmes."""
    if _is_general_paper(paper_title):
        return PROGRAMME_NAMES
    return [programme]


def _normalize_question_payload(item):
    if not isinstance(item, dict):
        raise ValueError("Each JSON item must be an object.")

    programme = (item.get("programme") or "").strip()
    paper_title = (item.get("paper_title") or "").strip()
    question_text = (item.get("question") or item.get("question_text") or "").strip()
    explanation = (item.get("explanation") or "").strip()
    correct_option = (item.get("correct_option") or "").strip().upper()

    options = item.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("The 'options' field must be an object with A/B/C keys.")

    cleaned_options = {}
    for key, value in options.items():
        clean_key = str(key).strip().upper()
        if clean_key in {"A", "B", "C"} and str(value).strip():
            cleaned_options[clean_key] = str(value).strip()

    if not paper_title:
        raise ValueError("Paper title is required.")
    if _is_general_paper(paper_title):
        paper_title = GENERAL_PAPER_TITLE
        if programme and programme not in PROGRAMME_PAPERS:
            raise ValueError(f"Invalid programme: {programme}")
    else:
        if not programme:
            raise ValueError("Programme is required for non-General papers.")
        if programme not in PROGRAMME_PAPERS:
            raise ValueError(f"Invalid programme: {programme}")
        if paper_title not in PROGRAMME_PAPERS[programme]:
            raise ValueError(f"Paper title '{paper_title}' does not belong to programme '{programme}'.")
    if not question_text:
        raise ValueError("Question text is required.")
    if len(cleaned_options) < 2:
        raise ValueError("At least 2 options are required.")
    if correct_option not in cleaned_options:
        raise ValueError("Correct option must match one of the provided options.")

    return {
        "programme": programme,
        "paper_title": paper_title,
        "question_text": question_text,
        "options": cleaned_options,
        "correct_option": correct_option,
        "explanation": explanation,
    }


MOCK_QUESTION_BATCH_SIZE = 60
MOCK_DURATION_MINUTES = 30
GLOBAL_MOCK_PROGRAMME = "All Programmes"
# Mock exams: this many questions are drawn at random from General Paper (student's programme);
# the random pool is at most this many GP questions before picking.
MOCK_GENERAL_PAPER_QUESTIONS = 15
MOCK_GENERAL_PAPER_POOL_CAP = 120
# Admin-created free accounts: subscriptions row uses this reference; also used when profiles.is_free_access is missing.
FREE_ACCESS_PAYMENT_REFERENCE = "free_access"
GENERAL_TEST_QUESTION_BATCH_SIZE = MOCK_QUESTION_BATCH_SIZE

# PostgREST / Supabase often caps each response (~1000 rows). Use range() chunks.
POSTGREST_LIST_CHUNK = 1000


def _collect_general_paper_ids_for_mock_pool(admin_client, programme, frame_cap):
    """
    Collect General Paper question ids for a programme. If more than frame_cap exist,
    return a random subset of size frame_cap (sampling frame for mock picks).
    """
    programme = (programme or "").strip()
    if not programme or frame_cap < 1:
        return []
    ids = []
    offset = 0
    chunk = POSTGREST_LIST_CHUNK
    hard_cap = 8000
    while len(ids) < hard_cap:
        rows = (
            admin_client.table("question_bank")
            .select("id")
            .eq("programme", programme)
            .eq("paper_title", GENERAL_PAPER_TITLE)
            .order("created_at", desc=False)
            .range(offset, offset + chunk - 1)
            .execute()
            .data
            or []
        )
        for r in rows:
            qid = r.get("id")
            if qid is not None:
                ids.append(qid)
        if len(rows) < chunk:
            break
        offset += chunk
    if not ids:
        return []
    if len(ids) > frame_cap:
        return random.sample(ids, frame_cap)
    return ids


def _build_mock_exam_question_pool(admin_client, *, student_programme, needed, mock_number):
    """
    Build the list of question_bank ids for a mock attempt:
    - Up to MOCK_GENERAL_PAPER_QUESTIONS questions randomly chosen from at most
      MOCK_GENERAL_PAPER_POOL_CAP General Paper questions (student's programme).
    - Remainder from the global mock pool (GLOBAL_MOCK_PROGRAMME) using the usual
      per-mock slice: mock N uses the N-th block of MOCK_QUESTION_BATCH_SIZE rows.
    Returns a list of length `needed` or None if the mock slice is short on questions.
    """
    needed = int(needed)
    mock_number = max(1, int(mock_number or 1))
    if needed < 1:
        return None

    block = MOCK_QUESTION_BATCH_SIZE
    gp_want = min(MOCK_GENERAL_PAPER_QUESTIONS, needed)
    gp_frame = _collect_general_paper_ids_for_mock_pool(
        admin_client, student_programme, MOCK_GENERAL_PAPER_POOL_CAP
    )
    gp_n = min(gp_want, len(gp_frame))
    gp_chosen = random.sample(gp_frame, gp_n) if gp_n > 0 else []

    mock_sub = needed - len(gp_chosen)
    range_start = (mock_number - 1) * block

    mock_part = []
    if mock_sub > 0:
        resp = (
            admin_client.table("question_bank")
            .select("id")
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .order("created_at", desc=False)
            .range(range_start, range_start + mock_sub - 1)
            .execute()
            .data
            or []
        )
        mock_part = [r["id"] for r in resp if r.get("id")]
        if len(mock_part) < mock_sub:
            return None

    out = gp_chosen + mock_part
    random.shuffle(out)
    return out


def _mq_fetch_question_rows(
    admin, escaped, filter_prog, filter_paper, max_rows, start_offset=0
):
    """
    Fetch up to max_rows question_bank rows for the manage-questions list, newest first.
    Uses repeated range() windows so we are not limited to a single response cap.
    start_offset skips the first N rows of the ordered query (for backfill after a split fetch).
    """
    collected = []
    offset = start_offset
    while len(collected) < max_rows:
        q = admin.table("question_bank").select(
            "id, programme, paper_title, question_text, correct_option, created_at"
        ).order("created_at", desc=True)
        if escaped:
            filt = (
                f"question_text.ilike.%{escaped}%,programme.ilike.%{escaped}%,"
                f"paper_title.ilike.%{escaped}%"
            )
            q = q.or_(filt)
        if filter_prog == "__mock__":
            q = q.eq("programme", GLOBAL_MOCK_PROGRAMME)
        elif filter_prog == "__non_mock__":
            q = q.neq("programme", GLOBAL_MOCK_PROGRAMME)
        elif filter_prog:
            q = q.eq("programme", filter_prog)
        if filter_paper:
            q = q.eq("paper_title", filter_paper)
        end = offset + POSTGREST_LIST_CHUNK - 1
        chunk = q.range(offset, end).execute().data or []
        collected.extend(chunk)
        if len(chunk) < POSTGREST_LIST_CHUNK:
            break
        offset += POSTGREST_LIST_CHUNK
    return collected[:max_rows]


def _mq_merge_mock_nonmock_rows(admin, escaped, filter_paper, max_rows):
    """
    For the default 'all programmes' list (no search): take up to half of max_rows
    from non-mock and half from mock, then sort by created_at. Stops mock-only
    uploads from crowding out general-test rows in the newest-N window.

    If one partition has fewer than half the rows, backfill from the other stream
    (continuing past its first chunk) so we still load up to max_rows when possible.
    """
    half = max(1, max_rows // 2)
    nm1 = _mq_fetch_question_rows(admin, escaped, "__non_mock__", filter_paper, half)
    mk1 = _mq_fetch_question_rows(admin, escaped, "__mock__", filter_paper, half)
    nm_all = list(nm1)
    mk_all = list(mk1)

    def _sorted_merge(rows_nm, rows_mk):
        by_id = {}
        for r in rows_nm + rows_mk:
            rid = r.get("id")
            if rid is not None:
                by_id[rid] = r
        return sorted(
            by_id.values(),
            key=lambda r: str(r.get("created_at") or ""),
            reverse=True,
        )

    merged = _sorted_merge(nm_all, mk_all)
    if len(merged) >= max_rows:
        return merged[:max_rows]

    surplus = max_rows - len(merged)
    if len(nm1) >= half and surplus > 0:
        nm_all.extend(
            _mq_fetch_question_rows(
                admin,
                escaped,
                "__non_mock__",
                filter_paper,
                surplus,
                start_offset=len(nm1),
            )
        )
        merged = _sorted_merge(nm_all, mk_all)
    if len(merged) >= max_rows:
        return merged[:max_rows]

    surplus = max_rows - len(merged)
    if len(mk1) >= half and surplus > 0:
        mk_all.extend(
            _mq_fetch_question_rows(
                admin,
                escaped,
                "__mock__",
                filter_paper,
                surplus,
                start_offset=len(mk1),
            )
        )
        merged = _sorted_merge(nm_all, mk_all)
    return merged[:max_rows]


def _mq_question_bank_stats(admin):
    """
    Bank-wide stats using count=head (exact) and chunked reads where needed.
    Avoids the single-request row cap that made dashboard numbers wrong.
    """
    db_total = (
        admin.table("question_bank")
        .select("id", count="exact", head=True)
        .execute()
        .count
        or 0
    )
    mock_count = (
        admin.table("question_bank")
        .select("id", count="exact", head=True)
        .eq("programme", GLOBAL_MOCK_PROGRAMME)
        .execute()
        .count
        or 0
    )
    non_mock_rows = (
        admin.table("question_bank")
        .select("id", count="exact", head=True)
        .neq("programme", GLOBAL_MOCK_PROGRAMME)
        .execute()
        .count
        or 0
    )
    general_paper_rows = (
        admin.table("question_bank")
        .select("id", count="exact", head=True)
        .eq("paper_title", GENERAL_PAPER_TITLE)
        .execute()
        .count
        or 0
    )

    general_unique_texts = set()
    offset = 0
    while True:
        chunk = (
            admin.table("question_bank")
            .select("question_text")
            .eq("paper_title", GENERAL_PAPER_TITLE)
            .range(offset, offset + POSTGREST_LIST_CHUNK - 1)
            .execute()
            .data
            or []
        )
        for row in chunk:
            general_unique_texts.add((row.get("question_text") or "").strip().lower())
        if len(chunk) < POSTGREST_LIST_CHUNK:
            break
        offset += POSTGREST_LIST_CHUNK

    prog_specific = {}
    for prog in PROGRAMME_NAMES:
        cnt = (
            admin.table("question_bank")
            .select("id", count="exact", head=True)
            .eq("programme", prog)
            .neq("paper_title", GENERAL_PAPER_TITLE)
            .execute()
            .count
            or 0
        )
        if cnt:
            prog_specific[prog] = cnt

    general_count = len(general_unique_texts)
    unique_total = general_count + sum(prog_specific.values())
    mock_batches = mock_count // MOCK_QUESTION_BATCH_SIZE
    mock_remainder = mock_count % MOCK_QUESTION_BATCH_SIZE

    paper_by_programme = []
    for programme, papers in PROGRAMME_PAPERS.items():
        counts = _question_bank_counts_by_paper(admin, programme)
        paper_rows = [{"paper": p, "count": int(counts.get(p, 0))} for p in papers]
        paper_by_programme.append(
            {
                "programme": programme,
                "papers": paper_rows,
                "subtotal": sum(pr["count"] for pr in paper_rows),
            }
        )

    return {
        "stats_db_total_rows": db_total,
        "stats_non_mock_rows": non_mock_rows,
        "stats_general_paper_rows": general_paper_rows,
        "stats_mock_count": mock_count,
        "stats_mock_batches": mock_batches,
        "stats_mock_remainder": mock_remainder,
        "stats_general_count": general_count,
        "stats_prog_specific": sorted(prog_specific.items()),
        "stats_unique_total": unique_total,
        "stats_prog_specific_row_sum": sum(prog_specific.values()),
        "stats_paper_by_programme": paper_by_programme,
    }


def _enrich_mock_exams_for_admin(exams, pool_total):
    """Add effective_question_count (pool slice size) and created_at_display per row."""
    pool_total = int(pool_total or 0)
    for exam in exams:
        needed = int(exam.get("question_count") or MOCK_QUESTION_BATCH_SIZE)
        mn = int(exam.get("mock_number") or 1)
        range_start = (mn - 1) * needed
        available = max(0, pool_total - range_start)
        exam["effective_question_count"] = min(needed, available)
        ca = exam.get("created_at")
        if ca:
            exam["created_at_display"] = str(ca).replace("T", " ")[:19].rstrip("Z")
        else:
            exam["created_at_display"] = "—"
    return exams


def _score_message(percentage):
    if percentage >= 90:
        return "Outstanding work! You are exam-ready. Keep the momentum."
    if percentage >= 75:
        return "Great job! You are performing strongly. Fine-tune weak areas."
    if percentage >= 60:
        return "Good effort. You are improving. Review explanations and try again."
    return "Keep pushing. Every attempt builds mastery. Review and come back stronger."


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

def home(request):
    plans = _get_plans()
    context = {
        "basic": plans.get("basic", {}),
        "premium": plans.get("premium", {}),
    }
    return render(request, "index.html", context)


def about(request):
    return render(request, "about.html")


def nclex_page(request):
    return render(request, "nclex.html")


def _normalize_nclex_question_payload(raw):
    if not isinstance(raw, dict):
        raise ValueError("Each NCLEX item must be an object.")

    raw_type = str(
        raw.get("question_type")
        or raw.get("type")
        or raw.get("format")
        or "mcq"
    ).strip().lower()
    type_map = {
        "mcq": "mcq",
        "multiple_choice": "mcq",
        "multiple-choice": "mcq",
        "sata": "sata",
        "select_all": "sata",
        "select-all-that-apply": "sata",
        "fill_blank": "fill_blank",
        "fill-in-the-blank": "fill_blank",
        "ordered_response": "ordered_response",
        "drag_and_drop": "ordered_response",
        "drag-drop": "ordered_response",
    }
    question_type = type_map.get(raw_type, raw_type)
    if question_type not in NCLEX_QUESTION_TYPES:
        raise ValueError(
            "question_type must be one of: "
            + ", ".join(NCLEX_QUESTION_TYPES.keys())
        )

    question_text = str(raw.get("question_text") or raw.get("question") or "").strip()
    if not question_text:
        raise ValueError("question_text is required.")

    options = raw.get("options", [])
    normalized_options = []
    if isinstance(options, dict):
        for key in sorted(options.keys()):
            value = str(options.get(key) or "").strip()
            if value:
                normalized_options.append(value)
    elif isinstance(options, list):
        normalized_options = [str(o).strip() for o in options if str(o).strip()]

    if question_type in {"mcq", "sata", "ordered_response"} and len(normalized_options) < 2:
        raise ValueError(f"{question_type} requires at least 2 options.")

    provided_answers = raw.get("correct_answers")
    if provided_answers is None:
        single = raw.get("correct_answer") or raw.get("correct_option") or raw.get("answer")
        provided_answers = [single] if single not in (None, "") else []
    if not isinstance(provided_answers, list):
        provided_answers = [provided_answers]

    normalized_answers = []
    for ans in provided_answers:
        val = str(ans).strip()
        if not val:
            continue
        if len(val) == 1 and val.upper() in {"A", "B", "C", "D", "E", "F"} and normalized_options:
            idx = ord(val.upper()) - ord("A")
            if 0 <= idx < len(normalized_options):
                val = normalized_options[idx]
        normalized_answers.append(val)

    if not normalized_answers:
        raise ValueError("At least one correct answer is required.")
    if question_type == "mcq" and len(normalized_answers) != 1:
        raise ValueError("mcq must have exactly one correct answer.")

    rationale = str(raw.get("rationale") or raw.get("explanation") or "").strip()
    difficulty = str(raw.get("difficulty") or "medium").strip().lower()
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"

    try:
        display_order = int(raw.get("display_order", 0) or 0)
    except Exception:
        display_order = 0

    return {
        "question_type": question_type,
        "question_text": question_text,
        "options": normalized_options,
        "correct_answers": normalized_answers,
        "rationale": rationale,
        "difficulty": difficulty,
        "display_order": display_order,
        "is_active": bool(raw.get("is_active", True)),
    }


def _book_download_url(external_url):
    url = str(external_url or "").strip()
    if not url:
        return ""
    m = re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    m2 = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if "drive.google.com" in url and m2:
        file_id = m2.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def _parse_lenient_json_payload(payload_text):
    """
    Parse admin JSON with light auto-fixes for common paste issues.
    Fixes:
    - trailing commas before ] or }
    """
    raw = str(payload_text or "").strip()
    if not raw:
        raise ValueError("JSON payload is required.")

    try:
        return json.loads(raw)
    except Exception:
        pass

    # Remove trailing commas before array/object close.
    cleaned = re.sub(r",\s*(\]|\})", r"\1", raw)
    try:
        return json.loads(cleaned)
    except Exception as exc:
        raise ValueError(f"Invalid JSON format: {exc}") from exc


# ---------------------------------------------------------------------------
# Site maintenance (public page + admin)
# ---------------------------------------------------------------------------

def _parse_utc_datetime_local(raw):
    """HTML datetime-local → aware UTC datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def maintenance_page(request):
    from website.site_maintenance import DEFAULT_MAINTENANCE_IMAGE_URL

    m = get_active_site_maintenance()
    if not m:
        uid = request.session.get("user_id")
        role = request.session.get("role")
        if uid and role == "student":
            return redirect("/dashboard/")
        return redirect("/")

    img = (m.get("image_url") or "").strip() or DEFAULT_MAINTENANCE_IMAGE_URL
    show_modal = bool(request.session.pop("maintenance_notice_pending", None))
    request.session.modified = True

    ends_val = m.get("ends_at")
    ends_iso = str(ends_val).strip() if ends_val is not None else ""

    return render(
        request,
        "maintenance.html",
        {
            "maintenance": m,
            "maintenance_image_url": img,
            "maintenance_title": (m.get("title") or "Scheduled maintenance").strip(),
            "maintenance_message": (m.get("message") or "").strip()
            or "We are performing scheduled maintenance. Please check back soon.",
            "show_maintenance_modal": show_modal,
            "maintenance_row_id": str(m.get("id") or ""),
            "maintenance_ends_at_iso": ends_iso,
        },
    )


def admin_system_maintenance(request):
    guard = _require_admin(request)
    if guard:
        return guard

    from website.site_maintenance import DEFAULT_MAINTENANCE_IMAGE_URL, maintenance_row_is_live

    admin = _supabase_admin()
    success = error = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "create":
                title = (request.POST.get("title") or "").strip() or "Scheduled maintenance"
                message = (request.POST.get("message") or "").strip()
                image_url = (request.POST.get("image_url") or "").strip() or DEFAULT_MAINTENANCE_IMAGE_URL
                starts = _parse_utc_datetime_local(request.POST.get("starts_at"))
                ends_raw = (request.POST.get("ends_at") or "").strip()
                ends = _parse_utc_datetime_local(ends_raw) if ends_raw else None
                if not starts:
                    error = "Start date and time are required."
                elif ends and ends <= starts:
                    error = "End time must be after start time."
                else:
                    admin.table("site_maintenance").insert(
                        {
                            "title": title[:500],
                            "message": message[:8000],
                            "image_url": image_url[:4000],
                            "starts_at": starts.isoformat(),
                            "ends_at": ends.isoformat() if ends else None,
                            "is_enabled": True,
                        }
                    ).execute()
                    success = "Maintenance schedule saved."
                    invalidate_site_maintenance_cache()

            elif action == "toggle":
                rid = (request.POST.get("row_id") or "").strip()
                if rid:
                    row = admin.table("site_maintenance").select("is_enabled").eq("id", rid).limit(1).execute()
                    cur = (row.data or [{}])[0].get("is_enabled", True)
                    admin.table("site_maintenance").update({"is_enabled": not cur}).eq("id", rid).execute()
                    success = "Updated."
                    invalidate_site_maintenance_cache()

            elif action == "delete":
                rid = (request.POST.get("row_id") or "").strip()
                if rid:
                    admin.table("site_maintenance").delete().eq("id", rid).execute()
                    success = "Removed."
                    invalidate_site_maintenance_cache()

        except Exception as exc:
            err = str(exc).lower()
            if "pgrst205" in err or "could not find the table" in err or "does not exist" in err:
                error = (
                    "Table site_maintenance is missing. Run sql/create_site_maintenance.sql in Supabase, then try again."
                )
            else:
                error = str(exc)

    rows = []
    try:
        rows = (
            admin.table("site_maintenance")
            .select("*")
            .order("starts_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    for row in rows:
        if isinstance(row, dict):
            row["is_live_now"] = maintenance_row_is_live(row)

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "admin_system",
        "maintenance_rows": rows,
        "default_maintenance_image": DEFAULT_MAINTENANCE_IMAGE_URL,
        "success": success,
        "error": error,
    }
    return render(request, "dashboard/admin_system.html", context)


# ---------------------------------------------------------------------------
# Auth views
# ---------------------------------------------------------------------------

def login_page(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        if not email or not password:
            return render(request, "login.html", {"error": "Email and password are required."})

        try:
            supabase = _supabase()
            auth_resp = supabase.auth.sign_in_with_password({"email": email, "password": password})

            if not auth_resp.user:
                return render(request, "login.html", {"error": "Invalid email or password."})

            user_id = str(auth_resp.user.id)

            # Fetch profile from DB
            admin = _supabase_admin()
            profile_resp = admin.table("profiles").select("*").eq("id", user_id).limit(1).execute()
            profile = profile_resp.data[0] if profile_resp.data else None

            if not profile:
                return render(request, "login.html", {"error": "Account profile not found. Contact support."})

            # Block disabled accounts immediately
            if profile.get("is_active") is False:
                return render(request, "login.html", {
                    "error": "Your account has been disabled. Please contact support to restore access."
                })

            role = profile.get("role", "student")
            full_name = profile.get("full_name") or email.split("@")[0]

            # Concurrent-login guard (students only — admins are exempt).
            # Check if an active session already exists for this account.
            # If it does, compare the stored device fingerprint (IP + user-agent)
            # against the current request:
            #   • Same device  → user is simply re-logging in (session lapsed,
            #                    browser restarted, etc.) — just replace the session.
            #   • Different device → the account is being used on two devices at once,
            #                        meaning credentials were shared.  Disable it.
            if role == "student":
                existing = (
                    admin.table("active_sessions")
                    .select("ip_address, user_agent")
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    current_ip = request.META.get("REMOTE_ADDR", "")[:45]
                    current_ua = request.META.get("HTTP_USER_AGENT", "")[:500]
                    stored_ip  = existing.data[0].get("ip_address", "")
                    stored_ua  = existing.data[0].get("user_agent", "")
                    same_device = (current_ip == stored_ip and current_ua == stored_ua)

                    if not same_device:
                        # Different device: evict the old session and allow this new login.
                        # The previous session is immediately invalidated.
                        admin.table("active_sessions").delete().eq("user_id", user_id).execute()
                    # Same device or evicted old session: fall through to create new session

            _create_session(request, user_id, profile["email"], full_name, role)

            # Store plan info for feature-gating in templates and view guards.
            # profile was fetched with select("*") so all columns are available.
            request.session["plan_slug"]     = (profile.get("plan_slug") or "standard")
            request.session["is_free_access"] = _user_has_free_access(admin, user_id)

            _sync_pending_academic_profile(request, profile)

            if role == "admin":
                return redirect("/admin-panel/dashboard/")
            if role == "student":
                if get_active_site_maintenance():
                    request.session["maintenance_notice_pending"] = True
                    request.session.modified = True
                    return redirect("/maintenance/")
                _reconcile_pending_subscription_from_paystack(user_id, force=True)
                # Ensure a pending Paystack checkout row exists and matches current plan price (same as email signup).
                if not subscription_allows_dashboard(user_id):
                    plans = _get_plans()
                    _ensure_pending_checkout_row(request, plans, None)
            allowed, reason = _subscription_access_state(user_id)
            if not allowed:
                return redirect(f"/subscribe/?reason={reason}")
            return redirect("/dashboard/")

        except Exception as exc:
            msg = str(exc).lower()
            if "invalid" in msg or "credentials" in msg or "not found" in msg:
                return render(request, "login.html", {"error": "Invalid email or password."})
            return render(request, "login.html", {"error": "Login failed. Please try again."})

    reason = request.GET.get("reason", "")
    return render(request, "login.html", {"reason": reason})


PROGRAMME_CHOICES = [
    "Registered General Nursing (RGN)",
    "Registered Midwifery (RM)",
    "Nurse Assistant Clinical (NAC/NAP)",
    "Registered Mental Health Nursing (RMHN)",
    "Registered Public Health Nursing (RPHN)",
]

PROGRAMME_INITIALS = {
    "Registered General Nursing (RGN)": "RGN",
    "Registered Midwifery (RM)": "RM",
    "Nurse Assistant Clinical (NAC/NAP)": "NAC/NAP",
    "Registered Mental Health Nursing (RMHN)": "RMHN",
    "Registered Public Health Nursing (RPHN)": "RPHN",
}

# Must match signup.html year-of-study options (drives cohort features and profile validation).
YEAR_OF_STUDY_CODES = frozenset({"year1", "year2", "year3", "year4", "finalyear"})
ACADEMIC_YEAR_CHOICES = (
    ("year1", "Year 1"),
    ("year2", "Year 2"),
    ("year3", "Year 3"),
    ("year4", "Year 4"),
    ("finalyear", "Final Year"),
)


def _academic_profile_is_complete(profile):
    """
    Programme matches question_bank.programme filters across quizzes, general tests, mock counts, etc.
    """
    if not profile:
        return False
    prog = (profile.get("programme") or "").strip()
    year = (profile.get("year_of_study") or "").strip()
    school = (profile.get("school") or "").strip()
    if prog not in PROGRAMME_CHOICES:
        return False
    if year not in YEAR_OF_STUDY_CODES:
        return False
    if not school:
        return False
    return True


def _sync_pending_academic_profile(request, profile):
    """Set session flag so dashboard can show the blocking academic-profile modal."""
    if request.session.get("role") != "student":
        request.session["pending_academic_profile"] = False
        return
    request.session["pending_academic_profile"] = not _academic_profile_is_complete(profile)


def _provision_oauth_student_profile(admin, user_id, email, full_name):
    """
    Create profiles + pending subscription for a new Google OAuth user.
    Same billing rules as email signup: Paystack (or Stripe) annual checkout on /subscribe/
    must succeed before dashboard access (see login + google_oauth_callback gating).
    """
    plans = _get_plans()
    plan_slug = "standard"
    plan = plans.get(plan_slug, {})
    # Leave programme/year/school empty so the dashboard modal must collect them; programme
    # is then used everywhere we filter question_bank (e.g. user_dashboard, general tests).
    admin.table("profiles").upsert({
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "phone_number": "",
        "year_of_study": "",
        "school": "",
        "programme": "",
        "role": "student",
        "plan_slug": plan_slug,
        "subscription_status": "pending_payment",
    }).execute()
    admin.table("subscriptions").insert({
        "user_id": user_id,
        "user_email": email,
        "user_name": full_name,
        "plan_slug": plan_slug,
        "amount_due": plan.get("price", 0),
        "currency": plan.get("currency", "GHS"),
        "status": "pending_payment",
    }).execute()


def google_oauth_start(request):
    """
    Redirect to Supabase → Google. Configure the same Google OAuth client in the
    Supabase Dashboard (Authentication → Providers → Google): Client ID + secret,
    and add this callback URL to Redirect URLs: {origin}/auth/google/callback/
    """
    if not getattr(settings, "SUPABASE_URL", None) or not getattr(settings, "SUPABASE_KEY", None):
        return render(request, "login.html", {"error": "Google sign-in is not configured."})
    try:
        # In this Django app, auth code exchange is handled server-side in
        # google_oauth_callback(), so OAuth must return to that callback URL.
        callback = _google_oauth_callback_url(request)
        client = _supabase_oauth_client(request)
        oauth = client.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": callback},
        })
        return HttpResponseRedirect(oauth.url)
    except Exception:
        logging.getLogger(__name__).exception("Google OAuth start failed")
        return render(request, "login.html", {"error": "Could not start Google sign-in. Please try again."})


def google_oauth_callback(request):
    err = request.GET.get("error")
    if err:
        desc = request.GET.get("error_description") or err
        return render(request, "login.html", {"error": f"Google sign-in was cancelled or failed ({desc})."})

    code = request.GET.get("code")
    if not code:
        return render(request, "login.html", {"error": "Missing authorization code. Please try Google sign-in again."})

    try:
        client = _supabase_oauth_client(request)
        auth_resp = client.auth.exchange_code_for_session({
            "auth_code": code,
        })
    except Exception:
        logging.getLogger(__name__).exception("Google OAuth callback exchange failed")
        return render(request, "login.html", {"error": "Google sign-in failed. Please try again."})
    finally:
        request.session.pop(_DjangoOAuthStorage.SESSION_BUCKET, None)

    user = auth_resp.user
    if not user:
        return render(request, "login.html", {"error": "Google sign-in did not return a user."})

    user_id = str(user.id)
    email = (user.email or "").strip().lower()
    meta = user.user_metadata or {}
    full_name = (meta.get("full_name") or meta.get("name") or "").strip()
    if not full_name and email:
        full_name = email.split("@")[0]
    if not email:
        return render(request, "login.html", {"error": "Google did not provide an email address."})

    admin = _supabase_admin()
    profile_resp = admin.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    profile = profile_resp.data[0] if profile_resp.data else None

    if not profile:
        existing_email = admin.table("profiles").select("id").eq("email", email).limit(1).execute()
        if existing_email.data:
            return render(request, "login.html", {
                "error": "An account with this email already exists. Please sign in with your email and password.",
            })
        try:
            _provision_oauth_student_profile(admin, user_id, email, full_name)
        except Exception:
            logging.getLogger(__name__).exception("OAuth profile provisioning failed")
            return render(request, "login.html", {
                "error": "Could not finish registration. Please try again or contact support.",
            })

        profile_resp = admin.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        profile = profile_resp.data[0] if profile_resp.data else None

    if not profile:
        return render(request, "login.html", {"error": "Account profile not found. Contact support."})

    if profile.get("is_active") is False:
        return render(request, "login.html", {
            "error": "Your account has been disabled. Please contact support to restore access.",
        })

    role = profile.get("role", "student")
    profile_email = profile.get("email") or email
    display_name = profile.get("full_name") or full_name

    if role == "student":
        existing = (
            admin.table("active_sessions")
            .select("ip_address, user_agent")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            current_ip = request.META.get("REMOTE_ADDR", "")[:45]
            current_ua = request.META.get("HTTP_USER_AGENT", "")[:500]
            stored_ip = existing.data[0].get("ip_address", "")
            stored_ua = existing.data[0].get("user_agent", "")
            same_device = (current_ip == stored_ip and current_ua == stored_ua)
            if not same_device:
                admin.table("active_sessions").delete().eq("user_id", user_id).execute()

    _create_session(request, user_id, profile_email, display_name, role)
    request.session["plan_slug"] = (profile.get("plan_slug") or "standard")
    request.session["is_free_access"] = _user_has_free_access(admin, user_id)

    _sync_pending_academic_profile(request, profile)

    if role == "admin":
        return redirect("/admin-panel/dashboard/")
    if role == "student":
        if get_active_site_maintenance():
            request.session["maintenance_notice_pending"] = True
            request.session.modified = True
            return redirect("/maintenance/")
        _reconcile_pending_subscription_from_paystack(user_id, force=True)
        if not subscription_allows_dashboard(user_id):
            plans = _get_plans()
            _ensure_pending_checkout_row(request, plans, None)
    allowed, reason = _subscription_access_state(user_id)
    if not allowed:
        return redirect(f"/subscribe/?reason={reason}")
    return redirect("/dashboard/")


@require_POST
def complete_academic_profile(request):
    """
    Required for students who signed in with Google (or any profile missing
    programme / year / school). Updates Supabase profiles so question filters match their programme.
    """
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") != "student":
        return redirect("/dashboard/")

    programme = request.POST.get("programme", "").strip()
    year_of_study = request.POST.get("yearOfStudy", "").strip()
    institution = request.POST.get("institution", "").strip()

    errors = []
    if programme not in PROGRAMME_CHOICES:
        errors.append("Please select a valid nursing programme.")
    if year_of_study not in YEAR_OF_STUDY_CODES:
        errors.append("Please select your year of study.")
    if not institution:
        errors.append("School / Institution is required.")

    next_url = (request.POST.get("next") or "").strip() or "/dashboard/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/dashboard/"

    if errors:
        for msg in errors:
            messages.error(request, msg)
        return redirect(next_url)

    user_id = str(request.session.get("user_id") or "").strip()
    if not user_id:
        messages.error(request, "Your session is invalid. Please log in again.")
        return redirect("/login/")

    admin = _supabase_admin()
    try:
        admin.table("profiles").update({
            "programme": programme,
            "year_of_study": year_of_study,
            "school": institution,
        }).eq("id", user_id).execute()
    except Exception:
        logging.getLogger(__name__).exception("complete_academic_profile update failed")
        messages.error(request, "Could not save your profile. Please try again.")
        return redirect(next_url)

    # Sync session from submitted values immediately. Relying on a follow-up SELECT can
    # race PostgREST/read replicas and leave pending_academic_profile stuck True.
    _sync_pending_academic_profile(request, {
        "programme": programme,
        "year_of_study": year_of_study,
        "school": institution,
    })
    request.session.modified = True
    messages.success(request, "Your programme details have been saved.")
    return redirect(next_url)


def signup_page(request):
    plans = _get_plans()

    if request.method == "POST":
        full_name        = request.POST.get("fullName", "").strip()
        email            = request.POST.get("email", "").strip().lower()
        phone            = request.POST.get("phone", "").strip()
        year_of_study    = request.POST.get("yearOfStudy", "").strip()
        institution      = request.POST.get("institution", "").strip()
        programme        = request.POST.get("programme", "").strip()
        password         = request.POST.get("password", "")
        confirm_password = request.POST.get("confirmPassword", "")
        plan_slug        = "standard"

        form_data = {
            "fullName": full_name,
            "email": email,
            "phone": phone,
            "yearOfStudy": year_of_study,
            "institution": institution,
            "programme": programme,
            "plan_slug": plan_slug,
        }

        errors = []

        if not all([full_name, email, phone, year_of_study, institution, programme, password, confirm_password]):
            errors.append("All fields are required.")

        if programme and programme not in PROGRAMME_CHOICES:
            errors.append("Please select a valid programme category.")

        if password != confirm_password:
            errors.append("Passwords do not match.")

        errors.extend(_validate_password(password))

        if errors:
            return render(request, "signup.html", {
                "errors": errors,
                "form_data": form_data,
                "plans": plans,
                "basic": plans.get("basic", {}),
                "premium": plans.get("premium", {}),
                "standard": plans.get("standard", {}),
                "paystack_pub_key": (getattr(settings, "PAYSTACK_PUBLIC_KEY", None) or "").strip(),
                "signup_uses_paystack": bool((getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()),
            })

        payment_reference = (
            request.POST.get("payment_reference", "").strip()
            or request.POST.get("paystack_reference", "").strip()
        )
        paystack_pub = (getattr(settings, "PAYSTACK_PUBLIC_KEY", None) or "").strip()

        def _signup_err(msg):
            return render(request, "signup.html", {
                "errors": [msg],
                "form_data": form_data,
                "plans": plans,
                "basic": plans.get("basic", {}),
                "premium": plans.get("premium", {}),
                "standard": plans.get("standard", {}),
                "paystack_pub_key": paystack_pub,
                "signup_uses_paystack": bool((getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()),
            })

        try:
            admin = _supabase_admin()

            # Check Supabase profiles table only
            existing = admin.table("profiles").select("id").eq("email", email).limit(1).execute()
            if existing.data:
                return _signup_err("This email is already registered. Please log in instead.")

            try:
                auth_resp = admin.auth.admin.create_user({
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                    "user_metadata": {"full_name": full_name, "role": "student"},
                })
                if not auth_resp.user:
                    return _signup_err("Failed to create account. Please try again.")
                user_id = str(auth_resp.user.id)
            except Exception as auth_exc:
                emsg = str(auth_exc).lower()
                if "already" in emsg or "registered" in emsg or "exists" in emsg:
                    # Orphaned auth user (no profile row) — recover it
                    orphan = _find_auth_user_by_email(email)
                    if not orphan:
                        return _signup_err(
                            "This email exists in our system but is incomplete. "
                            "Please contact support or try a different email."
                        )
                    user_id = str(orphan["id"])
                    # Update password so new credentials work
                    try:
                        admin.auth.admin.update_user_by_id(
                            user_id,
                            {"password": password, "email_confirm": True},
                        )
                    except Exception:
                        pass
                else:
                    return _signup_err(f"Registration failed: {auth_exc}")
            plan    = plans.get(plan_slug, {})

            # Upsert profile with plan info
            admin.table("profiles").upsert({
                "id": user_id,
                "email": email,
                "full_name": full_name,
                "phone_number": phone,
                "year_of_study": year_of_study,
                "school": institution,
                "programme": programme,
                "role": "student",
                "plan_slug": plan_slug,
                "subscription_status": "pending_payment",
            }).execute()

            # Create pending subscription record and capture its id
            sub_resp = admin.table("subscriptions").insert({
                "user_id": user_id,
                "user_email": email,
                "user_name": full_name,
                "plan_slug": plan_slug,
                "amount_due": plan.get("price", 0),
                "currency": plan.get("currency", "GHS"),
                "status": "pending_payment",
            }).execute()
            sub_id = (sub_resp.data or [{}])[0].get("id")

            # Keep Paystack reference on the row even if server-side verify fails (wrong key, 403, etc.)
            # so login /subscribe/ can retry verification without asking the user to pay again.
            if payment_reference and sub_id:
                admin.table("subscriptions").update({
                    "payment_reference": payment_reference,
                }).eq("id", sub_id).execute()

            price_val = float(plan.get("price") or 0)
            paystack_secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
            bulkclix_configured = bool((getattr(settings, "BULKCLIX_API_KEY", None) or "").strip())

            if price_val <= 0 and sub_id:
                _apply_successful_subscription_payment(user_id, sub_id, plan_slug, 0, "complimentary")
                _create_session(request, user_id, email, full_name, "student")
                request.session["pending_dashboard_disclaimer"] = True
                _sync_pending_academic_profile(request, {
                    "programme": programme,
                    "year_of_study": year_of_study,
                    "school": institution,
                })
                return redirect("/dashboard/")

            # Paystack: verify server-side before activating (never trust raw reference alone).
            if payment_reference and sub_id and paystack_secret:
                import urllib.parse as _up
                vresp, verr = _paystack_request(
                    "GET", "/transaction/verify/" + _up.quote(payment_reference, safe="")
                )
                if (vresp and vresp.get("status")
                        and (vresp.get("data") or {}).get("status") == "success"):
                    amount_paid = float((vresp["data"].get("amount") or 0)) / 100
                    _apply_successful_subscription_payment(
                        user_id, sub_id, plan_slug, amount_paid, payment_reference
                    )
                    _create_session(request, user_id, email, full_name, "student")
                    request.session["pending_dashboard_disclaimer"] = True
                    _sync_pending_academic_profile(request, {
                        "programme": programme,
                        "year_of_study": year_of_study,
                        "school": institution,
                    })
                    return redirect("/dashboard/")

            # Bulkclix MoMo: initiation API already charged; reference is from their success response only.
            if payment_reference and sub_id and bulkclix_configured and not paystack_secret:
                _apply_successful_subscription_payment(
                    user_id, sub_id, plan_slug, price_val, payment_reference
                )
                _create_session(request, user_id, email, full_name, "student")
                request.session["pending_dashboard_disclaimer"] = True
                _sync_pending_academic_profile(request, {
                    "programme": programme,
                    "year_of_study": year_of_study,
                    "school": institution,
                })
                return redirect("/dashboard/")

            _create_session(request, user_id, email, full_name, "student")
            request.session["pending_dashboard_disclaimer"] = True
            _sync_pending_academic_profile(request, {
                "programme": programme,
                "year_of_study": year_of_study,
                "school": institution,
            })
            return redirect("/subscribe/")

        except Exception as exc:
            return _signup_err(f"Registration failed: {exc}")

    paystack_pub = (getattr(settings, "PAYSTACK_PUBLIC_KEY", None) or "").strip()
    return render(request, "signup.html", {
        "plans": plans,
        "standard": plans.get("standard", {}),
        "basic": plans.get("basic", {}),
        "premium": plans.get("premium", {}),
        "paystack_pub_key": paystack_pub,
        "signup_uses_paystack": bool((getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()),
    })


def signup_account_exists_api(request):
    """
    Lightweight preflight check used by signup payment flow.
    Prevents opening checkout for emails that already have an account.
    """
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "Method not allowed"}, status=405)

    email = (request.GET.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    try:
        admin = _supabase_admin()
        profile = _profile_by_email_flexible(admin, email)
        auth_user = _find_auth_user_by_email(email)
        exists = bool(profile or auth_user)
        return JsonResponse({"ok": True, "exists": exists})
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Could not validate account status right now."},
            status=500,
        )


def signup_initiate_payment_api(request):
    """
    Initialize subscription payment for signup: Paystack (preferred) or Bulkclix MoMo.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed"}, status=405)

    paystack_secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
    paystack_public = (getattr(settings, "PAYSTACK_PUBLIC_KEY", None) or "").strip()

    full_name = (request.POST.get("full_name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    email = (request.POST.get("email") or "").strip().lower()
    amount_raw = (request.POST.get("amount") or "0").strip()
    try:
        amount = float(amount_raw)
    except Exception:
        amount = 0.0

    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Invalid payment amount."}, status=400)

    if paystack_secret:
        if not email:
            return JsonResponse({"ok": False, "error": "Email is required for Paystack checkout."}, status=400)
        origin = _public_site_origin(request)
        callback_url = origin.rstrip("/") + "/signup/"
        data, err = _paystack_transaction_initialize(
            email=email,
            amount_ghs=amount,
            callback_url=callback_url,
            metadata={"signup_email": email, "full_name": (full_name or "")[:200]},
        )
        if err or not data:
            msg = _paystack_api_error_message(err) or str(err or "paystack_init_failed")
            return JsonResponse({"ok": False, "error": msg}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "reference": data.get("reference"),
                "access_code": data.get("access_code"),
                "authorization_url": data.get("authorization_url"),
                "public_key": paystack_public,
                "amount_paid": amount,
            }
        )

    if not full_name or not phone:
        return JsonResponse({"ok": False, "error": "Full name and phone are required."}, status=400)

    result, err = _bulkclix_start_subscription_payment(
        full_name=full_name,
        phone_number=phone,
        amount=amount,
    )
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)

    return JsonResponse(
        {
            "ok": True,
            "reference": result.get("reference"),
            "amount_paid": result.get("amount_paid"),
        }
    )


def logout_view(request):
    _destroy_session(request)
    return redirect("/login/")


# ---------------------------------------------------------------------------
# Forgot / Reset password
# ---------------------------------------------------------------------------

def forgot_password_page(request):
    """
    Step 1 of the password reset flow.
    GET  → shows the email form.
    POST → looks up the email in Supabase profiles, generates a 6-digit OTP,
           stores it in the session (15-minute TTL), sends the reset email,
           then redirects to /reset-password/.
    """
    from django.core.mail import send_mail
    import time

    if request.method != "POST":
        return render(request, "forgot_password.html", {})

    email = request.POST.get("email", "").strip().lower()
    if not email:
        return render(request, "forgot_password.html", {"error": "Please enter your email address."})

    profile = None
    auth_user = None
    try:
        admin = _supabase_admin()
        profile = _profile_by_email_flexible(admin, email)
    except Exception:
        if settings.DEBUG:
            logging.getLogger(__name__).exception("Profile lookup for password reset failed")
        profile = None

    if not profile:
        auth_user = _find_auth_user_by_email(email)

    # Send a code if we have either a profile row or a Supabase Auth user (password lives in Auth).
    can_reset = bool(profile or auth_user)

    # Always redirect to reset page — never reveal whether the email exists
    if can_reset:
        code = str(secrets.randbelow(900000) + 100000)  # 6-digit, never leading-zero
        if profile:
            full_name = (profile.get("full_name") or "").strip() or "Student"
            mail_to = (profile.get("email") or email).strip().lower()
        else:
            meta = (auth_user.get("user_metadata") or {}) if auth_user else {}
            full_name = (meta.get("full_name") or "").strip() or "Student"
            mail_to = (auth_user.get("email") or email).strip().lower()

        subject = "nursesedge  — Your password reset code"
        body = (
            f"Hi {full_name},\n\n"
            f"You requested a password reset for your nursesedge  account.\n\n"
            f"Your 6-digit reset code is:\n\n"
            f"    {code}\n\n"
            f"This code expires in 15 minutes.\n"
            f"If you did not request this, you can safely ignore this email.\n\n"
            f"— The nursesedge  Team"
        )
        try:
            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                [mail_to],
                fail_silently=False,
            )
        except Exception:
            if settings.DEBUG:
                logging.getLogger(__name__).exception(
                    "Password reset email failed (check EMAIL_* in .env and Gmail app password)."
                )
            # Do not store a code in session if the email never left the server.
            return redirect("/reset-password/?mail_err=1")

        request.session["pw_reset_email"] = mail_to
        request.session["pw_reset_code"] = code
        request.session["pw_reset_expires"] = int(time.time()) + 900  # 15 minutes
        request.session["pw_reset_just_sent"] = True
        request.session.modified = True
    elif settings.DEBUG:
        logging.getLogger(__name__).warning(
            "Password reset: no profile or auth user for email %s — no email sent.",
            email,
        )

    return redirect("/reset-password/")


def reset_password_page(request):
    """
    Step 2 of the password reset flow.
    GET  → shows code + new-password form.
    POST → validates OTP from session, updates password via Supabase Admin API,
           clears the session token, redirects to /login/ on success.
    """
    import time

    if request.method == "GET":
        if request.session.get("pw_reset_email"):
            code_just_sent = bool(request.session.pop("pw_reset_just_sent", False))
            request.session.modified = True
            return render(
                request,
                "reset_password.html",
                {"code_just_sent": code_just_sent},
            )
        if request.GET.get("mail_err") == "1":
            return render(request, "reset_password.html", {"mail_delivery_failed": True})
        return render(request, "reset_password.html", {"no_active_reset": True})

    # ── POST ──────────────────────────────────────────────────────────────────
    code             = request.POST.get("code", "").strip()
    new_password     = request.POST.get("password", "")
    confirm_password = request.POST.get("confirm_password", "")

    stored_code    = request.session.get("pw_reset_code", "")
    stored_email   = request.session.get("pw_reset_email", "")
    stored_expires = request.session.get("pw_reset_expires", 0)

    def _err(msg):
        return render(request, "reset_password.html", {"error": msg})

    if not stored_email or not stored_code:
        return redirect("/forgot-password/")

    if int(time.time()) > stored_expires:
        # Wipe expired token from session
        for k in ("pw_reset_code", "pw_reset_email", "pw_reset_expires"):
            request.session.pop(k, None)
        return render(request, "reset_password.html", {
            "error": "Your reset code has expired. Please request a new one.",
            "expired": True,
        })

    if code != stored_code:
        return _err("Incorrect reset code. Please check your email and try again.")

    if new_password != confirm_password:
        return _err("Passwords do not match.")

    pw_errors = _validate_password(new_password)
    if pw_errors:
        return _err(pw_errors[0])

    # Update password in Supabase Auth
    try:
        admin = _supabase_admin()
        # Find the auth user by email
        auth_user = _find_auth_user_by_email(stored_email)
        if not auth_user:
            return _err("Account not found. Please contact support.")

        admin.auth.admin.update_user_by_id(
            auth_user["id"],
            {"password": new_password},
        )
    except Exception as exc:
        return _err(f"Could not update password. Please try again or contact support.")

    # Clear the reset token from session
    for k in ("pw_reset_code", "pw_reset_email", "pw_reset_expires"):
        request.session.pop(k, None)

    return redirect("/login/?reason=password_reset")


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

def _notify_admin_new_contact_message(name, email, phone, subject, message):
    """Email admin mailbox when a new contact form message is received."""
    recipient = (getattr(settings, "CONTACT_ALERT_EMAIL", "") or "").strip()
    if not recipient:
        return
    subject_text = f"New mail from NursesEdge: {subject or 'Contact form message'}"
    body = (
        "A new contact message was submitted on NursesEdge.\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone}\n"
        f"Subject: {subject or '-'}\n\n"
        "Message:\n"
        f"{message}\n"
    )
    send_mail(
        subject_text,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
        fail_silently=False,
    )


def contact_page(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        subject = request.POST.get("subject", "").strip()
        message = request.POST.get("message", "").strip()

        if not all([name, email, phone, message]):
            return render(request, "contact.html", {"error": "Name, email, phone number, and message are required."})

        try:
            _supabase_admin().table("contact_messages").insert({
                "name": name,
                "email": email,
                "phone": phone,
                "subject": subject,
                "message": message,
            }).execute()
            try:
                _notify_admin_new_contact_message(name, email, phone, subject, message)
            except Exception:
                logger.exception("Failed to send new contact message alert email.")
            return render(request, "contact.html", {"success": "Your message has been sent. We'll get back to you shortly."})
        except Exception:
            return render(request, "contact.html", {"error": "Failed to send message. Please try again."})

    return render(request, "contact.html")


# ---------------------------------------------------------------------------
# Student dashboard
# ---------------------------------------------------------------------------

def _subject_label_from_mock_title(title):
    txt = (title or "").strip()
    low = txt.lower()
    keyword_map = [
        ("medicine", "Medicine"),
        ("surgery", "Surgery"),
        ("paedi", "Paediatrics"),
        ("pediatric", "Paediatrics"),
        ("obstetric", "Obstetrics"),
        ("gynaec", "Gynaecology"),
        ("community", "Community Health"),
        ("mental", "Mental Health"),
    ]
    for needle, label in keyword_map:
        if needle in low:
            return label
    for sep in (" - ", " | ", ":"):
        if sep in txt:
            first = txt.split(sep, 1)[0].strip()
            if first:
                return first
    return txt or "Mock Exam"


def _build_weekly_peer_comparison(admin_client, user_id):
    """
    Anonymized weekly cohort comparison based on mock attempts.
    Cohort = same programme + year_of_study.
    """
    profile_rows = (
        admin_client.table("profiles")
        .select("programme, year_of_study")
        .eq("id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not profile_rows:
        return None
    programme = (profile_rows[0].get("programme") or "").strip()
    year_of_study = (profile_rows[0].get("year_of_study") or "").strip()
    if not programme or not year_of_study:
        return None

    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    my_attempts = (
        admin_client.table("mock_attempts")
        .select("mock_exam_id, percentage, submitted_at")
        .eq("student_id", user_id)
        .not_.is_("submitted_at", "null")
        .gte("submitted_at", week_start)
        .order("submitted_at", desc=True)
        .limit(20)
        .execute()
        .data
        or []
    )
    latest = next((a for a in my_attempts if a.get("mock_exam_id")), None)
    if not latest:
        return None

    mock_exam_id = latest.get("mock_exam_id")
    my_score = float(latest.get("percentage") or 0)
    exam_rows = (
        admin_client.table("mock_exams")
        .select("title")
        .eq("id", mock_exam_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    subject_label = _subject_label_from_mock_title(exam_rows[0].get("title") if exam_rows else "")

    cohort_rows = (
        admin_client.table("profiles")
        .select("id")
        .eq("programme", programme)
        .eq("year_of_study", year_of_study)
        .execute()
        .data
        or []
    )
    cohort_ids = [str(r.get("id")) for r in cohort_rows if r.get("id")]
    if not cohort_ids:
        return None

    attempt_rows = (
        admin_client.table("mock_attempts")
        .select("student_id, percentage")
        .eq("mock_exam_id", mock_exam_id)
        .in_("student_id", cohort_ids)
        .not_.is_("submitted_at", "null")
        .gte("submitted_at", week_start)
        .execute()
        .data
        or []
    )
    if not attempt_rows:
        return None

    best_by_student = {}
    for row in attempt_rows:
        sid = str(row.get("student_id") or "").strip()
        if not sid:
            continue
        pct = float(row.get("percentage") or 0)
        if sid not in best_by_student or pct > best_by_student[sid]:
            best_by_student[sid] = pct

    scores = list(best_by_student.values())
    cohort_size = len(scores)
    if cohort_size < 5:
        return None

    higher_count = sum(1 for s in scores if s > my_score)
    less_or_equal_count = sum(1 for s in scores if s <= my_score)
    top_percent = max(1, min(100, int(math.ceil(((higher_count + 1) / cohort_size) * 100))))
    percentile = max(1, min(100, int(round((less_or_equal_count / cohort_size) * 100))))

    return {
        "score": int(round(my_score)),
        "subject_label": subject_label,
        "year_of_study": year_of_study,
        "programme": programme,
        "cohort_size": cohort_size,
        "top_percent": top_percent,
        "percentile": percentile,
    }


def user_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    user_id = request.session.get("user_id")

    # Mid-session expiry check — catches subscriptions that expired while the
    # user was already logged in (login only checks at login time).
    if request.session.get("role") == "student":
        allowed, reason = _subscription_access_state(user_id)
        if not allowed:
            return redirect(f"/subscribe/?reason={reason}")
    unread_count = _student_unread_count(user_id)
    mock_exam_count = 0
    best_mock_percentage = 0
    total_questions_available = 0
    recent_messages = []
    peer_comparison = None
    try:
        admin = _supabase_admin()
        profile_rows = admin.table("profiles").select("programme").eq("id", user_id).limit(1).execute().data or []
        programme = (profile_rows[0].get("programme") if profile_rows else "") or ""
        if programme:
            exam_count_resp = (
                admin.table("mock_exams")
                .select("id", count="exact", head=True)
                .eq("programme", programme)
                .eq("is_published", True)
                .execute()
            )
            mock_exam_count = exam_count_resp.count or 0

            questions_resp = (
                admin.table("question_bank")
                .select("id", count="exact", head=True)
                .eq("programme", programme)
                .execute()
            )
            total_questions_available = questions_resp.count or 0

            recent_messages_resp = (
                admin.table("student_notifications")
                .select("id, title, message_body, is_read, created_at")
                .eq("student_id", user_id)
                .order("created_at", desc=True)
                .limit(3)
                .execute()
            )
            recent_messages = recent_messages_resp.data or []
        else:
            # Fallback: show total available questions across all programmes.
            questions_resp = (
                admin.table("question_bank")
                .select("id", count="exact", head=True)
                .execute()
            )
            total_questions_available = questions_resp.count or 0

            recent_messages_resp = (
                admin.table("student_notifications")
                .select("id, title, message_body, is_read, created_at")
                .eq("student_id", user_id)
                .order("created_at", desc=True)
                .limit(3)
                .execute()
            )
            recent_messages = recent_messages_resp.data or []
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
        peer_comparison = _build_weekly_peer_comparison(admin, user_id)
    except Exception:
        pass

    # Subscription info for dashboard cards
    subscription = _get_active_subscription(user_id)
    plan_name = "nursesedge  Access"
    plan_slug = "standard"
    sub_status = "pending_payment"
    sub_expires = None
    try:
        if subscription:
            plan_slug  = subscription.get("plan_slug", "standard")
            sub_status = subscription.get("status", "pending_payment")
            sub_expires= subscription.get("expires_at", None)
        if sub_status == "active":
            plan_name = "Subscribed"
        elif sub_status == "pending_payment":
            plan_name = "Pending Activation"
        else:
            plan_name = "Inactive"
    except Exception:
        pass

    # Joined communities for dashboard widget
    joined_communities = []
    community_unread = 0
    try:
        admin = _supabase_admin()
        memberships = (
            admin.table("community_members")
            .select("community_id")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )
        if memberships:
            cids = [m["community_id"] for m in memberships]
            comm_rows = (
                admin.table("communities")
                .select("id, name, slug, icon, color_hex, member_count")
                .in_("id", cids)
                .eq("is_active", True)
                .order("name")
                .execute()
                .data or []
            )
            joined_communities = comm_rows
        community_unread = _community_unread_count(user_id)
    except Exception:
        pass

    # Refresh plan_slug in session — catches upgrades made since last login
    request.session["plan_slug"] = plan_slug

    show_nmc_disclaimer = False
    if request.session.get("role") != "admin":
        show_nmc_disclaimer = _student_needs_dashboard_nmc_disclaimer(request, user_id)

    # Subscription countdown ─────────────────────────────────────────────────
    days_remaining    = None   # None = unknown / no expiry (free access)
    plan_progress_pct = 100    # bar width % (100 = full / just activated)
    sub_expires_display = ""   # human-readable date string
    from datetime import date as _date
    _sub_expires_str = (sub_expires or "")[:10]
    if _sub_expires_str and sub_status == "active":
        try:
            expiry_date      = _date.fromisoformat(_sub_expires_str)
            days_remaining   = (expiry_date - _date.today()).days
            plan_progress_pct = max(0, min(100, round(max(days_remaining, 0) / 365 * 100)))
            sub_expires_display = "{} {} {}".format(expiry_date.day, expiry_date.strftime("%b"), expiry_date.year)
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "dashboard",
        "show_nmc_disclaimer": show_nmc_disclaimer,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "mock_exam_count": mock_exam_count,
        "best_mock_percentage": best_mock_percentage,
        "total_questions_available": total_questions_available,
        "recent_messages": recent_messages,
        "joined_communities": joined_communities,
        "community_unread": community_unread,
        "subscription": subscription,
        "plan_name": plan_name,
        "plan_slug": plan_slug,
        "sub_status": sub_status,
        "sub_expires": _sub_expires_str,
        "sub_expires_display": sub_expires_display,
        "days_remaining": days_remaining,
        "plan_progress_pct": plan_progress_pct,
        "peer_comparison": peer_comparison,
    }
    return render(request, "dashboard/user_dashboard.html", context)


def dashboard_ack_nmc_disclaimer(request):
    """POST: record one-time acknowledgement of the dashboard NMC independence disclaimer."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed"}, status=405)
    guard = _require_login(request)
    if guard:
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)
    if request.session.get("role") == "admin":
        return JsonResponse({"ok": True})
    user_id = request.session.get("user_id")
    request.session.pop("pending_dashboard_disclaimer", None)
    try:
        admin = _supabase_admin()
        admin.table("profiles").update({
            "dashboard_nmc_disclaimer_ack_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", str(user_id)).execute()
    except Exception:
        pass
    return JsonResponse({"ok": True})


def student_nmc_mastery(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "nmc_mastery",
        "student_unread_notifications": _student_unread_count(user_id),
        "community_unread": _community_unread_count(user_id),
    }
    return render(request, "dashboard/student_nmc_mastery.html", context)


def _fetch_clinical_visual_gallery_urls_from_db(admin_client):
    """
    Active clinical visual image URLs from Supabase, ordered by sort_order then created_at.
    Returns None on failure (missing table / network); empty list if none configured.
    """
    try:
        rows = (
            admin_client.table("clinical_visual_gallery")
            .select("image_url")
            .eq("is_active", True)
            .order("sort_order", desc=False)
            .limit(2000)
            .execute()
            .data
            or []
        )
        out = []
        for r in rows:
            u = (r.get("image_url") or "").strip()
            if u.lower().startswith(("http://", "https://")):
                out.append(u)
        return out
    except Exception:
        return None


def student_clinical_visual_library(request):
    """High-resolution clinical / study reference images (grid on desktop; swipe-style nav on phones)."""
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    admin = _supabase_admin()
    urls = _fetch_clinical_visual_gallery_urls_from_db(admin)
    if urls is None:
        from website.clinical_visual_gallery_urls import CLINICAL_VISUAL_GALLERY_URLS

        urls = list(CLINICAL_VISUAL_GALLERY_URLS)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "clinical_visuals",
        "student_unread_notifications": _student_unread_count(user_id),
        "community_unread": _community_unread_count(user_id),
        "gallery_urls": urls,
        "gallery_urls_json": json.dumps(urls),
        "gallery_count": len(urls),
    }
    return render(request, "dashboard/student_clinical_visual_library.html", context)


def student_messages(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    try:
        messages = (
            _supabase_admin()
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
        messages = []

    unread_count = _student_unread_count(user_id)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "student_messages",
        "messages": messages,
        "student_unread_notifications": unread_count,
    }
    return render(request, "dashboard/student_messages.html", context)


def student_mark_message_read(request, message_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/dashboard/messages/")

    user_id = request.session.get("user_id")
    try:
        (
            _supabase_admin()
            .table("student_notifications")
            .update({"is_read": True})
            .eq("id", str(message_id))
            .eq("student_id", user_id)
            .execute()
        )
    except Exception:
        pass
    return redirect("/dashboard/messages/")


def student_mark_all_messages_read(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/dashboard/messages/")

    user_id = request.session.get("user_id")
    try:
        (
            _supabase_admin()
            .table("student_notifications")
            .update({"is_read": True})
            .eq("student_id", user_id)
            .eq("is_read", False)
            .execute()
        )
    except Exception:
        pass
    return redirect("/dashboard/messages/")


# ---------------------------------------------------------------------------
# Admin dashboard pages
# ---------------------------------------------------------------------------

def admin_dashboard(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    try:
        total_users_resp = admin.table("profiles").select("id", count="exact", head=True).execute()
        total_users = total_users_resp.count or 0

        total_students_resp = admin.table("profiles").select("id", count="exact", head=True).eq("role", "student").execute()
        total_students = total_students_resp.count or 0

        unread_messages_resp = admin.table("contact_messages").select("id", count="exact", head=True).eq("is_read", False).execute()
        unread_messages = unread_messages_resp.count or 0
    except Exception:
        total_students = total_users = unread_messages = 0

    total_visits, visits_24h, unique_ips_24h = _get_visit_stats(admin)

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "dashboard",
        "total_students": total_students,
        "total_users": total_users,
        "unread_messages": unread_messages,
        "total_visits": total_visits,
        "visits_24h": visits_24h,
        "unique_ips_24h": unique_ips_24h,
    }
    return render(request, "dashboard/admin_dashboard.html", context)


def admin_broadcast_messages(request):
    guard = _require_admin(request)
    if guard:
        return guard

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "broadcast_messages",
        "recent_broadcasts": [],
        "form_data": {"title": "", "message_body": ""},
    }

    admin = _supabase_admin()
    try:
        if request.method == "POST":
            action = request.POST.get("action", "send").strip()
            if action == "delete":
                notification_id = request.POST.get("notification_id", "").strip()
                if not notification_id:
                    raise ValueError("Notification ID is required for delete.")
                admin.table("student_notifications").delete().eq("id", notification_id).execute()
                context["success"] = "Broadcast deleted."
            else:
                title = request.POST.get("title", "").strip()
                message_body = request.POST.get("message_body", "").strip()
                if not title or not message_body:
                    raise ValueError("Title and message body are required.")

                students = (
                    admin.table("profiles").select("id").eq("role", "student").execute().data
                    or []
                )
                student_ids = [s.get("id") for s in students if s.get("id")]
                if not student_ids:
                    raise ValueError("No students found to receive this message.")

                rows = [
                    {
                        "student_id": student_id,
                        "title": title,
                        "message_body": message_body,
                        "sent_by": request.session.get("user_id"),
                        "is_read": False,
                    }
                    for student_id in student_ids
                ]
                admin.table("student_notifications").insert(rows).execute()
                context["success"] = f"Message sent to {len(rows)} student(s)."
                context["form_data"] = {"title": "", "message_body": ""}

        broadcasts = (
            admin.table("student_notifications")
            .select("id, title, message_body, created_at")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        context["recent_broadcasts"] = broadcasts
    except Exception as exc:
        context["error"] = str(exc)
        if request.method == "POST":
            context["form_data"] = {
                "title": request.POST.get("title", ""),
                "message_body": request.POST.get("message_body", ""),
            }

    return render(request, "dashboard/admin_broadcast_messages.html", context)


def _get_visit_stats(admin_client):
    """Return (total_visits, visits_24h, unique_ips_24h) from site_visits."""
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        total_resp = (
            admin_client.table("site_visits")
            .select("id", count="exact", head=True)
            .execute()
        )
        total_visits = total_resp.count or 0

        day_resp = (
            admin_client.table("site_visits")
            .select("ip_address", count="exact")
            .gte("visited_at", cutoff)
            .execute()
        )
        visits_24h = day_resp.count or 0

        rows_24h = day_resp.data or []
        unique_ips_24h = len({r["ip_address"] for r in rows_24h})

        return total_visits, visits_24h, unique_ips_24h
    except Exception:
        return 0, 0, 0


def admin_users(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    try:
        users = admin.table("profiles").select("*").eq("role", "student").order("created_at", desc=True).execute().data or []
        for user in users:
            programme = (user.get("programme") or "").strip()
            user["programme_initial"] = PROGRAMME_INITIALS.get(programme, programme or "—")
    except Exception:
        users = []

    total_visits, visits_24h, unique_ips_24h = _get_visit_stats(admin)

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "users",
        "users": users,
        "total_visits": total_visits,
        "visits_24h": visits_24h,
        "unique_ips_24h": unique_ips_24h,
    }
    return render(request, "dashboard/admin_users.html", context)


def admin_toggle_user_status(request, user_id):
    """Enable or disable a student account. POST-only, admin-only.
    Admin accounts are never touched regardless of who calls this."""
    guard = _require_admin(request)
    if guard:
        return guard

    if request.method != "POST":
        return redirect("/admin-panel/users/")

    # Determine which page to return to (users list or locked accounts list)
    next_url = request.POST.get("next", "/admin-panel/users/")
    if next_url not in ("/admin-panel/users/", "/admin-panel/locked-accounts/"):
        next_url = "/admin-panel/users/"

    admin = _supabase_admin()
    try:
        profile_resp = (
            admin.table("profiles")
            .select("is_active, role")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
        )
        if not profile_resp.data:
            return redirect(next_url)

        profile = profile_resp.data[0]

        # Never touch admin accounts
        if profile.get("role") == "admin":
            return redirect(next_url)

        currently_active = profile.get("is_active", True)
        new_status = not currently_active

        admin.table("profiles").update({"is_active": new_status}).eq("id", str(user_id)).execute()

        # If disabling, also kill any live session so the user is immediately evicted
        if not new_status:
            admin.table("active_sessions").delete().eq("user_id", str(user_id)).execute()
    except Exception:
        pass

    return redirect(next_url)


def admin_delete_user(request, user_id):
    """Permanently delete a student account (admin-only, POST-only)."""
    guard = _require_admin(request)
    if guard:
        return guard

    if request.method != "POST":
        return redirect("/admin-panel/users/")

    admin = _supabase_admin()
    try:
        profile_resp = (
            admin.table("profiles")
            .select("id, role")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
        )
        if not profile_resp.data:
            return redirect("/admin-panel/users/")

        profile = profile_resp.data[0]
        # Never allow deleting admin accounts from this endpoint.
        if profile.get("role") == "admin":
            return redirect("/admin-panel/users/")

        # Clear live session token(s) first so user is immediately evicted.
        try:
            admin.table("active_sessions").delete().eq("user_id", str(user_id)).execute()
        except Exception:
            pass

        # Delete Supabase Auth user (usually cascades related auth identities).
        try:
            admin.auth.admin.delete_user(str(user_id))
        except Exception:
            pass

        # Best-effort cleanup for app-owned records linked by user_id/student_id.
        for table_name, col_name in (
            ("mock_attempt_answers", "student_id"),
            ("mock_attempts", "student_id"),
            ("quiz_attempt_answers", "student_id"),
            ("quiz_attempts", "student_id"),
            ("student_notifications", "student_id"),
            ("subscriptions", "user_id"),
            ("active_sessions", "user_id"),
        ):
            try:
                admin.table(table_name).delete().eq(col_name, str(user_id)).execute()
            except Exception:
                pass

        # Remove profile row.
        try:
            admin.table("profiles").delete().eq("id", str(user_id)).execute()
        except Exception:
            pass
    except Exception:
        pass

    return redirect("/admin-panel/users/")


def admin_locked_accounts(request):
    """Lists all student accounts that have been disabled (is_active = false).
    Provides a one-click unlock button for each."""
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    try:
        locked = (
            admin.table("profiles")
            .select("*")
            .eq("role", "student")
            .eq("is_active", False)
            .order("created_at", desc=True)
            .execute()
            .data or []
        )
        for user in locked:
            programme = (user.get("programme") or "").strip()
            user["programme_initial"] = PROGRAMME_INITIALS.get(programme, programme or "—")
    except Exception:
        locked = []

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "locked_accounts",
        "locked": locked,
    }
    return render(request, "dashboard/admin_locked_accounts.html", context)


# ---------------------------------------------------------------------------
# Admin: Free-access user management
# ---------------------------------------------------------------------------

def _admin_fetch_free_users_list(admin):
    """List free-access student profiles; works with or without profiles.is_free_access."""
    try:
        return (
            admin.table("profiles")
            .select("id, full_name, email, programme, is_free_access, is_active, created_at")
            .eq("is_free_access", True)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        pass
    try:
        subs = (
            admin.table("subscriptions")
            .select("user_id")
            .eq("payment_reference", FREE_ACCESS_PAYMENT_REFERENCE)
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        uids = list({str(s["user_id"]) for s in subs if s.get("user_id")})
        if not uids:
            return []
        out = []
        for i in range(0, len(uids), 100):
            batch = uids[i : i + 100]
            rows = (
                admin.table("profiles")
                .select("id, full_name, email, programme, is_active, created_at")
                .in_("id", batch)
                .execute()
                .data
                or []
            )
            out.extend(rows)
        for r in out:
            r["is_free_access"] = True
        out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return out
    except Exception:
        return []


def admin_free_users(request):
    """Create and manage accounts that are permanently exempt from payment."""
    guard = _require_admin(request)
    if guard:
        return guard

    admin   = _supabase_admin()
    success = error = None

    PROGRAMME_CHOICES_LOCAL = [
        "Registered General Nursing (RGN)",
        "Registered Midwifery (RM)",
        "Nurse Assistant Clinical (NAC/NAP)",
        "Registered Mental Health Nursing (RMHN)",
        "Registered Public Health Nursing (RPHN)",
    ]

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "create":
            full_name = request.POST.get("full_name", "").strip()
            email     = request.POST.get("email", "").strip().lower()
            password  = request.POST.get("password", "").strip()
            programme = request.POST.get("programme", "").strip()

            if not all([full_name, email, password, programme]):
                error = "All fields are required."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            else:
                try:
                    # Create Supabase Auth user
                    auth_resp = admin.auth.admin.create_user({
                        "email": email,
                        "password": password,
                        "email_confirm": True,  # skip email verification
                    })
                    user_id = str(auth_resp.user.id)

                    profile_row = {
                        "id": user_id,
                        "email": email,
                        "full_name": full_name,
                        "programme": programme,
                        "role": "student",
                        "plan_slug": "premium",
                        "subscription_status": "active",
                        "is_active": True,
                        "is_free_access": True,
                    }
                    try:
                        admin.table("profiles").upsert(profile_row).execute()
                    except Exception as exc:
                        err = str(exc).lower()
                        if "is_free_access" in err or "pgrst204" in err:
                            profile_row.pop("is_free_access", None)
                            admin.table("profiles").upsert(profile_row).execute()
                        else:
                            raise

                    # Create a permanent active subscription (no expiry)
                    admin.table("subscriptions").insert({
                        "user_id": user_id,
                        "user_email": email,
                        "user_name": full_name,
                        "plan_slug": "premium",
                        "amount_due": 0,
                        "amount_paid": 0,
                        "currency": "GHS",
                        "status": "active",
                        "payment_reference": FREE_ACCESS_PAYMENT_REFERENCE,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "expires_at": None,  # never expires
                    }).execute()

                    success = f"Free-access account created for {full_name} ({email})."
                except Exception as exc:
                    msg = str(exc).lower()
                    if "already registered" in msg or "already exists" in msg or "duplicate" in msg:
                        error = f"An account with email '{email}' already exists."
                    else:
                        error = f"Failed to create account: {exc}"

        elif action == "revoke":
            user_id = request.POST.get("user_id", "").strip()
            if user_id:
                try:
                    try:
                        admin.table("profiles").update({"is_free_access": False}).eq("id", user_id).execute()
                    except Exception:
                        pass
                    subs = (
                        admin.table("subscriptions")
                        .select("id")
                        .eq("user_id", user_id)
                        .eq("payment_reference", FREE_ACCESS_PAYMENT_REFERENCE)
                        .execute()
                        .data
                        or []
                    )
                    for s in subs:
                        sid = s.get("id")
                        if sid:
                            admin.table("subscriptions").update({"status": "cancelled"}).eq("id", sid).execute()
                    success = "Free access revoked. The user will need a subscription to log in."
                except Exception as exc:
                    error = str(exc)

        elif action == "restore":
            user_id = request.POST.get("user_id", "").strip()
            if user_id:
                try:
                    try:
                        admin.table("profiles").update({"is_free_access": True}).eq("id", user_id).execute()
                    except Exception:
                        pass
                    subs = (
                        admin.table("subscriptions")
                        .select("id")
                        .eq("user_id", user_id)
                        .eq("payment_reference", FREE_ACCESS_PAYMENT_REFERENCE)
                        .execute()
                        .data
                        or []
                    )
                    if not subs:
                        raise ValueError(
                            "No free-access subscription row found for this user. "
                            "Recreate the account or add the is_free_access column in Supabase."
                        )
                    for s in subs:
                        sid = s.get("id")
                        if sid:
                            admin.table("subscriptions").update({"status": "active"}).eq("id", sid).execute()
                    success = "Free access restored."
                except Exception as exc:
                    error = str(exc)

        elif action == "reset_password":
            user_id      = request.POST.get("user_id", "").strip()
            new_password = request.POST.get("new_password", "").strip()
            if not user_id or not new_password:
                error = "User ID and new password are required."
            elif len(new_password) < 6:
                error = "Password must be at least 6 characters."
            else:
                try:
                    admin.auth.admin.update_user_by_id(user_id, {"password": new_password})
                    success = "Password updated successfully."
                except Exception as exc:
                    error = str(exc)

        elif action == "delete":
            user_id = request.POST.get("user_id", "").strip()
            if user_id:
                try:
                    admin.auth.admin.delete_user(user_id)
                    admin.table("profiles").delete().eq("id", user_id).execute()
                    success = "Account permanently deleted."
                except Exception as exc:
                    error = str(exc)

    free_users = _admin_fetch_free_users_list(admin)

    context = {
        "full_name":          request.session.get("full_name", "Admin"),
        "email":              request.session.get("email", ""),
        "role":               "admin",
        "active_page":        "free_users",
        "programmes":         PROGRAMME_CHOICES_LOCAL,
        "free_users":         free_users,
        "success":            success,
        "error":              error,
    }
    return render(request, "dashboard/admin_free_users.html", context)


def _clinical_visual_url_ok(url):
    u = (url or "").strip()
    return u.lower().startswith(("http://", "https://")) and len(u) < 4000


def admin_clinical_visual_gallery(request):
    """Manage student Clinical Visual Library image URLs in Supabase."""
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    success = error = None
    rows = []

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        uid = request.session.get("user_id")

        try:
            if action == "add_one":
                image_url = (request.POST.get("image_url") or "").strip()
                caption = (request.POST.get("caption") or "").strip()[:500]
                sort_raw = (request.POST.get("sort_order") or "").strip()
                try:
                    sort_order = int(sort_raw) if sort_raw != "" else 0
                except Exception:
                    sort_order = 0
                if not _clinical_visual_url_ok(image_url):
                    error = "Enter a valid image URL (must start with http:// or https://)."
                else:
                    admin.table("clinical_visual_gallery").insert(
                        {
                            "image_url": image_url,
                            "caption": caption,
                            "sort_order": sort_order,
                            "is_active": True,
                            "created_by": str(uid) if uid else None,
                        }
                    ).execute()
                    success = "Image added."

            elif action == "bulk_add":
                raw = (request.POST.get("urls_bulk") or "")
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                payloads = [
                    {
                        "image_url": line,
                        "caption": "",
                        "sort_order": 0,
                        "is_active": True,
                        "created_by": str(uid) if uid else None,
                    }
                    for line in lines
                    if _clinical_visual_url_ok(line)
                ]
                if payloads:
                    admin.table("clinical_visual_gallery").insert(payloads).execute()
                    success = f"Added {len(payloads)} image(s)."
                else:
                    error = "No valid URLs found. Each line must be a full http(s) URL."

            elif action == "delete":
                rid = (request.POST.get("row_id") or "").strip()
                if rid:
                    admin.table("clinical_visual_gallery").delete().eq("id", rid).execute()
                    success = "Removed."

            elif action == "toggle_active":
                rid = (request.POST.get("row_id") or "").strip()
                nxt = (request.POST.get("next_state") or "false").strip().lower() == "true"
                if rid:
                    admin.table("clinical_visual_gallery").update({"is_active": nxt}).eq("id", rid).execute()
                    success = "Updated."

            elif action == "update_meta":
                rid = (request.POST.get("row_id") or "").strip()
                caption = (request.POST.get("caption") or "").strip()[:500]
                sort_raw = (request.POST.get("sort_order") or "").strip()
                try:
                    sort_order = int(sort_raw) if sort_raw != "" else 0
                except Exception:
                    sort_order = 0
                if rid:
                    admin.table("clinical_visual_gallery").update(
                        {"caption": caption, "sort_order": sort_order}
                    ).eq("id", rid).execute()
                    success = "Saved."

        except Exception as exc:
            err = str(exc).lower()
            if "pgrst205" in err or "could not find the table" in err or "does not exist" in err:
                error = (
                    "Table clinical_visual_gallery is missing. Run the SQL in sql/create_clinical_visual_gallery.sql "
                    "in the Supabase SQL editor, then try again."
                )
            else:
                error = str(exc)

    try:
        rows = (
            admin.table("clinical_visual_gallery")
            .select("*")
            .order("sort_order", desc=False)
            .limit(2000)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    gallery_total_count = len(rows)
    gallery_active_count = sum(1 for r in rows if r.get("is_active"))

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "admin_clinical_visuals",
        "gallery_rows": rows,
        "gallery_total_count": gallery_total_count,
        "gallery_active_count": gallery_active_count,
        "success": success,
        "error": error,
    }
    return render(request, "dashboard/admin_clinical_visual_gallery.html", context)


def admin_messages(request):
    guard = _require_admin(request)
    if guard:
        return guard

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "messages",
        "contact_messages": [],
        "success": None,
        "error": None,
    }

    try:
        admin = _supabase_admin()
        if request.method == "POST":
            action = request.POST.get("action", "").strip()
            if action == "reply_contact_message":
                message_id = request.POST.get("message_id", "").strip()
                reply_subject = request.POST.get("reply_subject", "").strip()
                reply_body = request.POST.get("reply_body", "").strip()
                if not message_id:
                    raise ValueError("Message ID is required.")
                if not reply_subject or not reply_body:
                    raise ValueError("Reply subject and message are required.")

                row = (
                    admin.table("contact_messages")
                    .select("id, name, email, subject")
                    .eq("id", message_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if not row:
                    raise ValueError("Message not found.")
                recipient = (row[0].get("email") or "").strip()
                if not recipient:
                    raise ValueError("Recipient email is missing for this message.")

                send_mail(
                    reply_subject,
                    reply_body,
                    settings.DEFAULT_FROM_EMAIL,
                    [recipient],
                    fail_silently=False,
                )
                try:
                    admin.table("contact_messages").update({"is_read": True}).eq("id", message_id).execute()
                except Exception:
                    pass
                context["success"] = f"Reply sent to {recipient}."
            elif action == "mark_contact_message_read":
                message_id = request.POST.get("message_id", "").strip()
                if not message_id:
                    raise ValueError("Message ID is required.")
                admin.table("contact_messages").update({"is_read": True}).eq("id", message_id).execute()
                context["success"] = "Message marked as read."
            elif action:
                raise ValueError("Invalid action.")

        contact_messages = (
            admin.table("contact_messages")
            .select("*")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        context["contact_messages"] = contact_messages
    except Exception as exc:
        context["error"] = str(exc)

    return render(request, "dashboard/admin_messages.html", context)


def admin_upload_questions(request):
    guard = _require_admin(request)
    if guard:
        return guard

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "upload_questions",
        "programme_papers": PROGRAMME_PAPERS,
        "programme_papers_json": json.dumps(PROGRAMME_PAPERS),
        "programmes": PROGRAMME_NAMES,
        "all_papers": ALL_PAPERS,
        "form_data": EMPTY_QUESTION_FORM.copy(),
    }

    if request.method != "POST":
        return render(request, "dashboard/admin_upload_questions.html", context)

    upload_mode = request.POST.get("upload_mode", "manual").strip()
    rows_to_insert = []

    try:
        if upload_mode == "manual":
            programme = request.POST.get("programme", "").strip()
            paper_title = request.POST.get("paper_title", "").strip()
            question_text = request.POST.get("question_text", "").strip()
            explanation = request.POST.get("explanation", "").strip()
            correct_option = request.POST.get("correct_option", "").strip().upper()

            options = {
                "A": request.POST.get("option_a", "").strip(),
                "B": request.POST.get("option_b", "").strip(),
                "C": request.POST.get("option_c", "").strip(),
            }
            options = {k: v for k, v in options.items() if v}

            normalized = _normalize_question_payload({
                "programme": programme,
                "paper_title": paper_title,
                "question_text": question_text,
                "options": options,
                "correct_option": correct_option,
                "explanation": explanation,
            })

            for target_programme in _programmes_for_paper(normalized["programme"], normalized["paper_title"]):
                rows_to_insert.append({
                    "programme": target_programme,
                    "paper_title": normalized["paper_title"],
                    "question_text": normalized["question_text"],
                    "options": normalized["options"],
                    "correct_option": normalized["correct_option"],
                    "explanation": normalized["explanation"],
                    "uploaded_by": request.session.get("user_id"),
                    "source_type": "manual",
                })

        elif upload_mode == "json":
            json_payload = request.POST.get("json_payload", "").strip()
            if not json_payload:
                raise ValueError("JSON payload is required for bulk upload.")

            parsed = json.loads(json_payload)
            if isinstance(parsed, dict):
                parsed_items = parsed.get("questions")
            else:
                parsed_items = parsed

            if not isinstance(parsed_items, list) or not parsed_items:
                raise ValueError("JSON must be an array of question objects or an object with a 'questions' array.")

            for index, item in enumerate(parsed_items, start=1):
                try:
                    normalized = _normalize_question_payload(item)
                except ValueError as exc:
                    raise ValueError(f"Question #{index}: {exc}") from exc

                for target_programme in _programmes_for_paper(normalized["programme"], normalized["paper_title"]):
                    rows_to_insert.append({
                        "programme": target_programme,
                        "paper_title": normalized["paper_title"],
                        "question_text": normalized["question_text"],
                        "options": normalized["options"],
                        "correct_option": normalized["correct_option"],
                        "explanation": normalized["explanation"],
                        "uploaded_by": request.session.get("user_id"),
                        "source_type": "json",
                    })
        else:
            raise ValueError("Invalid upload mode selected.")

        db = _supabase_admin()
        unique_rows, skipped_texts = _filter_duplicate_questions(db, rows_to_insert)

        if skipped_texts:
            skip_summary = "; ".join(
                f'"{s[:60]}…" ({reason})' if len(s) > 60 else f'"{s}" ({reason})'
                for s, reason in skipped_texts
            )
            context["warning"] = (
                f"Skipped {len(skipped_texts)} duplicate question(s): {skip_summary}"
            )

        if not unique_rows:
            raise ValueError(
                "No new questions to save — all submitted question(s) are duplicates "
                "of existing records (matched by programme + paper + question text)."
            )

        db.table("question_bank").insert(unique_rows).execute()
        saved_msg = f"Upload successful. Saved {len(unique_rows)} question record(s)."
        if skipped_texts:
            saved_msg += f" ({len(skipped_texts)} duplicate(s) skipped.)"
        context["success"] = saved_msg
    except Exception as exc:
        context["error"] = str(exc)
        context["form_data"] = {**EMPTY_QUESTION_FORM, **request.POST.dict()}

    return render(request, "dashboard/admin_upload_questions.html", context)


def admin_nclex_questions(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "admin_nclex",
        "question_types": NCLEX_QUESTION_TYPES,
        "nclex_json_sample": NCLEX_JSON_SAMPLE,
        "sample_formats_text": (
            "1) Multiple-Choice Questions (MCQs): one correct answer.\n"
            "2) Select-All-That-Apply (SATA): more than one correct answer.\n"
            "3) Fill-in-the-Blank (Calculation): numeric/text response.\n"
            "4) Drag-and-Drop / Ordered Response: place steps in sequence."
        ),
        "questions": [],
        "form_data": {
            "question_type": "mcq",
            "question_text": "",
            "options_raw": "",
            "correct_answers_raw": "",
            "rationale": "",
            "difficulty": "medium",
            "display_order": "0",
            "json_payload": "",
        },
        "book_form": {
            "book_id": "",
            "category": "nclex",
            "title": "",
            "description": "",
            "external_url": "",
        },
        "book_links": [],
        "total_uploaded_count": 0,
        "pagination": None,
    }
    try:
        page = int((request.GET.get("page") or "1").strip())
    except Exception:
        page = 1
    page = max(1, page)
    page_size = 50

    try:
        if request.method == "POST":
            action = (request.POST.get("action") or "").strip()
            if action == "upload_json":
                payload = (request.POST.get("json_payload") or "").strip()
                parsed = _parse_lenient_json_payload(payload)
                items = parsed.get("questions") if isinstance(parsed, dict) else parsed
                if not isinstance(items, list) or not items:
                    raise ValueError("JSON must be an array or an object with a 'questions' array.")

                rows = []
                for idx, item in enumerate(items, start=1):
                    try:
                        normalized = _normalize_nclex_question_payload(item)
                    except ValueError as exc:
                        raise ValueError(f"Question #{idx}: {exc}") from exc
                    normalized["created_by"] = request.session.get("user_id")
                    rows.append(normalized)
                unique_rows, skipped = _filter_duplicate_nclex_questions(admin, rows)
                if not unique_rows:
                    raise ValueError("No new NCLEX questions saved: all submitted items are duplicates.")
                admin.table("nclex_questions").insert(unique_rows).execute()
                context["success"] = f"Uploaded {len(unique_rows)} NCLEX question(s)."
                if skipped:
                    context["warning"] = f"Skipped {len(skipped)} duplicate question(s)."
                # Clear JSON upload field after successful upload.
                context["form_data"]["json_payload"] = ""

            elif action == "create_one":
                options_raw = (request.POST.get("options_raw") or "").strip()
                answers_raw = (request.POST.get("correct_answers_raw") or "").strip()
                options_list = [line.strip() for line in options_raw.splitlines() if line.strip()]
                answers_list = [line.strip() for line in answers_raw.splitlines() if line.strip()]
                normalized = _normalize_nclex_question_payload(
                    {
                        "question_type": request.POST.get("question_type"),
                        "question_text": request.POST.get("question_text"),
                        "options": options_list,
                        "correct_answers": answers_list,
                        "rationale": request.POST.get("rationale"),
                        "difficulty": request.POST.get("difficulty"),
                        "display_order": request.POST.get("display_order"),
                        "is_active": True,
                    }
                )
                normalized["created_by"] = request.session.get("user_id")
                unique_rows, skipped = _filter_duplicate_nclex_questions(admin, [normalized])
                if not unique_rows:
                    reason = skipped[0][1] if skipped else "duplicate"
                    raise ValueError(f"This NCLEX question was not created: {reason}.")
                admin.table("nclex_questions").insert(unique_rows[0]).execute()
                context["success"] = "NCLEX question created."

            elif action == "update_one":
                question_id = (request.POST.get("question_id") or "").strip()
                if not question_id:
                    raise ValueError("question_id is required.")
                options_list = [line.strip() for line in (request.POST.get("options_raw") or "").splitlines() if line.strip()]
                answers_list = [line.strip() for line in (request.POST.get("correct_answers_raw") or "").splitlines() if line.strip()]
                normalized = _normalize_nclex_question_payload(
                    {
                        "question_type": request.POST.get("question_type"),
                        "question_text": request.POST.get("question_text"),
                        "options": options_list,
                        "correct_answers": answers_list,
                        "rationale": request.POST.get("rationale"),
                        "difficulty": request.POST.get("difficulty"),
                        "display_order": request.POST.get("display_order"),
                        "is_active": (request.POST.get("is_active") == "true"),
                    }
                )
                unique_rows, skipped = _filter_duplicate_nclex_questions(admin, [normalized], exclude_ids=[question_id])
                if not unique_rows:
                    reason = skipped[0][1] if skipped else "duplicate"
                    raise ValueError(f"Update blocked: {reason}.")
                admin.table("nclex_questions").update(normalized).eq("id", question_id).execute()
                context["success"] = "NCLEX question updated."

            elif action == "delete_one":
                question_id = (request.POST.get("question_id") or "").strip()
                if not question_id:
                    raise ValueError("question_id is required.")
                admin.table("nclex_questions").delete().eq("id", question_id).execute()
                context["success"] = "NCLEX question deleted."

            elif action == "toggle_active":
                question_id = (request.POST.get("question_id") or "").strip()
                next_state = (request.POST.get("next_state") or "false").strip().lower() == "true"
                admin.table("nclex_questions").update({"is_active": next_state}).eq("id", question_id).execute()
                context["success"] = "NCLEX question status updated."
            elif action == "create_book":
                category = (request.POST.get("book_category") or "nclex").strip().lower()
                if category not in {"nclex", "ielts"}:
                    raise ValueError("Book category must be NCLEX or IELTS.")
                title = (request.POST.get("book_title") or "").strip()
                external_url = (request.POST.get("book_url") or "").strip()
                description = (request.POST.get("book_description") or "").strip()
                if not title:
                    raise ValueError("Book title is required.")
                if not external_url:
                    raise ValueError("Book URL is required.")

                # Duplicate guard: same category + normalized title OR exact URL
                existing = (
                    admin.table("resource_books")
                    .select("id, category, title, external_url")
                    .limit(1000)
                    .execute()
                    .data
                    or []
                )
                new_title_key = _normalize_nclex_text(title)
                new_url_key = external_url.strip().casefold()
                for row in existing:
                    if (
                        (str(row.get("category") or "").strip().lower() == category and _normalize_nclex_text(row.get("title")) == new_title_key)
                        or str(row.get("external_url") or "").strip().casefold() == new_url_key
                    ):
                        raise ValueError("Duplicate book link detected (same title/category or URL).")

                admin.table("resource_books").insert(
                    {
                        "category": category,
                        "title": title,
                        "description": description,
                        "external_url": external_url,
                        "created_by": request.session.get("user_id"),
                        "is_active": True,
                    }
                ).execute()
                context["success"] = "Book link added."

            elif action == "update_book":
                book_id = (request.POST.get("book_id") or "").strip()
                if not book_id:
                    raise ValueError("book_id is required.")
                category = (request.POST.get("book_category") or "nclex").strip().lower()
                if category not in {"nclex", "ielts"}:
                    raise ValueError("Book category must be NCLEX or IELTS.")
                title = (request.POST.get("book_title") or "").strip()
                external_url = (request.POST.get("book_url") or "").strip()
                description = (request.POST.get("book_description") or "").strip()
                if not title:
                    raise ValueError("Book title is required.")
                if not external_url:
                    raise ValueError("Book URL is required.")

                existing = (
                    admin.table("resource_books")
                    .select("id, category, title, external_url")
                    .limit(1000)
                    .execute()
                    .data
                    or []
                )
                new_title_key = _normalize_nclex_text(title)
                new_url_key = external_url.strip().casefold()
                for row in existing:
                    rid = str(row.get("id") or "")
                    if rid == book_id:
                        continue
                    if (
                        (str(row.get("category") or "").strip().lower() == category and _normalize_nclex_text(row.get("title")) == new_title_key)
                        or str(row.get("external_url") or "").strip().casefold() == new_url_key
                    ):
                        raise ValueError("Update blocked: duplicate book link detected.")

                admin.table("resource_books").update(
                    {
                        "category": category,
                        "title": title,
                        "description": description,
                        "external_url": external_url,
                    }
                ).eq("id", book_id).execute()
                context["success"] = "Book link updated."

            elif action == "delete_book":
                book_id = (request.POST.get("book_id") or "").strip()
                if not book_id:
                    raise ValueError("book_id is required.")
                admin.table("resource_books").delete().eq("id", book_id).execute()
                context["success"] = "Book link deleted."

            context["form_data"] = {
                **context["form_data"],
                **request.POST.dict(),
            }
            # Keep JSON field cleared after successful JSON upload.
            if action == "upload_json" and context.get("success"):
                context["form_data"]["json_payload"] = ""
            context["book_form"] = {
                "book_id": request.POST.get("book_id", "").strip(),
                "category": request.POST.get("book_category", "nclex").strip().lower() or "nclex",
                "title": request.POST.get("book_title", "").strip(),
                "description": request.POST.get("book_description", "").strip(),
                "external_url": request.POST.get("book_url", "").strip(),
            }
    except Exception as exc:
        context["error"] = str(exc)
        context["form_data"] = {**context["form_data"], **request.POST.dict()}
        context["book_form"] = {
            "book_id": request.POST.get("book_id", "").strip(),
            "category": request.POST.get("book_category", "nclex").strip().lower() or "nclex",
            "title": request.POST.get("book_title", "").strip(),
            "description": request.POST.get("book_description", "").strip(),
            "external_url": request.POST.get("book_url", "").strip(),
        }

    try:
        count_resp = (
            admin.table("nclex_questions")
            .select("id", count="exact")
            .limit(1)
            .execute()
        )
        total_uploaded_count = int(count_resp.count or 0)
        total_pages = max(1, math.ceil(total_uploaded_count / page_size)) if total_uploaded_count else 1
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        end = offset + page_size - 1
        rows = (
            admin.table("nclex_questions")
            .select("*")
            .order("display_order", desc=False)
            .order("created_at", desc=True)
            .range(offset, end)
            .execute()
            .data
            or []
        )
        page_window_start = max(1, page - 2)
        page_window_end = min(total_pages, page + 2)
        context["total_uploaded_count"] = total_uploaded_count
        context["pagination"] = {
            "page": page,
            "page_size": page_size,
            "total_count": total_uploaded_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": max(1, page - 1),
            "next_page": min(total_pages, page + 1),
            "start_index": (offset + 1) if total_uploaded_count else 0,
            "end_index": min(offset + len(rows), total_uploaded_count),
            "page_numbers": list(range(page_window_start, page_window_end + 1)),
        }
    except Exception:
        rows = []
        if not context.get("error"):
            context["error"] = "Could not load NCLEX questions. Ensure table nclex_questions exists in Supabase."

    for row in rows:
        row["question_type_label"] = NCLEX_QUESTION_TYPES.get(row.get("question_type"), row.get("question_type", "Unknown"))
        row["options_text"] = "\n".join(row.get("options") or [])
        row["correct_answers_text"] = "\n".join(row.get("correct_answers") or [])
    context["questions"] = rows
    try:
        book_rows = (
            admin.table("resource_books")
            .select("*")
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
            .data
            or []
        )
    except Exception:
        book_rows = []
    for row in book_rows:
        row["category"] = (row.get("category") or "").strip().lower()
    context["book_links"] = book_rows
    return render(request, "dashboard/admin_nclex_questions.html", context)


def admin_manage_questions(request):
    guard = _require_admin(request)
    if guard:
        return guard

    query      = request.GET.get("q", "").strip()
    filter_prog  = request.GET.get("programme", "").strip()
    filter_paper = request.GET.get("paper_title", "").strip()
    edit_id    = request.GET.get("edit", "").strip()
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "manage_questions",
        "programme_papers": PROGRAMME_PAPERS,
        "programme_papers_json": json.dumps(PROGRAMME_PAPERS),
        "programmes": PROGRAMME_NAMES,
        "all_papers": ALL_PAPERS,
        "mock_question_batch_size": MOCK_QUESTION_BATCH_SIZE,
        "manage_questions_max_fetch": MANAGE_QUESTIONS_MAX_FETCH,
        "query": query,
        "filter_prog": filter_prog,
        "filter_paper": filter_paper,
        "questions": [],
        "question_bank_count": 0,
        "question_list_shown": 0,
        "question_list_total_logical": 0,
        "pagination": None,
        "form_data": EMPTY_QUESTION_FORM.copy(),
    }

    admin = _supabase_admin()

    try:
        if request.method == "POST":
            action = request.POST.get("action", "").strip()

            if action == "create":
                payload = _normalize_question_payload({
                    "programme": request.POST.get("programme", "").strip(),
                    "paper_title": request.POST.get("paper_title", "").strip(),
                    "question_text": request.POST.get("question_text", "").strip(),
                    "options": {
                        "A": request.POST.get("option_a", "").strip(),
                        "B": request.POST.get("option_b", "").strip(),
                        "C": request.POST.get("option_c", "").strip(),
                    },
                    "correct_option": request.POST.get("correct_option", "").strip().upper(),
                    "explanation": request.POST.get("explanation", "").strip(),
                })

                rows_to_insert = []
                for target_programme in _programmes_for_paper(payload["programme"], payload["paper_title"]):
                    rows_to_insert.append({
                        "programme": target_programme,
                        "paper_title": payload["paper_title"],
                        "question_text": payload["question_text"],
                        "options": payload["options"],
                        "correct_option": payload["correct_option"],
                        "explanation": payload["explanation"],
                        "uploaded_by": request.session.get("user_id"),
                        "source_type": "manual",
                    })
                unique_rows, skipped_texts = _filter_duplicate_questions(admin, rows_to_insert)
                if not unique_rows:
                    raise ValueError(
                        "No new question to create — this question already exists for the selected paper/programme."
                    )
                admin.table("question_bank").insert(unique_rows).execute()
                context["success"] = f"Created {len(unique_rows)} question record(s)."
                if skipped_texts:
                    context["warning"] = (
                        f"Skipped {len(skipped_texts)} duplicate row(s) while creating this question."
                    )

            elif action == "update":
                question_id = request.POST.get("question_id", "").strip()
                if not question_id:
                    raise ValueError("Question ID is required for update.")

                existing_rows = (
                    admin.table("question_bank")
                    .select("id, programme, paper_title, question_text")
                    .eq("id", question_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                existing_row = existing_rows[0] if existing_rows else None
                if not existing_row:
                    raise ValueError("Question not found for update.")

                payload = _normalize_question_payload({
                    "programme": request.POST.get("programme", "").strip(),
                    "paper_title": request.POST.get("paper_title", "").strip(),
                    "question_text": request.POST.get("question_text", "").strip(),
                    "options": {
                        "A": request.POST.get("option_a", "").strip(),
                        "B": request.POST.get("option_b", "").strip(),
                        "C": request.POST.get("option_c", "").strip(),
                    },
                    "correct_option": request.POST.get("correct_option", "").strip().upper(),
                    "explanation": request.POST.get("explanation", "").strip(),
                })

                old_pt = existing_row.get("paper_title") or ""
                old_qt = (existing_row.get("question_text") or "").strip()
                if _is_general_paper(old_pt) and not _is_general_paper(payload["paper_title"]):
                    raise ValueError(
                        "This item is shared across all programmes as General Paper. "
                        "Keep paper as General Paper, or delete it and create a programme-specific question."
                    )

                update_body = {
                    "paper_title": payload["paper_title"],
                    "question_text": payload["question_text"],
                    "options": payload["options"],
                    "correct_option": payload["correct_option"],
                    "explanation": payload["explanation"],
                }

                if _is_general_paper(old_pt):
                    sibling_ids = _general_paper_row_ids_for_same_question(admin, old_qt)
                    if not sibling_ids:
                        sibling_ids = [question_id]
                    elif question_id not in sibling_ids:
                        sibling_ids = sibling_ids + [question_id]
                    for target_programme in PROGRAMME_NAMES:
                        if _has_duplicate_question(
                            admin,
                            target_programme,
                            payload["paper_title"],
                            payload["question_text"],
                            exclude_ids=sibling_ids,
                        ):
                            raise ValueError(
                                "Update would create a duplicate General Paper question for one or more programmes."
                            )
                    admin.table("question_bank").update(update_body).in_("id", sibling_ids).execute()
                    n = len(sibling_ids)
                    context["success"] = (
                        f"Question updated for all programmes ({n} copies)."
                        if n > 1
                        else "Question updated successfully."
                    )
                else:
                    target_programme = payload["programme"] or existing_row.get("programme", "")
                    if _has_duplicate_question(
                        admin,
                        target_programme,
                        payload["paper_title"],
                        payload["question_text"],
                        exclude_ids=[question_id],
                    ):
                        raise ValueError(
                            "Update would create a duplicate question for this programme and paper."
                        )
                    admin.table("question_bank").update({
                        **update_body,
                        "programme": target_programme,
                    }).eq("id", question_id).execute()
                    context["success"] = "Question updated successfully."

            elif action == "delete":
                question_id = request.POST.get("question_id", "").strip()
                if not question_id:
                    raise ValueError("Question ID is required for delete.")
                del_rows = (
                    admin.table("question_bank")
                    .select("id, paper_title, question_text")
                    .eq("id", question_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                del_row = del_rows[0] if del_rows else None
                if del_row and _is_general_paper(del_row.get("paper_title")):
                    ids = _general_paper_row_ids_for_same_question(
                        admin, del_row.get("question_text") or ""
                    )
                    if ids:
                        admin.table("question_bank").delete().in_("id", ids).execute()
                        context["success"] = (
                            f"Deleted {len(ids)} programme copies of this General Paper question."
                            if len(ids) > 1
                            else "Question deleted successfully."
                        )
                    else:
                        admin.table("question_bank").delete().eq("id", question_id).execute()
                        context["success"] = "Question deleted successfully."
                else:
                    admin.table("question_bank").delete().eq("id", question_id).execute()
                    context["success"] = "Question deleted successfully."
                if edit_id == question_id:
                    edit_id = ""
            else:
                raise ValueError("Invalid action.")

        # Exact row count for current filters, then fetch up to MANAGE_QUESTIONS_MAX_FETCH rows
        # using chunked range() requests (avoids PostgREST single-response row caps).
        escaped = query.replace("%", "").replace(",", " ").strip() if query else ""
        count_query = admin.table("question_bank").select("id", count="exact", head=True)

        if escaped:
            filt = (
                f"question_text.ilike.%{escaped}%,programme.ilike.%{escaped}%,"
                f"paper_title.ilike.%{escaped}%"
            )
            count_query = count_query.or_(filt)

        if filter_prog == "__mock__":
            count_query = count_query.eq("programme", GLOBAL_MOCK_PROGRAMME)
        elif filter_prog == "__non_mock__":
            count_query = count_query.neq("programme", GLOBAL_MOCK_PROGRAMME)
        elif filter_prog:
            count_query = count_query.eq("programme", filter_prog)

        if filter_paper:
            count_query = count_query.eq("paper_title", filter_paper)
        try:
            context["question_bank_count"] = count_query.execute().count or 0
        except Exception:
            context["question_bank_count"] = 0

        use_split_list = not filter_prog and not escaped
        if use_split_list:
            questions_raw = _mq_merge_mock_nonmock_rows(
                admin, escaped, filter_paper, MANAGE_QUESTIONS_MAX_FETCH
            )
        else:
            questions_raw = _mq_fetch_question_rows(
                admin, escaped, filter_prog, filter_paper, MANAGE_QUESTIONS_MAX_FETCH
            )
        qb_count = context["question_bank_count"]
        list_incomplete = qb_count > len(questions_raw) or (
            len(questions_raw) >= MANAGE_QUESTIONS_MAX_FETCH
            and qb_count > MANAGE_QUESTIONS_MAX_FETCH
        )
        questions_deduped = _dedupe_general_paper_rows_for_admin_list(questions_raw)
        total_logical = len(questions_deduped)
        context["question_list_total_logical"] = total_logical

        per_page = MANAGE_QUESTIONS_PER_PAGE
        num_pages = max(1, (total_logical + per_page - 1) // per_page) if total_logical else 1
        page_clamped = min(page, num_pages)
        start = (page_clamped - 1) * per_page
        questions = questions_deduped[start : start + per_page]
        context["questions"] = questions
        context["question_list_shown"] = len(questions)

        def _mq_url(page_num=1):
            params = {}
            if page_num and page_num > 1:
                params["page"] = page_num
            if query:
                params["q"] = query
            if filter_prog:
                params["programme"] = filter_prog
            if filter_paper:
                params["paper_title"] = filter_paper
            return "/admin-panel/manage-questions/" + ("?" + urlencode(params) if params else "")

        def _manage_q_pagelist(cur, total, radius=2):
            if total <= 0:
                return []
            if total <= 7:
                return list(range(1, total + 1))
            nums = set()
            for n in range(1, total + 1):
                if n <= 2 or n > total - 2 or abs(n - cur) <= radius:
                    nums.add(n)
            ordered = sorted(nums)
            out = []
            prev = None
            for n in ordered:
                if prev is not None and n > prev + 1:
                    out.append(None)
                out.append(n)
                prev = n
            return out

        nav_pages = _manage_q_pagelist(page_clamped, num_pages)
        nav_items = []
        for item in nav_pages:
            if item is None:
                nav_items.append({"ellipsis": True})
            else:
                nav_items.append(
                    {"n": item, "url": _mq_url(item), "current": item == page_clamped}
                )

        context["pagination"] = {
            "page": page_clamped,
            "num_pages": num_pages,
            "per_page": per_page,
            "total_logical": total_logical,
            "has_prev": page_clamped > 1,
            "has_next": page_clamped < num_pages,
            "prev_url": _mq_url(page_clamped - 1),
            "next_url": _mq_url(page_clamped + 1),
            "cancel_url": _mq_url(page_clamped),
            "nav_items": nav_items,
            "truncated_fetch": list_incomplete,
            "list_incomplete": list_incomplete,
            "raw_rows_loaded": len(questions_raw),
            "db_rows_matching": qb_count,
            "max_fetch": MANAGE_QUESTIONS_MAX_FETCH,
        }

        if edit_id:
            fetch = admin.table("question_bank").select("*").eq("id", edit_id).limit(1).execute().data or []
            ei = fetch[0] if fetch else None
            if ei and _is_general_paper(ei.get("paper_title")):
                n_copies = len(_general_paper_row_ids_for_same_question(admin, ei.get("question_text") or ""))
                ei = {
                    **ei,
                    "general_paper_copy_count": n_copies,
                    "programme": "",
                }
            context["edit_item"] = ei

    except Exception as exc:
        context["error"] = str(exc)
        context["form_data"] = {**EMPTY_QUESTION_FORM, **request.POST.dict()}

    # ── Question bank stats (exact counts + chunked reads; not limited to ~1000 rows) ──
    try:
        context.update(_mq_question_bank_stats(admin))
    except Exception:
        context["stats_db_total_rows"] = 0
        context["stats_non_mock_rows"] = 0
        context["stats_general_paper_rows"] = 0
        context["stats_mock_count"] = 0
        context["stats_mock_batches"] = 0
        context["stats_mock_remainder"] = 0
        context["stats_general_count"] = 0
        context["stats_prog_specific"] = []
        context["stats_unique_total"] = 0
        context["stats_prog_specific_row_sum"] = 0
        context["stats_paper_by_programme"] = []

    return render(request, "dashboard/admin_manage_questions.html", context)


# ---------------------------------------------------------------------------
# Mock Exams
# ---------------------------------------------------------------------------

MOCK_POOL_PAGE_SIZE = 40


def _delete_mock_pool_questions(admin_client):
    """
    Delete all questions from the mock pool (programme = GLOBAL_MOCK_PROGRAMME)
    together with every child record that references them, so FK constraints
    don't block the delete.
    """
    # 1. Collect every question ID in the pool.
    rows = (
        admin_client.table("question_bank")
        .select("id")
        .eq("programme", GLOBAL_MOCK_PROGRAMME)
        .execute()
        .data
        or []
    )
    qids = [r["id"] for r in rows if r.get("id")]
    if not qids:
        return 0

    # 2. Remove child FK rows in batches of 100 (PostgREST IN limit).
    for i in range(0, len(qids), 100):
        batch = qids[i:i + 100]
        admin_client.table("mock_attempt_answers").delete().in_("question_id", batch).execute()
        admin_client.table("mock_attempt_questions").delete().in_("question_id", batch).execute()

    # 3. Now safe to delete the questions themselves.
    admin_client.table("question_bank").delete().eq("programme", GLOBAL_MOCK_PROGRAMME).execute()
    return len(qids)


def _delete_mock_exam_cascade(admin_client, exam_id):
    exam_id = str(exam_id).strip()
    if not exam_id:
        raise ValueError("Exam id is required.")
    attempts = (
        admin_client.table("mock_attempts")
        .select("id")
        .eq("mock_exam_id", exam_id)
        .execute()
        .data
        or []
    )
    for att in attempts:
        aid = att.get("id")
        if not aid:
            continue
        admin_client.table("mock_attempt_answers").delete().eq("attempt_id", aid).execute()
        admin_client.table("mock_attempt_questions").delete().eq("attempt_id", aid).execute()
    admin_client.table("mock_attempts").delete().eq("mock_exam_id", exam_id).execute()
    admin_client.table("mock_exams").delete().eq("id", exam_id).execute()


def _assert_mock_pool_question(admin_client, question_id):
    rows = (
        admin_client.table("question_bank")
        .select("id, programme")
        .eq("id", str(question_id))
        .limit(1)
        .execute()
        .data
        or []
    )
    row = rows[0] if rows else None
    if not row or row.get("programme") != GLOBAL_MOCK_PROGRAMME:
        raise ValueError("Question not found or not part of the mock exam pool.")
    return row


def _mock_pool_question_payload_from_post(request):
    question_text = request.POST.get("question_text", "").strip()
    option_a = request.POST.get("option_a", "").strip()
    option_b = request.POST.get("option_b", "").strip()
    option_c = request.POST.get("option_c", "").strip()
    correct_option = request.POST.get("correct_option", "").strip().upper()
    explanation = request.POST.get("explanation", "").strip()
    paper_title = (request.POST.get("paper_title") or "Mock Paper").strip() or "Mock Paper"
    if not question_text:
        raise ValueError("Question text is required.")
    options = {}
    if option_a:
        options["A"] = option_a
    if option_b:
        options["B"] = option_b
    if option_c:
        options["C"] = option_c
    if len(options) < 2:
        raise ValueError("At least two options (A, B, or C) are required.")
    if correct_option not in options:
        raise ValueError("Correct option must match one of the provided options.")
    return {
        "programme": GLOBAL_MOCK_PROGRAMME,
        "paper_title": paper_title,
        "question_text": question_text,
        "options": options,
        "correct_option": correct_option,
        "explanation": explanation,
    }


def admin_mock_exams(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    try:
        mq_page = int(request.GET.get("mq_page", "1"))
    except ValueError:
        mq_page = 1
    mq_page = max(1, mq_page)
    mq_edit = request.GET.get("mq_edit", "").strip()
    mq_slice_for = request.GET.get("mq_slice_for", "").strip()

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "mock_exams",
        "mock_pool_questions": [],
        "mock_pool_total": 0,
        "mock_pool_page": mq_page,
        "mock_pool_num_pages": 1,
        "mock_pool_prev_page": None,
        "mock_pool_next_page": None,
        "mock_pool_page_size": MOCK_POOL_PAGE_SIZE,
        "edit_mock_question": None,
        "mock_question_batch": MOCK_QUESTION_BATCH_SIZE,
        "mock_duration_minutes": MOCK_DURATION_MINUTES,
        "mock_pool_slice_mock_number": None,
    }

    try:
        if request.method == "POST":
            action = request.POST.get("action", "").strip()
            if action in {"generate", "upload_json"}:
                programme = GLOBAL_MOCK_PROGRAMME

                if action == "upload_json":
                    json_payload = request.POST.get("json_payload", "").strip()
                    if not json_payload:
                        raise ValueError("JSON payload is required.")
                    parsed = json.loads(json_payload)
                    parsed_items = parsed.get("questions") if isinstance(parsed, dict) else parsed
                    if not isinstance(parsed_items, list) or not parsed_items:
                        raise ValueError("JSON must be an array, or an object with a 'questions' array.")

                    rows_to_insert = []
                    for index, item in enumerate(parsed_items, start=1):
                        if not isinstance(item, dict):
                            raise ValueError(f"Question #{index} must be an object.")
                        question_text = (item.get("question") or item.get("question_text") or "").strip()
                        options = item.get("options") or {}
                        correct_option = (item.get("correct_option") or item.get("answer") or "").strip().upper()
                        explanation = (item.get("explanation") or "").strip()

                        if not question_text:
                            raise ValueError(f"Question #{index}: question text is required.")
                        if not isinstance(options, dict):
                            raise ValueError(f"Question #{index}: options must be an object with A/B/C keys.")

                        cleaned_options = {}
                        for key, value in options.items():
                            k = str(key).strip().upper()
                            v = str(value).strip()
                            if k in {"A", "B", "C"} and v:
                                cleaned_options[k] = v
                        if len(cleaned_options) < 2:
                            raise ValueError(f"Question #{index}: at least 2 options are required.")
                        if correct_option not in cleaned_options:
                            raise ValueError(f"Question #{index}: answer/correct_option must match one of A/B/C options.")

                        rows_to_insert.append({
                            "programme": GLOBAL_MOCK_PROGRAMME,
                            "paper_title": "Mock Paper",
                            "question_text": question_text,
                            "options": cleaned_options,
                            "correct_option": correct_option,
                            "explanation": explanation,
                            "uploaded_by": request.session.get("user_id"),
                            "source_type": "mock_json",
                        })

                    force = request.POST.get("force_upload") == "1"

                    if force:
                        # Wipe the existing mock pool (with FK cascade), then insert fresh.
                        _delete_mock_pool_questions(admin)
                        to_insert = rows_to_insert
                        skipped = []
                    else:
                        to_insert, skipped = _filter_duplicate_questions(admin, rows_to_insert)
                        if skipped:
                            context["warning"] = (
                                f"{len(skipped)} duplicate question(s) were skipped. "
                                f"Tick 'Force re-upload' to wipe the pool and re-upload everything."
                            )
                        if not to_insert:
                            raise ValueError(
                                "All questions already exist in the mock pool. "
                                "Tick 'Force re-upload' below the textarea to replace them."
                            )

                    admin.table("question_bank").insert(to_insert).execute()

                    # Auto-generate any new mock slots from the updated pool.
                    total_q = admin.table("question_bank").select("id", count="exact", head=True).eq("programme", GLOBAL_MOCK_PROGRAMME).execute().count or 0
                    possible = total_q // MOCK_QUESTION_BATCH_SIZE
                    existing_mocks = admin.table("mock_exams").select("id").eq("programme", GLOBAL_MOCK_PROGRAMME).execute().data or []
                    existing_count = len(existing_mocks)
                    new_mocks = max(0, possible - existing_count)
                    for batch_index in range(existing_count + 1, existing_count + new_mocks + 1):
                        admin.table("mock_exams").insert({
                            "title": f"Mock {batch_index}",
                            "programme": GLOBAL_MOCK_PROGRAMME,
                            "mock_number": batch_index,
                            "question_count": MOCK_QUESTION_BATCH_SIZE,
                            "duration_minutes": MOCK_DURATION_MINUTES,
                            "is_published": True,
                            "created_by": request.session.get("user_id"),
                        }).execute()

                    msg = f"{'Force-uploaded' if force else 'Uploaded'} {len(to_insert)} question(s) to the mock pool."
                    if new_mocks:
                        msg += f" Auto-created {new_mocks} new mock exam(s)."
                    context["success"] = msg

                elif action == "generate":
                    count_resp = (
                        admin.table("question_bank")
                        .select("id", count="exact", head=True)
                        .eq("programme", GLOBAL_MOCK_PROGRAMME)
                        .execute()
                    )
                    total_questions = count_resp.count or 0
                    possible_batches = total_questions // MOCK_QUESTION_BATCH_SIZE
                    if possible_batches <= 0:
                        raise ValueError("Not enough questions for this programme. Need at least 60 questions.")

                    existing = (
                        admin.table("mock_exams")
                        .select("id")
                        .eq("programme", GLOBAL_MOCK_PROGRAMME)
                        .order("mock_number", desc=False)
                        .execute()
                        .data
                        or []
                    )
                    existing_count = len(existing)
                    create_count = max(0, possible_batches - existing_count)
                    if create_count <= 0:
                        raise ValueError("All possible mocks for this programme are already created.")

                    for batch_index in range(existing_count + 1, existing_count + create_count + 1):
                        admin.table("mock_exams").insert({
                            "title": f"Mock {batch_index}",
                            "programme": GLOBAL_MOCK_PROGRAMME,
                            "mock_number": batch_index,
                            "question_count": MOCK_QUESTION_BATCH_SIZE,
                            "duration_minutes": MOCK_DURATION_MINUTES,
                            "is_published": True,
                            "created_by": request.session.get("user_id"),
                        }).execute()

                    context["success"] = f"Created {create_count} mock exam(s)."

            elif action == "clear_mock_pool":
                deleted = _delete_mock_pool_questions(admin)
                context["success"] = f"Mock pool cleared — {deleted} question(s) deleted (including linked attempt records). You can now upload a fresh set."

            elif action == "toggle_publish":
                exam_id = request.POST.get("exam_id", "").strip()
                publish_to = request.POST.get("publish_to", "true").strip().lower() == "true"
                admin.table("mock_exams").update({"is_published": publish_to}).eq("id", exam_id).execute()
                context["success"] = "Mock exam publish status updated."

            elif action == "delete_exam":
                exam_id = request.POST.get("exam_id", "").strip()
                if not exam_id:
                    raise ValueError("Exam id is required.")
                _delete_mock_exam_cascade(admin, exam_id)
                context["success"] = "Mock exam and related attempts were deleted."

            elif action == "delete_mock_question":
                qid = request.POST.get("question_id", "").strip()
                if not qid:
                    raise ValueError("Question id is required.")
                _assert_mock_pool_question(admin, qid)
                try:
                    admin.table("question_bank").delete().eq("id", qid).execute()
                except Exception as del_exc:
                    raise ValueError(
                        "Could not delete this question. It may still be linked to student mock attempts."
                    ) from del_exc
                context["success"] = "Mock pool question deleted."

            elif action == "create_mock_question":
                body = _mock_pool_question_payload_from_post(request)
                body["uploaded_by"] = request.session.get("user_id")
                body["source_type"] = "mock_admin"
                admin.table("question_bank").insert(body).execute()
                context["success"] = "Mock pool question created."

            elif action == "update_mock_question":
                qid = request.POST.get("question_id", "").strip()
                if not qid:
                    raise ValueError("Question id is required.")
                _assert_mock_pool_question(admin, qid)
                body = _mock_pool_question_payload_from_post(request)
                admin.table("question_bank").update(body).eq("id", qid).execute()
                context["success"] = "Mock pool question updated."

            else:
                raise ValueError("Invalid action.")
    except Exception as exc:
        context["error"] = str(exc)

    try:
        exams = (
            admin.table("mock_exams")
            .select("*")
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .order("mock_number", desc=False)
            .execute()
            .data
            or []
        )
    except Exception:
        exams = []

    try:
        cnt_resp = (
            admin.table("question_bank")
            .select("id", count="exact", head=True)
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .execute()
        )
        pool_total = cnt_resp.count or 0
        context["mock_pool_total"] = pool_total

        slice_mock_num = None
        if mq_slice_for:
            try:
                slice_mock_num = int(mq_slice_for)
            except ValueError:
                slice_mock_num = None
        if slice_mock_num is not None:
            exam_match = next(
                (e for e in exams if int(e.get("mock_number") or 0) == slice_mock_num),
                None,
            )
            needed = (
                int(exam_match.get("question_count") or MOCK_QUESTION_BATCH_SIZE)
                if exam_match
                else MOCK_QUESTION_BATCH_SIZE
            )
            range_start = (slice_mock_num - 1) * needed
            range_end = range_start + needed - 1
            pool_rows = (
                admin.table("question_bank")
                .select("id, paper_title, question_text, correct_option, created_at")
                .eq("programme", GLOBAL_MOCK_PROGRAMME)
                .order("created_at", desc=False)
                .range(range_start, range_end)
                .execute()
                .data
                or []
            )
            context["mock_pool_questions"] = pool_rows
            context["mock_pool_page"] = 1
            context["mock_pool_num_pages"] = 1
            context["mock_pool_prev_page"] = None
            context["mock_pool_next_page"] = None
            context["mock_pool_slice_mock_number"] = slice_mock_num
        else:
            num_pages = max(1, (pool_total + MOCK_POOL_PAGE_SIZE - 1) // MOCK_POOL_PAGE_SIZE) if pool_total else 1
            page_use = min(mq_page, num_pages)
            start = (page_use - 1) * MOCK_POOL_PAGE_SIZE
            end = start + MOCK_POOL_PAGE_SIZE - 1
            pool_rows = (
                admin.table("question_bank")
                .select("id, paper_title, question_text, correct_option, created_at")
                .eq("programme", GLOBAL_MOCK_PROGRAMME)
                .order("created_at", desc=True)
                .range(start, end)
                .execute()
                .data
                or []
            )
            context["mock_pool_questions"] = pool_rows
            context["mock_pool_page"] = page_use
            context["mock_pool_num_pages"] = num_pages
            context["mock_pool_prev_page"] = page_use - 1 if page_use > 1 else None
            context["mock_pool_next_page"] = page_use + 1 if page_use < num_pages else None
    except Exception:
        pass

    _enrich_mock_exams_for_admin(exams, context.get("mock_pool_total", 0))

    if mq_edit:
        try:
            full = (
                admin.table("question_bank")
                .select("*")
                .eq("id", mq_edit)
                .limit(1)
                .execute()
                .data
                or []
            )
            row = full[0] if full else None
            if row and row.get("programme") == GLOBAL_MOCK_PROGRAMME:
                context["edit_mock_question"] = row
        except Exception:
            pass

    context["mock_exams"] = exams
    return render(request, "dashboard/admin_mock_exams.html", context)


def student_mock_exams(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/mock-exams/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    programme = GLOBAL_MOCK_PROGRAMME

    try:
        exams = (
            admin.table("mock_exams")
            .select("*")
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
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
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .execute()
            .count
            or 0
        )
    except Exception:
        pool_cnt = 0
    _enrich_mock_exams_for_admin(exams, pool_cnt)

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

    best_by_exam = {}   # exam_id -> {"pct": float, "attempt_id": str}
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

    # Build leaderboard preview per mock (top 10 + current user's rank)
    for exam in exams:
        rows, my_rank, my_row = _build_mock_leaderboard(admin, exam["id"], user_id)
        exam["leaderboard_rows"] = rows
        exam["my_rank"] = my_rank
        exam["my_row"] = my_row

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "mock_exams",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "programme": programme,
        "mock_exams": exams,
        "best_by_exam": best_by_exam,
    }
    return render(request, "dashboard/student_mock_exams.html", context)


def student_take_mock_exam(request, exam_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/mock-exams/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    exam_rows = admin.table("mock_exams").select("*").eq("id", str(exam_id)).limit(1).execute().data or []
    if not exam_rows:
        return redirect("/dashboard/mock-exams/")
    exam = exam_rows[0]
    try:
        pool_cnt = (
            admin.table("question_bank")
            .select("id", count="exact", head=True)
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .execute()
            .count
            or 0
        )
    except Exception:
        pool_cnt = 0
    _enrich_mock_exams_for_admin([exam], pool_cnt)
    if not exam.get("is_published"):
        return redirect("/dashboard/mock-exams/")

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

    # ── No retake: if a completed attempt exists, send to review ─────────────
    completed = (
        admin.table("mock_attempts")
        .select("id")
        .eq("mock_exam_id", str(exam_id))
        .eq("student_id", user_id)
        .not_.is_("submitted_at", "null")
        .order("submitted_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if completed:
        return redirect(f"/dashboard/performance/review/mock/{completed[0]['id']}/")

    # ── Pre-exam lobby ────────────────────────────────────────────────────────
    # Show the leaderboard before the timer starts.  The attempt (and therefore
    # the clock) is only created when the student explicitly clicks "Begin Exam".
    if not attempt_rows:
        if request.method == "POST" and request.POST.get("action") == "begin":
            # Student clicked Begin — create attempt now and redirect to exam.
            admin.table("mock_attempts").insert({
                "mock_exam_id": str(exam_id),
                "student_id": user_id,
                "time_limit_minutes": int(exam.get("duration_minutes") or MOCK_DURATION_MINUTES),
                "total_questions": int(exam.get("question_count") or MOCK_QUESTION_BATCH_SIZE),
            }).execute()
            return redirect(f"/dashboard/mock-exams/{exam_id}/start/")

        # Build leaderboard for the lobby page.
        lobby_leaderboard, lobby_my_rank, lobby_my_row = _build_mock_leaderboard(
            admin, str(exam_id), user_id
        )
        lobby_context = {
            "full_name": request.session.get("full_name", "Student"),
            "email": request.session.get("email", ""),
            "role": "student",
            "active_page": "mock_exams",
            "student_unread_notifications": unread_count,
            "has_unread_notifications": unread_count > 0,
            "exam": exam,
            "leaderboard_rows": lobby_leaderboard,
            "my_rank": lobby_my_rank,
            "my_row": lobby_my_row,
        }
        return render(request, "dashboard/student_mock_exam_lobby.html", lobby_context)

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
        needed = int(exam.get("question_count") or MOCK_QUESTION_BATCH_SIZE)
        mock_number = int(exam.get("mock_number") or 1)

        prof_rows = (
            admin.table("profiles")
            .select("programme")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
            .data
            or []
        )
        student_programme = (prof_rows[0].get("programme") or "").strip() if prof_rows else ""

        # Up to 15 General Paper questions (random from ≤120 GP pool) + rest from the
        # global mock bank slice for this mock_number (non-overlapping 60-ID blocks).
        pool_ids = _build_mock_exam_question_pool(
            admin,
            student_programme=student_programme,
            needed=needed,
            mock_number=mock_number,
        )
        if not pool_ids or len(pool_ids) < needed:
            return redirect("/dashboard/mock-exams/")

        pool = [{"id": qid} for qid in pool_ids]
        attempt_questions = [
            {
                "attempt_id": attempt["id"],
                "question_id": item["id"],
                "question_order": idx,
            }
            for idx, item in enumerate(pool, start=1)
        ]
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

    questions = []
    for row in links:
        qb = row.get("question_bank")
        if not qb:
            continue
        questions.append({
            "order": row.get("question_order"),
            "id": qb.get("id"),
            "question_text": qb.get("question_text"),
            "options": qb.get("options") or {},
            "correct_option": qb.get("correct_option"),
            "explanation": qb.get("explanation"),
        })

    if not questions:
        return redirect("/dashboard/mock-exams/")

    action = request.POST.get("action", "").strip()
    current_index = int(request.POST.get("current_index", "1") or "1")
    current_index = max(1, min(current_index, len(questions)))

    answers = (
        admin.table("mock_attempt_answers")
        .select("*")
        .eq("attempt_id", attempt["id"])
        .execute()
        .data
        or []
    )
    answer_map = {a["question_id"]: a for a in answers}

    if request.method == "POST":
        qid = request.POST.get("question_id", "").strip()
        selected_option = request.POST.get("selected_option", "").strip().upper()
        is_bookmarked = request.POST.get("is_bookmarked") == "1"
        is_flagged = request.POST.get("is_flagged") == "1"
        target = next((q for q in questions if q["id"] == qid), None)
        # For "skip": only write a record if there is a selected option or one already exists
        save_answer = (action != "skip") or bool(selected_option) or bool(answer_map.get(qid))
        if target and save_answer:
            payload = {
                "attempt_id": attempt["id"],
                "question_id": qid,
                "selected_option": selected_option or None,
                "is_correct": bool(selected_option and selected_option == (target.get("correct_option") or "").upper()),
                "is_bookmarked": is_bookmarked,
                "is_flagged": is_flagged,
                "answered_at": datetime.now(timezone.utc).isoformat(),
            }
            existing = answer_map.get(qid)
            if existing:
                admin.table("mock_attempt_answers").update(payload).eq("id", existing["id"]).execute()
            else:
                admin.table("mock_attempt_answers").insert(payload).execute()

        if action == "prev":
            current_index = max(1, current_index - 1)
        elif action in ("next", "skip"):
            current_index = min(len(questions), current_index + 1)
        elif action == "submit":
            final_answers = (
                admin.table("mock_attempt_answers")
                .select("*")
                .eq("attempt_id", attempt["id"])
                .execute()
                .data
                or []
            )
            total_questions = len(questions)
            correct_answers = sum(1 for x in final_answers if x.get("is_correct"))
            score = correct_answers
            percentage = round((correct_answers / total_questions) * 100, 2) if total_questions else 0.0
            admin.table("mock_attempts").update({
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "score": score,
                "correct_answers": correct_answers,
                "total_questions": total_questions,
                "percentage": percentage,
            }).eq("id", attempt["id"]).execute()
            return redirect(f"/dashboard/mock-exams/{exam_id}/result/")

    answers = (
        admin.table("mock_attempt_answers")
        .select("*")
        .eq("attempt_id", attempt["id"])
        .execute()
        .data
        or []
    )
    answer_map = {a["question_id"]: a for a in answers}

    now_utc = datetime.now(timezone.utc)
    started_at = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
    duration = int(attempt.get("time_limit_minutes") or MOCK_DURATION_MINUTES)
    end_time = started_at + timedelta(minutes=duration)
    remaining = max(0, int((end_time - now_utc).total_seconds()))
    if remaining == 0:
        return redirect(f"/dashboard/mock-exams/{exam_id}/result/")

    current_question = questions[current_index - 1]
    current_answer = answer_map.get(current_question["id"], {})
    answered_ids = {
        q["id"] for q in questions
        if answer_map.get(q["id"], {}).get("selected_option")
    }
    answered_count = len(answered_ids)
    bookmarked_count = sum(1 for a in answer_map.values() if a.get("is_bookmarked"))
    flagged_count = sum(1 for a in answer_map.values() if a.get("is_flagged"))

    questions_status = [
        {
            "order": q["order"],
            "id": q["id"],
            "is_answered": q["id"] in answered_ids,
            "is_current": q["id"] == current_question["id"],
        }
        for q in questions
    ]

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "mock_exams",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "exam": exam,
        "attempt": attempt,
        "questions": questions,
        "answered_ids": answered_ids,
        "questions_status_json": json.dumps(questions_status),
        "current_question": current_question,
        "current_index": current_index,
        "current_answer": current_answer,
        "remaining_seconds": remaining,
        "answered_count": answered_count,
        "bookmarked_count": bookmarked_count,
        "flagged_count": flagged_count,
    }
    return render(request, "dashboard/student_take_mock_exam.html", context)


def student_mock_exam_result(request, exam_id):
    guard = _require_login(request)
    if guard:
        return guard

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    exam_rows = admin.table("mock_exams").select("*").eq("id", str(exam_id)).limit(1).execute().data or []
    if not exam_rows:
        return redirect("/dashboard/mock-exams/")
    exam = exam_rows[0]

    attempt_rows = (
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
    if not attempt_rows:
        return redirect(f"/dashboard/mock-exams/{exam_id}/start/")
    attempt = attempt_rows[0]

    leaderboard_rows, rank, my_row = _build_mock_leaderboard(admin, str(exam_id), user_id)

    percentage = float(attempt.get("percentage") or 0)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "mock_exams",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "exam": exam,
        "attempt": attempt,
        "rank": rank,
        "leaderboard_rows": leaderboard_rows,
        "my_row": my_row,
        "encouragement": _score_message(percentage),
    }
    return render(request, "dashboard/student_mock_exam_result.html", context)


def _general_test_duration_minutes(paper_title):
    return 90 if _is_general_paper(paper_title) else 180


def _general_test_question_count(paper_title):
    """
    Standardized NMC structure:
    - General Paper: 100 questions
    - All other nursing papers: 180 questions
    """
    return 100 if _is_general_paper(paper_title) else 180


def _general_test_num_batches(total_count, batch_size):
    """Full batches plus one final batch when there is a remainder (e.g. 200 Q / 180 → 2 tests)."""
    if total_count <= 0 or batch_size <= 0:
        return 0
    return (total_count + batch_size - 1) // batch_size


def _general_test_actual_batch_size(total_count, batch_size, test_number):
    """How many questions are in test_number (1-based); 0 if out of range."""
    if test_number < 1 or batch_size <= 0:
        return 0
    start_idx = (test_number - 1) * batch_size
    if start_idx >= total_count:
        return 0
    return min(batch_size, total_count - start_idx)


def _general_test_batch_duration_minutes(paper_title, nominal_batch_size, actual_question_count):
    """Scale the standard paper duration when a batch has fewer than the nominal question count."""
    full_min = _general_test_duration_minutes(paper_title)
    if actual_question_count <= 0 or nominal_batch_size <= 0:
        return max(1, full_min)
    return max(1, round(full_min * actual_question_count / nominal_batch_size))


def _practice_quiz_title_from_sort_index(sort_index):
    """sort_index 0 -> Quiz A, 1 -> Quiz B, ... 25 -> Quiz Z, 26 -> Quiz AA."""
    n = int(sort_index)
    suffix = ""
    while n >= 0:
        suffix = chr(65 + (n % 26)) + suffix
        n = n // 26 - 1
    return f"Quiz {suffix}"


def _parse_practice_quiz_items_json(json_text, max_items=200):
    """
    One or more questions per upload. JSON: array or {"questions": [...]}.
    Each item: question/question_text; options as {A,B,C} or option_a/b/c;
    answer/correct_option (A/B/C); optional explanation.
    """
    raw = (json_text or "").strip()
    if not raw:
        raise ValueError("JSON is required.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    items = parsed.get("questions") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise ValueError('Use a JSON array of questions, or {"questions": [...]}.')
    if len(items) < 1:
        raise ValueError("Add at least one question.")
    if len(items) > max_items:
        raise ValueError(f"At most {max_items} questions per upload; you sent {len(items)}.")

    questions_payload = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Question #{index} must be an object.")
        qtext = (item.get("question") or item.get("question_text") or "").strip()

        opt_a = opt_b = opt_c = ""
        opts_in = item.get("options")
        if isinstance(opts_in, dict):
            cleaned = {}
            for key, value in opts_in.items():
                k = str(key).strip().upper()
                v = str(value).strip() if value is not None else ""
                if k in {"A", "B", "C"} and v:
                    cleaned[k] = v
            opt_a = cleaned.get("A", "")
            opt_b = cleaned.get("B", "")
            opt_c = cleaned.get("C", "")
        else:
            opt_a = (item.get("option_a") or item.get("a") or "").strip()
            opt_b = (item.get("option_b") or item.get("b") or "").strip()
            opt_c = (item.get("option_c") or item.get("c") or "").strip()

        ans = (
            item.get("answer")
            or item.get("correct_option")
            or item.get("correct_answer")
            or ""
        )
        ans = str(ans).strip().upper()
        expl = (item.get("explanation") or "").strip()

        if not qtext:
            raise ValueError(f"Question #{index}: question text is required.")
        if not opt_a or not opt_b or not opt_c:
            raise ValueError(f"Question #{index}: options A, B, and C are all required.")
        if ans not in ("A", "B", "C"):
            raise ValueError(f"Question #{index}: answer must be A, B, or C.")

        questions_payload.append({
            "question_text": qtext,
            "option_a": opt_a,
            "option_b": opt_b,
            "option_c": opt_c,
            "correct_option": ans,
            "explanation": expl or None,
        })
    return questions_payload


def _flush_practice_quiz_pending_to_batches(admin, admin_user_id):
    """
    Repeatedly take the oldest 10 pending rows for this admin, create Quiz * batch, delete pending.
    Returns list of created quiz titles.
    """
    created_titles = []
    uid = str(admin_user_id)
    while True:
        batch = (
            admin.table("practice_quiz_pending_questions")
            .select("id, question_text, option_a, option_b, option_c, correct_option, explanation")
            .eq("created_by", uid)
            .order("created_at", desc=False)
            .limit(10)
            .execute()
            .data
            or []
        )
        if len(batch) < 10:
            break

        last_rows = (
            admin.table("practice_quizzes")
            .select("sort_index")
            .order("sort_index", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        next_sort = (last_rows[0]["sort_index"] + 1) if last_rows else 0
        title = _practice_quiz_title_from_sort_index(next_sort)

        quiz_row = (
            admin.table("practice_quizzes")
            .insert({
                "title": title,
                "sort_index": next_sort,
                "is_published": True,
            })
            .execute()
            .data
        )[0]
        quiz_id = quiz_row["id"]

        questions_payload = []
        for i, row in enumerate(batch, start=1):
            questions_payload.append({
                "quiz_id": quiz_id,
                "question_order": i,
                "question_text": row["question_text"],
                "option_a": row["option_a"],
                "option_b": row["option_b"],
                "option_c": row["option_c"],
                "correct_option": row["correct_option"],
                "explanation": row.get("explanation"),
            })
        admin.table("practice_quiz_questions").insert(questions_payload).execute()

        ids = [row["id"] for row in batch]
        admin.table("practice_quiz_pending_questions").delete().in_("id", ids).execute()
        created_titles.append(title)
    return created_titles


def _quiz_admin_sample_question_items():
    """Ten sample questions (shared compact + pretty JSON strings)."""
    items = []
    for n in range(1, 11):
        items.append({
            "question": (
                f"Question {n}: A patient shows acute confusion. Which action does the nurse take first?"
            ),
            "options": {
                "A": "Leave the patient alone to rest",
                "B": "Assess airway, breathing, and circulation",
                "C": "Give sedative medication without assessment",
            },
            "answer": ("A", "B", "C")[(n - 1) % 3],
            "explanation": (
                f"Question {n}: Always assess ABCs and follow provider orders; "
                "do not medicate without assessment."
            ),
        })
    return items


_QUIZ_ADMIN_ITEMS = _quiz_admin_sample_question_items()
QUIZ_ADMIN_JSON_PLACEHOLDER = json.dumps(_QUIZ_ADMIN_ITEMS, ensure_ascii=False, separators=(",", ":"))


def student_nclex_questions(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/nclex/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    try:
        questions = (
            admin.table("nclex_questions")
            .select("id, question_type, question_text, options, correct_answers, rationale, difficulty, display_order")
            .eq("is_active", True)
            .order("display_order", desc=False)
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
            .data
            or []
        )
    except Exception:
        questions = []

    for idx, q in enumerate(questions, start=1):
        q["num"] = idx
        q["question_type_label"] = NCLEX_QUESTION_TYPES.get(q.get("question_type"), "NCLEX Question")
        raw_options = q.get("options")
        normalized_options = []
        if isinstance(raw_options, list):
            normalized_options = [str(v).strip() for v in raw_options if str(v).strip()]
        elif isinstance(raw_options, dict):
            for key in sorted(raw_options.keys()):
                val = str(raw_options.get(key) or "").strip()
                if val:
                    normalized_options.append(val)
        elif isinstance(raw_options, str):
            maybe = raw_options.strip()
            if maybe:
                try:
                    parsed = json.loads(maybe)
                    if isinstance(parsed, list):
                        normalized_options = [str(v).strip() for v in parsed if str(v).strip()]
                    elif isinstance(parsed, dict):
                        for key in sorted(parsed.keys()):
                            val = str(parsed.get(key) or "").strip()
                            if val:
                                normalized_options.append(val)
                    else:
                        normalized_options = [maybe]
                except Exception:
                    normalized_options = [line.strip() for line in maybe.splitlines() if line.strip()]
        q["options"] = normalized_options
        q["correct_answers"] = q.get("correct_answers") or []
        q["is_mcq"] = q.get("question_type") == "mcq"
        q["is_sata"] = q.get("question_type") == "sata"
        q["is_fill_blank"] = q.get("question_type") == "fill_blank"
        q["is_ordered_response"] = q.get("question_type") == "ordered_response"

    questions_per_test = 100
    seconds_per_question = 90
    question_groups = []
    for group_index, start in enumerate(range(0, len(questions), questions_per_test), start=1):
        items = questions[start:start + questions_per_test]
        if not items:
            continue
        question_groups.append(
            {
                "index": group_index,
                "question_count": len(items),
                "minutes": max(1, round((len(items) * seconds_per_question) / 60)),
                "start_link": f"/dashboard/nclex/test/?test={group_index}",
            }
        )

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "student_nclex",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "questions": questions,
        "question_groups": question_groups,
    }
    return render(request, "dashboard/student_nclex_questions.html", context)


def student_nclex_test(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/nclex/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    try:
        questions = (
            admin.table("nclex_questions")
            .select("id, question_type, question_text, options, correct_answers, rationale, difficulty, display_order")
            .eq("is_active", True)
            .order("display_order", desc=False)
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
            .data
            or []
        )
    except Exception:
        questions = []

    for idx, q in enumerate(questions, start=1):
        q["num"] = idx
        raw_options = q.get("options")
        normalized_options = []
        if isinstance(raw_options, list):
            normalized_options = [str(v).strip() for v in raw_options if str(v).strip()]
        elif isinstance(raw_options, dict):
            for key in sorted(raw_options.keys()):
                val = str(raw_options.get(key) or "").strip()
                if val:
                    normalized_options.append(val)
        elif isinstance(raw_options, str):
            maybe = raw_options.strip()
            if maybe:
                try:
                    parsed = json.loads(maybe)
                    if isinstance(parsed, list):
                        normalized_options = [str(v).strip() for v in parsed if str(v).strip()]
                    elif isinstance(parsed, dict):
                        for key in sorted(parsed.keys()):
                            val = str(parsed.get(key) or "").strip()
                            if val:
                                normalized_options.append(val)
                    else:
                        normalized_options = [maybe]
                except Exception:
                    normalized_options = [line.strip() for line in maybe.splitlines() if line.strip()]
        q["options"] = normalized_options
        q["correct_answers"] = q.get("correct_answers") or []

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "student_nclex",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "questions": questions,
        "test_index": request.GET.get("test", "1"),
    }
    return render(request, "dashboard/student_nclex_test.html", context)


def student_nclex_guide(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/nclex/")

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "student_nclex",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
    }
    return render(request, "dashboard/student_nclex_guide.html", context)


def student_mnemonics_guide(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    sections = _student_mnemonic_sections()
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "mnemonics_guide",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "sections": sections,
        "sections_total": len(sections),
    }
    return render(request, "dashboard/student_mnemonics_guide.html", context)


def _student_mnemonic_sections():
    fundamentals = [
        {"title": "Vital Signs", "code": "TPR BP", "rows": [
            {"letter": "T", "meaning": "Temperature (36.5-37.5 C)", "explanation": "Normal body temperature range for a healthy adult."},
            {"letter": "P", "meaning": "Pulse (60-100 bpm)", "explanation": "Normal resting heart rate for an adult."},
            {"letter": "R", "meaning": "Respiration (12-20 bpm)", "explanation": "Normal breathing rate for a healthy adult at rest."},
            {"letter": "BP", "meaning": "Blood Pressure (~120/80 mmHg)", "explanation": "Ideal blood pressure reading for a healthy adult."},
        ]},
        {"title": "Drug Administration", "code": "DR TIMED", "rows": [
            {"letter": "D", "meaning": "Drug", "explanation": "Verify you have the correct medication as prescribed."},
            {"letter": "R", "meaning": "Right patient", "explanation": "Confirm the patient's identity using two identifiers."},
            {"letter": "T", "meaning": "Time", "explanation": "Administer at the correct scheduled time."},
            {"letter": "I", "meaning": "Route", "explanation": "Ensure the route (PO, IV, IM, etc.) matches the order."},
            {"letter": "M", "meaning": "Dose", "explanation": "Double-check the prescribed dose against the available concentration."},
            {"letter": "E", "meaning": "Education", "explanation": "Teach the patient about the medication's purpose and side effects."},
            {"letter": "D", "meaning": "Documentation", "explanation": "Record administration immediately after giving the drug."},
        ]},
        {"title": "Hypoglycemia", "code": "TIRED", "rows": [
            {"letter": "T", "meaning": "Tremors", "explanation": "Shaking or trembling due to low blood sugar."},
            {"letter": "I", "meaning": "Irritability", "explanation": "Mood changes and agitation from neuroglycopenia."},
            {"letter": "R", "meaning": "Rapid pulse", "explanation": "Tachycardia from sympathetic nervous system activation."},
            {"letter": "E", "meaning": "Excess hunger", "explanation": "Polyphagia as the body craves glucose."},
            {"letter": "D", "meaning": "Diaphoresis", "explanation": "Sweating from autonomic nervous system response."},
        ]},
        {"title": "Shock", "code": "SHOCK", "rows": [
            {"letter": "S", "meaning": "Sweating", "explanation": "Cool, clammy skin from sympathetic activation."},
            {"letter": "H", "meaning": "Hypotension", "explanation": "Low blood pressure due to inadequate tissue perfusion."},
            {"letter": "O", "meaning": "Oliguria", "explanation": "Decreased urine output (<0.5 mL/kg/hr) from reduced renal perfusion."},
            {"letter": "C", "meaning": "Confusion", "explanation": "Altered mental status from decreased cerebral blood flow."},
            {"letter": "K", "meaning": "Cold skin", "explanation": "Peripheral vasoconstriction shunts blood to vital organs."},
        ]},
        {"title": "Pregnancy Danger Signs", "code": "BLEED", "rows": [
            {"letter": "B", "meaning": "Bleeding", "explanation": "Any vaginal bleeding in pregnancy may indicate abruption or placenta previa."},
            {"letter": "L", "meaning": "Loss of fetal movement", "explanation": "Decreased or absent fetal movement suggests fetal distress."},
            {"letter": "E", "meaning": "Excess vomiting", "explanation": "Hyperemesis gravidarum can cause dehydration and electrolyte imbalance."},
            {"letter": "E", "meaning": "Edema", "explanation": "Sudden swelling of face/hands may indicate preeclampsia."},
            {"letter": "D", "meaning": "Dizziness", "explanation": "Lightheadedness or syncope may indicate anemia or hypotension."},
        ]},
        {"title": "APGAR Score", "code": "APGAR", "rows": [
            {"letter": "A", "meaning": "Appearance", "explanation": "Skin color (cyanotic, pink, or pale)."},
            {"letter": "P", "meaning": "Pulse", "explanation": "Heart rate (absent, <100, or >100 bpm)."},
            {"letter": "G", "meaning": "Grimace", "explanation": "Reflex irritability in response to stimulation."},
            {"letter": "A", "meaning": "Activity", "explanation": "Muscle tone (limp, some flexion, or active movement)."},
            {"letter": "R", "meaning": "Respiration", "explanation": "Breathing effort (absent, weak cry, or strong cry)."},
        ]},
        {"title": "Hand Hygiene", "code": "BEFAR", "rows": [
            {"letter": "B", "meaning": "Before patient", "explanation": "Clean hands before touching any patient."},
            {"letter": "E", "meaning": "Before procedure", "explanation": "Perform hand hygiene before any aseptic or clean procedure."},
            {"letter": "F", "meaning": "After fluid exposure", "explanation": "Wash hands after exposure to body fluids."},
            {"letter": "A", "meaning": "After patient", "explanation": "Clean hands after touching the patient or their environment."},
            {"letter": "R", "meaning": "After surroundings", "explanation": "Perform hand hygiene after contact with surfaces near the patient."},
        ]},
        {"title": "Cranial Nerves", "code": "Oh Oh Oh To Touch And Feel Very Good Velvet AH", "rows": [
            {"letter": "Oh", "meaning": "Olfactory (I)", "explanation": "Smell"},
            {"letter": "Oh", "meaning": "Optic (II)", "explanation": "Vision"},
            {"letter": "Oh", "meaning": "Oculomotor (III)", "explanation": "Eye movement, pupil constriction"},
            {"letter": "To", "meaning": "Trochlear (IV)", "explanation": "Downward and inward eye movement"},
            {"letter": "Touch", "meaning": "Trigeminal (V)", "explanation": "Facial sensation, chewing"},
            {"letter": "And", "meaning": "Abducens (VI)", "explanation": "Lateral eye movement"},
            {"letter": "Feel", "meaning": "Facial (VII)", "explanation": "Facial expression, taste (anterior 2/3 of tongue)"},
            {"letter": "Very", "meaning": "Vestibulocochlear (VIII)", "explanation": "Hearing and balance"},
            {"letter": "Good", "meaning": "Glossopharyngeal (IX)", "explanation": "Taste (posterior 1/3 of tongue), swallowing"},
            {"letter": "Velvet", "meaning": "Vagus (X)", "explanation": "Autonomic functions, speech, swallowing"},
            {"letter": "AH", "meaning": "Accessory (XI)", "explanation": "Shoulder shrug, head turning"},
            {"letter": "AH", "meaning": "Hypoglossal (XII)", "explanation": "Tongue movement"},
        ]},
        {"title": "Anemia", "code": "PALE", "rows": [
            {"letter": "P", "meaning": "Pallor", "explanation": "Pale skin and mucous membranes from reduced hemoglobin."},
            {"letter": "A", "meaning": "Anorexia/weakness", "explanation": "Loss of appetite and generalized fatigue."},
            {"letter": "L", "meaning": "Lethargy", "explanation": "Persistent tiredness and lack of energy."},
            {"letter": "E", "meaning": "Easy fatigue", "explanation": "Rapid exhaustion with minimal exertion."},
        ]},
        {"title": "Hypertension Complications", "code": "HARD", "rows": [
            {"letter": "H", "meaning": "Heart disease", "explanation": "Hypertension increases risk of MI, heart failure, and LVH."},
            {"letter": "A", "meaning": "Aneurysm", "explanation": "Chronic high pressure weakens arterial walls, causing aneurysms."},
            {"letter": "R", "meaning": "Renal failure", "explanation": "Hypertensive nephrosclerosis leads to chronic kidney disease."},
            {"letter": "D", "meaning": "Death", "explanation": "Uncontrolled hypertension increases overall mortality risk."},
        ]},
    ]
    medical_surgical = [
        {"title": "Systems of the Body", "code": "My Very Easy Method Just Speeds Up Naming Parts", "rows": [
            {"letter": "My", "meaning": "Musculoskeletal", "explanation": "Bones, muscles, joints."},
            {"letter": "Very", "meaning": "Vascular", "explanation": "Blood vessels (arteries, veins, capillaries)."},
            {"letter": "Easy", "meaning": "Endocrine", "explanation": "Hormone-producing glands (thyroid, pituitary, etc.)."},
            {"letter": "Method", "meaning": "Metabolic", "explanation": "Biochemical processes (e.g., glucose metabolism)."},
            {"letter": "Just", "meaning": "Gastrointestinal", "explanation": "Digestive tract (stomach, intestines, liver)."},
            {"letter": "Speeds", "meaning": "Urinary", "explanation": "Kidneys, ureters, bladder, urethra."},
            {"letter": "Up", "meaning": "Nervous", "explanation": "Brain, spinal cord, peripheral nerves."},
            {"letter": "Naming", "meaning": "Pulmonary", "explanation": "Lungs and airways."},
            {"letter": "Parts", "meaning": "(Reproductive/Integumentary)", "explanation": "Often added for completion."},
        ]},
        {"title": "Cholelithiasis Risk", "code": "5 F's", "rows": [
            {"letter": "F", "meaning": "Female", "explanation": "Women have higher estrogen levels, increasing cholesterol saturation in bile."},
            {"letter": "F", "meaning": "Fat", "explanation": "Obesity increases cholesterol secretion into bile."},
            {"letter": "F", "meaning": "Forty", "explanation": "Risk increases with age, especially after 40."},
            {"letter": "F", "meaning": "Fertile", "explanation": "Multiple pregnancies increase risk due to hormonal changes."},
            {"letter": "F", "meaning": "Fair", "explanation": "Fair-skinned individuals of Northern European descent have higher risk."},
        ]},
        {"title": "Malignant Mole (Melanoma)", "code": "ABCDE", "rows": [
            {"letter": "A", "meaning": "Asymmetry", "explanation": "Two halves of the mole do not match."},
            {"letter": "B", "meaning": "Border irregular", "explanation": "Edges are ragged, notched, or blurred."},
            {"letter": "C", "meaning": "Color variation", "explanation": "Multiple colors within one mole."},
            {"letter": "D", "meaning": "Diameter > 6 mm", "explanation": "Larger than a pencil eraser, though melanomas can be smaller."},
            {"letter": "E", "meaning": "Evolution", "explanation": "Any change in size, shape, color, or symptoms over time."},
        ]},
        {"title": "Hypoplasia vs Hyperplasia", "code": "Hypo vs Hyper", "rows": [
            {"letter": "Hypo = LOW cells", "meaning": "Hypoplasia", "explanation": "Decreased cell production, resulting in underdeveloped tissue."},
            {"letter": "Hyper = HIGH cells", "meaning": "Hyperplasia", "explanation": "Increased cell division, leading to tissue enlargement."},
        ]},
        {"title": "Blood Group O Compatibility", "code": "O = Only Give O", "rows": [
            {"letter": "O = Only give O", "meaning": "Universal donor", "explanation": "Type O negative blood can be given to any recipient."},
            {"letter": "Universal donor", "meaning": "O negative", "explanation": "No A, B, or Rh antigens to trigger reaction."},
            {"letter": "Receives only O", "meaning": "Recipient limitation", "explanation": "Type O patients can only receive type O blood."},
        ]},
        {"title": "Minor Bleeding Signs", "code": "BRUISE", "rows": [
            {"letter": "B", "meaning": "Bleeding gums", "explanation": "Gums bleed easily with minimal trauma or brushing."},
            {"letter": "R", "meaning": "Red spots (petechiae)", "explanation": "Tiny red/purple spots from capillary bleeding."},
            {"letter": "U", "meaning": "Unusual bruising", "explanation": "Bruising without known injury or from minor bumps."},
            {"letter": "I", "meaning": "Injury bleeding", "explanation": "Prolonged bleeding from small cuts or abrasions."},
            {"letter": "S", "meaning": "Skin spots (ecchymosis)", "explanation": "Larger areas of bruising or purpura."},
            {"letter": "E", "meaning": "Epistaxis", "explanation": "Frequent or prolonged nosebleeds."},
        ]},
        {"title": "Sickle Cell Disease", "code": "SICKLE", "rows": [
            {"letter": "S", "meaning": "Swelling", "explanation": "Dactylitis (hand-foot syndrome) in infants."},
            {"letter": "I", "meaning": "Infection", "explanation": "Functional asplenia increases risk of encapsulated bacteria."},
            {"letter": "C", "meaning": "Crisis pain", "explanation": "Vaso-occlusive crisis causes severe bone and joint pain."},
            {"letter": "K", "meaning": "Kidney issues", "explanation": "Sickle nephropathy leads to hematuria and impaired concentration."},
            {"letter": "L", "meaning": "Low Hb", "explanation": "Chronic hemolytic anemia with hemoglobin 6-9 g/dL."},
            {"letter": "E", "meaning": "Episodes", "explanation": "Acute exacerbations triggered by stress, dehydration, or infection."},
        ]},
        {"title": "MgSO4 Toxicity", "code": "3 D's", "rows": [
            {"letter": "D", "meaning": "Decreased reflexes", "explanation": "Loss of patellar reflex is an early sign of magnesium toxicity."},
            {"letter": "D", "meaning": "Depressed respiration", "explanation": "Respiratory rate <12/min indicates severe toxicity."},
            {"letter": "D", "meaning": "Drowsiness", "explanation": "Altered mental status progressing to coma."},
        ]},
        {"title": "Congestive Heart Failure", "code": "FACES", "rows": [
            {"letter": "F", "meaning": "Fatigue", "explanation": "Reduced cardiac output causes generalized weakness."},
            {"letter": "A", "meaning": "Activity intolerance", "explanation": "Dyspnea or exhaustion with minimal exertion."},
            {"letter": "C", "meaning": "Congestion", "explanation": "Pulmonary or systemic congestion."},
            {"letter": "E", "meaning": "Edema", "explanation": "Peripheral edema from fluid retention."},
            {"letter": "S", "meaning": "Shortness of breath", "explanation": "DOE, orthopnea, or PND."},
        ]},
        {"title": "Hypoxia", "code": "RESTLESS / BLUE", "rows": [
            {"letter": "Early R", "meaning": "Restlessness", "explanation": "Early sign of hypoxia as the brain senses low oxygen."},
            {"letter": "Early E", "meaning": "Anxiety", "explanation": "Patient feels uneasy or fearful without clear cause."},
            {"letter": "Early S", "meaning": "Tachycardia", "explanation": "Heart rate increases to compensate for low oxygen."},
            {"letter": "Late B", "meaning": "Bradycardia", "explanation": "Late sign indicating impending respiratory arrest."},
            {"letter": "Late L", "meaning": "Low BP", "explanation": "Hypotension from cardiovascular decompensation."},
            {"letter": "Late U", "meaning": "Unconscious", "explanation": "Loss of consciousness due to cerebral hypoxia."},
            {"letter": "Late E", "meaning": "Extreme cyanosis", "explanation": "Severe bluish discoloration of skin and mucous membranes."},
        ]},
        {"title": "Increased ICP", "code": "CUSHING", "rows": [
            {"letter": "C", "meaning": "Increased BP", "explanation": "Cushing's triad: hypertension with widening pulse pressure."},
            {"letter": "U", "meaning": "Decreased pulse", "explanation": "Cushing's triad: bradycardia."},
            {"letter": "S", "meaning": "Irregular breathing", "explanation": "Cushing's triad respiratory pattern changes."},
            {"letter": "H", "meaning": "Headache", "explanation": "Severe, constant headache often worse in the morning."},
            {"letter": "I", "meaning": "Impaired consciousness", "explanation": "Reduced LOC from brain compression."},
            {"letter": "N", "meaning": "Nausea/vomiting", "explanation": "Projectile vomiting may occur."},
            {"letter": "G", "meaning": "Gaze changes", "explanation": "Pupil dilation/fixation or gaze palsies."},
        ]},
        {"title": "Parkinson's Disease", "code": "TRAP", "rows": [
            {"letter": "T", "meaning": "Tremor", "explanation": "Resting tremor (pill-rolling)."},
            {"letter": "R", "meaning": "Rigidity", "explanation": "Cogwheel or lead-pipe rigidity."},
            {"letter": "A", "meaning": "Akinesia", "explanation": "Bradykinesia and difficulty initiating movement."},
            {"letter": "P", "meaning": "Postural instability", "explanation": "Impaired balance and frequent falls."},
        ]},
        {"title": "Splenomegaly Causes", "code": "CHIMPS", "rows": [
            {"letter": "C", "meaning": "Cirrhosis", "explanation": "Portal hypertension causes splenic congestion."},
            {"letter": "H", "meaning": "Hemolysis", "explanation": "Hemolytic states enlarge spleen."},
            {"letter": "I", "meaning": "Infection", "explanation": "EBV, malaria, endocarditis, typhoid etc."},
            {"letter": "M", "meaning": "Malaria", "explanation": "Common cause of massive splenomegaly in endemic regions."},
            {"letter": "P", "meaning": "Portal HTN", "explanation": "Increased portal pressure enlarges spleen."},
            {"letter": "S", "meaning": "Sickle cell", "explanation": "Early splenomegaly; later autosplenectomy."},
        ]},
        {"title": "Scarlet Fever", "code": "PASTIA", "rows": [
            {"letter": "P", "meaning": "Pastia lines", "explanation": "Linear petechial rash in skin folds."},
            {"letter": "A", "meaning": "Angina", "explanation": "Severe sore throat with exudative tonsillitis."},
            {"letter": "S", "meaning": "Strawberry tongue", "explanation": "Classic tongue change in scarlet fever."},
            {"letter": "T", "meaning": "Tonsillitis", "explanation": "Inflamed, swollen tonsils often with exudate."},
            {"letter": "I", "meaning": "Infection rash", "explanation": "Fine sandpaper-like rash spreading from trunk."},
            {"letter": "A", "meaning": "Antibiotics needed", "explanation": "Treat to prevent rheumatic fever."},
        ]},
        {"title": "Cor Pulmonale", "code": "RIGHT", "rows": [
            {"letter": "R", "meaning": "Right heart failure", "explanation": "Cor pulmonale is RV failure secondary to lung disease."},
            {"letter": "I", "meaning": "Increased JVP", "explanation": "Raised right atrial pressure."},
            {"letter": "G", "meaning": "General edema", "explanation": "Systemic venous congestion."},
            {"letter": "H", "meaning": "Hypoxia", "explanation": "Chronic hypoxemia from pulmonary disease."},
            {"letter": "T", "meaning": "Tachycardia", "explanation": "Compensatory increase in HR."},
        ]},
        {"title": "WBC Order", "code": "Never Let Monkeys Eat Bananas", "rows": [
            {"letter": "Never", "meaning": "Neutrophils", "explanation": "Most abundant, first responders to bacterial infection."},
            {"letter": "Let", "meaning": "Lymphocytes", "explanation": "Key in viral infections and adaptive immunity."},
            {"letter": "Monkeys", "meaning": "Monocytes", "explanation": "Become macrophages in tissues."},
            {"letter": "Eat", "meaning": "Eosinophils", "explanation": "Increase in parasitic infections and allergies."},
            {"letter": "Bananas", "meaning": "Basophils", "explanation": "Release histamine in allergic reactions."},
        ]},
        {"title": "Mitosis", "code": "IPMAT", "rows": [
            {"letter": "I", "meaning": "Interphase", "explanation": "Cell growth and DNA replication."},
            {"letter": "P", "meaning": "Prophase", "explanation": "Chromosomes condense, nuclear envelope dissolves."},
            {"letter": "M", "meaning": "Metaphase", "explanation": "Chromosomes align at metaphase plate."},
            {"letter": "A", "meaning": "Anaphase", "explanation": "Sister chromatids separate."},
            {"letter": "T", "meaning": "Telophase", "explanation": "Nuclear membranes reform."},
        ]},
        {"title": "Viral Diarrhea", "code": "RRRA", "rows": [
            {"letter": "R", "meaning": "Rotavirus", "explanation": "Most common severe diarrhea in infants/young children."},
            {"letter": "R", "meaning": "Norovirus", "explanation": "Common outbreak cause in communities."},
            {"letter": "R", "meaning": "Adenovirus", "explanation": "Enteric serotypes 40/41 in young children."},
            {"letter": "A", "meaning": "Astrovirus", "explanation": "Mild, self-limiting diarrhea."},
        ]},
        {"title": "Cancer Warning Signs", "code": "CAUTION", "rows": [
            {"letter": "C", "meaning": "Change in bowel/bladder habits", "explanation": "Persistent bowel/urine change with or without blood."},
            {"letter": "A", "meaning": "A sore that does not heal", "explanation": "Persistent non-healing lesion."},
            {"letter": "U", "meaning": "Unusual bleeding/discharge", "explanation": "Blood in sputum, urine, stool, nipple etc."},
            {"letter": "T", "meaning": "Thickening or lump", "explanation": "Palpable mass."},
            {"letter": "I", "meaning": "Indigestion or difficulty swallowing", "explanation": "Persistent dyspepsia/dysphagia."},
            {"letter": "O", "meaning": "Obvious change in wart/mole", "explanation": "Concerning skin lesion changes."},
            {"letter": "N", "meaning": "Nagging cough or hoarseness", "explanation": "Persistent respiratory/voice symptom."},
        ]},
        {"title": "DKA Management", "code": "3 F's", "rows": [
            {"letter": "F", "meaning": "Fluids", "explanation": "IV normal saline to restore volume."},
            {"letter": "F", "meaning": "Fix insulin", "explanation": "IV regular insulin to reverse ketoacidosis."},
            {"letter": "F", "meaning": "Fix electrolytes", "explanation": "Replace potassium (and phosphate if needed)."},
        ]},
        {"title": "Ventricular Fibrillation", "code": "DEFIB", "rows": [
            {"letter": "DEFIB", "meaning": "Defibrillate immediately", "explanation": "VFib is a shockable rhythm requiring immediate defibrillation."},
        ]},
        {"title": "MI Treatment", "code": "MONA", "rows": [
            {"letter": "M", "meaning": "Morphine", "explanation": "Relieves pain and reduces preload/oxygen demand."},
            {"letter": "O", "meaning": "Oxygen", "explanation": "Give if hypoxemic or in respiratory distress."},
            {"letter": "N", "meaning": "Nitrates", "explanation": "Vasodilation and pain relief."},
            {"letter": "A", "meaning": "Aspirin", "explanation": "Antiplatelet; chew immediately unless contraindicated."},
        ]},
        {"title": "Coma Causes", "code": "AEIOU TIPS", "rows": [
            {"letter": "A", "meaning": "Alcohol", "explanation": "Intoxication, withdrawal, or Wernicke's."},
            {"letter": "E", "meaning": "Epilepsy/Endocrine", "explanation": "Postictal state or endocrine emergencies."},
            {"letter": "I", "meaning": "Insulin", "explanation": "Hypoglycemia/hyperglycemia."},
            {"letter": "O", "meaning": "Overdose/Oxygen", "explanation": "Drug overdose or hypoxia."},
            {"letter": "U", "meaning": "Uremia", "explanation": "Renal failure toxin buildup."},
            {"letter": "T", "meaning": "Trauma", "explanation": "Head injury/ICH."},
            {"letter": "I", "meaning": "Infection", "explanation": "Meningitis, encephalitis, sepsis."},
            {"letter": "P", "meaning": "Psych", "explanation": "Selected psychiatric etiologies."},
            {"letter": "S", "meaning": "Stroke/Shock", "explanation": "CVA or severe circulatory collapse."},
        ]},
        {"title": "Neurovascular Occlusion", "code": "5 Ps", "rows": [
            {"letter": "P", "meaning": "Pain", "explanation": "Pain out of proportion is early warning."},
            {"letter": "P", "meaning": "Pallor", "explanation": "Pale cool limb."},
            {"letter": "P", "meaning": "Pulselessness", "explanation": "Absent/distal weak pulses."},
            {"letter": "P", "meaning": "Paresthesia", "explanation": "Numbness/tingling."},
            {"letter": "P", "meaning": "Paralysis", "explanation": "Late sign, threatened viability."},
        ]},
        {"title": "Angina Triggers", "code": "4 E's", "rows": [
            {"letter": "E", "meaning": "Exercise", "explanation": "Increases myocardial oxygen demand."},
            {"letter": "E", "meaning": "Emotion", "explanation": "Stress catecholamine surge."},
            {"letter": "E", "meaning": "Eating", "explanation": "Postprandial increased workload."},
            {"letter": "E", "meaning": "Environment", "explanation": "Cold causes vasoconstriction/afterload rise."},
        ]},
        {"title": "Acid-Base Interpretation", "code": "ROME", "rows": [
            {"letter": "R", "meaning": "Respiratory Opposite", "explanation": "pH and PaCO2 move in opposite directions."},
            {"letter": "M", "meaning": "Metabolic Equal", "explanation": "pH and HCO3 move in same direction."},
        ]},
        {"title": "Hypocalcemia", "code": "CATS", "rows": [
            {"letter": "C", "meaning": "Convulsions", "explanation": "Seizures from neuronal irritability."},
            {"letter": "A", "meaning": "Arrhythmias", "explanation": "QT prolongation and conduction effects."},
            {"letter": "T", "meaning": "Tetany", "explanation": "Spasms, Chvostek/Trousseau signs."},
            {"letter": "S", "meaning": "Spasms", "explanation": "May include life-threatening laryngospasm."},
        ]},
        {"title": "Hypernatremia", "code": "DRY", "rows": [
            {"letter": "D", "meaning": "Dehydration", "explanation": "Cellular water loss and shrinkage."},
            {"letter": "R", "meaning": "Restlessness", "explanation": "Neurologic irritability/mental status change."},
            {"letter": "Y", "meaning": "Increased thirst", "explanation": "Osmotic thirst response."},
        ]},
        {"title": "Hyperkalemia Causes", "code": "MACHINE", "rows": [
            {"letter": "M", "meaning": "Metabolic acidosis", "explanation": "K shifts out of cells."},
            {"letter": "A", "meaning": "Addison's disease", "explanation": "Aldosterone deficiency reduces K excretion."},
            {"letter": "C", "meaning": "Cell destruction", "explanation": "Hemolysis/rhabdo/tumor lysis."},
            {"letter": "H", "meaning": "Hypoaldosteronism", "explanation": "Reduced mineralocorticoid effect."},
            {"letter": "I", "meaning": "Intake excess", "explanation": "Excess oral/IV potassium."},
            {"letter": "N", "meaning": "Nephron failure", "explanation": "Kidney disease reduces excretion."},
            {"letter": "E", "meaning": "Excretion reduced", "explanation": "Drug-related reduced potassium elimination."},
        ]},
        {"title": "Hyperkalemia Signs", "code": "MURDER", "rows": [
            {"letter": "M", "meaning": "Muscle weakness", "explanation": "Ascending weakness may occur."},
            {"letter": "U", "meaning": "Urine reduced", "explanation": "Often from underlying renal cause."},
            {"letter": "R", "meaning": "Respiratory distress", "explanation": "Respiratory muscle weakness in severe cases."},
            {"letter": "D", "meaning": "Decreased cardiac contractility", "explanation": "Reduced myocardial performance."},
            {"letter": "E", "meaning": "ECG changes", "explanation": "Peaked T, widened QRS, sine-wave risk."},
            {"letter": "R", "meaning": "Reflexes reduced", "explanation": "Hyporeflexia from neuromuscular effect."},
        ]},
    ]
    midwifery = [
        {"title": "Placenta-Crossing Substances", "code": "DAAMP", "rows": [
            {"letter": "D", "meaning": "Drugs", "explanation": "Most medications cross the placenta (e.g., opioids, anticonvulsants, ACE inhibitors)."},
            {"letter": "A", "meaning": "Alcohol", "explanation": "Ethanol crosses freely and causes fetal alcohol spectrum disorders."},
            {"letter": "A", "meaning": "Antibodies (IgG)", "explanation": "Maternal IgG crosses and can cause hemolytic disease of the newborn (Rh incompatibility)."},
            {"letter": "M", "meaning": "Microorganisms", "explanation": "Viruses and bacteria can cross and affect the fetus."},
            {"letter": "P", "meaning": "Poisons", "explanation": "Nicotine, lead, mercury, and environmental toxins can cross to the fetus."},
            {"letter": "Tip", "meaning": "Exam tip", "explanation": "Most substances cross the placenta except large proteins (e.g., IgM, insulin)."},
        ]},
        {"title": "Preterm Infant Problems", "code": "IMMATURE", "rows": [
            {"letter": "I", "meaning": "Infection risk", "explanation": "Immature immunity increases sepsis susceptibility."},
            {"letter": "M", "meaning": "Metabolic issues", "explanation": "Hypoglycemia from poor glycogen stores."},
            {"letter": "M", "meaning": "Minimal fat", "explanation": "Hypothermia risk from low adipose tissue."},
            {"letter": "A", "meaning": "Apnea", "explanation": "Immature respiratory control."},
            {"letter": "T", "meaning": "Temperature instability", "explanation": "Poor thermoregulation in preterms."},
            {"letter": "U", "meaning": "Underdeveloped lungs", "explanation": "Surfactant deficiency and RDS risk."},
            {"letter": "R", "meaning": "Respiratory distress", "explanation": "Grunting/flaring/retractions."},
            {"letter": "E", "meaning": "Eating difficulty", "explanation": "Poor suck-swallow-breathe coordination."},
        ]},
        {"title": "Obstetric History", "code": "GTPAL", "rows": [
            {"letter": "G", "meaning": "Gravida", "explanation": "Total pregnancies including current."},
            {"letter": "T", "meaning": "Term births", "explanation": "Births at 37 weeks or later."},
            {"letter": "P", "meaning": "Preterm births", "explanation": "Births between 20-36 weeks."},
            {"letter": "A", "meaning": "Abortions", "explanation": "Pregnancy losses before 20 weeks."},
            {"letter": "L", "meaning": "Living children", "explanation": "Currently living offspring count."},
        ]},
        {"title": "Newborn Assessment", "code": "APGAR + HEAD TO TOE", "rows": [
            {"letter": "APGAR", "meaning": "Immediate transition", "explanation": "Scored at 1 and 5 minutes."},
            {"letter": "HEAD TO TOE", "meaning": "Full physical exam", "explanation": "Systematic newborn exam after stabilization."},
        ]},
        {"title": "IUD Problems", "code": "PAINS", "rows": [
            {"letter": "P", "meaning": "Period late", "explanation": "May indicate pregnancy (including ectopic)."},
            {"letter": "A", "meaning": "Abdominal pain", "explanation": "Could signal perforation, infection, or expulsion."},
            {"letter": "I", "meaning": "Infection", "explanation": "PID concern, especially early post-insertion."},
            {"letter": "N", "meaning": "Not feeling strings", "explanation": "Possible migration/expulsion."},
            {"letter": "S", "meaning": "Spotting", "explanation": "Could indicate malposition or infection."},
            {"letter": "Tip", "meaning": "Exam focus", "explanation": "PAINS is very commonly tested."},
        ]},
        {"title": "Oral Contraceptive Danger Signs", "code": "ACHES", "rows": [
            {"letter": "A", "meaning": "Abdominal pain", "explanation": "May indicate hepatic or pancreatic complication."},
            {"letter": "C", "meaning": "Chest pain", "explanation": "Possible PE/MI."},
            {"letter": "H", "meaning": "Headache", "explanation": "Severe headache may indicate stroke/HTN."},
            {"letter": "E", "meaning": "Eye problems", "explanation": "Visual disturbance may indicate thrombotic event."},
            {"letter": "S", "meaning": "Severe leg pain", "explanation": "Possible DVT."},
        ]},
        {"title": "Infections in Pregnancy", "code": "TORCH", "rows": [
            {"letter": "T", "meaning": "Toxoplasmosis", "explanation": "Can cause congenital neurologic/ocular disease."},
            {"letter": "O", "meaning": "Other (syphilis, HIV, VZV, parvovirus B19)", "explanation": "Other vertically transmitted infections."},
            {"letter": "R", "meaning": "Rubella", "explanation": "Congenital syndrome with cataract/heart/deafness."},
            {"letter": "C", "meaning": "CMV", "explanation": "Common congenital infection with neurodevelopment impact."},
            {"letter": "H", "meaning": "Herpes simplex", "explanation": "Can cause severe neonatal HSV disease."},
        ]},
        {"title": "Episiotomy Assessment", "code": "REEDA", "rows": [
            {"letter": "R", "meaning": "Redness", "explanation": "Inflammation/infection sign."},
            {"letter": "E", "meaning": "Edema", "explanation": "Swelling from tissue trauma/fluid."},
            {"letter": "E", "meaning": "Ecchymosis", "explanation": "Bruising around wound."},
            {"letter": "D", "meaning": "Discharge", "explanation": "Purulent/foul discharge suggests infection."},
            {"letter": "A", "meaning": "Approximation", "explanation": "Wound edge alignment quality."},
        ]},
        {"title": "Dystocia Etiology", "code": "3 Ps", "rows": [
            {"letter": "P", "meaning": "Power", "explanation": "Contraction strength/frequency issues."},
            {"letter": "P", "meaning": "Passenger", "explanation": "Fetal size/position/anomaly factors."},
            {"letter": "P", "meaning": "Passage", "explanation": "Pelvic or birth canal constraints."},
        ]},
        {"title": "Dystocia Maternal Factors", "code": "PELVIS", "rows": [
            {"letter": "P", "meaning": "Pelvic size", "explanation": "Contracted or unfavorable pelvis."},
            {"letter": "E", "meaning": "Exhaustion", "explanation": "Maternal fatigue in prolonged labour."},
            {"letter": "L", "meaning": "Labour dysfunction", "explanation": "Abnormal labour pattern."},
            {"letter": "V", "meaning": "Vaginal issues", "explanation": "Structural obstruction or pathology."},
            {"letter": "I", "meaning": "Infection", "explanation": "Can worsen labour effectiveness and outcomes."},
            {"letter": "S", "meaning": "Stress", "explanation": "Catecholamine surge can reduce contractions."},
        ]},
        {"title": "Severe Preeclampsia Complications", "code": "HELLP", "rows": [
            {"letter": "H", "meaning": "Hemolysis", "explanation": "Microangiopathic hemolytic anemia."},
            {"letter": "E", "meaning": "Elevated liver enzymes", "explanation": "AST/ALT elevation due to hepatic injury."},
            {"letter": "L", "meaning": "Low platelets", "explanation": "Thrombocytopenia from consumption."},
            {"letter": "L", "meaning": "Liver damage", "explanation": "Risk of infarction/rupture."},
            {"letter": "P", "meaning": "Poor outcomes", "explanation": "Maternal-fetal morbidity/mortality risk."},
        ]},
    ]
    psychiatric = [
        {"title": "Wernicke-Korsakoff Syndrome", "code": "CAN'T SEE, CAN'T WALK, CAN'T THINK", "rows": [
            {"letter": "CAN'T SEE", "meaning": "Ophthalmoplegia", "explanation": "Eye movement abnormalities (nystagmus, gaze palsies)."},
            {"letter": "CAN'T WALK", "meaning": "Ataxia", "explanation": "Unsteady gait and poor coordination."},
            {"letter": "CAN'T THINK", "meaning": "Confusion", "explanation": "Global confusion, disorientation, and memory impairment."},
            {"letter": "Cause", "meaning": "Thiamine deficiency", "explanation": "Typically due to chronic alcohol use disorder."},
            {"letter": "Tip", "meaning": "Clinical exam tip", "explanation": "Give thiamine BEFORE glucose."},
        ]},
        {"title": "Schizophrenia Primary Symptoms", "code": "4 A's (Bleuler's)", "rows": [
            {"letter": "A", "meaning": "Affect (flat)", "explanation": "Reduced emotional expression (blunted/flat)."},
            {"letter": "A", "meaning": "Autism (withdrawal)", "explanation": "Social withdrawal and detachment."},
            {"letter": "A", "meaning": "Ambivalence", "explanation": "Contradictory thoughts and indecision."},
            {"letter": "A", "meaning": "Association loosened", "explanation": "Disorganized thought and loose associations."},
        ]},
        {"title": "Schizophrenia Positive Symptoms", "code": "HALL", "rows": [
            {"letter": "H", "meaning": "Hallucinations", "explanation": "False sensory perceptions (often auditory)."},
            {"letter": "A", "meaning": "Agitation", "explanation": "Restlessness/aggression/catatonic excitement."},
            {"letter": "L", "meaning": "Loosened thoughts", "explanation": "Formal thought disorder, derailment."},
            {"letter": "L", "meaning": "Loss of reality", "explanation": "Delusions and impaired insight."},
        ]},
        {"title": "Tricyclic Antidepressants (TCAs)", "code": "3 Ts", "rows": [
            {"letter": "T", "meaning": "Tofranil (Imipramine)", "explanation": "Prototype TCA; also used in enuresis."},
            {"letter": "T", "meaning": "Tryptanol (Amitriptyline)", "explanation": "Sedating TCA option."},
            {"letter": "T", "meaning": "TCA class", "explanation": "Shared mechanism and risk profile."},
        ]},
        {"title": "TCA Side Effects", "code": "ABC", "rows": [
            {"letter": "A", "meaning": "Anticholinergic", "explanation": "Dry mouth, constipation, urinary retention."},
            {"letter": "B", "meaning": "Blurred vision", "explanation": "Anticholinergic ciliary effect."},
            {"letter": "C", "meaning": "Cardiotoxicity", "explanation": "Conduction delay and arrhythmia risk, especially overdose."},
        ]},
        {"title": "Intellectual Disability Care Plan", "code": "CARE", "rows": [
            {"letter": "C", "meaning": "Consistency", "explanation": "Stable routines/caregivers reduce anxiety."},
            {"letter": "A", "meaning": "Assist ADLs", "explanation": "Support function while preserving dignity."},
            {"letter": "R", "meaning": "Reinforce learning", "explanation": "Repetition and positive reinforcement."},
            {"letter": "E", "meaning": "Encourage independence", "explanation": "Promote maximal safe self-care."},
        ]},
        {"title": "Cognitive Disorders Assessment", "code": "MEMORY", "rows": [
            {"letter": "M", "meaning": "Memory loss", "explanation": "Recent memory usually affected early."},
            {"letter": "E", "meaning": "Executive dysfunction", "explanation": "Planning/organization decline."},
            {"letter": "M", "meaning": "Mood changes", "explanation": "Depression, anxiety, irritability, apathy."},
            {"letter": "O", "meaning": "Orientation loss", "explanation": "Disorientation to time/place/person."},
            {"letter": "R", "meaning": "Reasoning impaired", "explanation": "Poor judgment and abstract thinking."},
            {"letter": "Y", "meaning": "Year confusion", "explanation": "Time confusion and anosognosia may appear."},
        ]},
        {"title": "Alcohol Withdrawal", "code": "WITHDRAWAL", "rows": [
            {"letter": "W", "meaning": "Weakness", "explanation": "Generalized fatigue and malaise."},
            {"letter": "I", "meaning": "Irritability", "explanation": "Mood lability and agitation."},
            {"letter": "T", "meaning": "Tremors", "explanation": "Fine hand tremors early in withdrawal."},
            {"letter": "H", "meaning": "Hallucinations", "explanation": "Visual/tactile/auditory hallucinations may occur."},
            {"letter": "D", "meaning": "Delirium", "explanation": "Severe withdrawal can progress to DTs."},
            {"letter": "R", "meaning": "Restlessness", "explanation": "Psychomotor agitation."},
            {"letter": "A", "meaning": "Anxiety", "explanation": "Marked unease/panic symptoms."},
            {"letter": "W", "meaning": "Withdrawal seizures", "explanation": "Generalized tonic-clonic seizures risk."},
            {"letter": "A", "meaning": "Autonomic instability", "explanation": "Tachycardia, HTN, fever, diaphoresis."},
            {"letter": "L", "meaning": "Loss of appetite", "explanation": "Anorexia and GI upset are common."},
        ]},
    ]
    sections = [
        {
            "slug": "fundamentals-in-nursing",
            "title": "Fundamentals in Nursing",
            "subtitle": "Core day-to-day nursing recall mnemonics.",
            "icon": "bx bx-book-reader",
            "mnemonics": fundamentals,
        },
        {
            "slug": "medical-surgical-nursing",
            "title": "Medical & Surgical Nursing",
            "subtitle": "High-yield NMC-focused medical-surgical mnemonics and tips.",
            "icon": "bx bx-plus-medical",
            "mnemonics": medical_surgical,
        },
        {
            "slug": "midwifery-nmc-high-yield",
            "title": "Midwifery (NMC High-Yield)",
            "subtitle": "Focused mnemonics for ANC, labour, postpartum, and newborn exam prep.",
            "icon": "bx bx-baby-carriage",
            "mnemonics": midwifery,
        },
        {
            "slug": "psychiatric-nursing",
            "title": "Psychiatric Nursing",
            "subtitle": "Mental health high-yield mnemonics for NMC exams and clinical recall.",
            "icon": "bx bx-brain",
            "mnemonics": psychiatric,
        },
        ANATOMY_MNEMONICS_SECTION,
    ]
    for section in sections:
        for mnemonic in section.get("mnemonics") or []:
            if mnemonic.get("rows") or mnemonic.get("body_html"):
                continue
            mnemonic["rows"] = _build_mnemonic_rows(mnemonic.get("items") or [])
    return sections


def _build_mnemonic_rows(items):
    rows = []
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        if " - " in text:
            key, rest = text.split(" - ", 1)
            key = key.strip()
            rest = rest.strip()
            explanation = ""
            if "(" in rest and ")" in rest:
                try:
                    explanation = rest[rest.index("(") + 1:rest.rindex(")")].strip()
                except Exception:
                    explanation = ""
            rows.append({
                "letter": key,
                "meaning": rest,
                "explanation": explanation,
            })
            continue
        if ":" in text:
            key, rest = text.split(":", 1)
            rows.append({
                "letter": key.strip(),
                "meaning": rest.strip(),
                "explanation": "",
            })
            continue
        rows.append({
            "letter": "Tip",
            "meaning": text,
            "explanation": "",
        })
    return rows


def student_mnemonics_section(request, section_slug):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    sections = _student_mnemonic_sections()
    section = next((s for s in sections if s["slug"] == section_slug), None)
    if not section:
        return redirect("/dashboard/mnemonics-guide/")

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "mnemonics_guide",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "section": section,
        "mnemonics_total": len(section.get("mnemonics") or []),
    }
    return render(request, "dashboard/student_mnemonics_section.html", context)


def student_books_library(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/nclex/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    try:
        rows = (
            admin.table("resource_books")
            .select("*")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1000)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    nclex_books = []
    ielts_books = []
    for row in rows:
        item = {
            "id": row.get("id"),
            "title": (row.get("title") or "").strip() or "Untitled Book",
            "description": (row.get("description") or "").strip(),
            "external_url": (row.get("external_url") or "").strip(),
            "download_url": _book_download_url(row.get("external_url")),
            "category": (row.get("category") or "").strip().lower(),
        }
        if item["category"] == "ielts":
            ielts_books.append(item)
        else:
            nclex_books.append(item)

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "student_books",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "nclex_books": nclex_books,
        "ielts_books": ielts_books,
    }
    return render(request, "dashboard/student_books_library.html", context)


def student_general_tests(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    profile_rows = admin.table("profiles").select("programme").eq("id", user_id).limit(1).execute().data or []
    programme = (profile_rows[0].get("programme") if profile_rows else "") or ""

    # Fetch all active (non-submitted) attempts so we know which tests have a
    # paused or in-progress session the student can resume.
    active_attempts_map = {}  # full_paper_title → attempt row
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
        if programme in PROGRAMME_PAPERS:
            grouped = _question_bank_counts_by_paper(admin, programme)
        else:
            grouped = _question_bank_counts_by_paper_chunked(admin, programme)

        # Build General Test batches in order:
        # Test 1 consumes the first N questions, Test 2 consumes the next N, etc.
        tests = []
        for paper, count in grouped.items():
            if count <= 0:
                continue
            batch_size = _general_test_question_count(paper)
            num_batches = _general_test_num_batches(count, batch_size)
            for test_number in range(1, num_batches + 1):
                actual_q = _general_test_actual_batch_size(count, batch_size, test_number)
                if actual_q <= 0:
                    continue
                full_title = f"{paper} — General Test {test_number}"
                active = active_attempts_map.get(full_title)

                entry = {
                    "paper_title": paper,
                    "test_number": test_number,
                    "question_count": actual_q,
                    "duration_minutes": _general_test_batch_duration_minutes(paper, batch_size, actual_q),
                    "attempt_id": active["id"] if active else None,
                    "attempt_status": active.get("status", "in_progress") if active else None,
                }
                tests.append(entry)

        # Group by paper in the UI: sort by paper title, then test number.
        tests.sort(key=lambda x: (x["paper_title"], x["test_number"]))
    except Exception:
        tests = []

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "general_tests",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "tests": tests,
    }
    return render(request, "dashboard/student_general_tests.html", context)


def student_general_test_start(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    paper_title = request.GET.get("paper_title", "").strip()
    if not paper_title:
        return redirect("/dashboard/general-tests/")

    test_number_raw = request.GET.get("test_number", "1").strip()
    try:
        test_number = max(1, int(test_number_raw))
    except Exception:
        test_number = 1

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    full_title = f"{paper_title} — General Test {test_number}"

    # If a paused or in-progress attempt already exists for this exact test,
    # redirect straight to it — never create a duplicate.
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
        return redirect(f"/dashboard/general-tests/attempt/{existing[0]['id']}/")

    profile_rows = admin.table("profiles").select("programme").eq("id", user_id).limit(1).execute().data or []
    programme = (profile_rows[0].get("programme") if profile_rows else "") or ""

    batch_size = _general_test_question_count(paper_title)

    # Fetch enough questions to cover this batch, then slice in Python.
    # This avoids needing server-side offset support.
    limit_needed = test_number * batch_size
    rows = (
        admin.table("question_bank")
        .select("id")
        .eq("programme", programme)
        .eq("paper_title", paper_title)
        .order("created_at", desc=False)
        .limit(limit_needed)
        .execute()
        .data
        or []
    )
    question_ids = [r.get("id") for r in rows if r.get("id")]
    start_idx = (test_number - 1) * batch_size
    end_idx = start_idx + batch_size
    batch_ids = question_ids[start_idx:end_idx]

    if not batch_ids:
        return redirect("/dashboard/general-tests/")

    attempt_insert = (
        admin.table("general_test_attempts")
        .insert({
            "student_id": user_id,
            "programme": programme,
            "paper_title": full_title,
            "time_limit_minutes": _general_test_batch_duration_minutes(
                paper_title, batch_size, len(batch_ids)
            ),
            "total_questions": len(batch_ids),
            "status": "in_progress",
        })
        .execute()
    )
    attempt = attempt_insert.data[0]
    links = [
        {"attempt_id": attempt["id"], "question_id": qid, "question_order": idx}
        for idx, qid in enumerate(batch_ids, start=1)
    ]
    admin.table("general_test_attempt_questions").insert(links).execute()
    return redirect(f"/dashboard/general-tests/attempt/{attempt['id']}/")


def student_general_test_attempt(request, attempt_id):
    guard = _require_login(request)
    if guard:
        return guard

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)

    attempt_rows = (
        admin.table("general_test_attempts")
        .select("*")
        .eq("id", str(attempt_id))
        .eq("student_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not attempt_rows:
        return redirect("/dashboard/general-tests/")
    attempt = attempt_rows[0]

    if attempt.get("submitted_at"):
        return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/result/")

    # If paused, show the paused landing screen
    if attempt.get("status") == "paused":
        return render(request, "dashboard/student_general_test_paused.html", {
            "full_name": request.session.get("full_name", "Student"),
            "email": request.session.get("email", ""),
            "role": "student",
            "active_page": "general_tests",
            "student_unread_notifications": unread_count,
            "has_unread_notifications": unread_count > 0,
            "attempt": attempt,
            "paused_remaining_seconds": attempt.get("paused_remaining_seconds") or 0,
        })

    links = (
        admin.table("general_test_attempt_questions")
        .select("question_order, question_id, question_bank(id, question_text, options, correct_option, explanation)")
        .eq("attempt_id", str(attempt_id))
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    questions = []
    for row in links:
        qb = row.get("question_bank")
        if qb:
            questions.append({
                "order": row.get("question_order"),
                "id": qb.get("id"),
                "question_text": qb.get("question_text"),
                "options": qb.get("options") or {},
                "correct_option": qb.get("correct_option"),
            })
    if not questions:
        return redirect("/dashboard/general-tests/")

    answers = (
        admin.table("general_test_attempt_answers")
        .select("*")
        .eq("attempt_id", str(attempt_id))
        .execute()
        .data
        or []
    )
    answer_map = {a["question_id"]: a for a in answers}

    action = request.POST.get("action", "").strip()
    current_index = int(request.POST.get("current_index", "1") or "1")
    current_index = max(1, min(current_index, len(questions)))

    if request.method == "POST":
        qid = request.POST.get("question_id", "").strip()
        selected_option = request.POST.get("selected_option", "").strip().upper()
        is_bookmarked = request.POST.get("is_bookmarked") == "1"
        is_flagged = request.POST.get("is_flagged") == "1"

        # Compute remaining seconds at the moment this POST arrived
        now_utc = datetime.now(timezone.utc)
        if attempt.get("resumed_at"):
            resumed_at = datetime.fromisoformat(attempt["resumed_at"].replace("Z", "+00:00"))
            remaining_secs_db = int(attempt.get("paused_remaining_seconds") or 0)
            end_time = resumed_at + timedelta(seconds=remaining_secs_db)
        else:
            started_at = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
            end_time = started_at + timedelta(minutes=int(attempt.get("time_limit_minutes") or 90))
        remaining_now = max(0, int((end_time - now_utc).total_seconds()))

        # ---- Pause ----
        if action == "pause":
            target = next((q for q in questions if q["id"] == qid), None)
            if target:
                payload = {
                    "attempt_id": str(attempt_id),
                    "question_id": qid,
                    "selected_option": selected_option or None,
                    "is_correct": bool(selected_option and selected_option == (target.get("correct_option") or "").upper()),
                    "is_bookmarked": is_bookmarked,
                    "is_flagged": is_flagged,
                    "answered_at": datetime.now(timezone.utc).isoformat(),
                }
                existing_ans = answer_map.get(qid)
                if existing_ans:
                    admin.table("general_test_attempt_answers").update(payload).eq("id", existing_ans["id"]).execute()
                else:
                    admin.table("general_test_attempt_answers").insert(payload).execute()

            admin.table("general_test_attempts").update({
                "status": "paused",
                "paused_remaining_seconds": remaining_now,
                "paused_at_index": current_index,
            }).eq("id", str(attempt_id)).execute()
            return redirect("/dashboard/general-tests/")

        # ---- Resume (POST from paused screen) ----
        if action == "resume":
            admin.table("general_test_attempts").update({
                "status": "in_progress",
                "resumed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", str(attempt_id)).execute()
            return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/")

        # ---- Normal answer save ----
        target = next((q for q in questions if q["id"] == qid), None)
        if target:
            payload = {
                "attempt_id": str(attempt_id),
                "question_id": qid,
                "selected_option": selected_option or None,
                "is_correct": bool(selected_option and selected_option == (target.get("correct_option") or "").upper()),
                "is_bookmarked": is_bookmarked,
                "is_flagged": is_flagged,
                "answered_at": datetime.now(timezone.utc).isoformat(),
            }
            existing_ans = answer_map.get(qid)
            if existing_ans:
                admin.table("general_test_attempt_answers").update(payload).eq("id", existing_ans["id"]).execute()
            else:
                admin.table("general_test_attempt_answers").insert(payload).execute()

        if action == "prev":
            current_index = max(1, current_index - 1)
        elif action == "next":
            current_index = min(len(questions), current_index + 1)
        elif action == "submit":
            final_answers = (
                admin.table("general_test_attempt_answers")
                .select("*")
                .eq("attempt_id", str(attempt_id))
                .execute()
                .data
                or []
            )
            total_questions = len(questions)
            correct_answers = sum(1 for x in final_answers if x.get("is_correct"))
            percentage = round((correct_answers / total_questions) * 100, 2) if total_questions else 0.0
            admin.table("general_test_attempts").update({
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "score": correct_answers,
                "correct_answers": correct_answers,
                "percentage": percentage,
                "status": "submitted",
            }).eq("id", str(attempt_id)).execute()
            return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/result/")

        answers = (
            admin.table("general_test_attempt_answers")
            .select("*")
            .eq("attempt_id", str(attempt_id))
            .execute()
            .data
            or []
        )
        answer_map = {a["question_id"]: a for a in answers}

    # ---- Compute remaining for GET (or after navigation) ----
    now_utc = datetime.now(timezone.utc)
    if attempt.get("resumed_at"):
        resumed_at = datetime.fromisoformat(attempt["resumed_at"].replace("Z", "+00:00"))
        remaining_secs_db = int(attempt.get("paused_remaining_seconds") or 0)
        end_time = resumed_at + timedelta(seconds=remaining_secs_db)
    else:
        started_at = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
        end_time = started_at + timedelta(minutes=int(attempt.get("time_limit_minutes") or 90))
    remaining = max(0, int((end_time - now_utc).total_seconds()))

    if remaining == 0:
        final_answers = (
            admin.table("general_test_attempt_answers")
            .select("*")
            .eq("attempt_id", str(attempt_id))
            .execute()
            .data
            or []
        )
        total_questions = len(questions)
        correct_answers = sum(1 for x in final_answers if x.get("is_correct"))
        percentage = round((correct_answers / total_questions) * 100, 2) if total_questions else 0.0
        admin.table("general_test_attempts").update({
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "score": correct_answers,
            "correct_answers": correct_answers,
            "percentage": percentage,
            "status": "submitted",
        }).eq("id", str(attempt_id)).execute()
        return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/result/")

    # Restore last saved question index when coming back after a pause (GET)
    if request.method == "GET" and attempt.get("paused_at_index"):
        current_index = int(attempt["paused_at_index"])
        current_index = max(1, min(current_index, len(questions)))

    current_question = questions[current_index - 1]
    current_answer = answer_map.get(current_question["id"], {})
    for q in questions:
        _ans = answer_map.get(q["id"], {})
        q["nav_flagged"] = bool(_ans.get("is_flagged"))
    answered_count = sum(1 for q in questions if q["id"] in answer_map and (answer_map[q["id"]].get("selected_option") or ""))
    bookmarked_count = sum(1 for a in answer_map.values() if a.get("is_bookmarked"))
    flagged_count = sum(1 for a in answer_map.values() if a.get("is_flagged"))

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "general_tests",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "attempt": attempt,
        "questions": questions,
        "current_question": current_question,
        "current_index": current_index,
        "current_answer": current_answer,
        "remaining_seconds": remaining,
        "answered_count": answered_count,
        "bookmarked_count": bookmarked_count,
        "flagged_count": flagged_count,
    }
    return render(request, "dashboard/student_general_test_attempt.html", context)


def student_general_test_result(request, attempt_id):
    guard = _require_login(request)
    if guard:
        return guard
    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

    rows = (
        admin.table("general_test_attempts")
        .select("*")
        .eq("id", str(attempt_id))
        .eq("student_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return redirect("/dashboard/general-tests/")
    attempt = rows[0]
    if not attempt.get("submitted_at"):
        return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/")

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "general_tests",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "attempt": attempt,
        "encouragement": _score_message(float(attempt.get("percentage") or 0)),
    }
    return render(request, "dashboard/student_general_test_result.html", context)


def admin_quizzes(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    success = None
    error = None
    admin_user_id = request.session.get("user_id")

    if request.method == "POST":
        try:
            items = _parse_practice_quiz_items_json(request.POST.get("questions_json", ""))
            pending_rows = [
                {
                    "created_by": str(admin_user_id),
                    "question_text": it["question_text"],
                    "option_a": it["option_a"],
                    "option_b": it["option_b"],
                    "option_c": it["option_c"],
                    "correct_option": it["correct_option"],
                    "explanation": it.get("explanation"),
                }
                for it in items
            ]
            admin.table("practice_quiz_pending_questions").insert(pending_rows).execute()
            created_titles = _flush_practice_quiz_pending_to_batches(admin, admin_user_id)

            pend_resp = (
                admin.table("practice_quiz_pending_questions")
                .select("id", count="exact", head=True)
                .eq("created_by", str(admin_user_id))
                .execute()
            )
            left = pend_resp.count or 0

            msg_bits = [f"Queued {len(items)} question(s)."]
            if created_titles:
                msg_bits.append("Created: " + ", ".join(created_titles) + ".")
            if left:
                msg_bits.append(
                    f"{left} in your queue (oldest first). Add {10 - left} more to create the next quiz batch."
                )
            else:
                msg_bits.append("Your queue is empty.")
            success = " ".join(msg_bits)
        except Exception as exc:
            error = str(exc)

    quiz_list = []
    try:
        quiz_list = (
            admin.table("practice_quizzes")
            .select("id, title, sort_index, created_at, is_published")
            .order("sort_index", desc=False)
            .limit(100)
            .execute()
            .data
            or []
        )
        for qz in quiz_list:
            cnt_resp = (
                admin.table("practice_quiz_questions")
                .select("id", count="exact", head=True)
                .eq("quiz_id", qz["id"])
                .execute()
            )
            qz["question_count"] = cnt_resp.count or 0
    except Exception as exc:
        error = error or str(exc)
        quiz_list = []

    pending_count = 0
    try:
        pr = (
            admin.table("practice_quiz_pending_questions")
            .select("id", count="exact", head=True)
            .eq("created_by", str(admin_user_id))
            .execute()
        )
        pending_count = pr.count or 0
    except Exception:
        pending_count = 0

    if pending_count == 0:
        need_more_for_batch = 10
    else:
        need_more_for_batch = 10 - pending_count

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "quizzes",
        "quiz_list": quiz_list,
        "success": success,
        "error": error,
        "pending_count": pending_count,
        "need_more_for_batch": need_more_for_batch,
        "quiz_json_placeholder": QUIZ_ADMIN_JSON_PLACEHOLDER,
        "quiz_sample_data": _QUIZ_ADMIN_ITEMS,
    }
    return render(request, "dashboard/admin_quizzes.html", context)


PRACTICE_QUIZ_QUESTION_TARGET = 10


def _practice_quiz_question_count(admin, quiz_id):
    try:
        resp = (
            admin.table("practice_quiz_questions")
            .select("id", count="exact", head=True)
            .eq("quiz_id", str(quiz_id))
            .execute()
        )
        return int(resp.count or 0)
    except Exception:
        return 0


def _practice_quiz_renumber_question_orders(admin, quiz_id):
    rows = (
        admin.table("practice_quiz_questions")
        .select("id, question_order")
        .eq("quiz_id", str(quiz_id))
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    for i, r in enumerate(rows, start=1):
        if int(r.get("question_order") or 0) != i:
            admin.table("practice_quiz_questions").update({"question_order": i}).eq("id", r["id"]).execute()


def admin_quiz_questions_api(request):
    """
    Admin JSON API: list / create / update / delete practice_quiz_questions for a quiz.
    GET  ?quiz_id=uuid
    POST JSON { action: list|create|update|delete, ... }
    """
    if not request.session.get("user_id"):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)
    if request.session.get("role") != "admin":
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    admin = _supabase_admin()

    def _quiz_payload(quiz_id):
        qrows = (
            admin.table("practice_quizzes")
            .select("id, title, is_published, sort_index")
            .eq("id", str(quiz_id))
            .limit(1)
            .execute()
            .data
            or []
        )
        if not qrows:
            return None, JsonResponse({"ok": False, "error": "Quiz not found"}, status=404)
        qz = qrows[0]
        questions = (
            admin.table("practice_quiz_questions")
            .select(
                "id, quiz_id, question_order, question_text, option_a, option_b, option_c, "
                "correct_option, explanation"
            )
            .eq("quiz_id", str(quiz_id))
            .order("question_order", desc=False)
            .execute()
            .data
            or []
        )
        n = len(questions)
        qz["question_count"] = n
        qz["student_ready"] = n == PRACTICE_QUIZ_QUESTION_TARGET
        return {"quiz": qz, "questions": questions}, None

    if request.method == "GET":
        quiz_id = (request.GET.get("quiz_id") or "").strip()
        if not quiz_id:
            return JsonResponse({"ok": False, "error": "quiz_id required"}, status=400)
        payload, err = _quiz_payload(quiz_id)
        if err:
            return err
        return JsonResponse({"ok": True, **payload})

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    action = (body.get("action") or "").strip().lower()

    def _norm_opt(x):
        return (x or "").strip().upper()

    if action == "list":
        quiz_id = (body.get("quiz_id") or "").strip()
        if not quiz_id:
            return JsonResponse({"ok": False, "error": "quiz_id required"}, status=400)
        payload, err = _quiz_payload(quiz_id)
        if err:
            return err
        return JsonResponse({"ok": True, **payload})

    if action == "update":
        qid = (body.get("id") or "").strip()
        if not qid:
            return JsonResponse({"ok": False, "error": "id required"}, status=400)
        existing = (
            admin.table("practice_quiz_questions")
            .select("id, quiz_id")
            .eq("id", qid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not existing:
            return JsonResponse({"ok": False, "error": "Question not found"}, status=404)
        quiz_id = str(existing[0]["quiz_id"])
        qt = (body.get("question_text") or "").strip()
        oa = (body.get("option_a") or "").strip()
        ob = (body.get("option_b") or "").strip()
        oc = (body.get("option_c") or "").strip()
        co = _norm_opt(body.get("correct_option"))
        expl = (body.get("explanation") or "").strip()
        if not qt or not oa or not ob or not oc:
            return JsonResponse({"ok": False, "error": "All options and question text are required."}, status=400)
        if co not in ("A", "B", "C"):
            return JsonResponse({"ok": False, "error": "correct_option must be A, B, or C."}, status=400)
        admin.table("practice_quiz_questions").update({
            "question_text": qt,
            "option_a": oa,
            "option_b": ob,
            "option_c": oc,
            "correct_option": co,
            "explanation": expl or None,
        }).eq("id", qid).execute()
        payload, err = _quiz_payload(quiz_id)
        if err:
            return err
        return JsonResponse({"ok": True, **payload})

    if action == "delete":
        qid = (body.get("id") or "").strip()
        if not qid:
            return JsonResponse({"ok": False, "error": "id required"}, status=400)
        row = (
            admin.table("practice_quiz_questions")
            .select("id, quiz_id")
            .eq("id", qid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not row:
            return JsonResponse({"ok": False, "error": "Question not found"}, status=404)
        quiz_id = str(row[0]["quiz_id"])
        admin.table("practice_quiz_questions").delete().eq("id", qid).execute()
        _practice_quiz_renumber_question_orders(admin, quiz_id)
        payload, err = _quiz_payload(quiz_id)
        if err:
            return err
        return JsonResponse({"ok": True, **payload})

    if action == "create":
        quiz_id = (body.get("quiz_id") or "").strip()
        if not quiz_id:
            return JsonResponse({"ok": False, "error": "quiz_id required"}, status=400)
        qcheck = (
            admin.table("practice_quizzes")
            .select("id")
            .eq("id", quiz_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not qcheck:
            return JsonResponse({"ok": False, "error": "Quiz not found"}, status=404)
        cnt = _practice_quiz_question_count(admin, quiz_id)
        if cnt >= PRACTICE_QUIZ_QUESTION_TARGET:
            return JsonResponse(
                {"ok": False, "error": f"This quiz already has {PRACTICE_QUIZ_QUESTION_TARGET} questions. Delete one to add another."},
                status=400,
            )
        qt = (body.get("question_text") or "").strip()
        oa = (body.get("option_a") or "").strip()
        ob = (body.get("option_b") or "").strip()
        oc = (body.get("option_c") or "").strip()
        co = _norm_opt(body.get("correct_option"))
        expl = (body.get("explanation") or "").strip()
        if not qt or not oa or not ob or not oc:
            return JsonResponse({"ok": False, "error": "All options and question text are required."}, status=400)
        if co not in ("A", "B", "C"):
            return JsonResponse({"ok": False, "error": "correct_option must be A, B, or C."}, status=400)
        next_order = cnt + 1
        admin.table("practice_quiz_questions").insert({
            "quiz_id": quiz_id,
            "question_order": next_order,
            "question_text": qt,
            "option_a": oa,
            "option_b": ob,
            "option_c": oc,
            "correct_option": co,
            "explanation": expl or None,
        }).execute()
        _practice_quiz_renumber_question_orders(admin, quiz_id)
        payload, err = _quiz_payload(quiz_id)
        if err:
            return err
        return JsonResponse({"ok": True, **payload})

    return JsonResponse({"ok": False, "error": "Unknown action"}, status=400)


def student_quizzes(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "quizzes")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

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

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "quizzes",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "quizzes": quizzes,
    }
    return render(request, "dashboard/student_quizzes.html", context)


def student_quiz_take(request, quiz_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "quizzes")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

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
        return redirect("/dashboard/quizzes/")
    quiz = quiz_rows[0]

    questions = (
        admin.table("practice_quiz_questions")
        .select("id, question_order, question_text, option_a, option_b, option_c")
        .eq("quiz_id", str(quiz_id))
        .order("question_order", desc=False)
        .execute()
        .data
        or []
    )
    if len(questions) != 10:
        return redirect("/dashboard/quizzes/")

    for q in questions:
        q["options"] = {
            "A": q.get("option_a") or "",
            "B": q.get("option_b") or "",
            "C": q.get("option_c") or "",
        }

    if request.method == "POST":
        correct_map = {
            q["id"]: (q.get("correct_option") or "").upper()
            for q in (
                admin.table("practice_quiz_questions")
                .select("id, correct_option")
                .eq("quiz_id", str(quiz_id))
                .execute()
                .data
                or []
            )
        }
        answer_rows = []
        correct_count = 0
        for q in questions:
            qid = q["id"]
            sel = (request.POST.get(f"answer_{qid}") or "").strip().upper()
            if sel not in ("A", "B", "C"):
                sel = ""
            corr = correct_map.get(qid, "")
            is_ok = bool(sel and sel == corr)
            if is_ok:
                correct_count += 1
            answer_rows.append({
                "question_id": qid,
                "selected_option": sel or None,
                "is_correct": is_ok,
            })

        percentage = round((correct_count / 10) * 100, 2)
        att = (
            admin.table("practice_quiz_attempts")
            .insert({
                "student_id": user_id,
                "quiz_id": str(quiz_id),
                "total_questions": 10,
                "score": correct_count,
                "correct_answers": correct_count,
                "percentage": percentage,
            })
            .execute()
            .data
        )[0]
        attempt_id = att["id"]
        for row in answer_rows:
            row["attempt_id"] = attempt_id
        admin.table("practice_quiz_attempt_answers").insert(answer_rows).execute()
        return redirect(f"/dashboard/quizzes/attempt/{attempt_id}/result/")

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "quizzes",
        "hide_assistant_bot": True,
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "quiz": quiz,
        "questions": questions,
    }
    return render(request, "dashboard/student_quiz_take.html", context)


def student_quiz_result(request, attempt_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "quizzes")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

    rows = (
        admin.table("practice_quiz_attempts")
        .select("id, quiz_id, percentage, score, total_questions, submitted_at")
        .eq("id", str(attempt_id))
        .eq("student_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return redirect("/dashboard/quizzes/")
    attempt = rows[0]

    quiz_rows = (
        admin.table("practice_quizzes")
        .select("title")
        .eq("id", attempt.get("quiz_id"))
        .limit(1)
        .execute()
        .data
        or []
    )
    quiz_title = quiz_rows[0].get("title") if quiz_rows else "Quiz"

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "quizzes",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "attempt": attempt,
        "quiz_title": quiz_title,
        "encouragement": _score_message(float(attempt.get("percentage") or 0)),
    }
    return render(request, "dashboard/student_quiz_result.html", context)


# ---------------------------------------------------------------------------
# Report Question (AJAX POST)
# ---------------------------------------------------------------------------

def screenshot_attempt_api(request):
    """
    AJAX POST: record a screenshot attempt for the current student.
    - 1st offence  → returns {ok, attempt_number: 1, disabled: false}
    - 2nd+ offence → disables account + kills session,
                     returns {ok, attempt_number: N, disabled: true}
    """
    from django.http import JsonResponse

    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)

    user_id = request.session.get("user_id")
    if not user_id:
        return JsonResponse({"ok": False, "reason": "not_authenticated"}, status=401)

    try:
        admin = _supabase_admin()

        profile_resp = (
            admin.table("profiles")
            .select("screenshot_attempts, is_active")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if not profile_resp.data:
            return JsonResponse({"ok": False}, status=404)

        profile = profile_resp.data[0]
        current = profile.get("screenshot_attempts") or 0
        new_count = current + 1

        if new_count >= 2:
            # Second offence: disable account and evict session immediately
            admin.table("profiles").update({
                "screenshot_attempts": new_count,
                "is_active": False,
            }).eq("id", user_id).execute()
            admin.table("active_sessions").delete().eq("user_id", user_id).execute()
            request.session.flush()
            return JsonResponse({"ok": True, "attempt_number": new_count, "disabled": True})
        else:
            # First offence: warn only
            admin.table("profiles").update({
                "screenshot_attempts": new_count,
            }).eq("id", user_id).execute()
            return JsonResponse({"ok": True, "attempt_number": new_count, "disabled": False})

    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


def student_report_question(request):
    """Receives an AJAX POST from any question-answering page and inserts a
    row into question_reports.  Returns JSON {ok, already?, error?}."""
    from django.http import JsonResponse

    guard = _require_login(request)
    if guard:
        return JsonResponse({"ok": False, "error": "Not logged in"}, status=403)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed"}, status=405)

    user_id     = request.session.get("user_id")
    question_id = request.POST.get("question_id", "").strip()
    reason      = request.POST.get("reason", "").strip()
    notes       = request.POST.get("notes", "").strip()

    VALID_REASONS = {"Wrong answer", "Wrong or missing explanation"}
    if not question_id or reason not in VALID_REASONS:
        return JsonResponse({"ok": False, "error": "Invalid input."}, status=400)

    admin = _supabase_admin()
    try:
        existing = (
            admin.table("question_reports")
            .select("id")
            .eq("question_id", question_id)
            .eq("student_id", user_id)
            .eq("status", "pending")
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            return JsonResponse({"ok": True, "already": True})

        admin.table("question_reports").insert({
            "question_id": question_id,
            "student_id":  user_id,
            "reason":      reason,
            "notes":       notes or None,
            "status":      "pending",
        }).execute()
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Admin: Reported Questions
# ---------------------------------------------------------------------------

def admin_reported_questions(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin   = _supabase_admin()
    success = error = None

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "resolve":
            report_id = request.POST.get("report_id", "").strip()
            if report_id:
                try:
                    admin.table("question_reports").update({"status": "resolved"}).eq("id", report_id).execute()
                    success = "Report marked as resolved."
                except Exception as exc:
                    error = str(exc)

        elif action == "update_question":
            question_id = request.POST.get("question_id", "").strip()
            report_id   = request.POST.get("report_id", "").strip()
            if question_id:
                payload        = {}
                correct_option = request.POST.get("correct_option", "").strip().upper()
                explanation    = request.POST.get("explanation", "").strip()
                if correct_option in ("A", "B", "C"):
                    payload["correct_option"] = correct_option
                if explanation:
                    payload["explanation"] = explanation
                try:
                    if payload:
                        admin.table("question_bank").update(payload).eq("id", question_id).execute()
                    if report_id:
                        admin.table("question_reports").update({"status": "resolved"}).eq("id", report_id).execute()
                    success = "Question updated and report resolved."
                except Exception as exc:
                    error = str(exc)

    # Fetch all reports (latest first) with joined question data
    try:
        reports = (
            admin.table("question_reports")
            .select("*, question_bank(id, question_text, paper_title, programme, correct_option, explanation, options)")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        reports = []

    # Resolve student names
    student_ids = list({r["student_id"] for r in reports if r.get("student_id")})
    name_map = {}
    if student_ids:
        try:
            profiles = (
                admin.table("profiles")
                .select("id, full_name")
                .in_("id", student_ids)
                .execute()
                .data
                or []
            )
            name_map = {p["id"]: p.get("full_name", "Unknown") for p in profiles}
        except Exception:
            pass

    for r in reports:
        qb = r.get("question_bank") or {}
        r["student_name"]  = name_map.get(r.get("student_id"), "Unknown")
        r["question_text"] = qb.get("question_text", "—")
        r["paper_title"]   = qb.get("paper_title", "—")
        r["programme"]     = qb.get("programme", "—")
        r["correct_option"]= qb.get("correct_option", "—")
        r["explanation"]   = qb.get("explanation") or ""
        r["options"]       = qb.get("options") or {}
        r["q_id"]          = qb.get("id") or r.get("question_id")

    pending_count = sum(1 for r in reports if r.get("status") == "pending")

    context = {
        "full_name":    request.session.get("full_name", "Admin"),
        "email":        request.session.get("email", ""),
        "role":         "admin",
        "active_page":  "reported_questions",
        "reports":      reports,
        "pending_count": pending_count,
        "success":      success,
        "error":        error,
    }
    return render(request, "dashboard/admin_reported_questions.html", context)


def student_bookmarks(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

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
            # defensive; relation nesting may vary by PostgREST version
            title = (nested.get("title") or title)
        bookmarks.append({
            "source": "Mock",
            "source_name": title,
            "question_text": q.get("question_text") or "Question",
            "options": q.get("options") or {},
            "selected_option": row.get("selected_option") or "",
            "explanation": q.get("explanation") or "",
            "created_at": row.get("created_at") or "",
            "submitted_at": mock_attempt.get("submitted_at") or "",
        })

    for row in general_rows:
        q = row.get("question_bank") or {}
        attempt = row.get("general_test_attempts") or {}
        bookmarks.append({
            "source": "General Test",
            "source_name": attempt.get("paper_title") or "General Test",
            "question_text": q.get("question_text") or "Question",
            "options": q.get("options") or {},
            "selected_option": row.get("selected_option") or "",
            "explanation": q.get("explanation") or "",
            "created_at": row.get("created_at") or "",
            "submitted_at": attempt.get("submitted_at") or "",
        })

    bookmarks.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "bookmarks",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "bookmarks": bookmarks,
    }
    return render(request, "dashboard/student_bookmarks.html", context)


def student_performance(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "performance")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

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
    avg_score = round(sum(float(a.get("percentage") or 0) for a in all_attempts) / len(all_attempts), 1) if all_attempts else 0

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "performance",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "all_attempts": all_attempts,
        "avg_score": avg_score,
    }
    return render(request, "dashboard/student_performance.html", context)


def student_attempt_review(request, test_type, attempt_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "performance")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    admin = _supabase_admin()

    test_type = (test_type or "").strip().lower()
    review_rows = []
    header = {"title": "Attempt Review", "subtitle": ""}

    if test_type == "mock":
        attempt_rows = (
            admin.table("mock_attempts")
            .select("id, mock_exam_id, percentage, score, total_questions, submitted_at")
            .eq("id", str(attempt_id))
            .eq("student_id", user_id)
            .not_.is_("submitted_at", "null")
            .limit(1)
            .execute()
            .data
            or []
        )
        if not attempt_rows:
            return redirect("/dashboard/performance/")
        attempt = attempt_rows[0]
        exam_rows = admin.table("mock_exams").select("title").eq("id", attempt.get("mock_exam_id")).limit(1).execute().data or []
        exam_title = exam_rows[0].get("title") if exam_rows else "Mock Exam"
        header = {"title": exam_title, "subtitle": f"Score: {attempt.get('score', 0)} / {attempt.get('total_questions', 0)}"}

        review_rows = (
            admin.table("mock_attempt_answers")
            .select("selected_option, is_correct, is_bookmarked, is_flagged, question_bank(question_text, options, correct_option, explanation)")
            .eq("attempt_id", str(attempt_id))
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )
    elif test_type == "general":
        attempt_rows = (
            admin.table("general_test_attempts")
            .select("id, paper_title, percentage, score, total_questions, submitted_at")
            .eq("id", str(attempt_id))
            .eq("student_id", user_id)
            .not_.is_("submitted_at", "null")
            .limit(1)
            .execute()
            .data
            or []
        )
        if not attempt_rows:
            return redirect("/dashboard/performance/")
        attempt = attempt_rows[0]
        header = {
            "title": attempt.get("paper_title") or "General Test",
            "subtitle": f"Score: {attempt.get('score', 0)} / {attempt.get('total_questions', 0)}",
        }

        review_rows = (
            admin.table("general_test_attempt_answers")
            .select("selected_option, is_correct, is_bookmarked, is_flagged, question_bank(question_text, options, correct_option, explanation)")
            .eq("attempt_id", str(attempt_id))
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )
    elif test_type == "quiz":
        attempt_rows = (
            admin.table("practice_quiz_attempts")
            .select("id, quiz_id, percentage, score, total_questions, submitted_at")
            .eq("id", str(attempt_id))
            .eq("student_id", user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not attempt_rows:
            return redirect("/dashboard/performance/")
        attempt = attempt_rows[0]
        quiz_rows = (
            admin.table("practice_quizzes")
            .select("title")
            .eq("id", attempt.get("quiz_id"))
            .limit(1)
            .execute()
            .data
            or []
        )
        qtitle = quiz_rows[0].get("title") if quiz_rows else "Quiz"
        header = {
            "title": qtitle,
            "subtitle": f"Score: {attempt.get('score', 0)} / {attempt.get('total_questions', 0)}",
        }

        review_rows = (
            admin.table("practice_quiz_attempt_answers")
            .select("question_id, selected_option, is_correct")
            .eq("attempt_id", str(attempt_id))
            .execute()
            .data
            or []
        )
    else:
        return redirect("/dashboard/performance/")

    questions = []
    if test_type == "quiz":
        q_ids = [r.get("question_id") for r in review_rows if r.get("question_id")]
        q_meta = {}
        if q_ids:
            q_rows = (
                admin.table("practice_quiz_questions")
                .select("id, question_text, option_a, option_b, option_c, correct_option, explanation, question_order")
                .in_("id", q_ids)
                .execute()
                .data
                or []
            )
            q_meta = {r["id"]: r for r in q_rows}

        def _ord_quiz_row(row):
            pq = q_meta.get(row.get("question_id")) or {}
            return pq.get("question_order") or 0

        for idx, row in enumerate(sorted(review_rows, key=_ord_quiz_row), start=1):
            pq = q_meta.get(row.get("question_id")) or {}
            questions.append({
                "index": idx,
                "question_text": pq.get("question_text") or "Question",
                "options": {
                    "A": pq.get("option_a") or "",
                    "B": pq.get("option_b") or "",
                    "C": pq.get("option_c") or "",
                },
                "selected_option": row.get("selected_option") or "",
                "correct_option": pq.get("correct_option") or "",
                "is_correct": bool(row.get("is_correct")),
                "is_bookmarked": False,
                "is_flagged": False,
                "explanation": pq.get("explanation") or "",
            })
    else:
        for idx, row in enumerate(review_rows, start=1):
            q = row.get("question_bank") or {}
            questions.append({
                "index": idx,
                "question_text": q.get("question_text") or "Question",
                "options": q.get("options") or {},
                "selected_option": row.get("selected_option") or "",
                "correct_option": q.get("correct_option") or "",
                "is_correct": bool(row.get("is_correct")),
                "is_bookmarked": bool(row.get("is_bookmarked")),
                "is_flagged": bool(row.get("is_flagged")),
                "explanation": q.get("explanation") or "",
            })

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "performance",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "header": header,
        "questions": questions,
        "test_type": test_type,
    }
    return render(request, "dashboard/student_attempt_review.html", context)


def _flashcard_session_key(user_id, review_date_iso):
    return f"flashcard_reviewed:{user_id}:{review_date_iso}"


def _session_flashcard_reviewed_ids(request, user_id, review_date_iso):
    key = _flashcard_session_key(user_id, review_date_iso)
    raw = request.session.get(key) or []
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if x is not None}


def _append_session_flashcard_review(request, user_id, review_date_iso, question_id):
    key = _flashcard_session_key(user_id, review_date_iso)
    lst = [str(x) for x in (request.session.get(key) or [])]
    q = str(question_id)
    if q not in lst:
        lst.append(q)
    request.session[key] = lst
    request.session.modified = True


def _remove_from_session_flashcard_review(request, user_id, review_date_iso, question_id):
    key = _flashcard_session_key(user_id, review_date_iso)
    lst = [str(x) for x in (request.session.get(key) or [])]
    q = str(question_id)
    lst = [x for x in lst if x != q]
    if lst:
        request.session[key] = lst
    else:
        request.session.pop(key, None)
    request.session.modified = True


def _save_flashcard_daily_review(admin, user_id, review_date_iso, question_id):
    """
    Record one flashcard as reviewed for (user_id, date, question_id).
    Tries upsert (composite conflict), then insert, then update — survives
    missing/odd Supabase upsert constraints in some projects.
    """
    row = {
        "student_id": user_id,
        "review_date": review_date_iso,
        "question_id": question_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    log = logging.getLogger(__name__)
    try:
        admin.table("flashcard_daily_reviews").upsert(
            [row],
            on_conflict="student_id,review_date,question_id",
        ).execute()
        return True
    except Exception as exc1:
        log.warning("flashcard_daily_reviews upsert failed: %s", exc1)
    try:
        admin.table("flashcard_daily_reviews").insert(row).execute()
        return True
    except Exception as exc2:
        log.warning("flashcard_daily_reviews insert failed: %s", exc2)
    try:
        admin.table("flashcard_daily_reviews").update(
            {"reviewed_at": row["reviewed_at"]}
        ).eq("student_id", user_id).eq(
            "review_date", review_date_iso
        ).eq("question_id", question_id).execute()
        return True
    except Exception as exc3:
        log.exception("flashcard_daily_reviews could not be saved: %s", exc3)
    return False


def student_flashcards(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")
    gate = _plan_gate(request, "flashcards")
    if gate:
        return gate

    admin = _supabase_admin()
    user_id = str(request.session.get("user_id") or "").strip()
    unread_count = _student_unread_count(user_id)
    today = datetime.now(timezone.utc).date().isoformat()

    question_pool = []
    try:
        question_pool = (
            admin.table("question_bank")
            .select("id, question_text, options, correct_option, explanation")
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )
    except Exception:
        question_pool = []

    if not question_pool:
        context = {
            "full_name": request.session.get("full_name", "Student"),
            "email": request.session.get("email", ""),
            "role": "student",
            "active_page": "flashcards",
            "student_unread_notifications": unread_count,
            "has_unread_notifications": unread_count > 0,
            "empty_state": True,
        }
        return render(request, "dashboard/student_flashcards.html", context)

    rng = random.Random(f"{user_id}:{today}")
    shuffled = list(question_pool)
    rng.shuffle(shuffled)
    daily_cards = shuffled[:10]
    # Normalize IDs to strings for safe comparisons across form POST / DB payloads.
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
    reviewed_ids = {
        str(row.get("question_id"))
        for row in reviewed_rows
        if row.get("question_id") is not None
    }
    reviewed_ids |= _session_flashcard_reviewed_ids(request, user_id, today)

    if request.method == "POST":
        question_id = str(request.POST.get("question_id", "").strip())
        if question_id and question_id in daily_id_set and user_id:
            if _save_flashcard_daily_review(admin, user_id, today, question_id):
                _remove_from_session_flashcard_review(request, user_id, today, question_id)
            else:
                _append_session_flashcard_review(request, user_id, today, question_id)
        return redirect("/dashboard/flashcards/")

    pending_cards = [
        card for card in daily_cards
        if str(card.get("id")) not in reviewed_ids
    ]
    reviewed_count = len(reviewed_ids.intersection(daily_id_set))
    done_for_today = reviewed_count >= len(daily_cards)

    current_card = pending_cards[0] if pending_cards else None

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "flashcards",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "today": today,
        "total_cards": len(daily_cards),
        "reviewed_count": reviewed_count,
        "done_for_today": done_for_today,
        "current_card": current_card,
        "pending_count": len(pending_cards),
    }
    return render(request, "dashboard/student_flashcards.html", context)


LECTURE_NOTE_FONT_SIZES_PX = (12, 14, 16, 18, 20, 22, 24)

# Embedded in content_html so font size persists even if Supabase has no content_font_size_px column.
_LECTURE_FS_PREFIX = "__LECTURE_FS_"
_LECTURE_FS_SUFFIX = "__ENDFS__"


def _coerce_lecture_note_font_px(raw):
    allowed = LECTURE_NOTE_FONT_SIZES_PX
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return 16
    return min(allowed, key=lambda a: abs(a - n))


def _lecture_meta_decode(stored):
    """Split stored HTML into (font_px_or_None, body_for_editor)."""
    h = stored or ""
    if not h.startswith(_LECTURE_FS_PREFIX):
        return None, h
    start = len(_LECTURE_FS_PREFIX)
    end = h.find(_LECTURE_FS_SUFFIX, start)
    if end < 0:
        return None, h
    try:
        px = int(h[start:end])
    except ValueError:
        return None, h
    body = h[end + len(_LECTURE_FS_SUFFIX) :]
    return _coerce_lecture_note_font_px(px), body


def _lecture_meta_encode(font_px, inner_html):
    """Prefix body with font meta (invisible to students when stripped in view)."""
    px = _coerce_lecture_note_font_px(font_px)
    _, body = _lecture_meta_decode(inner_html or "")
    body = (body or "").strip()
    return f"{_LECTURE_FS_PREFIX}{px}{_LECTURE_FS_SUFFIX}{body}"


def _lecture_display_font_px(note_row):
    """Resolve font size: embedded meta in content_html first, then DB column."""
    meta_px, _ = _lecture_meta_decode(note_row.get("content_html") or "")
    if meta_px is not None:
        return meta_px
    col = note_row.get("content_font_size_px")
    if col is not None and str(col).strip() != "":
        try:
            return _coerce_lecture_note_font_px(col)
        except Exception:
            pass
    return 16


def _lecture_body_for_render(note_row):
    _, body = _lecture_meta_decode(note_row.get("content_html") or "")
    return body


DEFAULT_LECTURE_NOTE_CATEGORY_LABEL = "Surgery"


def _group_student_lecture_notes(notes_list):
    """Bucket notes by optional `category`; blank uses DEFAULT_LECTURE_NOTE_CATEGORY_LABEL."""
    buckets = {}
    for n in notes_list:
        raw = (n.get("category") or "").strip()
        label = raw if raw else DEFAULT_LECTURE_NOTE_CATEGORY_LABEL
        buckets.setdefault(label, []).append(n)
    order = sorted(
        buckets.keys(),
        key=lambda x: (0 if x == DEFAULT_LECTURE_NOTE_CATEGORY_LABEL else 1, x.lower()),
    )
    return [{"name": name, "notes": buckets[name]} for name in order]


def student_lecture_notes(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/lecture-notes/")
    gate = _plan_gate(request, "lecture_notes")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    notes = []
    try:
        raw_notes = (
            _supabase_admin()
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
                "render_html": _lecture_body_for_render(n),
                "render_font_px": _lecture_display_font_px(n),
            }
            for n in raw_notes
        ]
    except Exception:
        notes = []

    note_groups = _group_student_lecture_notes(notes)

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "lecture_notes",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "notes": notes,
        "note_groups": note_groups,
    }
    return render(request, "dashboard/student_lecture_notes.html", context)


def admin_lecture_notes(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    success = None
    error = None
    edit_id = request.GET.get("edit", "").strip()
    edit_note = None

    def _insert_lecture_note_with_fallback(payload):
        row = dict(payload)
        while True:
            try:
                admin.table("lecture_notes").insert(row).execute()
                break
            except Exception as exc:
                err_txt = str(exc).lower()
                if "category" in row and (
                    "category" in err_txt
                    or "pgrst204" in err_txt
                    or "schema cache" in err_txt
                ):
                    row.pop("category", None)
                    continue
                if "content_font_size_px" in row and (
                    "content_font_size_px" in err_txt
                    or "pgrst204" in err_txt
                    or "schema cache" in err_txt
                ):
                    row.pop("content_font_size_px", None)
                    continue
                raise

    def _html_from_note_json(item):
        direct_html = str(item.get("content_html", "")).strip()
        if direct_html:
            return direct_html

        plain_content = str(item.get("content", "")).strip()
        if plain_content:
            lines = [ln.strip() for ln in plain_content.splitlines() if ln.strip()]
            if lines:
                return "".join(f"<p>{escape(ln)}</p>" for ln in lines)

        sections = item.get("sections")
        if not isinstance(sections, list) or not sections:
            return ""

        html_parts = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            heading = str(sec.get("heading", "")).strip()
            if heading:
                html_parts.append(f"<h3>{escape(heading)}</h3>")
            paragraphs = sec.get("paragraphs") or []
            if isinstance(paragraphs, list):
                for p in paragraphs:
                    txt = str(p).strip()
                    if txt:
                        html_parts.append(f"<p>{escape(txt)}</p>")
            points = sec.get("points") or []
            if isinstance(points, list):
                clean_points = [escape(str(pt).strip()) for pt in points if str(pt).strip()]
                if clean_points:
                    html_parts.append("<ul>" + "".join(f"<li>{pt}</li>" for pt in clean_points) + "</ul>")
        return "".join(html_parts)

    if request.method == "POST":
        action = request.POST.get("action", "create").strip()
        try:
            if action == "delete":
                note_id = request.POST.get("note_id", "").strip()
                if not note_id:
                    raise ValueError("Note ID is required.")
                admin.table("lecture_notes").delete().eq("id", note_id).execute()
                success = "Lecture note deleted."
                return redirect("/admin-panel/lecture-notes/")
            if action == "update":
                note_id = request.POST.get("note_id", "").strip()
                if not note_id:
                    raise ValueError("Note ID is required.")
                topic = request.POST.get("topic", "").strip()
                subtopic = request.POST.get("subtopic", "").strip()
                category = request.POST.get("category", "").strip()
                content_html = request.POST.get("content_html", "").strip()
                if not topic or not content_html:
                    raise ValueError("Topic and content are required.")
                font_px = _coerce_lecture_note_font_px(request.POST.get("content_font_size_px"))
                stored_html = _lecture_meta_encode(font_px, content_html)
                row = {
                    "topic": topic,
                    "subtopic": subtopic or None,
                    "category": category or None,
                    "content_html": stored_html,
                    "content_font_size_px": font_px,
                }
                while True:
                    try:
                        admin.table("lecture_notes").update(row).eq("id", note_id).execute()
                        break
                    except Exception as exc:
                        err_txt = str(exc).lower()
                        if "category" in row and (
                            "category" in err_txt
                            or "pgrst204" in err_txt
                            or "schema cache" in err_txt
                        ):
                            row.pop("category", None)
                            continue
                        if "content_font_size_px" in row and (
                            "content_font_size_px" in err_txt
                            or "pgrst204" in err_txt
                            or "schema cache" in err_txt
                        ):
                            row.pop("content_font_size_px", None)
                            continue
                        raise
                success = "Lecture note updated."
                return redirect("/admin-panel/lecture-notes/")
            if action == "bulk_create_json":
                raw_json = request.POST.get("bulk_notes_json", "").strip()
                if not raw_json:
                    raise ValueError("Please provide JSON data for bulk upload.")
                try:
                    decoded = json.loads(raw_json)
                except Exception as exc:
                    raise ValueError(f"Invalid JSON: {exc}")

                if isinstance(decoded, dict):
                    items = decoded.get("notes")
                    if items is None:
                        raise ValueError(
                            "JSON object must contain a 'notes' array. "
                            "Example: {\"notes\":[{\"topic\":\"...\",\"content_html\":\"...\"}]}"
                        )
                elif isinstance(decoded, list):
                    items = decoded
                else:
                    raise ValueError("JSON payload must be an array or an object with a 'notes' array.")

                if not isinstance(items, list) or not items:
                    raise ValueError("No notes found. Provide at least one note object.")

                created_count = 0
                for index, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        raise ValueError(f"Item #{index} must be an object.")
                    topic = str(item.get("topic", "")).strip()
                    subtopic = str(item.get("subtopic", "")).strip()
                    content_html = _html_from_note_json(item)
                    if not topic or not content_html:
                        raise ValueError(
                            f"Item #{index} requires 'topic' and content. "
                            "Use 'content_html', 'content', or structured 'sections'."
                        )
                    font_px = _coerce_lecture_note_font_px(item.get("content_font_size_px"))
                    stored_html = _lecture_meta_encode(font_px, content_html)
                    ins = {
                        "topic": topic,
                        "subtopic": subtopic or None,
                        "category": None,
                        "content_html": stored_html,
                        "is_published": True,
                        "created_by": request.session.get("user_id"),
                        "content_font_size_px": font_px,
                    }
                    _insert_lecture_note_with_fallback(ins)
                    created_count += 1
                success = f"{created_count} lecture notes uploaded successfully."
                return redirect("/admin-panel/lecture-notes/")

            topic = request.POST.get("topic", "").strip()
            subtopic = request.POST.get("subtopic", "").strip()
            category = request.POST.get("category", "").strip()
            content_html = request.POST.get("content_html", "").strip()
            if not topic or not content_html:
                raise ValueError("Topic and content are required.")
            font_px = _coerce_lecture_note_font_px(request.POST.get("content_font_size_px"))
            stored_html = _lecture_meta_encode(font_px, content_html)
            ins = {
                "topic": topic,
                "subtopic": subtopic or None,
                "category": category or None,
                "content_html": stored_html,
                "is_published": True,
                "created_by": request.session.get("user_id"),
                "content_font_size_px": font_px,
            }
            _insert_lecture_note_with_fallback(ins)
            success = "Lecture note saved."
            return redirect("/admin-panel/lecture-notes/")
        except Exception as exc:
            error = str(exc)
        if request.method == "POST" and error:
            edit_id = request.POST.get("preserved_edit_id", "").strip() or edit_id

    notes = []
    try:
        notes = (
            admin.table("lecture_notes")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        error = error or str(exc)

    if edit_id:
        try:
            rows = (
                admin.table("lecture_notes")
                .select("*")
                .eq("id", edit_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            edit_note = rows[0] if rows else None
        except Exception:
            edit_note = None
        if not edit_note:
            edit_id = ""

    if edit_note:
        disp_px = _lecture_display_font_px(edit_note)
        body_only = _lecture_body_for_render(edit_note)
        edit_note = {
            **edit_note,
            "content_html": body_only,
            "content_font_size_px": disp_px,
        }

    edit_form_initial = None
    if edit_note:
        edit_form_initial = {
            "id": str(edit_note.get("id")),
            "topic": edit_note.get("topic") or "",
            "subtopic": edit_note.get("subtopic") or "",
            "category": edit_note.get("category") or "",
            "content_html": edit_note.get("content_html") or "",
            "font_px": int(edit_note.get("content_font_size_px") or 16),
        }

    notes = [{**n, "display_font_px": _lecture_display_font_px(n)} for n in notes]

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "lecture_notes",
        "notes": notes,
        "success": success,
        "error": error,
        "edit_note": edit_note,
        "edit_form_initial": edit_form_initial,
        "lecture_note_font_sizes": LECTURE_NOTE_FONT_SIZES_PX,
        "lecture_note_default_category": DEFAULT_LECTURE_NOTE_CATEGORY_LABEL,
    }
    return render(request, "dashboard/admin_lecture_notes.html", context)


def dosage_calculator(request):
    guard = _require_login(request)
    if guard:
        return guard

    user_id = request.session.get("user_id")
    save_success = False
    save_error = None

    if request.method == "POST" and request.POST.get("action") == "save":
        calc_type = request.POST.get("calc_type", "")
        inputs_summary = request.POST.get("inputs_summary", "")
        result_text = request.POST.get("result_text", "")
        try:
            _supabase_admin().table("dosage_calculations").insert({
                "user_id": user_id,
                "calc_type": calc_type,
                "inputs_summary": inputs_summary,
                "result_text": result_text,
            }).execute()
            save_success = True
        except Exception as e:
            save_error = "Could not save calculation. Please try again."

    # Fetch recent history
    calc_history = []
    try:
        resp = (
            _supabase_admin()
            .table("dosage_calculations")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        calc_history = resp.data or []
    except Exception:
        pass

    unread_count = _student_unread_count(user_id)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "dosage_calculator",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "calc_history": calc_history,
        "save_success": save_success,
        "save_error": save_error,
    }
    return render(request, "dashboard/dosage_calculator.html", context)


# ---------------------------------------------------------------------------
# Drug Cards
# ---------------------------------------------------------------------------

DRUG_CATEGORIES = [
    "Cardiovascular",
    "Respiratory",
    "Gastrointestinal",
    "Central Nervous System",
    "Endocrine & Metabolic",
    "Anti-infective",
    "Musculoskeletal",
    "Renal & Urinary",
    "Obstetric & Gynaecological",
    "Emergency & Critical Care",
    "Oncology & Immunology",
    "Haematology",
]


def drug_cards(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/drug-cards/")

    query = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()
    user_id = request.session.get("user_id")

    drugs = []
    try:
        q = _supabase_admin().table("drug_cards").select("*").eq("is_active", True)
        if selected_category:
            q = q.eq("category", selected_category)
        if query:
            q = q.ilike("drug_name", f"%{query}%")
        drugs = q.order("drug_name").execute().data or []
    except Exception:
        pass

    unread_count = _student_unread_count(user_id)
    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "drug_cards",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "drugs": drugs,
        "categories": DRUG_CATEGORIES,
        "selected_category": selected_category,
        "query": query,
        "drug_count": len(drugs),
    }
    return render(request, "dashboard/drug_cards.html", context)


def admin_drug_cards(request):
    guard = _require_admin(request)
    if guard:
        return guard

    query = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()
    admin = _supabase_admin()
    success = None
    error = None

    # Handle success GET params from redirects
    success_param = request.GET.get("success", "")
    if success_param == "created":
        success = "Drug card created successfully."
    elif success_param == "updated":
        success = "Drug card updated successfully."

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "delete":
            drug_id = request.POST.get("drug_id", "").strip()
            try:
                admin.table("drug_cards").delete().eq("id", drug_id).execute()
                success = "Drug card deleted successfully."
            except Exception as exc:
                error = str(exc)

    drugs = []
    total_count = 0
    try:
        q = admin.table("drug_cards").select("*")
        if selected_category:
            q = q.eq("category", selected_category)
        if query:
            q = q.ilike("drug_name", f"%{query}%")
        drugs = q.order("drug_name").execute().data or []
        total_count = len(drugs)
    except Exception as exc:
        error = error or str(exc)

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "drug_cards",
        "drugs": drugs,
        "categories": DRUG_CATEGORIES,
        "query": query,
        "selected_category": selected_category,
        "total_count": total_count,
        "success": success,
        "error": error,
    }
    return render(request, "dashboard/admin_drug_cards.html", context)


def admin_drug_card_form(request, drug_id=None):
    guard = _require_admin(request)
    if guard:
        return guard

    admin_client = _supabase_admin()
    is_edit = drug_id is not None
    drug = {}
    error = None

    DRUG_FIELDS = [
        "drug_name", "brand_names", "drug_class", "category", "routes",
        "mechanism", "indications", "contraindications", "side_effects",
        "drug_interactions", "dosage_info", "special_populations",
        "nursing_considerations", "patient_education",
    ]

    if is_edit:
        try:
            rows = admin_client.table("drug_cards").select("*").eq("id", str(drug_id)).limit(1).execute().data
            if not rows:
                return redirect("/admin-panel/drug-cards/")
            drug = rows[0]
        except Exception:
            return redirect("/admin-panel/drug-cards/")

    if request.method == "POST":
        action = request.POST.get("action", "")
        payload = {f: request.POST.get(f, "").strip() for f in DRUG_FIELDS}
        if not payload.get("drug_name") or not payload.get("drug_class") or not payload.get("category"):
            error = "Drug Name, Drug Class and Category are required."
        else:
            try:
                if action == "create":
                    payload["is_active"] = True
                    admin_client.table("drug_cards").insert(payload).execute()
                    return redirect("/admin-panel/drug-cards/?success=created")
                elif action == "update" and is_edit:
                    admin_client.table("drug_cards").update(payload).eq("id", str(drug_id)).execute()
                    return redirect("/admin-panel/drug-cards/?success=updated")
            except Exception as exc:
                error = str(exc)
                drug = payload

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "drug_cards",
        "drug": drug,
        "categories": DRUG_CATEGORIES,
        "is_edit": is_edit,
        "drug_id": str(drug_id) if drug_id else "",
        "error": error,
    }
    return render(request, "dashboard/admin_drug_card_form.html", context)


# ===========================================================================
# COMMUNITY MODULE
# ===========================================================================

# ---------------------------------------------------------------------------
# Community helpers
# ---------------------------------------------------------------------------

def _community_unread_count(user_id):
    """Number of unread community notifications for a user."""
    try:
        resp = (
            _supabase_admin()
            .table("community_notifications")
            .select("id", count="exact", head=True)
            .eq("user_id", str(user_id))
            .eq("is_read", False)
            .execute()
        )
        return resp.count or 0
    except Exception:
        return 0


def _base_community_ctx(request):
    """Shared context dict for all community pages."""
    user_id = request.session.get("user_id")
    return {
        "full_name": request.session.get("full_name", ""),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "student_unread_notifications": _student_unread_count(user_id),
        "community_unread": _community_unread_count(user_id),
    }


def _get_community_by_slug(slug):
    try:
        rows = (
            _supabase_admin()
            .table("communities")
            .select("*")
            .eq("slug", slug)
            .eq("is_active", True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _is_member(user_id, community_id):
    try:
        rows = (
            _supabase_admin()
            .table("community_members")
            .select("id")
            .eq("user_id", str(user_id))
            .eq("community_id", str(community_id))
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)
    except Exception:
        return False


def _push_community_notif(user_id, notif_type, message, post_id=None, comment_id=None):
    try:
        payload = {
            "user_id": str(user_id),
            "notif_type": notif_type,
            "message": message,
            "is_read": False,
        }
        if post_id:
            payload["post_id"] = str(post_id)
        if comment_id:
            payload["comment_id"] = str(comment_id)
        _supabase_admin().table("community_notifications").insert(payload).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Student: community home — list all 6 communities
# ---------------------------------------------------------------------------

def community_home(request):
    guard = _require_login(request)
    if guard:
        return guard
    gate = _plan_gate(request, "community")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    db = _supabase_admin()

    try:
        communities = db.table("communities").select("*").eq("is_active", True).order("name").execute().data or []
    except Exception:
        communities = []

    # Which ones has this user joined?
    try:
        memberships = (
            db.table("community_members")
            .select("community_id")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )
        joined_ids = {m["community_id"] for m in memberships}
    except Exception:
        joined_ids = set()

    for c in communities:
        c["is_member"] = c["id"] in joined_ids

    ctx = _base_community_ctx(request)
    ctx.update({
        "active_page": "community",
        "communities": communities,
        "joined_count": len(joined_ids),
    })
    return render(request, "dashboard/community_home.html", ctx)


# ---------------------------------------------------------------------------
# Student: join / leave a community
# ---------------------------------------------------------------------------

def community_join(request, slug):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect(f"/dashboard/community/{slug}/")

    user_id = request.session.get("user_id")
    community = _get_community_by_slug(slug)
    if not community:
        return redirect("/dashboard/community/")

    try:
        _supabase_admin().table("community_members").insert({
            "community_id": community["id"],
            "user_id": str(user_id),
        }).execute()
    except Exception:
        pass  # Already a member — ignore duplicate

    return redirect(f"/dashboard/community/{slug}/")


def community_leave(request, slug):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect(f"/dashboard/community/{slug}/")

    user_id = request.session.get("user_id")
    community = _get_community_by_slug(slug)
    if not community:
        return redirect("/dashboard/community/")

    try:
        _supabase_admin().table("community_members").delete().eq(
            "community_id", community["id"]
        ).eq("user_id", str(user_id)).execute()
    except Exception:
        pass

    return redirect(f"/dashboard/community/{slug}/")


# ---------------------------------------------------------------------------
# Student: community detail — list posts
# ---------------------------------------------------------------------------

def community_detail(request, slug):
    guard = _require_login(request)
    if guard:
        return guard
    gate = _plan_gate(request, "community")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    community = _get_community_by_slug(slug)
    if not community:
        return redirect("/dashboard/community/")

    is_member = _is_member(user_id, community["id"])
    db = _supabase_admin()

    # Filters
    sort = request.GET.get("sort", "latest")  # latest | most_liked | unanswered
    search_q = request.GET.get("q", "").strip()

    try:
        query = (
            db.table("community_posts")
            .select("*")
            .eq("community_id", community["id"])
            .eq("is_deleted", False)
        )
        if search_q:
            query = query.ilike("title", f"%{search_q}%")
        if sort == "most_liked":
            query = query.order("reaction_count", desc=True)
        elif sort == "unanswered":
            query = query.eq("comment_count", 0).order("created_at", desc=True)
        else:
            query = query.order("is_pinned", desc=True).order("created_at", desc=True)

        posts = query.limit(50).execute().data or []
    except Exception:
        posts = []

    # Which posts has the user liked?
    try:
        user_reactions = (
            db.table("post_reactions")
            .select("post_id")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )
        liked_post_ids = {r["post_id"] for r in user_reactions}
    except Exception:
        liked_post_ids = set()

    for p in posts:
        p["user_liked"] = p["id"] in liked_post_ids

    ctx = _base_community_ctx(request)
    ctx.update({
        "active_page": "community",
        "community": community,
        "posts": posts,
        "is_member": is_member,
        "sort": sort,
        "search_q": search_q,
    })
    return render(request, "dashboard/community_detail.html", ctx)


# ---------------------------------------------------------------------------
# Student: create post
# ---------------------------------------------------------------------------

def community_create_post(request, slug):
    guard = _require_login(request)
    if guard:
        return guard

    community = _get_community_by_slug(slug)
    if not community:
        return redirect("/dashboard/community/")

    user_id = request.session.get("user_id")

    # Only members can post
    if not _is_member(user_id, community["id"]):
        return redirect(f"/dashboard/community/{slug}/")

    if request.method != "POST":
        return redirect(f"/dashboard/community/{slug}/")

    title = request.POST.get("title", "").strip()
    content = request.POST.get("content", "").strip()

    if not title or not content:
        return redirect(f"/dashboard/community/{slug}/")

    try:
        result = _supabase_admin().table("community_posts").insert({
            "community_id": community["id"],
            "author_id": str(user_id),
            "author_name": request.session.get("full_name", "Student"),
            "title": title[:200],
            "content": content[:5000],
        }).execute()
        if result.data:
            post_id = result.data[0]["id"]
            return redirect(f"/dashboard/community/{slug}/post/{post_id}/")
    except Exception:
        pass

    return redirect(f"/dashboard/community/{slug}/")


# ---------------------------------------------------------------------------
# Student: post detail — view post + threaded comments
# ---------------------------------------------------------------------------

def community_post_detail(request, slug, post_id):
    guard = _require_login(request)
    if guard:
        return guard
    gate = _plan_gate(request, "community")
    if gate:
        return gate

    user_id = request.session.get("user_id")
    community = _get_community_by_slug(slug)
    if not community:
        return redirect("/dashboard/community/")

    db = _supabase_admin()

    try:
        rows = (
            db.table("community_posts")
            .select("*")
            .eq("id", str(post_id))
            .eq("is_deleted", False)
            .limit(1)
            .execute()
            .data
        )
        post = rows[0] if rows else None
    except Exception:
        post = None

    if not post:
        return redirect(f"/dashboard/community/{slug}/")

    # Has user liked the post?
    try:
        liked = bool(
            db.table("post_reactions")
            .select("id")
            .eq("post_id", str(post_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        liked = False
    post["user_liked"] = liked

    # Fetch top-level comments
    try:
        all_comments = (
            db.table("post_comments")
            .select("*")
            .eq("post_id", str(post_id))
            .eq("is_deleted", False)
            .order("created_at")
            .execute()
            .data or []
        )
    except Exception:
        all_comments = []

    # Which comments has the user liked?
    try:
        liked_comments = (
            db.table("comment_reactions")
            .select("comment_id")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )
        liked_comment_ids = {c["comment_id"] for c in liked_comments}
    except Exception:
        liked_comment_ids = set()

    # Build threaded structure: top-level + nested replies
    top_comments = []
    comment_map = {}
    for c in all_comments:
        c["user_liked"] = c["id"] in liked_comment_ids
        c["replies"] = []
        comment_map[c["id"]] = c

    for c in all_comments:
        if c.get("parent_id") and c["parent_id"] in comment_map:
            comment_map[c["parent_id"]]["replies"].append(c)
        elif not c.get("parent_id"):
            top_comments.append(c)

    is_member = _is_member(user_id, community["id"])

    # Mark related notifications as read
    try:
        db.table("community_notifications").update({"is_read": True}).eq(
            "user_id", str(user_id)
        ).eq("post_id", str(post_id)).eq("is_read", False).execute()
    except Exception:
        pass

    ctx = _base_community_ctx(request)
    ctx.update({
        "active_page": "community",
        "community": community,
        "post": post,
        "top_comments": top_comments,
        "is_member": is_member,
        "is_author": str(post["author_id"]) == str(user_id),
    })
    return render(request, "dashboard/community_post.html", ctx)


# ---------------------------------------------------------------------------
# Student: add comment / reply
# ---------------------------------------------------------------------------

def community_add_comment(request, post_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/dashboard/community/")

    user_id = request.session.get("user_id")
    db = _supabase_admin()

    # Verify post exists
    try:
        rows = (
            db.table("community_posts")
            .select("id, community_id, author_id, title")
            .eq("id", str(post_id))
            .eq("is_deleted", False)
            .limit(1)
            .execute()
            .data
        )
        post = rows[0] if rows else None
    except Exception:
        post = None

    if not post:
        return redirect("/dashboard/community/")

    # Get community slug for redirect
    try:
        comm_rows = (
            db.table("communities")
            .select("slug")
            .eq("id", post["community_id"])
            .limit(1)
            .execute()
            .data
        )
        slug = comm_rows[0]["slug"] if comm_rows else "general-qa"
    except Exception:
        slug = "general-qa"

    content = request.POST.get("content", "").strip()
    parent_id = request.POST.get("parent_id", "").strip() or None

    if not content:
        return redirect(f"/dashboard/community/{slug}/post/{post_id}/")

    try:
        db.table("post_comments").insert({
            "post_id": str(post_id),
            "parent_id": parent_id,
            "author_id": str(user_id),
            "author_name": request.session.get("full_name", "Student"),
            "content": content[:2000],
        }).execute()

        # Notify original post author (not self)
        if str(post["author_id"]) != str(user_id):
            notif_type = "new_reply" if parent_id else "new_comment"
            msg = f"{request.session.get('full_name', 'Someone')} {'replied to a comment on' if parent_id else 'commented on'} your post: \"{post['title'][:60]}\""
            _push_community_notif(post["author_id"], notif_type, msg, post_id=post_id)

    except Exception:
        pass

    return redirect(f"/dashboard/community/{slug}/post/{post_id}/")


# ---------------------------------------------------------------------------
# Student: react (like/unlike) a post — JSON or redirect
# ---------------------------------------------------------------------------

def community_react_post(request, post_id):
    guard = _require_login(request)
    if guard:
        return redirect("/login/")
    if request.method != "POST":
        return redirect("/dashboard/community/")

    user_id = request.session.get("user_id")
    db = _supabase_admin()

    # Check existing reaction
    try:
        existing = (
            db.table("post_reactions")
            .select("id")
            .eq("post_id", str(post_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
            .data
        )
        if existing:
            # Unlike
            db.table("post_reactions").delete().eq("post_id", str(post_id)).eq("user_id", str(user_id)).execute()
            liked = False
        else:
            # Like
            db.table("post_reactions").insert({
                "post_id": str(post_id),
                "user_id": str(user_id),
            }).execute()
            liked = True

            # Notify post author
            post_rows = db.table("community_posts").select("author_id, title").eq("id", str(post_id)).limit(1).execute().data
            if post_rows and str(post_rows[0]["author_id"]) != str(user_id):
                msg = f"{request.session.get('full_name', 'Someone')} liked your post: \"{post_rows[0]['title'][:60]}\""
                _push_community_notif(post_rows[0]["author_id"], "post_liked", msg, post_id=post_id)

        # Get updated count
        count_resp = db.table("community_posts").select("reaction_count").eq("id", str(post_id)).limit(1).execute().data
        count = count_resp[0]["reaction_count"] if count_resp else 0

        from django.http import JsonResponse
        return JsonResponse({"liked": liked, "count": count})
    except Exception as e:
        from django.http import JsonResponse
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Student: react (like/unlike) a comment
# ---------------------------------------------------------------------------

def community_react_comment(request, comment_id):
    guard = _require_login(request)
    if guard:
        return redirect("/login/")
    if request.method != "POST":
        return redirect("/dashboard/community/")

    user_id = request.session.get("user_id")
    db = _supabase_admin()

    try:
        existing = (
            db.table("comment_reactions")
            .select("id")
            .eq("comment_id", str(comment_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
            .data
        )
        if existing:
            db.table("comment_reactions").delete().eq("comment_id", str(comment_id)).eq("user_id", str(user_id)).execute()
            liked = False
        else:
            db.table("comment_reactions").insert({
                "comment_id": str(comment_id),
                "user_id": str(user_id),
            }).execute()
            liked = True

        count_resp = db.table("post_comments").select("reaction_count").eq("id", str(comment_id)).limit(1).execute().data
        count = count_resp[0]["reaction_count"] if count_resp else 0

        from django.http import JsonResponse
        return JsonResponse({"liked": liked, "count": count})
    except Exception as e:
        from django.http import JsonResponse
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Student: report a post
# ---------------------------------------------------------------------------

def community_report_post(request, post_id):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/dashboard/community/")

    user_id = request.session.get("user_id")
    reason = request.POST.get("reason", "").strip()[:500]

    try:
        _supabase_admin().table("post_reports").insert({
            "post_id": str(post_id),
            "reporter_id": str(user_id),
            "reason": reason or "No reason provided",
        }).execute()
    except Exception:
        pass

    # Redirect back to wherever they came from
    next_url = request.POST.get("next", "/dashboard/community/")
    return redirect(next_url)


# ---------------------------------------------------------------------------
# Student: mark all community notifications read
# ---------------------------------------------------------------------------

def community_notifications_read(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.method == "POST":
        user_id = request.session.get("user_id")
        try:
            _supabase_admin().table("community_notifications").update({"is_read": True}).eq(
                "user_id", str(user_id)
            ).eq("is_read", False).execute()
        except Exception:
            pass
    return redirect(request.POST.get("next", "/dashboard/community/"))


# ---------------------------------------------------------------------------
# Admin: community management panel
# ---------------------------------------------------------------------------

def admin_community_manage(request):
    guard = _require_admin(request)
    if guard:
        return guard

    db = _supabase_admin()
    tab = request.GET.get("tab", "posts")  # posts | reports | warnings | communities

    try:
        communities = db.table("communities").select("*").order("name").execute().data or []
    except Exception:
        communities = []

    posts = []
    reports = []
    warnings = []

    if tab == "posts":
        try:
            posts = (
                db.table("community_posts")
                .select("*")
                .eq("is_deleted", False)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data or []
            )
            # Attach community name
            comm_map = {c["id"]: c["name"] for c in communities}
            for p in posts:
                p["community_name"] = comm_map.get(p["community_id"], "Unknown")
        except Exception:
            pass

    elif tab == "reports":
        try:
            reports = (
                db.table("post_reports")
                .select("*, community_posts(title, author_name, community_id)")
                .eq("is_resolved", False)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data or []
            )
        except Exception:
            reports = []

    elif tab == "warnings":
        try:
            warnings = (
                db.table("community_warnings")
                .select("*")
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data or []
            )
        except Exception:
            warnings = []

    ctx = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "community",
        "student_unread_notifications": 0,
        "communities": communities,
        "posts": posts,
        "reports": reports,
        "warnings": warnings,
        "tab": tab,
    }
    return render(request, "dashboard/admin_community.html", ctx)


# ---------------------------------------------------------------------------
# Admin: delete a post
# ---------------------------------------------------------------------------

def admin_community_delete_post(request, post_id):
    guard = _require_admin(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/admin-panel/community/")

    try:
        _supabase_admin().table("community_posts").update({
            "is_deleted": True
        }).eq("id", str(post_id)).execute()
    except Exception:
        pass

    return redirect(request.POST.get("next", "/admin-panel/community/"))


# ---------------------------------------------------------------------------
# Admin: toggle verified answer on a post
# ---------------------------------------------------------------------------

def admin_community_verify_answer(request, post_id):
    guard = _require_admin(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/admin-panel/community/")

    db = _supabase_admin()
    try:
        rows = db.table("community_posts").select("is_verified_answer, author_id, title").eq("id", str(post_id)).limit(1).execute().data
        if rows:
            current = rows[0]["is_verified_answer"]
            db.table("community_posts").update({"is_verified_answer": not current}).eq("id", str(post_id)).execute()
            if not current:
                msg = f"Your post \"{rows[0]['title'][:60]}\" has been marked as a Verified Answer by admin."
                _push_community_notif(rows[0]["author_id"], "verified_answer", msg, post_id=post_id)
    except Exception:
        pass

    return redirect(request.POST.get("next", "/admin-panel/community/"))


# ---------------------------------------------------------------------------
# Admin: warn a user
# ---------------------------------------------------------------------------

def admin_community_warn_user(request):
    guard = _require_admin(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/admin-panel/community/")

    admin_id = request.session.get("user_id")
    target_user_id = request.POST.get("user_id", "").strip()
    reason = request.POST.get("reason", "").strip()[:500]

    if target_user_id and reason:
        try:
            _supabase_admin().table("community_warnings").insert({
                "user_id": target_user_id,
                "warned_by": str(admin_id),
                "reason": reason,
            }).execute()
            msg = f"You have received a community warning: {reason}"
            _push_community_notif(target_user_id, "warning", msg)
        except Exception:
            pass

    return redirect("/admin-panel/community/?tab=warnings")


# ---------------------------------------------------------------------------
# Admin: resolve a report
# ---------------------------------------------------------------------------

def admin_community_resolve_report(request, report_id):
    guard = _require_admin(request)
    if guard:
        return guard
    if request.method != "POST":
        return redirect("/admin-panel/community/?tab=reports")

    admin_id = request.session.get("user_id")
    try:
        _supabase_admin().table("post_reports").update({
            "is_resolved": True,
            "resolved_by": str(admin_id),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", str(report_id)).execute()
    except Exception:
        pass

    return redirect("/admin-panel/community/?tab=reports")


# ===========================================================================
# SUBSCRIPTION / PAYMENT MODULE
# ===========================================================================

_PLAN_DEFAULTS_SEEDED = False


def _ensure_plan_defaults():
    """
    Seed subscription_plans table on first call per process.
    Product policy: one annual plan ("standard").
    Guarded by a module-level flag so it only hits Supabase once per worker.
    """
    global _PLAN_DEFAULTS_SEEDED
    if _PLAN_DEFAULTS_SEEDED:
        return
    try:
        db  = _supabase_admin()
        now = datetime.now(timezone.utc).isoformat()
        existing = db.table("subscription_plans").select("slug, price").execute().data or []
        em = {r["slug"]: float(r.get("price") or 0) for r in existing}

        # Canonical single plan.
        if "standard" not in em:
            db.table("subscription_plans").insert({
                "slug": "standard", "name": "Annual Access",
                "tagline": "Full access to all nursesedge  features",
                "price": 50.0, "currency": "GHS", "duration_days": 365,
                "is_active": True, "features": [], "payment_instructions": "",
                "updated_at": now,
            }).execute()
        # Do not overwrite existing "standard" pricing — admin controls this from dashboard.

        _PLAN_DEFAULTS_SEEDED = True
    except Exception:
        pass


def _get_plans():
    """Fetch plans and expose one standardized annual plan (with legacy aliases)."""
    _ensure_plan_defaults()
    try:
        rows = _supabase_admin().table("subscription_plans").select("*").eq("is_active", True).execute().data or []
        plans = {r["slug"]: r for r in rows}
        base = plans.get("standard") or plans.get("basic") or plans.get("premium") or {}
        standard = {
            **base,
            "slug": "standard",
            "name": base.get("name") or "Annual Access",
            "tagline": base.get("tagline") or "Full access to all nursesedge  features",
            "price": float(base.get("price") or 50.0),
            "currency": base.get("currency") or "GHS",
            "duration_days": int(base.get("duration_days") or 365),
            "is_active": True,
        }
        feats = standard.get("features")
        if not isinstance(feats, list):
            feats = []
        if not any(str(f).strip().lower() in ("competitive quiz", "competitive quizzes") for f in feats):
            feats.append("Competitive Quizzes")
        standard["features"] = feats
        return {
            "standard": standard,
            "basic": standard,   # legacy key alias
            "premium": standard, # legacy key alias
        }
    except Exception:
        standard = {
            "slug": "standard", "name": "Annual Access", "price": 50, "currency": "GHS",
            "tagline": "Full access to all nursesedge  features",
            "features": ["Competitive Quizzes"], "payment_instructions": "", "duration_days": 365,
        }
        return {"standard": standard, "basic": standard, "premium": standard}


# Legacy MTN boilerplate saved in Supabase (any amount) — strip from student /payment/ only.
_LEGACY_MOMO_INSTRUCTION_RE = re.compile(
    r"Pay\s+GHS\s+[\d.]+\s+via\s+MTN\s+Mobile\s+Money\s+to\s+024\s+000\s+0000\s*"
    r"\(Account\s+name:\s*nursesedge \)\.\s*"
    r"Use\s+your\s+registered\s+email\s+address\s+as\s+the\s+payment\s+reference\.",
    re.IGNORECASE | re.MULTILINE,
)


def _scrub_student_payment_instructions(text):
    """Remove obsolete boilerplate still stored in subscription_plans.payment_instructions."""
    if not text:
        return ""
    s = _LEGACY_MOMO_INSTRUCTION_RE.sub("", str(text))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _plan_row_for_payment_page(plan_row):
    if not plan_row:
        return {}
    out = dict(plan_row)
    out["payment_instructions"] = _scrub_student_payment_instructions(
        out.get("payment_instructions") or ""
    )
    return out


def _get_active_subscription(user_id):
    """Return the user's most recent subscription row, or None."""
    try:
        rows = (
            _supabase_admin()
            .table("subscriptions")
            .select("*")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _expires_at_still_valid(expires_at):
    if not expires_at:
        return True
    try:
        raw = str(expires_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return True


def _user_has_free_access(admin, user_id):
    """
    Permanent free access: profiles.is_free_access, or an active subscription row with
    payment_reference = free_access (used when the profiles column is not migrated yet).
    """
    uid = str(user_id)
    try:
        prof = (
            admin.table("profiles")
            .select("is_free_access")
            .eq("id", uid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if prof and prof[0].get("is_free_access"):
            return True
    except Exception:
        pass
    try:
        rows = (
            admin.table("subscriptions")
            .select("status, payment_reference, expires_at")
            .eq("user_id", uid)
            .eq("payment_reference", FREE_ACCESS_PAYMENT_REFERENCE)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("status") == "active":
            return _expires_at_still_valid(rows[0].get("expires_at"))
    except Exception:
        pass
    return False


def subscription_allows_dashboard(user_id):
    """Active subscription required for /dashboard/ (student).
    Free-access accounts (profile flag or free_access subscription row) are always allowed in."""
    admin = _supabase_admin()
    if _user_has_free_access(admin, user_id):
        return True
    sub = _get_active_subscription(user_id)
    if not sub or sub.get("status") != "active":
        return False
    return _expires_at_still_valid(sub.get("expires_at"))


def _subscription_access_state(user_id):
    """
    Returns (allowed: bool, reason: str).
    reason is one of: ok, payment_required, renewal_required.
    """
    admin = _supabase_admin()
    if _user_has_free_access(admin, user_id):
        return True, "ok"

    sub = _get_active_subscription(user_id)
    if not sub or sub.get("status") != "active":
        return False, "payment_required"
    if not _expires_at_still_valid(sub.get("expires_at")):
        return False, "renewal_required"
    return True, "ok"


_PAYSTACK_RECONCILE_THROTTLE_SEC = 45.0
_PAYSTACK_RECONCILE_LAST_TS = {}


def _reconcile_pending_subscription_from_paystack(user_id, *, force=False):
    """
    If the latest subscription is pending_payment but has a stored Paystack reference,
    call Paystack verify and activate when the charge succeeded. Use after signup verify
    failed (e.g. bad secret key) or on login/subscribe so fixing the key unlocks access
    without charging again.
    """
    user_id = str(user_id)
    if not (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip():
        return
    sub = _get_active_subscription(user_id)
    if not sub or sub.get("status") != "pending_payment":
        return
    ref = (sub.get("payment_reference") or "").strip()
    if not ref or ref == "complimentary":
        return

    import time
    import urllib.parse as _up

    now = time.time()
    if not force:
        last = _PAYSTACK_RECONCILE_LAST_TS.get(user_id, 0.0)
        if now - last < _PAYSTACK_RECONCILE_THROTTLE_SEC:
            return
    _PAYSTACK_RECONCILE_LAST_TS[user_id] = now

    vresp, verr = _paystack_request("GET", "/transaction/verify/" + _up.quote(ref, safe=""))
    if verr == "paystack_not_configured" or not vresp or not vresp.get("status"):
        return
    data = vresp.get("data") or {}
    if data.get("status") != "success":
        return
    plan_slug = (sub.get("plan_slug") or "standard").strip()
    sub_id = sub.get("id")
    if not sub_id:
        return
    amount_paid = float((data.get("amount") or 0)) / 100
    try:
        _apply_successful_subscription_payment(user_id, sub_id, plan_slug, amount_paid, ref)
    except Exception:
        pass


def _public_site_origin(request):
    base = getattr(settings, "PUBLIC_SITE_URL", "") or ""
    base = base.strip().rstrip("/")
    if base:
        return base
    return request.build_absolute_uri("/").rstrip("/")


def _paystack_api_error_message(err):
    """Turn _paystack_request() error codes into text for the subscribe UI."""
    if not err or err == "paystack_not_configured":
        return None
    if err == "paystack_public_key_used_as_secret":
        return (
            "PAYSTACK_SECRET_KEY is set to a public key (pk_test_… or pk_live_…). "
            "Open Paystack Dashboard → Settings → API Keys and copy the Secret key (sk_test_… or sk_live_…)."
        )
    if err == "paystack_secret_bad_format":
        return (
            "PAYSTACK_SECRET_KEY does not look like a Paystack secret (expected sk_test_… or sk_live_…). "
            "Check for typos, extra spaces, or a truncated copy in your .env file."
        )
    if err.startswith("http_401"):
        return (
            "Paystack refused authentication (HTTP 401). Check that PAYSTACK_SECRET_KEY in your "
            ".env is the secret key from Paystack Dashboard → Settings → API Keys (not the public key)."
        )
    if err.startswith("http_403"):
        return (
            "Paystack returned HTTP 403. If your secret key is correct, this is often Cloudflare "
            "blocking the request (now mitigated in the app). Also verify: sk_test_/sk_live_ secret "
            "matches the same mode as your public key, no extra quotes in .env, and the key was "
            "copied from Paystack → Settings → API Keys."
        )
    if err.startswith("http_404"):
        return "Paystack could not find this transaction (HTTP 404). The reference may be invalid or expired."
    if err.startswith("http_"):
        head, _, tail = err.partition(":")
        code = head.replace("http_", "", 1)
        tail = tail.strip()
        base = f"Paystack returned HTTP {code}."
        return f"{base} {tail}".strip() if tail else base
    return err


def _paystack_request(method, path, body_dict=None):
    import json
    import urllib.error
    import urllib.request

    secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
    if not secret:
        return None, "paystack_not_configured"

    sk = secret.lower()
    if sk.startswith("pk_test_") or sk.startswith("pk_live_"):
        return None, "paystack_public_key_used_as_secret"
    if not (sk.startswith("sk_test_") or sk.startswith("sk_live_")):
        return None, "paystack_secret_bad_format"

    # Paystack is behind Cloudflare; default User-Agent "Python-urllib/…" is often blocked with HTTP 403.
    _paystack_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 nursesedge /1.0"
    )

    url = "https://api.paystack.co" + path
    payload = None
    if method != "GET":
        payload = json.dumps(body_dict if body_dict is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("User-Agent", _paystack_ua)
    req.add_header("Accept", "application/json")
    if method != "GET":
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return json.loads(raw), None
        except Exception:
            snippet = " ".join(raw.split())[:280]
            if snippet:
                return None, f"http_{e.code}:{snippet}"
            return None, f"http_{e.code}"
    except Exception as e:
        return None, str(e)


def _paystack_transaction_initialize(*, email, amount_ghs, callback_url, metadata=None):
    """
    Start a Paystack hosted checkout. amount_ghs is major units (e.g. 50.00 GHS);
    Paystack expects amount in pesewas (×100).
    Returns (data_dict with authorization_url, access_code, reference, or None, error_string).
    """
    try:
        amount_minor = int(round(float(amount_ghs) * 100))
    except Exception:
        amount_minor = 0
    if amount_minor < 1:
        return None, "invalid_amount"
    payload = {
        "email": (email or "customer@example.com").strip()[:120],
        "amount": amount_minor,
        "currency": "GHS",
        "callback_url": (callback_url or "").strip()[:500],
    }
    if metadata:
        payload["metadata"] = {str(k): str(v) for k, v in metadata.items() if v is not None}
    resp, err = _paystack_request("POST", "/transaction/initialize", payload)
    if err:
        return None, err
    if not resp or not resp.get("status"):
        msg = (resp.get("message") if isinstance(resp, dict) else None) or "initialize_failed"
        return None, str(msg)
    data = resp.get("data") or {}
    if not data.get("reference"):
        return None, "paystack_missing_reference"
    return data, None


def _bulkclix_request(method, path, body_dict=None):
    import json
    import urllib.error
    import urllib.request

    api_key = (getattr(settings, "BULKCLIX_API_KEY", None) or "").strip()
    if not api_key:
        return None, "bulkclix_not_configured"

    base_url = (getattr(settings, "BULKCLIX_BASE_URL", None) or "https://api.bulkclix.com").strip().rstrip("/")
    url = f"{base_url}{path}"

    payload = None
    if method != "GET":
        payload = json.dumps(body_dict if body_dict is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("x-api-key", api_key)
    req.add_header("Authorization", f"ApiKey {api_key}")
    req.add_header("Accept", "application/json")
    if method != "GET":
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return json.loads(raw), None
        except Exception:
            snippet = " ".join(raw.split())[:280]
            return None, f"http_{e.code}:{snippet}" if snippet else f"http_{e.code}"
    except Exception as e:
        return None, str(e)


def _infer_network_from_phone(phone_number):
    digits = re.sub(r"\D+", "", phone_number or "")
    if digits.startswith("233"):
        digits = "0" + digits[3:]
    if len(digits) < 3:
        return "MTN"
    prefix = digits[:3]
    mtn = {"024", "025", "053", "054", "055", "059"}
    telecel = {"020", "050"}
    airteltigo = {"026", "027", "056", "057"}
    if prefix in mtn:
        return "MTN"
    if prefix in telecel:
        return "Telecel"
    if prefix in airteltigo:
        return "AirtelTigo"
    return "MTN"


def _bulkclix_start_subscription_payment(*, full_name, phone_number, amount):
    network = _infer_network_from_phone(phone_number)
    client_reference = f"NE-{secrets.token_hex(8)}"

    payload = {
        "amount": str(float(amount or 0)).rstrip("0").rstrip("."),
        "account_number": phone_number,
        "channel": network,
        "account_name": (full_name or "Student").strip()[:120],
        "client_reference": client_reference,
    }
    # Mobile money transfer endpoint per Bulkclix payment API.
    resp, err = _bulkclix_request("POST", "/api/v1/payment-api/send/mobilemoney", payload)
    if err:
        return None, err
    if not resp:
        return None, "bulkclix_invalid_response"
    if isinstance(resp, dict) and resp.get("status") is False:
        return None, str(resp.get("message") or "bulkclix_request_failed")
    if "data" not in resp:
        return None, str(resp.get("message") or "bulkclix_invalid_response")
    data_block = (resp.get("data") or {}) if isinstance(resp, dict) else {}
    if not data_block:
        msg = (resp.get("message") if isinstance(resp, dict) else "") or ""
        return None, str(msg or "bulkclix_invalid_response")
    payment = (data_block.get("payment") or {}) if isinstance(data_block, dict) else {}

    def _pick_reference(obj):
        if not isinstance(obj, dict):
            return ""
        for key in (
            "transaction_id",
            "order_id",
            "ext_transaction_id",
            "payment_reference",
            "reference",
            "trxref",
            "id",
        ):
            val = obj.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    reference = (
        _pick_reference(payment)
        or _pick_reference(data_block)
        or _pick_reference(resp if isinstance(resp, dict) else {})
    )
    if not reference:
        msg = (resp.get("message") if isinstance(resp, dict) else "") or ""
        return None, str(msg or "bulkclix_missing_reference")
    amount_paid = float(
        payment.get("amount")
        or data_block.get("amount")
        or (resp.get("amount") if isinstance(resp, dict) else 0)
        or amount
        or 0
    )
    return {"reference": str(reference), "amount_paid": amount_paid}, None


def _apply_successful_subscription_payment(user_id, sub_id, plan_slug, amount_paid, payment_reference):
    user_id = str(user_id)
    if plan_slug not in ("standard", "basic", "premium"):
        plan_slug = "standard"
    plans = _get_plans()
    plan = plans.get(plan_slug, plans.get("standard", {}))
    dur = int(plan.get("duration_days") or 365)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=dur)

    db = _supabase_admin()
    db.table("subscriptions").update({
        "status": "active",
        "started_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "amount_due": plan.get("price", 0),
        "amount_paid": amount_paid,
        "currency": (plan.get("currency") or "GHS"),
        "payment_reference": payment_reference,
        "plan_slug": plan_slug,
    }).eq("id", sub_id).eq("user_id", user_id).execute()

    db.table("profiles").update({
        "plan_slug": plan_slug,
        "subscription_status": "active",
    }).eq("id", user_id).execute()


def _subscription_history_for_user(user_id, limit=50):
    try:
        return (
            _supabase_admin()
            .table("subscriptions")
            .select("*")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def _ensure_pending_checkout_row(request, plans, selected_plan_slug=None):
    """Return the subscription row Stripe checkout should attach to (pending_payment)."""
    user_id = str(request.session.get("user_id", ""))
    db = _supabase_admin()
    profile_rows = (
        db.table("profiles")
        .select("plan_slug")
        .eq("id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    profile_plan = profile_rows[0].get("plan_slug") if profile_rows else "standard"
    _ = profile_plan, selected_plan_slug  # single-plan product policy
    plan_slug = "standard"

    sub = _get_active_subscription(user_id)
    if sub and sub.get("status") == "pending_payment":
        pl = plans.get(plan_slug, {})
        if sub.get("plan_slug") != plan_slug or str(sub.get("amount_due")) != str(pl.get("price", 0)):
            db.table("subscriptions").update({
                "plan_slug": plan_slug,
                "amount_due": pl.get("price", 0),
                "currency": pl.get("currency", "GHS"),
            }).eq("id", sub["id"]).execute()
            db.table("profiles").update({"plan_slug": plan_slug}).eq("id", user_id).execute()
            sub = _get_active_subscription(user_id)
        return sub

    pl = plans.get(plan_slug, {})
    ins = (
        db.table("subscriptions")
        .insert({
            "user_id": user_id,
            "user_email": request.session.get("email", ""),
            "user_name": request.session.get("full_name", ""),
            "plan_slug": plan_slug,
            "amount_due": pl.get("price", 0),
            "currency": pl.get("currency", "GHS"),
            "status": "pending_payment",
        })
        .execute()
    )
    rows = ins.data or []
    return rows[0] if rows else None


def _subscribe_ctx(request, user_id, plans, checkout_row, config_error=None, error=""):
    """Shared context dict for all student_subscribe render() calls."""
    return {
        "full_name": request.session.get("full_name", ""),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "subscribe",
        "student_unread_notifications": _student_unread_count(user_id),
        "community_unread": _community_unread_count(user_id),
        "plans": plans,
        "standard": _plan_row_for_payment_page(plans.get("standard")),
        "basic": _plan_row_for_payment_page(plans.get("basic")),
        "premium": _plan_row_for_payment_page(plans.get("premium")),
        "checkout_row": checkout_row,
        "config_error": config_error,
        "error": error,
        "using_paystack": bool((getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()),
        "using_bulkclix": bool((getattr(settings, "BULKCLIX_API_KEY", None) or "").strip()),
    }


def plan_upgrade_required(request):
    """
    Shown when a Basic-plan student tries to access a Premium-only feature.
    Displays a branded upgrade prompt with the blocked feature name and a
    list of Premium benefits.
    """
    guard = _require_login(request)
    if guard:
        return guard
    # Already premium — redirect to dashboard (shouldn't normally reach here)
    if _is_premium(request):
        return redirect("/dashboard/")

    feature = (request.GET.get("feature") or "").strip()
    label, description = _FEATURE_LABELS.get(
        feature, ("Premium Feature", "This feature requires a Premium subscription.")
    )
    user_id = request.session.get("user_id")
    context = {
        "full_name":   request.session.get("full_name", "Student"),
        "email":       request.session.get("email", ""),
        "role":        request.session.get("role", "student"),
        "active_page": "",
        "plan_slug":   request.session.get("plan_slug", "standard"),
        "student_unread_notifications": _student_unread_count(user_id),
        "has_unread_notifications":     _student_unread_count(user_id) > 0,
        "community_unread": 0,
        "feature":          feature,
        "feature_label":    label,
        "feature_description": description,
    }
    return render(request, "dashboard/upgrade_required.html", context)


def student_subscribe(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") != "student":
        return redirect("/admin-panel/dashboard/")

    user_id = request.session.get("user_id")
    _reconcile_pending_subscription_from_paystack(user_id, force=True)
    plans = _get_plans()
    error = request.GET.get("error", "")

    if subscription_allows_dashboard(user_id):
        return redirect("/dashboard/")

    selected = None
    if request.method == "POST":
        selected = request.POST.get("plan_slug", "").strip()
        if selected not in ("standard", "basic", "premium"):
            selected = "standard"
        selected = "standard"

    checkout_row = _ensure_pending_checkout_row(request, plans, selected)
    if checkout_row is None:
        return redirect("/subscribe/?error=setup")

    if request.method == "POST" and request.POST.get("start_checkout"):
        paystack_secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
        bulkclix_key = (getattr(settings, "BULKCLIX_API_KEY", None) or "").strip()

        plan_slug = checkout_row.get("plan_slug", "standard")
        plan = plans.get(plan_slug, plans.get("standard", {}))
        price = float(plan.get("price") or 0)
        if price <= 0:
            return render(request, "subscribe.html",
                _subscribe_ctx(request, user_id, plans, checkout_row,
                    config_error="Plan price must be greater than zero. Ask your admin to set it.",
                    error=error))

        if paystack_secret:
            payer_email = (request.session.get("email") or "").strip()
            if not payer_email:
                return render(
                    request,
                    "subscribe.html",
                    _subscribe_ctx(
                        request, user_id, plans, checkout_row,
                        config_error="Your account email is missing. Log out and back in, then try again.",
                        error=error,
                    ),
                )
            origin = _public_site_origin(request)
            callback_url = origin.rstrip("/") + "/subscribe/success/"
            meta = {
                "user_id": str(user_id),
                "subscription_id": str(checkout_row.get("id")),
                "plan_slug": plan_slug,
            }
            pdata, perr = _paystack_transaction_initialize(
                email=payer_email,
                amount_ghs=price,
                callback_url=callback_url,
                metadata=meta,
            )
            if perr or not pdata:
                msg = _paystack_api_error_message(perr) or str(perr or "paystack_init_failed")
                return render(
                    request,
                    "subscribe.html",
                    _subscribe_ctx(request, user_id, plans, checkout_row, config_error=msg, error=error),
                )
            auth_url = (pdata.get("authorization_url") or "").strip()
            if auth_url:
                return redirect(auth_url)
            return render(
                request,
                "subscribe.html",
                _subscribe_ctx(
                    request, user_id, plans, checkout_row,
                    config_error="Paystack did not return a checkout URL. Check your API keys and try again.",
                    error=error,
                ),
            )

        if not bulkclix_key:
            return render(
                request,
                "subscribe.html",
                _subscribe_ctx(
                    request, user_id, plans, checkout_row,
                    config_error=(
                        "Online payments are not configured. "
                        "Add PAYSTACK_SECRET_KEY (and PAYSTACK_PUBLIC_KEY) to your .env, or set BULKCLIX_X_API_KEY for MoMo."
                    ),
                    error=error,
                ),
            )

        try:
            profile_rows = (
                _supabase_admin()
                .table("profiles")
                .select("full_name, phone_number")
                .eq("id", str(user_id))
                .limit(1)
                .execute()
                .data
                or []
            )
            profile = profile_rows[0] if profile_rows else {}
            full_name = (profile.get("full_name") or request.session.get("full_name") or "Student").strip()
            phone_number = (profile.get("phone_number") or "").strip()
            if not phone_number:
                raise ValueError("Add your phone number to your profile before making payment.")
            payment, berr = _bulkclix_start_subscription_payment(
                full_name=full_name,
                phone_number=phone_number,
                amount=price,
            )
            if berr:
                raise ValueError(berr)
            _apply_successful_subscription_payment(
                user_id,
                checkout_row.get("id"),
                plan_slug,
                float(payment.get("amount_paid") or price),
                payment.get("reference"),
            )
            request.session["plan_slug"] = plan_slug
            return redirect("/dashboard/")
        except Exception as exc:
            emsg = str(exc)
            if "not allowed for momo collection" in emsg.lower():
                emsg = (
                    "Bulkclix is not enabled for MoMo collection on this account. "
                    "Contact Bulkclix support to enable it, then try again."
                )
            return render(request, "subscribe.html",
                _subscribe_ctx(request, user_id, plans, checkout_row,
                    config_error=emsg, error=error))

    return render(request, "subscribe.html",
        _subscribe_ctx(request, user_id, plans, checkout_row, error=error))


def student_subscribe_success(request):
    guard = _require_login(request)
    if guard:
        return guard

    import urllib.parse

    user_id = str(request.session.get("user_id", ""))
    reference = (request.GET.get("reference") or request.GET.get("trxref") or "").strip()
    session_id = (request.GET.get("session_id") or "").strip()
    paystack_secret = (getattr(settings, "PAYSTACK_SECRET_KEY", None) or "").strip()
    stripe_secret = (getattr(settings, "STRIPE_SECRET_KEY", None) or "").strip()

    if reference and paystack_secret:
        path = "/transaction/verify/" + urllib.parse.quote(reference, safe="")
        resp, err = _paystack_request("GET", path)
        if err == "paystack_not_configured" or resp is None:
            return redirect("/subscribe/?error=verify_failed")
        if not resp.get("status"):
            msg = urllib.parse.quote((resp.get("message") or "verify")[:120], safe="")
            return redirect(f"/subscribe/?error={msg}")
        data = resp.get("data") or {}
        if data.get("status") != "success":
            return redirect("/subscribe/?error=unpaid")
        meta = data.get("metadata") or {}
        if str(meta.get("user_id") or "").strip() != str(user_id).strip():
            return redirect("/subscribe/?error=forbidden")
        plan_slug = (str(meta.get("plan_slug") or "standard")).strip() or "standard"
        sub_id = meta.get("subscription_id")
        if not sub_id:
            return redirect("/subscribe/?error=missing_subscription")
        pending = _get_active_subscription(user_id)
        if not pending or str(pending.get("id")) != str(sub_id).strip():
            return redirect("/subscribe/?error=forbidden")
        amount_minor = int(data.get("amount") or 0)
        amount_paid = amount_minor / 100.0
        try:
            _apply_successful_subscription_payment(user_id, sub_id, plan_slug, amount_paid, reference)
        except Exception:
            return redirect("/subscribe/?error=save_failed")
        # Refresh plan in session so feature gates take effect immediately
        request.session["plan_slug"] = plan_slug
        return redirect("/dashboard/")

    if session_id and stripe_secret:
        try:
            import stripe

            stripe.api_key = settings.STRIPE_SECRET_KEY
            session = stripe.checkout.Session.retrieve(session_id)
        except Exception:
            return redirect("/subscribe/?error=stripe_session")

        if str(session.metadata.get("user_id") or "") != user_id:
            return redirect("/subscribe/?error=forbidden")
        if session.payment_status != "paid":
            return redirect("/subscribe/?error=unpaid")

        plan_slug = (session.metadata.get("plan_slug") or "standard").strip()
        sub_id = session.metadata.get("subscription_id")
        if not sub_id:
            return redirect("/subscribe/?error=missing_subscription")
        amount_paid = (session.amount_total or 0) / 100.0
        try:
            _apply_successful_subscription_payment(user_id, sub_id, plan_slug, amount_paid, session_id)
        except Exception:
            return redirect("/subscribe/?error=save_failed")
        return redirect("/dashboard/")

    if reference:
        return redirect("/subscribe/?error=paystack_not_configured")
    if session_id:
        return redirect("/subscribe/?error=stripe_not_configured")
    return redirect("/subscribe/?error=missing_session")


def student_subscribe_cancel(request):
    guard = _require_login(request)
    if guard:
        return guard
    return redirect("/subscribe/?reason=cancelled")


# ---------------------------------------------------------------------------
# Student: subscription / payment history (/payment/)
# ---------------------------------------------------------------------------

def payment_page(request):
    guard = _require_login(request)
    if guard:
        return guard

    user_id = request.session.get("user_id")
    _reconcile_pending_subscription_from_paystack(user_id, force=True)
    plans = _get_plans()
    history = _subscription_history_for_user(user_id)
    latest = history[0] if history else None
    paid_ok = subscription_allows_dashboard(user_id)

    ctx = {
        "full_name": request.session.get("full_name", ""),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "payment",
        "student_unread_notifications": _student_unread_count(user_id),
        "community_unread": _community_unread_count(user_id),
        "payment_history": history,
        "latest_subscription": latest,
        "subscription_access_ok": paid_ok,
        "plans": plans,
        "standard": plans.get("standard", {}),
        "basic": plans.get("basic", {}),
        "premium": plans.get("premium", {}),
    }
    return render(request, "payment.html", ctx)


# ---------------------------------------------------------------------------
# Admin: payments management — plan editor + all subscribers
# ---------------------------------------------------------------------------

def admin_payments(request):
    guard = _require_admin(request)
    if guard:
        return guard

    db = _supabase_admin()
    plans = _get_plans()
    error = None
    success = None

    if request.method == "POST":
        action   = request.POST.get("action", "")
        plan_slug = request.POST.get("plan_slug", "").strip()

        if action == "update_plan" and plan_slug in ("standard", "basic", "premium"):
            try:
                name         = request.POST.get("name", "").strip()[:80]
                tagline      = request.POST.get("tagline", "").strip()[:200]
                price_raw    = request.POST.get("price", "0").strip()
                duration_raw = request.POST.get("duration_days", "365").strip()
                instructions = request.POST.get("payment_instructions", "").strip()[:1000]
                features_raw = request.POST.get("features", "").strip()
                features     = [f.strip() for f in features_raw.splitlines() if f.strip()]

                price = round(float(price_raw), 2) if price_raw else 0.0
                dur   = int(duration_raw) if duration_raw.isdigit() else 365

                db.table("subscription_plans").update({
                    "name": name,
                    "tagline": tagline,
                    "price": price,
                    "duration_days": dur,
                    "payment_instructions": instructions,
                    "features": features,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("slug", "standard").execute()

                plans = _get_plans()
                success = "Subscription amount and plan details updated successfully."
            except Exception as exc:
                error = f"Update failed: {exc}"

        elif action == "activate_subscription":
            sub_id        = request.POST.get("subscription_id", "").strip()
            sub_plan_slug = request.POST.get("sub_plan_slug", "basic").strip()
            plan          = plans.get(sub_plan_slug, {})
            dur           = plan.get("duration_days", 365)
            now           = datetime.now(timezone.utc)
            expires       = now + timedelta(days=dur)
            try:
                db.table("subscriptions").update({
                    "status": "active",
                    "started_at": now.isoformat(),
                    "expires_at": expires.isoformat(),
                    "amount_paid": plan.get("price", 0),
                    "activated_by": str(request.session.get("user_id", "")),
                }).eq("id", sub_id).execute()
                success = "Subscription activated."
            except Exception as exc:
                error = f"Activation failed: {exc}"

        elif action == "cancel_subscription":
            sub_id = request.POST.get("subscription_id", "").strip()
            try:
                db.table("subscriptions").update({"status": "cancelled"}).eq("id", sub_id).execute()
                success = "Subscription cancelled."
            except Exception as exc:
                error = f"Cancellation failed: {exc}"

    subscribers = []
    selected_filter = (request.GET.get("filter", "") or "").strip().lower()
    counts = {"total": 0, "active": 0, "pending": 0, "standard": 0}
    try:
        subscribers = (
            db.table("subscriptions")
            .select("*")
            .order("created_at", desc=True)
            .limit(300)
            .execute()
            .data or []
        )
        for s in subscribers:
            counts["total"] += 1
            if s.get("status") == "active":
                counts["active"] += 1
            if s.get("status") == "pending_payment":
                counts["pending"] += 1
            if (s.get("plan_slug") or "").strip() in ("standard", "basic", "premium"):
                counts["standard"] += 1
    except Exception:
        pass

    if selected_filter == "active":
        filtered_subscribers = [s for s in subscribers if (s.get("status") or "").strip() == "active"]
    elif selected_filter == "pending":
        filtered_subscribers = [s for s in subscribers if (s.get("status") or "").strip() == "pending_payment"]
    else:
        filtered_subscribers = subscribers

    export_type = (request.GET.get("export", "") or "").strip().lower()
    if export_type == "csv":
        out = StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "Name",
            "Email",
            "Plan",
            "Amount Due (GHS)",
            "Amount Paid (GHS)",
            "Payment Reference",
            "Status",
            "Start Date",
            "Expiry Date",
            "Created At",
        ])
        for s in filtered_subscribers:
            writer.writerow([
                s.get("user_name") or "",
                s.get("user_email") or "",
                s.get("plan_slug") or "",
                s.get("amount_due") or "",
                s.get("amount_paid") or "",
                s.get("payment_reference") or "",
                s.get("status") or "",
                (s.get("started_at") or "")[:19],
                (s.get("expires_at") or "")[:19],
                (s.get("created_at") or "")[:19],
            ])

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = selected_filter if selected_filter in {"active", "pending"} else "all"
        response = HttpResponse(out.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="subscribers_{suffix}_{stamp}.csv"'
        return response

    ctx = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "payments",
        "student_unread_notifications": 0,
        "community_unread": 0,
        "plans": plans,
        "standard": plans.get("standard", {}),
        "basic": plans.get("basic", {}),
        "premium": plans.get("premium", {}),
        "subscribers": filtered_subscribers,
        "all_subscribers_count": len(subscribers),
        "selected_filter": selected_filter,
        "counts": counts,
        "error": error,
        "success": success,
    }
    return render(request, "dashboard/admin_payments.html", ctx)
