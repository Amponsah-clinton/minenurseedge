from django.test import Client, TestCase


class MobileApiSmokeTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_health_endpoint(self):
        resp = self.client.get("/api/mobile/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("ok"), True)

    def test_protected_endpoint_requires_bearer_token(self):
        resp = self.client.get("/api/mobile/quizzes/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json().get("error"), "missing_token")

    def test_admin_endpoint_requires_bearer_token(self):
        resp = self.client.get("/api/mobile/admin/summary/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json().get("error"), "missing_token")
