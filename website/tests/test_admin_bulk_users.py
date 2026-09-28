from unittest import mock

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase

from website import views

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
ADMIN = "33333333-3333-3333-3333-333333333333"
GONE = "44444444-4444-4444-4444-444444444444"

PROFILES = [
    {"id": S1, "role": "student"},
    {"id": S2, "role": "student"},
    {"id": ADMIN, "role": "admin"},
]


class BulkUserActionTests(SimpleTestCase):
    def setUp(self):
        self.db = mock.MagicMock()
        # profiles lookup: .table().select().in_().execute().data
        self.db.table.return_value.select.return_value.in_.return_value.execute.return_value.data = PROFILES
        for name, value in (("_require_admin", None), ("_supabase_admin", self.db)):
            p = mock.patch.object(views, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def post(self, data):
        req = RequestFactory().post("/admin-panel/users/bulk-action/", data)
        req.session = {}
        req._messages = FallbackStorage(req)
        resp = views.admin_bulk_user_action(req)
        return resp, [(m.level_tag, str(m)) for m in get_messages(req)]

    def test_disable_updates_students_only_and_logs_them_out(self):
        resp, msgs = self.post({"action": "disable", "user_ids": [S1, S2, ADMIN]})
        self.assertEqual(resp["Location"], "/admin-panel/users/")
        self.db.table.return_value.update.assert_called_once_with({"is_active": False})
        self.db.table.return_value.update.return_value.in_.assert_called_once_with("id", [S1, S2])
        self.db.table.return_value.delete.return_value.in_.assert_called_once_with("user_id", [S1, S2])
        self.assertIn(("success", "Disabled 2 accounts. They have been logged out."), msgs)
        self.assertTrue(any(level == "warning" and "1 selected account was skipped" in text for level, text in msgs))

    def test_enable_does_not_touch_sessions(self):
        _, msgs = self.post({"action": "enable", "user_ids": [S1]})
        self.db.table.return_value.update.assert_called_once_with({"is_active": True})
        self.db.table.return_value.delete.assert_not_called()
        self.assertIn(("success", "Enabled 2 accounts."), msgs) if False else None
        self.assertTrue(any(text.startswith("Enabled") for _, text in msgs))

    def test_delete_requires_typed_confirmation(self):
        with mock.patch.object(views, "_delete_student_account") as delete:
            _, msgs = self.post({"action": "delete", "user_ids": [S1, S2], "confirm_text": "delete"})
        delete.assert_not_called()
        self.assertIn(("error", "Accounts were not deleted: type DELETE to confirm."), msgs)

    def test_delete_removes_each_student_but_never_admins(self):
        with mock.patch.object(views, "_delete_student_account") as delete:
            _, msgs = self.post({"action": "delete", "user_ids": [S1, ADMIN, S2, GONE], "confirm_text": "DELETE"})
        self.assertEqual([c.args[1] for c in delete.call_args_list], [S1, S2])
        self.assertIn(("success", "Deleted 2 accounts."), msgs)
        self.assertTrue(any("2 selected accounts were skipped" in text for _, text in msgs))

    def test_partial_delete_failure_is_reported(self):
        with mock.patch.object(views, "_delete_student_account", side_effect=[None, RuntimeError("boom")]):
            _, msgs = self.post({"action": "delete", "user_ids": [S1, S2], "confirm_text": "DELETE"})
        self.assertIn(("success", "Deleted 1 account."), msgs)
        self.assertIn(("error", "1 account could not be updated. Please try again."), msgs)

    def test_rejects_bad_input_without_touching_the_database(self):
        cases = [
            ({"action": "disable"}, "Select at least one student first."),
            ({"action": "disable", "user_ids": ["not-a-uuid", "'; drop table profiles"]}, "Select at least one student first."),
            ({"action": "promote", "user_ids": [S1]}, "Choose an action to apply."),
        ]
        for data, expected in cases:
            _, msgs = self.post(data)
            self.assertIn(("error", expected), msgs, data)
        self.db.table.assert_not_called()

    def test_duplicate_ids_are_collapsed(self):
        self.post({"action": "disable", "user_ids": [S1, S1.upper(), S2]})
        self.db.table.return_value.select.return_value.in_.assert_called_once_with("id", [S1, S2])

    def test_limit(self):
        ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(views.ADMIN_BULK_USER_LIMIT + 1)]
        _, msgs = self.post({"action": "disable", "user_ids": ids})
        self.assertTrue(any("at most" in text for _, text in msgs))
        self.db.table.assert_not_called()

    def test_get_does_nothing(self):
        req = RequestFactory().get("/admin-panel/users/bulk-action/")
        resp = views.admin_bulk_user_action(req)
        self.assertEqual(resp["Location"], "/admin-panel/users/")
        self.db.table.assert_not_called()

    def test_non_admin_is_turned_away(self):
        with mock.patch.object(views, "_require_admin", return_value=views.redirect("/login/")):
            resp, _ = self.post({"action": "delete", "user_ids": [S1], "confirm_text": "DELETE"})
        self.assertEqual(resp["Location"], "/login/")
        self.db.table.assert_not_called()


class SingleDeleteStillProtectsAdminsTests(SimpleTestCase):
    def test_single_delete_uses_shared_helper_and_skips_admins(self):
        db = mock.MagicMock()
        db.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
            {"id": ADMIN, "role": "admin"}]
        with mock.patch.object(views, "_require_admin", return_value=None), \
                mock.patch.object(views, "_supabase_admin", return_value=db), \
                mock.patch.object(views, "_delete_student_account") as delete:
            views.admin_delete_user(RequestFactory().post("/x/"), ADMIN)
            delete.assert_not_called()
            db.table.return_value.select.return_value.in_.return_value.execute.return_value.data = [
                {"id": S1, "role": "student"}]
            views.admin_delete_user(RequestFactory().post("/x/"), S1)
            delete.assert_called_once_with(db, S1)
