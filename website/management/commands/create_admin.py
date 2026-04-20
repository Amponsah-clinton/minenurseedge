"""
Management command to seed the nursesedge  admin account in Supabase.

Usage:
    python manage.py create_admin
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from supabase import create_client

ADMIN_EMAIL = "amponsahc306@gmail.com"
ADMIN_PASSWORD = "Amponsah1997@"
ADMIN_NAME = "Clinton Amponsah"


class Command(BaseCommand):
    help = "Create the nursesedge  admin account in Supabase (safe to run multiple times)"

    def handle(self, *args, **options):
        admin_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

        # Check whether the profile already exists
        existing = admin_client.table("profiles").select("id").eq("email", ADMIN_EMAIL).limit(1).execute()
        if existing.data:
            self.stdout.write(self.style.WARNING(f"Admin account already exists for {ADMIN_EMAIL}. Skipping."))
            return

        # Create auth user
        try:
            resp = admin_client.auth.admin.create_user({
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": ADMIN_NAME,
                    "role": "admin",
                },
            })
        except Exception as exc:
            msg = str(exc).lower()
            if "already" in msg or "registered" in msg:
                self.stdout.write(self.style.WARNING(f"Auth user already exists for {ADMIN_EMAIL}."))
                # Try to find existing auth user and upsert profile
                try:
                    users_list = admin_client.auth.admin.list_users()
                    user_id = None
                    for u in users_list:
                        if hasattr(u, "email") and u.email == ADMIN_EMAIL:
                            user_id = str(u.id)
                            break
                    if user_id:
                        admin_client.table("profiles").upsert({
                            "id": user_id,
                            "email": ADMIN_EMAIL,
                            "full_name": ADMIN_NAME,
                            "role": "admin",
                        }).execute()
                        self.stdout.write(self.style.SUCCESS(f"Admin profile upserted for {ADMIN_EMAIL}."))
                except Exception as inner_exc:
                    self.stdout.write(self.style.ERROR(f"Could not upsert profile: {inner_exc}"))
                return
            self.stdout.write(self.style.ERROR(f"Failed to create admin auth user: {exc}"))
            return

        if not resp.user:
            self.stdout.write(self.style.ERROR("Auth user creation returned no user."))
            return

        user_id = str(resp.user.id)

        # Upsert the profile with role=admin
        admin_client.table("profiles").upsert({
            "id": user_id,
            "email": ADMIN_EMAIL,
            "full_name": ADMIN_NAME,
            "role": "admin",
        }).execute()

        self.stdout.write(self.style.SUCCESS(f"Admin account created successfully: {ADMIN_EMAIL}"))
