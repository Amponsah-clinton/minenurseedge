from django.urls import path
from . import views

urlpatterns = [
    # Public
    path("", views.home, name="home"),
    path("about/", views.about, name="about"),
    path("contact/", views.contact_page, name="contact"),

    # Auth
    path("login/", views.login_page, name="login"),
    path("signup/", views.signup_page, name="signup"),
    path("logout/", views.logout_view, name="logout"),

    # Student dashboard
    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    path("dashboard/messages/", views.student_messages, name="student_messages"),
    path("dashboard/messages/read/<uuid:message_id>/", views.student_mark_message_read, name="student_mark_message_read"),
    path("dashboard/messages/read-all/", views.student_mark_all_messages_read, name="student_mark_all_messages_read"),
    path("dashboard/dosage-calculator/", views.dosage_calculator, name="dosage_calculator"),
    path("dashboard/drug-cards/", views.drug_cards, name="drug_cards"),
    path("dashboard/lecture-notes/", views.student_lecture_notes, name="student_lecture_notes"),
    path("dashboard/flashcards/", views.student_flashcards, name="student_flashcards"),
    path("dashboard/general-tests/", views.student_general_tests, name="student_general_tests"),
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

    # Admin panel
    path("admin-panel/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-panel/users/", views.admin_users, name="admin_users"),
    path("admin-panel/messages/", views.admin_messages, name="admin_messages"),
    path("admin-panel/broadcast-messages/", views.admin_broadcast_messages, name="admin_broadcast_messages"),
    path("admin-panel/upload-questions/", views.admin_upload_questions, name="admin_upload_questions"),
    path("admin-panel/manage-questions/", views.admin_manage_questions, name="admin_manage_questions"),
    path("admin-panel/mock-exams/", views.admin_mock_exams, name="admin_mock_exams"),
    path("admin-panel/quizzes/", views.admin_quizzes, name="admin_quizzes"),
    path("admin-panel/lecture-notes/", views.admin_lecture_notes, name="admin_lecture_notes"),
    path("admin-panel/drug-cards/", views.admin_drug_cards, name="admin_drug_cards"),
    path("admin-panel/drug-cards/add/", views.admin_drug_card_form, name="admin_drug_card_add"),
    path("admin-panel/drug-cards/<uuid:drug_id>/edit/", views.admin_drug_card_form, name="admin_drug_card_edit"),
]
