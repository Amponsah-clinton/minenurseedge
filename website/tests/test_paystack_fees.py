import hashlib
import hmac
import json
import math
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from website import views

SECRET = "sk_test_" + "x" * 40
PLANS = {
    "standard": {"slug": "standard", "name": "Annual Access", "price": 60.0, "currency": "GHS", "duration_days": 365},
}
PLANS["basic"] = PLANS["premium"] = PLANS["standard"]
REAL_RECONCILE = views._reconcile_pending_subscription_from_paystack  # PaymentFlowTests mocks it in views


def _paystack_fee(total_minor, pct, rounding):
    """Fee Paystack deducts from a charge, in pesewas, under a given rounding rule."""
    return int((Decimal(total_minor) * Decimal(pct) / 100).quantize(Decimal(1), rounding=rounding))


@override_settings(PAYSTACK_FEE_PERCENT="1.95", PAYSTACK_PASS_FEE_TO_CUSTOMER=True)
class CheckoutBreakdownTests(SimpleTestCase):
    def test_sixty_cedis_charges_61_20_and_settles_full_price(self):
        b = views._paystack_checkout_breakdown(60)
        self.assertEqual(b, {**b, "base_minor": 6000, "fee_minor": 120, "total_minor": 6120})
        # Paystack takes 1.95% of 61.20 = 1.1934 → settles 60.01 (or 60.00 if it rounds the fee up).
        self.assertGreaterEqual(6120 - _paystack_fee(6120, "1.95", ROUND_HALF_UP), 6000)
        self.assertGreaterEqual(6120 - _paystack_fee(6120, "1.95", ROUND_CEILING), 6000)

    def test_every_price_up_to_2000_cedis_settles_in_full_and_is_minimal(self):
        for base_minor in range(1, 200_001):
            b = views._paystack_checkout_breakdown(base_minor / 100)
            total = b["total_minor"]
            self.assertEqual(b["base_minor"], base_minor)
            self.assertEqual(b["fee_minor"], total - base_minor)
            # Merchant receives at least the plan price whichever way Paystack rounds its fee.
            for rounding in (ROUND_HALF_UP, ROUND_CEILING):
                net = total - _paystack_fee(total, "1.95", rounding)
                if net < base_minor:
                    self.fail(f"GHS {base_minor / 100:.2f}: charged {total}, settled {net} ({rounding})")
            # And the customer is never over-charged: one pesewa less would fall short.
            if total - 1 - _paystack_fee(total - 1, "1.95", ROUND_CEILING) >= base_minor:
                self.fail(f"GHS {base_minor / 100:.2f}: {total} is not the minimal charge")

    def test_float_prices_do_not_drift(self):
        # 0.1 + 0.2 style float noise must not change the pesewa amount.
        self.assertEqual(views._paystack_checkout_breakdown(0.1 + 0.2)["base_minor"], 30)
        self.assertEqual(views._paystack_checkout_breakdown("59.99")["base_minor"], 5999)
        self.assertEqual(views._paystack_checkout_breakdown(Decimal("100"))["total_minor"], 10199)

    def test_invalid_amounts(self):
        for bad in (0, -5, "abc", None, ""):
            b = views._paystack_checkout_breakdown(bad)
            self.assertLessEqual(b["base_minor"], 0, bad)
            self.assertEqual(b["fee_minor"], 0, bad)

    @override_settings(PAYSTACK_PASS_FEE_TO_CUSTOMER=False)
    def test_fee_not_passed_on_when_disabled(self):
        self.assertEqual(views._paystack_checkout_breakdown(60)["total_minor"], 6000)

    @override_settings(PAYSTACK_FEE_PERCENT="1.5")
    def test_custom_fee_percent(self):
        total = views._paystack_checkout_breakdown(60)["total_minor"]
        self.assertEqual(total, math.ceil(6000 / 0.985))
        self.assertGreaterEqual(total - _paystack_fee(total, "1.5", ROUND_CEILING), 6000)

    @override_settings(PAYSTACK_FEE_PERCENT="not-a-number")
    def test_bad_fee_setting_falls_back_to_1_95(self):
        self.assertEqual(views._paystack_checkout_breakdown(60)["total_minor"], 6120)


class AmountCreditedTests(SimpleTestCase):
    def test_records_plan_price_not_gross(self):
        data = {"amount": 6120, "metadata": {"base_amount_minor": "6000"}}
        self.assertEqual(views._paystack_amount_credited_ghs(data), 60.0)

    def test_metadata_sent_as_json_string(self):
        data = {"amount": 6120, "metadata": json.dumps({"base_amount_minor": "6000"})}
        self.assertEqual(views._paystack_amount_credited_ghs(data), 60.0)

    def test_legacy_transaction_without_base_uses_amount_charged(self):
        self.assertEqual(views._paystack_amount_credited_ghs({"amount": 6000, "metadata": {}}), 60.0)
        self.assertEqual(views._paystack_amount_credited_ghs({"amount": 5000, "metadata": ""}), 50.0)

    def test_base_larger_than_charge_is_ignored(self):
        data = {"amount": 100, "metadata": {"base_amount_minor": "6000"}}
        self.assertEqual(views._paystack_amount_credited_ghs(data), 1.0)


@override_settings(PAYSTACK_FEE_PERCENT="1.95", PAYSTACK_PASS_FEE_TO_CUSTOMER=True, PAYSTACK_SECRET_KEY=SECRET)
class InitializeTests(SimpleTestCase):
    def test_sends_grossed_up_amount_and_breakdown_metadata(self):
        resp = {"status": True, "data": {"reference": "ref1", "authorization_url": "https://checkout/x"}}
        with mock.patch.object(views, "_paystack_request", return_value=(resp, None)) as req:
            data, err = views._paystack_transaction_initialize(
                email="a@b.com", amount_ghs=60.0, callback_url="https://site/cb", metadata={"user_id": "u1"},
            )
        self.assertIsNone(err)
        self.assertEqual(data["reference"], "ref1")
        method, path, payload = req.call_args.args
        self.assertEqual((method, path), ("POST", "/transaction/initialize"))
        self.assertEqual(payload["amount"], 6120)
        self.assertEqual(payload["currency"], "GHS")
        meta = payload["metadata"]
        self.assertEqual(meta["user_id"], "u1")
        self.assertEqual(meta["base_amount_minor"], "6000")
        self.assertEqual(meta["processing_fee_minor"], "120")
        self.assertEqual([f["value"] for f in meta["custom_fields"]], ["GHS 60.00", "GHS 1.20"])
        json.dumps(payload)  # must be serialisable for the HTTP request

    def test_rejects_zero_amount(self):
        with mock.patch.object(views, "_paystack_request") as req:
            self.assertEqual(views._paystack_transaction_initialize(
                email="a@b.com", amount_ghs=0, callback_url="x")[1], "invalid_amount")
        req.assert_not_called()


@override_settings(PAYSTACK_FEE_PERCENT="1.95", PAYSTACK_PASS_FEE_TO_CUSTOMER=True, PAYSTACK_SECRET_KEY=SECRET)
class PaymentFlowTests(SimpleTestCase):
    """End-to-end through the views, with Supabase and Paystack HTTP mocked out."""

    def setUp(self):
        self.rf = RequestFactory()
        self.checkout_row = {"id": "sub1", "plan_slug": "standard", "status": "pending_payment"}
        patches = {
            "_get_plans": mock.Mock(return_value=PLANS),
            "_reconcile_pending_subscription_from_paystack": mock.Mock(),
            "subscription_allows_dashboard": mock.Mock(return_value=False),
            "_ensure_pending_checkout_row": mock.Mock(return_value=self.checkout_row),
            "_supabase_admin": mock.MagicMock(),
            "_student_unread_count": mock.Mock(return_value=0),
            "_community_unread_count": mock.Mock(return_value=0),
            "_apply_successful_subscription_payment": mock.Mock(),
        }
        for name, m in patches.items():
            p = mock.patch.object(views, name, m)
            p.start()
            self.addCleanup(p.stop)
        self.apply_payment = patches["_apply_successful_subscription_payment"]

    def _request(self, method, path, data=None):
        req = getattr(self.rf, method)(path, data or {})
        req.session = {"user_id": "u1", "email": "s@example.com", "role": "student", "full_name": "Stu"}
        return req

    def _paystack_verify(self, amount_minor, metadata):
        return {"status": True, "data": {
            "status": "success", "amount": amount_minor, "currency": "GHS", "reference": "ref1", "metadata": metadata,
        }}

    def test_subscribe_page_shows_fee_and_total(self):
        html = views.student_subscribe(self._request("get", "/subscribe/")).content.decode()
        self.assertIn("GHS 1.20 Paystack processing fee (1.95%)", html)
        self.assertIn("Total GHS 61.20", html)
        self.assertIn("Pay GHS 61.20", html)
        self.assertNotIn("with Paystack", html)

    @override_settings(PAYSTACK_PASS_FEE_TO_CUSTOMER=False)
    def test_subscribe_page_hides_fee_when_disabled(self):
        html = views.student_subscribe(self._request("get", "/subscribe/")).content.decode()
        self.assertNotIn("processing fee", html)
        self.assertIn("Pay GHS 60.00", html)

    def test_checkout_charges_customer_the_fee(self):
        init = {"status": True, "data": {"reference": "ref1", "authorization_url": "https://checkout.paystack.com/x"}}
        with mock.patch.object(views, "_paystack_request", return_value=(init, None)) as req:
            resp = views.student_subscribe(self._request("post", "/subscribe/", {"start_checkout": "1"}))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://checkout.paystack.com/x")
        payload = req.call_args.args[2]
        self.assertEqual(payload["amount"], 6120)
        self.assertEqual(payload["metadata"]["subscription_id"], "sub1")
        self.assertEqual(payload["metadata"]["base_amount_minor"], "6000")

    def test_success_callback_records_plan_price(self):
        meta = {"user_id": "u1", "subscription_id": "sub1", "plan_slug": "standard", "base_amount_minor": "6000"}
        with mock.patch.object(views, "_paystack_request", return_value=(self._paystack_verify(6120, meta), None)), \
             mock.patch.object(views, "_get_active_subscription", return_value=self.checkout_row):
            resp = views.student_subscribe_success(self._request("get", "/subscribe/success/", {"reference": "ref1"}))
        self.assertEqual(resp["Location"], "/dashboard/")
        self.apply_payment.assert_called_once_with("u1", "sub1", "standard", 60.0, "ref1")

    def test_webhook_records_plan_price(self):
        body = json.dumps({"event": "charge.success", "data": self._paystack_verify(6120, {
            "user_id": "u1", "subscription_id": "sub1", "plan_slug": "standard", "base_amount_minor": "6000",
        })["data"]}).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
        views._supabase_admin.return_value.table.return_value.select.return_value.eq.return_value \
            .eq.return_value.limit.return_value.execute.return_value.data = [
                {"id": "sub1", "status": "pending_payment", "user_id": "u1"}]
        req = self.rf.post("/paystack/webhook/", body, content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)
        self.assertEqual(views.paystack_webhook(req).status_code, 200)
        self.apply_payment.assert_called_once_with("u1", "sub1", "standard", 60.0, "ref1")

    def test_login_reconcile_records_plan_price(self):
        sub = {**self.checkout_row, "payment_reference": "ref1"}
        verify = self._paystack_verify(6120, {"base_amount_minor": "6000"})
        with mock.patch.object(views, "_paystack_request", return_value=(verify, None)), \
             mock.patch.object(views, "_get_active_subscription", return_value=sub):
            REAL_RECONCILE("u1", force=True)
        self.apply_payment.assert_called_once_with("u1", "sub1", "standard", 60.0, "ref1")

