import json
import random
import re
import secrets
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.shortcuts import redirect, render

from supabase import create_client
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



# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supabase():
    """Anon/public key – used for auth sign-in."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


def _supabase_admin():
    """Service-role key – bypasses RLS, used for all DB writes."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


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


def _programmes_for_paper(programme, paper_title):
    """General Paper should be available for all programmes."""
    if paper_title == "General Paper":
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
    if paper_title == "General Paper":
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


MOCK_QUESTION_BATCH_SIZE = 180
MOCK_DURATION_MINUTES = 90
GLOBAL_MOCK_PROGRAMME = "All Programmes"
GENERAL_TEST_QUESTION_BATCH_SIZE = MOCK_QUESTION_BATCH_SIZE


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
    return render(request, "index.html")


def about(request):
    return render(request, "about.html")


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

            role = profile.get("role", "student")
            full_name = profile.get("full_name") or email.split("@")[0]

            _create_session(request, user_id, profile["email"], full_name, role)

            if role == "admin":
                return redirect("/admin-panel/dashboard/")
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


def signup_page(request):
    if request.method == "POST":
        full_name = request.POST.get("fullName", "").strip()
        email = request.POST.get("email", "").strip().lower()
        phone = request.POST.get("phone", "").strip()
        year_of_study = request.POST.get("yearOfStudy", "").strip()
        institution = request.POST.get("institution", "").strip()
        programme = request.POST.get("programme", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirmPassword", "")

        form_data = {
            "fullName": full_name,
            "email": email,
            "phone": phone,
            "yearOfStudy": year_of_study,
            "institution": institution,
            "programme": programme,
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
            return render(request, "signup.html", {"errors": errors, "form_data": form_data})

        try:
            admin = _supabase_admin()

            auth_resp = admin.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": full_name,
                    "role": "student",
                },
            })

            if not auth_resp.user:
                return render(request, "signup.html", {
                    "errors": ["Failed to create account. Please try again."],
                    "form_data": form_data,
                })

            user_id = str(auth_resp.user.id)

            # Upsert profile (trigger may have already created a partial row)
            admin.table("profiles").upsert({
                "id": user_id,
                "email": email,
                "full_name": full_name,
                "phone_number": phone,
                "year_of_study": year_of_study,
                "school": institution,
                "programme": programme,
                "role": "student",
            }).execute()

            _create_session(request, user_id, email, full_name, "student")
            return redirect("/dashboard/")

        except Exception as exc:
            msg = str(exc).lower()
            if "already" in msg or "registered" in msg or "duplicate" in msg:
                return render(request, "signup.html", {
                    "errors": ["This email is already registered. Please log in instead."],
                    "form_data": form_data,
                })
            return render(request, "signup.html", {
                "errors": ["Registration failed. Please try again."],
                "form_data": form_data,
            })

    return render(request, "signup.html")


def logout_view(request):
    _destroy_session(request)
    return redirect("/login/")


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

def contact_page(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        subject = request.POST.get("subject", "").strip()
        message = request.POST.get("message", "").strip()

        if not all([name, email, message]):
            return render(request, "contact.html", {"error": "Name, email, and message are required."})

        try:
            _supabase_admin().table("contact_messages").insert({
                "name": name,
                "email": email,
                "subject": subject,
                "message": message,
            }).execute()
            return render(request, "contact.html", {"success": "Your message has been sent. We'll get back to you shortly."})
        except Exception:
            return render(request, "contact.html", {"error": "Failed to send message. Please try again."})

    return render(request, "contact.html")


# ---------------------------------------------------------------------------
# Student dashboard
# ---------------------------------------------------------------------------

def user_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    mock_exam_count = 0
    best_mock_percentage = 0
    total_questions_available = 0
    recent_messages = []
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
    except Exception:
        pass

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": request.session.get("role", "student"),
        "active_page": "dashboard",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "mock_exam_count": mock_exam_count,
        "best_mock_percentage": best_mock_percentage,
        "total_questions_available": total_questions_available,
        "recent_messages": recent_messages,
    }
    return render(request, "dashboard/user_dashboard.html", context)


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


def admin_messages(request):
    guard = _require_admin(request)
    if guard:
        return guard

    try:
        messages = _supabase_admin().table("contact_messages").select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        messages = []

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "messages",
        "messages": messages,
    }
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

        _supabase_admin().table("question_bank").insert(rows_to_insert).execute()
        context["success"] = f"Upload successful. Saved {len(rows_to_insert)} question record(s)."
    except Exception as exc:
        context["error"] = str(exc)
        context["form_data"] = {**EMPTY_QUESTION_FORM, **request.POST.dict()}

    return render(request, "dashboard/admin_upload_questions.html", context)


def admin_manage_questions(request):
    guard = _require_admin(request)
    if guard:
        return guard

    query = request.GET.get("q", "").strip()
    edit_id = request.GET.get("edit", "").strip()

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "manage_questions",
        "programme_papers": PROGRAMME_PAPERS,
        "programme_papers_json": json.dumps(PROGRAMME_PAPERS),
        "programmes": PROGRAMME_NAMES,
        "query": query,
        "questions": [],
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

                admin.table("question_bank").insert(rows_to_insert).execute()
                context["success"] = f"Created {len(rows_to_insert)} question record(s)."

            elif action == "update":
                question_id = request.POST.get("question_id", "").strip()
                if not question_id:
                    raise ValueError("Question ID is required for update.")

                existing_rows = admin.table("question_bank").select("id, programme").eq("id", question_id).limit(1).execute().data or []
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

                # During single-row edit, keep the row's programme when General Paper is selected without a programme.
                target_programme = payload["programme"] or existing_row.get("programme", "")

                admin.table("question_bank").update({
                    "programme": target_programme,
                    "paper_title": payload["paper_title"],
                    "question_text": payload["question_text"],
                    "options": payload["options"],
                    "correct_option": payload["correct_option"],
                    "explanation": payload["explanation"],
                }).eq("id", question_id).execute()
                context["success"] = "Question updated successfully."

            elif action == "delete":
                question_id = request.POST.get("question_id", "").strip()
                if not question_id:
                    raise ValueError("Question ID is required for delete.")
                admin.table("question_bank").delete().eq("id", question_id).execute()
                context["success"] = "Question deleted successfully."
                if edit_id == question_id:
                    edit_id = ""
            else:
                raise ValueError("Invalid action.")

        # Keep listing lightweight for faster page load; fetch full row only for edit.
        list_query = (
            admin.table("question_bank")
            .select("id, programme, paper_title, question_text, correct_option, created_at")
            .order("created_at", desc=True)
            .limit(50)
        )
        if query:
            escaped = query.replace("%", "").replace(",", " ").strip()
            list_query = list_query.or_(
                f"question_text.ilike.%{escaped}%,programme.ilike.%{escaped}%,paper_title.ilike.%{escaped}%"
            )
        questions = list_query.execute().data or []
        context["questions"] = questions

        if edit_id:
            fetch = admin.table("question_bank").select("*").eq("id", edit_id).limit(1).execute().data or []
            context["edit_item"] = fetch[0] if fetch else None

    except Exception as exc:
        context["error"] = str(exc)
        context["form_data"] = {**EMPTY_QUESTION_FORM, **request.POST.dict()}

    return render(request, "dashboard/admin_manage_questions.html", context)


# ---------------------------------------------------------------------------
# Mock Exams
# ---------------------------------------------------------------------------

def admin_mock_exams(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "mock_exams",
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
                    admin.table("question_bank").insert(rows_to_insert).execute()

                count_resp = (
                    admin.table("question_bank")
                    .select("id", count="exact", head=True)
                    .eq("programme", GLOBAL_MOCK_PROGRAMME)
                    .execute()
                )
                total_questions = count_resp.count or 0
                possible_batches = total_questions // MOCK_QUESTION_BATCH_SIZE
                if possible_batches <= 0:
                    raise ValueError("Not enough questions for this programme. Need at least 180 questions.")

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
                if create_count <= 0 and action == "generate":
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

                if action == "upload_json":
                    context["success"] = f"Questions uploaded via JSON. Created {create_count} new mock exam(s)."
                else:
                    context["success"] = f"Created {create_count} mock exam(s)."

            elif action == "toggle_publish":
                exam_id = request.POST.get("exam_id", "").strip()
                publish_to = request.POST.get("publish_to", "true").strip().lower() == "true"
                admin.table("mock_exams").update({"is_published": publish_to}).eq("id", exam_id).execute()
                context["success"] = "Mock exam publish status updated."
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
        exam_id = attempt.get("mock_exam_id")
        pct = float(attempt.get("percentage") or 0)
        old = best_by_exam.get(exam_id)
        if old is None or pct > old:
            best_by_exam[exam_id] = pct
    for exam in exams:
        exam["my_best"] = best_by_exam.get(exam["id"])
        exam["leaderboard_rows"] = []
        exam["my_rank"] = None

    # Build leaderboard preview per mock (top 10 + current user's rank)
    for exam in exams:
        try:
            attempts_exam = (
                admin.table("mock_attempts")
                .select("student_id, percentage")
                .eq("mock_exam_id", exam["id"])
                .not_.is_("submitted_at", "null")
                .order("percentage", desc=True)
                .order("submitted_at", desc=False)
                .limit(300)
                .execute()
                .data
                or []
            )
            top_by_student = {}
            for row in attempts_exam:
                sid = row.get("student_id")
                pct = float(row.get("percentage") or 0)
                if sid not in top_by_student or pct > float(top_by_student[sid].get("percentage") or 0):
                    top_by_student[sid] = row

            ranking = sorted(
                top_by_student.values(),
                key=lambda r: float(r.get("percentage") or 0),
                reverse=True,
            )

            for idx, row in enumerate(ranking, start=1):
                if row.get("student_id") == user_id:
                    exam["my_rank"] = idx
                    break

            top_ten = ranking[:10]
            ids = [r.get("student_id") for r in top_ten if r.get("student_id")]
            names_map = {}
            if ids:
                p_rows = admin.table("profiles").select("id, full_name").in_("id", ids).execute().data or []
                names_map = {p["id"]: (p.get("full_name") or "Student") for p in p_rows}

            exam["leaderboard_rows"] = [
                {
                    "rank": idx,
                    "student_name": names_map.get(row.get("student_id"), "Student"),
                    "percentage": float(row.get("percentage") or 0),
                    "is_me": row.get("student_id") == user_id,
                }
                for idx, row in enumerate(top_ten, start=1)
            ]
        except Exception:
            exam["leaderboard_rows"] = []
            exam["my_rank"] = None

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

    if attempt_rows:
        attempt = attempt_rows[0]
    else:
        new_attempt = (
            admin.table("mock_attempts")
            .insert({
                "mock_exam_id": str(exam_id),
                "student_id": user_id,
                "time_limit_minutes": int(exam.get("duration_minutes") or MOCK_DURATION_MINUTES),
                "total_questions": int(exam.get("question_count") or MOCK_QUESTION_BATCH_SIZE),
            })
            .execute()
        )
        attempt = new_attempt.data[0]

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
        pool = (
            admin.table("question_bank")
            .select("id")
            .eq("programme", GLOBAL_MOCK_PROGRAMME)
            .execute()
            .data
            or []
        )
        needed = int(exam.get("question_count") or MOCK_QUESTION_BATCH_SIZE)
        if len(pool) < needed:
            return redirect("/dashboard/mock-exams/")
        random.shuffle(pool)
        selected = pool[:needed]
        attempt_questions = []
        for idx, item in enumerate(selected, start=1):
            attempt_questions.append({
                "attempt_id": attempt["id"],
                "question_id": item["id"],
                "question_order": idx,
            })
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
        if target:
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
        elif action == "next":
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
    answered_count = sum(1 for q in questions if q["id"] in answer_map and (answer_map[q["id"]].get("selected_option") or ""))
    bookmarked_count = sum(1 for a in answer_map.values() if a.get("is_bookmarked"))
    flagged_count = sum(1 for a in answer_map.values() if a.get("is_flagged"))

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

    attempts_all = (
        admin.table("mock_attempts")
        .select("student_id, percentage")
        .eq("mock_exam_id", str(exam_id))
        .not_.is_("submitted_at", "null")
        .order("percentage", desc=True)
        .execute()
        .data
        or []
    )
    top_by_student = {}
    for row in attempts_all:
        sid = row.get("student_id")
        pct = float(row.get("percentage") or 0)
        if sid not in top_by_student or pct > float(top_by_student[sid].get("percentage") or 0):
            top_by_student[sid] = row
    leaderboard = sorted(top_by_student.values(), key=lambda r: float(r.get("percentage") or 0), reverse=True)
    profile_ids = [row.get("student_id") for row in leaderboard if row.get("student_id")]
    names_map = {}
    if profile_ids:
        try:
            profiles = admin.table("profiles").select("id, full_name").in_("id", profile_ids).execute().data or []
            names_map = {p["id"]: (p.get("full_name") or "Student") for p in profiles}
        except Exception:
            names_map = {}

    leaderboard_rows = []
    for idx, row in enumerate(leaderboard[:10], start=1):
        leaderboard_rows.append({
            "rank": idx,
            "student_name": names_map.get(row.get("student_id"), "Student"),
            "percentage": float(row.get("percentage") or 0),
            "is_me": row.get("student_id") == user_id,
        })

    rank = 1
    for idx, row in enumerate(leaderboard, start=1):
        if row.get("student_id") == user_id:
            rank = idx
            break

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
        "encouragement": _score_message(percentage),
    }
    return render(request, "dashboard/student_mock_exam_result.html", context)


def _general_test_duration_minutes(paper_title):
    return 90 if (paper_title or "").strip() == "General Paper" else 180


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

    tests = []
    try:
        q = (
            admin.table("question_bank")
            .select("paper_title")
            .eq("programme", programme)
            .order("paper_title", desc=False)
            .execute()
            .data
            or []
        )
        grouped = {}
        for row in q:
            paper = (row.get("paper_title") or "Untitled").strip()
            grouped[paper] = grouped.get(paper, 0) + 1

        # Build General Test batches in order:
        # Test 1 consumes the first N questions, Test 2 consumes the next N, etc.
        tests = []
        for paper, count in grouped.items():
            if count <= 0:
                continue
            available_batches = count // GENERAL_TEST_QUESTION_BATCH_SIZE
            for test_number in range(1, available_batches + 1):
                tests.append({
                    "paper_title": paper,
                    "test_number": test_number,
                    "question_count": GENERAL_TEST_QUESTION_BATCH_SIZE,
                    "duration_minutes": _general_test_duration_minutes(paper),
                })

        # Order by test number first (so Test 1 rows appear before Test 2),
        # and then by paper title for a stable UI.
        tests.sort(key=lambda x: (x["test_number"], x["paper_title"]))
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
    profile_rows = admin.table("profiles").select("programme").eq("id", user_id).limit(1).execute().data or []
    programme = (profile_rows[0].get("programme") if profile_rows else "") or ""

    # Fetch enough questions to cover this batch, then slice in Python.
    # This avoids needing server-side offset support.
    limit_needed = test_number * GENERAL_TEST_QUESTION_BATCH_SIZE
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
    start_idx = (test_number - 1) * GENERAL_TEST_QUESTION_BATCH_SIZE
    end_idx = start_idx + GENERAL_TEST_QUESTION_BATCH_SIZE
    batch_ids = question_ids[start_idx:end_idx]

    if not batch_ids:
        return redirect("/dashboard/general-tests/")

    attempt_insert = (
        admin.table("general_test_attempts")
        .insert({
            "student_id": user_id,
            "programme": programme,
            "paper_title": f"{paper_title} — General Test {test_number}",
            "time_limit_minutes": _general_test_duration_minutes(paper_title),
            "total_questions": len(batch_ids),
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
            existing = answer_map.get(qid)
            if existing:
                admin.table("general_test_attempt_answers").update(payload).eq("id", existing["id"]).execute()
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

    now_utc = datetime.now(timezone.utc)
    started_at = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
    end_time = started_at + timedelta(minutes=int(attempt.get("time_limit_minutes") or 90))
    remaining = max(0, int((end_time - now_utc).total_seconds()))
    if remaining == 0:
        return redirect(f"/dashboard/general-tests/attempt/{attempt_id}/result/")

    current_question = questions[current_index - 1]
    current_answer = answer_map.get(current_question["id"], {})
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


def student_quizzes(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

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


def student_flashcards(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/dashboard/")

    admin = _supabase_admin()
    user_id = request.session.get("user_id")
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
    daily_ids = [item["id"] for item in daily_cards]

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
    reviewed_ids = {row.get("question_id") for row in reviewed_rows}

    if request.method == "POST":
        question_id = request.POST.get("question_id", "").strip()
        if question_id and question_id in daily_ids:
            try:
                admin.table("flashcard_daily_reviews").upsert(
                    {
                        "student_id": user_id,
                        "review_date": today,
                        "question_id": question_id,
                        "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="student_id,review_date,question_id",
                ).execute()
                reviewed_ids.add(question_id)
            except Exception:
                pass

    pending_cards = [card for card in daily_cards if card["id"] not in reviewed_ids]
    reviewed_count = len(reviewed_ids)
    done_for_today = reviewed_count >= len(daily_cards)

    current_idx = int(request.GET.get("idx", "0") or "0")
    if current_idx < 0:
        current_idx = 0
    current_card = None
    if pending_cards:
        current_idx = min(current_idx, len(pending_cards) - 1)
        current_card = pending_cards[current_idx]

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


def student_lecture_notes(request):
    guard = _require_login(request)
    if guard:
        return guard
    if request.session.get("role") == "admin":
        return redirect("/admin-panel/lecture-notes/")

    user_id = request.session.get("user_id")
    unread_count = _student_unread_count(user_id)
    notes = []
    try:
        notes = (
            _supabase_admin()
            .table("lecture_notes")
            .select("*")
            .eq("is_published", True)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        notes = []

    context = {
        "full_name": request.session.get("full_name", "Student"),
        "email": request.session.get("email", ""),
        "role": "student",
        "active_page": "lecture_notes",
        "student_unread_notifications": unread_count,
        "has_unread_notifications": unread_count > 0,
        "notes": notes,
    }
    return render(request, "dashboard/student_lecture_notes.html", context)


def admin_lecture_notes(request):
    guard = _require_admin(request)
    if guard:
        return guard

    admin = _supabase_admin()
    success = None
    error = None

    if request.method == "POST":
        action = request.POST.get("action", "create").strip()
        try:
            if action == "delete":
                note_id = request.POST.get("note_id", "").strip()
                if not note_id:
                    raise ValueError("Note ID is required.")
                admin.table("lecture_notes").delete().eq("id", note_id).execute()
                success = "Lecture note deleted."
            else:
                topic = request.POST.get("topic", "").strip()
                subtopic = request.POST.get("subtopic", "").strip()
                content_html = request.POST.get("content_html", "").strip()
                if not topic or not content_html:
                    raise ValueError("Topic and content are required.")
                admin.table("lecture_notes").insert({
                    "topic": topic,
                    "subtopic": subtopic or None,
                    "content_html": content_html,
                    "is_published": True,
                    "created_by": request.session.get("user_id"),
                }).execute()
                success = "Lecture note saved."
        except Exception as exc:
            error = str(exc)

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

    context = {
        "full_name": request.session.get("full_name", "Admin"),
        "email": request.session.get("email", ""),
        "role": "admin",
        "active_page": "lecture_notes",
        "notes": notes,
        "success": success,
        "error": error,
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
