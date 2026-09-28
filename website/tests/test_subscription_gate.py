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

    def test_gate_fails_closed_when_the_check_errors(self):
        request = RequestFactory().get("/dashboard/")
        request.session = dict(STUDENT)
        with mock.patch.object(views, "_subscription_access_state", side_effect=RuntimeError("supabase down")):
            response = self.gate(request)
        self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_reconcile_error_does_not_open_the_gate(self):
        request = RequestFactory().get("/dashboard/")
        request.session = dict(STUDENT)
        with mock.patch.object(views, "_reconcile_pending_subscription_from_paystack", side_effect=RuntimeError("paystack")), \
             mock.patch.object(views, "_subscription_access_state", return_value=(False, "payment_required")):
            response = self.gate(request)
        self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_admins_and_non_dashboard_pages_are_not_gated(self):
        self.assertEqual(self._get("/dashboard/", session={"user_id": "a", "role": "admin"}).content, b"page")
        self.assertEqual(self._get("/subscribe/").content, b"page")
        self.assertEqual(self._get("/about/").content, b"page")


class ViewLevelPaymentChecks(SimpleTestCase):
    """The views check payment themselves too, in case the middleware is ever bypassed."""

    def _request(self, path):
        request = RequestFactory().get(path)
        request.session = SessionStore()
        request.session.update(STUDENT)
        return request

    def test_dashboard_view_redirects_unpaid_student(self):
        with mock.patch.object(views, "_subscription_access_state", return_value=(False, "payment_required")):
            response = views.user_dashboard(self._request("/dashboard/"))
        self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_payment_history_sends_never_paid_student_to_checkout(self):
        with mock.patch.object(views, "_reconcile_pending_subscription_from_paystack"), \
             mock.patch.object(views, "_subscription_access_state", return_value=(False, "payment_required")):
            response = views.payment_page(self._request("/payment/"))
        self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_checkout_page_is_standalone_without_dashboard_navigation(self):
        request = self._request("/subscribe/?reason=new_account")
        plans = {"standard": {"slug": "standard", "name": "Annual Access", "price": 60.0, "currency": "GHS", "duration_days": 365}}
        with mock.patch.object(views, "_reconcile_pending_subscription_from_paystack"), \
             mock.patch.object(views, "subscription_allows_dashboard", return_value=False), \
             mock.patch.object(views, "_get_plans", return_value=plans), \
             mock.patch.object(views, "_ensure_pending_checkout_row", return_value={"id": "s1", "plan_slug": "standard"}), \
             mock.patch.object(views, "_student_unread_count", return_value=0), \
             mock.patch.object(views, "_community_unread_count", return_value=0):
            response = views.student_subscribe(request)
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Your account has been created", html)
        self.assertIn('name="start_checkout"', html)
        self.assertNotIn("/dashboard/mock-exams/", html)   # no sidebar links into the dashboard
        self.assertNotIn("chatbot", html.lower())          # no AI assistant before paying


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


class GateRoleTests(SimpleTestCase):
    def setUp(self):
        self.gate = StudentSubscriptionGateMiddleware(lambda request: HttpResponse("page"))
        patcher = mock.patch.object(views, "_reconcile_pending_subscription_from_paystack")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, session):
        request = RequestFactory().get("/dashboard/")
        request.session = dict(session)
        with mock.patch.object(views, "_subscription_access_state", return_value=(False, "payment_required")):
            return self.gate(request)

    def test_missing_or_unknown_role_is_still_checked(self):
        for role in (None, "", "user", "Student"):
            with self.subTest(role=role):
                response = self._get({"user_id": STUDENT["user_id"], "role": role})
                self.assertEqual(response["Location"], "/subscribe/?reason=payment_required")

    def test_no_session_goes_to_login(self):
        response = self._get({})
        self.assertTrue(response["Location"].startswith("/login/?next="))


PAYSTACK = {"PAYSTACK_SECRET_KEY": "sk_test_" + "x" * 40}
PLANS = {"standard": {"slug": "standard", "name": "Annual Access", "price": 60.0, "currency": "GHS", "duration_days": 365}}
PENDING = {"id": "sub1", "user_id": STUDENT["user_id"], "status": "pending_payment", "amount_due": 60,
           "plan_slug": "standard", "payment_reference": "ref1"}


def _verify(amount_minor=6120, currency="GHS", user_id=STUDENT["user_id"], status="success"):
    return {"status": True, "data": {
        "status": status, "amount": amount_minor, "currency": currency, "reference": "ref1",
        "metadata": {"user_id": user_id, "subscription_id": "sub1", "plan_slug": "standard", "base_amount_minor": "6000"},
    }}


class PaymentConfirmationTests(SimpleTestCase):
    """Access only opens for a real, full, GHS payment made by this account."""

    def setUp(self):
        for name, value in (("_get_plans", PLANS), ("_get_active_subscription", dict(PENDING))):
            p = mock.patch.object(views, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(views, "_apply_successful_subscription_payment")
        self.apply = p.start()
        self.addCleanup(p.stop)

    def _success(self, verify):
        request = RequestFactory().get("/subscribe/success/?reference=ref1")
        request.session = SessionStore()
        request.session.update(STUDENT)
        with self.settings(**PAYSTACK), mock.patch.object(views, "_paystack_request", return_value=(verify, None)):
            return views.student_subscribe_success(request), request

    def test_full_payment_activates_and_queues_the_welcome(self):
        response, request = self._success(_verify())
        self.assertEqual(response["Location"], "/dashboard/")
        self.apply.assert_called_once()
        self.assertTrue(request.session.get("payment_welcome"))

    def test_underpayment_is_rejected(self):
        response, request = self._success(_verify(amount_minor=100))
        self.assertEqual(response["Location"], "/subscribe/?error=amount_mismatch")
        self.apply.assert_not_called()
        self.assertFalse(request.session.get("payment_welcome"))

    def test_wrong_currency_is_rejected(self):
        response, _ = self._success(_verify(currency="NGN"))
        self.assertEqual(response["Location"], "/subscribe/?error=amount_mismatch")
        self.apply.assert_not_called()

    def test_someone_elses_payment_is_rejected(self):
        response, _ = self._success(_verify(user_id="22222222-2222-2222-2222-222222222222"))
        self.assertEqual(response["Location"], "/subscribe/?error=forbidden")
        self.apply.assert_not_called()

    def test_failed_payment_is_rejected(self):
        response, _ = self._success(_verify(status="abandoned"))
        self.assertEqual(response["Location"], "/subscribe/?error=unpaid")
        self.apply.assert_not_called()

    def test_webhook_ignores_underpayment(self):
        import hashlib
        import hmac
        import json
        body = json.dumps({"event": "charge.success", "data": _verify(amount_minor=500)["data"]}).encode()
        sig = hmac.new(PAYSTACK["PAYSTACK_SECRET_KEY"].encode(), body, hashlib.sha512).hexdigest()
        request = RequestFactory().post("/paystack/webhook/", body, content_type="application/json",
                                        HTTP_X_PAYSTACK_SIGNATURE=sig)
        admin = mock.MagicMock()
        (admin.table.return_value.select.return_value.eq.return_value.eq.return_value
         .limit.return_value.execute.return_value.data) = [dict(PENDING)]
        with self.settings(**PAYSTACK), mock.patch.object(views, "_supabase_admin", return_value=admin):
            self.assertEqual(views.paystack_webhook(request).status_code, 200)
        self.apply.assert_not_called()

    def test_reconcile_rejects_another_users_payment(self):
        with self.settings(**PAYSTACK), mock.patch.object(
                views, "_paystack_request", return_value=(_verify(user_id="someone-else"), None)):
            self.assertFalse(views._reconcile_pending_subscription_from_paystack(STUDENT["user_id"], force=True))
        self.apply.assert_not_called()

    def test_reconcile_reports_activation(self):
        with self.settings(**PAYSTACK), mock.patch.object(views, "_paystack_request", return_value=(_verify(), None)):
            self.assertTrue(views._reconcile_pending_subscription_from_paystack(STUDENT["user_id"], force=True))
        self.apply.assert_called_once()


class PaystackOnlyCheckoutTests(SimpleTestCase):
    """Paystack is the only provider: Pay opens its checkout and nothing activates until it confirms."""

    def _checkout(self, **settings):
        request = RequestFactory().post("/subscribe/", {"start_checkout": "1", "plan_slug": "standard"})
        request.session = SessionStore()
        request.session.update(dict(STUDENT, email="ama@example.com"))
        admin = mock.MagicMock()
        init = {"status": True, "data": {"reference": "ref1", "authorization_url": "https://checkout.paystack.com/abc"}}
        with self.settings(**settings), \
             mock.patch.object(views, "_reconcile_pending_subscription_from_paystack", return_value=False), \
             mock.patch.object(views, "_subscription_access_state", return_value=(False, "payment_required")), \
             mock.patch.object(views, "_get_plans", return_value=PLANS), \
             mock.patch.object(views, "_ensure_pending_checkout_row", return_value=dict(PENDING)), \
             mock.patch.object(views, "_supabase_admin", return_value=admin), \
             mock.patch.object(views, "_student_unread_count", return_value=0), \
             mock.patch.object(views, "_community_unread_count", return_value=0), \
             mock.patch.object(views, "_paystack_request", return_value=(init, None)), \
             mock.patch.object(views, "_apply_successful_subscription_payment") as apply:
            response = views.student_subscribe(request)
        return response, request, apply, admin

    def test_pay_goes_to_paystack_and_only_records_the_reference(self):
        response, request, apply, admin = self._checkout(**PAYSTACK)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://checkout.paystack.com/abc")
        admin.table.return_value.update.assert_called_once_with({"payment_reference": "ref1"})
        apply.assert_not_called()
        self.assertNotIn("payment_welcome", request.session)

    def test_without_paystack_keys_nothing_is_charged_or_unlocked(self):
        response, request, apply, _ = self._checkout(PAYSTACK_SECRET_KEY="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Online payments are not configured", response.content.decode())
        apply.assert_not_called()
        self.assertNotIn("payment_welcome", request.session)

    def test_no_bulkclix_code_left(self):
        self.assertFalse(hasattr(views, "_bulkclix_start_subscription_payment"))
        self.assertFalse(hasattr(views, "_bulkclix_request"))


class RemovedEndpointTests(SimpleTestCase):
    def test_anonymous_signup_payment_api_is_gone(self):
        from django.urls import Resolver404, resolve
        with self.assertRaises(Resolver404):
            resolve("/api/signup/initiate-payment/")


class PaymentWelcomeTemplateTests(SimpleTestCase):
    def _render(self, payment_welcome):
        from django.template.loader import render_to_string
        request = RequestFactory().get("/dashboard/")
        request.session = SessionStore()
        request.session.update(STUDENT)
        return render_to_string("dashboard/user_dashboard.html", {
            "full_name": "Ama Owusu", "email": "ama@example.com", "role": "student",
            "payment_welcome": payment_welcome, "welcome_first_name": "Ama", "welcome_time": "morning",
            "sub_expires_display": "28 Sep 2027", "perf_stats": {"has_data": False, "target_pct": 75},
            "sub_status": "active", "days_remaining": 365,
        }, request=request)

    def test_welcome_shows_only_after_payment(self):
        html = self._render(True)
        self.assertIn("Payment confirmed", html)
        self.assertIn("Welcome to NursesEdge, Ama!", html)
        self.assertIn("28 Sep 2027", html)
        self.assertNotIn('id="pwWelcome"', self._render(False))
