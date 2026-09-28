from django.test import RequestFactory, SimpleTestCase

from website.context_processors import account_menu_ctx, user_initials


class UserInitialsTests(SimpleTestCase):
    def test_first_and_last_name(self):
        self.assertEqual(user_initials("Ama Serwaa Mensah"), "AM")
        self.assertEqual(user_initials("kwame  asante"), "KA")

    def test_single_name(self):
        self.assertEqual(user_initials("Kwame"), "K")

    def test_skips_words_not_starting_with_a_letter(self):
        self.assertEqual(user_initials("Dr. Efua Owusu"), "DO")
        self.assertEqual(user_initials("  (Nana) Yaw 2nd "), "Y")

    def test_falls_back_to_email_then_empty(self):
        self.assertEqual(user_initials("", "ama@example.com"), "A")
        self.assertEqual(user_initials("", "123@example.com"), "")
        self.assertEqual(user_initials(None, None), "")


class AccountMenuContextTests(SimpleTestCase):
    def test_reads_name_and_email_from_session(self):
        req = RequestFactory().get("/dashboard/")
        req.session = {"full_name": "Ama Mensah", "email": "ama@example.com"}
        self.assertEqual(account_menu_ctx(req), {
            "account_name": "Ama Mensah", "account_email": "ama@example.com", "account_initials": "AM",
        })

    def test_missing_name_uses_email(self):
        req = RequestFactory().get("/dashboard/")
        req.session = {"email": "kofi.b@example.com"}
        ctx = account_menu_ctx(req)
        self.assertEqual((ctx["account_name"], ctx["account_initials"]), ("kofi.b", "K"))

    def test_no_session(self):
        req = RequestFactory().get("/")
        self.assertEqual(account_menu_ctx(req)["account_initials"], "")
