from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # Public
    path("", views.home, name="home"),
    path("about/", views.about, name="about"),
    path("nclex/", views.nclex_page, name="nclex_page"),
    path("contact/", views.contact_page, name="contact"),

    # Auth
    path("login/", views.login_page, name="login"),
    path("auth/google/", views.google_oauth_start, name="google_oauth_start"),
    path("auth/google/callback/", views.google_oauth_callback, name="google_oauth_callback"),
    path("signup/", views.signup_page, name="signup"),
    path("logout/", views.logout_view, name="logout"),
    path("forgot-password/", views.forgot_password_page, name="forgot_password"),
    path("reset-password/", views.reset_password_page, name="reset_password"),

    # Student dashboard
    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    path(
        "dashboard/complete-academic-profile/",
        views.complete_academic_profile,
        name="complete_academic_profile",
    ),
    path("dashboard/ack-disclaimer/", views.dashboard_ack_nmc_disclaimer, name="dashboard_ack_nmc_disclaimer"),
    path("dashboard/nmc-mastery/", views.student_nmc_mastery, name="student_nmc_mastery"),
    path(
        "dashboard/hot-areas/",
        RedirectView.as_view(pattern_name="student_nmc_mastery", permanent=False),
    ),
    path("dashboard/messages/", views.student_messages, name="student_messages"),
    path("dashboard/messages/read/<uuid:message_id>/", views.student_mark_message_read, name="student_mark_message_read"),
    path("dashboard/messages/read-all/", views.student_mark_all_messages_read, name="student_mark_all_messages_read"),
    path("dashboard/dosage-calculator/", views.dosage_calculator, name="dosage_calculator"),
    path("dashboard/drug-cards/", views.drug_cards, name="drug_cards"),
    path("dashboard/lecture-notes/", views.student_lecture_notes, name="student_lecture_notes"),
    path("dashboard/flashcards/", views.student_flashcards, name="student_flashcards"),
    path("dashboard/general-tests/", views.student_general_tests, name="student_general_tests"),
    path("dashboard/nclex/", views.student_nclex_questions, name="student_nclex_questions"),
    path("dashboard/books/", views.student_books_library, name="student_books_library"),
    path("dashboard/general-tests/start/", views.student_general_test_start, name="student_general_test_start"),
    path("dashboard/general-tests/attempt/<uuid:attempt_id>/", views.student_general_test_attempt, name="student_general_test_attempt"),
    path("dashboard/general-tests/attempt/<uuid:attempt_id>/result/", views.student_general_test_result, name="student_general_test_result"),
    path("dashboard/quizzes/", views.student_quizzes, name="student_quizzes"),
    path("dashboard/quizzes/<uuid:quiz_id>/", views.student_quiz_take, name="student_quiz_take"),
    path("dashboard/quizzes/attempt/<uuid:attempt_id>/result/", views.student_quiz_result, name="student_quiz_result"),
    path("dashboard/bookmarks/", views.student_bookmarks, name="student_bookmarks"),
    path("dashboard/performance/", views.student_performance, name="student_performance"),
    path("dashboard/performance/review/<str:test_type>/<uuid:attempt_id>/", views.student_attempt_review, name="student_attempt_review"),
    path("dashboard/mock-exams/", views.student_mock_exams, name="student_mock_exams"),
    path("dashboard/mock-exams/<uuid:exam_id>/start/", views.student_take_mock_exam, name="student_take_mock_exam"),
    path("dashboard/mock-exams/<uuid:exam_id>/result/", views.student_mock_exam_result, name="student_mock_exam_result"),
    path("dashboard/report-question/", views.student_report_question, name="student_report_question"),
    path("dashboard/upgrade/", views.plan_upgrade_required, name="plan_upgrade_required"),

    # Admin panel
    path("admin-panel/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-panel/users/", views.admin_users, name="admin_users"),
    path("admin-panel/users/<uuid:user_id>/toggle-status/", views.admin_toggle_user_status, name="admin_toggle_user_status"),
    path("admin-panel/users/<uuid:user_id>/delete/", views.admin_delete_user, name="admin_delete_user"),
    path("admin-panel/locked-accounts/", views.admin_locked_accounts, name="admin_locked_accounts"),
    path("admin-panel/messages/", views.admin_messages, name="admin_messages"),
    path("admin-panel/broadcast-messages/", views.admin_broadcast_messages, name="admin_broadcast_messages"),
    path("admin-panel/upload-questions/", views.admin_upload_questions, name="admin_upload_questions"),
    path("admin-panel/nclex/", views.admin_nclex_questions, name="admin_nclex_questions"),
    path("admin-panel/manage-questions/", views.admin_manage_questions, name="admin_manage_questions"),
    path("admin-panel/mock-exams/", views.admin_mock_exams, name="admin_mock_exams"),
    path("admin-panel/quizzes/", views.admin_quizzes, name="admin_quizzes"),
    path("admin-panel/quizzes/questions/", views.admin_quiz_questions_api, name="admin_quiz_questions_api"),
    path("admin-panel/lecture-notes/", views.admin_lecture_notes, name="admin_lecture_notes"),
    path("admin-panel/drug-cards/", views.admin_drug_cards, name="admin_drug_cards"),
    path("admin-panel/drug-cards/add/", views.admin_drug_card_form, name="admin_drug_card_add"),
    path("admin-panel/drug-cards/<uuid:drug_id>/edit/", views.admin_drug_card_form, name="admin_drug_card_edit"),

    # Payment / Subscription
    path("payment/", views.payment_page, name="payment_page"),
    path("subscribe/", views.student_subscribe, name="student_subscribe"),
    path("subscribe/success/", views.student_subscribe_success, name="student_subscribe_success"),
    path("subscribe/cancel/", views.student_subscribe_cancel, name="student_subscribe_cancel"),

    # Community — Student
    path("dashboard/community/", views.community_home, name="community_home"),
    path("dashboard/community/<slug:slug>/", views.community_detail, name="community_detail"),
    path("dashboard/community/<slug:slug>/join/", views.community_join, name="community_join"),
    path("dashboard/community/<slug:slug>/leave/", views.community_leave, name="community_leave"),
    path("dashboard/community/<slug:slug>/create-post/", views.community_create_post, name="community_create_post"),
    path("dashboard/community/<slug:slug>/post/<uuid:post_id>/", views.community_post_detail, name="community_post_detail"),
    path("dashboard/community/post/<uuid:post_id>/comment/", views.community_add_comment, name="community_add_comment"),
    path("dashboard/community/post/<uuid:post_id>/react/", views.community_react_post, name="community_react_post"),
    path("dashboard/community/comment/<uuid:comment_id>/react/", views.community_react_comment, name="community_react_comment"),
    path("dashboard/community/post/<uuid:post_id>/report/", views.community_report_post, name="community_report_post"),
    path("dashboard/community/notifications/read/", views.community_notifications_read, name="community_notifications_read"),

    # Community — Admin
    path("admin-panel/payments/", views.admin_payments, name="admin_payments"),

    path("admin-panel/community/", views.admin_community_manage, name="admin_community_manage"),
    path("admin-panel/community/post/<uuid:post_id>/delete/", views.admin_community_delete_post, name="admin_community_delete_post"),
    path("admin-panel/community/post/<uuid:post_id>/verify/", views.admin_community_verify_answer, name="admin_community_verify_answer"),
    path("admin-panel/community/warn-user/", views.admin_community_warn_user, name="admin_community_warn_user"),
    path("admin-panel/community/report/<uuid:report_id>/resolve/", views.admin_community_resolve_report, name="admin_community_resolve_report"),
    path("admin-panel/reported-questions/", views.admin_reported_questions, name="admin_reported_questions"),
    path("admin-panel/free-users/", views.admin_free_users, name="admin_free_users"),

    # Security APIs
    path("api/screenshot-attempt/", views.screenshot_attempt_api, name="screenshot_attempt_api"),
]
