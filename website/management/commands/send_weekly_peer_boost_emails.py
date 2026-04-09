from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

from website.views import _build_weekly_peer_comparison, _supabase_admin


class Command(BaseCommand):
    help = "Send weekly anonymized peer-comparison motivation emails to students."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print recipients without sending.")
        parser.add_argument("--limit", type=int, default=300, help="Maximum number of students to evaluate.")

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        limit = int(options.get("limit") or 300)
        admin = _supabase_admin()

        profile_rows = (
            admin.table("profiles")
            .select("id, full_name, email, role, is_active")
            .eq("role", "student")
            .eq("is_active", True)
            .not_.is_("email", "null")
            .limit(limit)
            .execute()
            .data
            or []
        )

        sent_count = 0
        skipped_count = 0
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
        dashboard_url = getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/") + "/dashboard/"

        for row in profile_rows:
            student_id = row.get("id")
            email = (row.get("email") or "").strip()
            if not student_id or not email:
                skipped_count += 1
                continue

            try:
                comparison = _build_weekly_peer_comparison(admin, student_id)
            except Exception:
                comparison = None

            if not comparison:
                skipped_count += 1
                continue

            context = {
                "full_name": (row.get("full_name") or "Student").strip(),
                "score": comparison["score"],
                "subject_label": comparison["subject_label"],
                "top_percent": comparison["top_percent"],
                "year_of_study": comparison["year_of_study"],
                "programme": comparison["programme"],
                "dashboard_url": dashboard_url,
            }
            subject = (
                f"NurseEdge Weekly Insight: You are top {comparison['top_percent']}% in "
                f"{comparison['subject_label']}"
            )
            text_body = (
                f"Hi {context['full_name']},\n\n"
                f"You scored {context['score']}% on {context['subject_label']}.\n"
                f"You are in the top {context['top_percent']}% of {context['year_of_study']} "
                f"{context['programme']} students this week.\n\n"
                "This comparison is anonymized and based on your cohort's scores from the last 7 days.\n\n"
                f"Continue studying: {dashboard_url}\n"
            )
            html_body = render_to_string("emails/weekly_peer_boost.html", context)

            if dry_run:
                self.stdout.write(self.style.WARNING(f"[DRY RUN] {email} :: {subject}"))
                sent_count += 1
                continue

            msg = EmailMultiAlternatives(subject, text_body, from_email, [email])
            msg.attach_alternative(html_body, "text/html")
            try:
                msg.send(fail_silently=False)
                sent_count += 1
            except Exception:
                skipped_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Weekly peer boost run complete. Sent: {sent_count}, skipped: {skipped_count}, evaluated: {len(profile_rows)}"
        ))
