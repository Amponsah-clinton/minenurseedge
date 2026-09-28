from unittest import mock

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from website import views
from website.middleware import StudentSubscriptionGateMiddleware

STUDENT = {"user_id": "11111111-1111-1111-1111-111111111111", "role": "student"}


class SubscriptionGateTests(SimpleTestCase):
    """Unpaid students must not reach any dashboard page before paying."""

    def setUp(self):
        self.gate = StudentSubscriptionGateMiddleware(lambda request: HttpResponse("page"))
        patcher = mock.patch.object(views, "_reconcile_pending_subscription_from_paystack")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, path, session=STUDENT, access=(False, "payment_required")):
        request = RequestFactory().get(path)
        request.session = dict(session)
        with mock.patch.object(views, "_subscription_access_state", return_value=access):
            return self.gate(request)

    def test_unpaid_student_is_sent_to_payment_from_every_dashboard_page(self):
        for path in ("/dashboard/", "/dashboard", "/dashboard/free-test/", "/dashboard/search/",
                     "/dashboard/mock-exams/", "/dashboard/free-test/start/"):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_expired_student_is_asked_to_renew(self):
        response = self._get("/dashboard/", access=(False, "renewal_required"))
        self.assertEqual(response["Location"], "/subscribe/?reason=renewal_required")

    def test_paid_student_gets_through(self):
        for path in ("/dashboard/", "/dashboard/free-test/", "/dashboard/mock-exams/"):
            with self.subTest(path=path):
                self.assertEqual(self._get(path, access=(True, "ok")).content, b"page")

    def test_setup_endpoints_stay_open_before_payment(self):
        for path in ("/dashboard/complete-academic-profile/", "/dashboard/ack-disclaimer/"):
            with self.subTest(path=path):
                self.assertEqual(self._get(path).content, b"page")

    def test_admins_and_non_dashboard_pages_are_not_gated(self):
        self.assertEqual(self._get("/dashboard/", session={"user_id": "a", "role": "admin"}).content, b"page")
        self.assertEqual(self._get("/subscribe/").content, b"page")
        self.assertEqual(self._get("/about/").content, b"page")


class SignupRedirectTests(SimpleTestCase):
    def test_new_account_goes_to_payment_not_free_test(self):
        admin = mock.MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        admin.auth.admin.create_user.return_value.user.id = STUDENT["user_id"]

        request = RequestFactory().post("/signup/", {
            "fullName": "Ama Owusu", "email": "ama@example.com", "phone": "0240000000",
            "yearOfStudy": "finalyear", "institution": "Korle Bu NTC",
            "programme": views.PROGRAMME_CHOICES[0],
            "password": "Str0ng!Passw0rd", "confirmPassword": "Str0ng!Passw0rd",
        })
        request.session = SessionStore()

        with mock.patch.object(views, "_supabase_admin", return_value=admin), \
             mock.patch.object(views, "_get_plans", return_value={"standard": {"price": 60, "currency": "GHS"}}), \
             mock.patch.object(views, "_create_session"):
            response = views.signup_page(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/subscribe/?reason=new_account")
