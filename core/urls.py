from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from django.views.generic import TemplateView

from . import views

# Update Summary (2025-02-11): Added assistant chat, sync, and data portability routes.

app_name = "core"


urlpatterns = [
    # Dashboard & auth
    path("", views.dashboard, name="dashboard"),
    path("dashboard/", views.dashboard, name="dashboard_home"),
    path("post_login_redirect/", views.post_login_redirect, name="post_login_redirect"),
    path("welcome/", views.welcome_hub, name="welcome"),
    path("dashboard/live/", views.dashboard_live_data, name="dashboard_live_data"),
    path("dashboard/ai_insights/", views.ai_insights_feed, name="ai_insights_feed"),
    path("reports/ai/", views.ai_reports, name="ai_reports"),
    path("reports/ai/export/", views.export_ai_report_pdf, name="export_ai_report_pdf"),
    path("admin/hub/", views.admin_hub, name="admin_hub"),
    path("admin/analytics/", views.admin_analytics, name="admin_analytics"),
    path("admin/billing-expiring/", views.admin_billing_expiring, name="admin_billing_expiring"),
    path("admin/system-health/", views.system_health, name="system_health"),
    path("admin/users/", views.admin_user_management, name="admin_users"),
    path("admin/users/<int:user_id>/toggle/", views.admin_toggle_premium, name="admin_toggle_premium"),
    path("college/", views.college_dashboard, name="college"),
    path("gcse/", views.gcse_dashboard, name="gcse"),
    path("compare/levels/", views.compare_levels_view, name="compare_levels"),
    path("compare/all-levels/", views.compare_all_levels_view, name="compare_all_levels"),
    path("timeline/", views.progress_timeline_view, name="progress_timeline"),
    path("progress/timeline/", views.progress_timeline_view, name="progress_timeline_alt"),
    path("college/ucas/add/", views.add_ucas_offer, name="add_ucas_offer"),
    path("college/ucas/<int:pk>/update/", views.update_ucas_offer, name="update_ucas_offer"),
    path("college/ucas/<int:pk>/delete/", views.delete_ucas_offer, name="delete_ucas_offer"),
    path("college/ucas/simulate/", views.simulate_ucas_scenario, name="simulate_ucas_scenario"),
    path("college/personal-statement/", views.save_personal_statement, name="save_personal_statement"),
    path("college/super-curricular/toggle/", views.toggle_super_curricular, name="toggle_super_curricular"),
    path("gcse/revision/add/", views.add_revision_session, name="add_revision_session"),
    path("gcse/revision/<int:pk>/delete/", views.delete_revision_session, name="delete_revision_session"),
    path("gcse/papers/add/", views.add_past_paper, name="add_past_paper"),
    path("gcse/papers/<int:pk>/update/", views.update_past_paper, name="update_past_paper"),
    path("gcse/papers/<int:pk>/delete/", views.delete_past_paper, name="delete_past_paper"),
    path("gcse/exam-checklist/toggle/", views.toggle_exam_checklist, name="toggle_exam_checklist"),
    path("logout/", views.logout_view, name="logout"),
    path("delete-account/", views.delete_account, name="delete_account"),
    path("accounts/login-required/", views.login_required_view, name="login_required_view"),
    path("accounts/mock-login/", views.mock_login, name="mock_login"),
    path("accounts/signup/", views.signup_disabled_view, name="signup_disabled"),
    path("whats-new/", views.whats_new, name="whats_new"),
    path("contact-support/", views.contact_support, name="contact_support"),

    # Planner APIs
    path("save_future_modules/", views.save_future_modules, name="save_future_modules"),
    path("save_upcoming_deadlines/", views.save_upcoming_deadlines, name="save_upcoming_deadlines"),
    path("dashboard/goals/", views.study_goals, name="study_goals"),
    path("dashboard/goals/<int:pk>/", views.study_goal_update, name="study_goal_update"),
    path("deadlines/<int:pk>/complete/", views.deadline_complete, name="deadline_complete"),
    path("deadlines/<int:pk>/reschedule/", views.deadline_reschedule, name="deadline_reschedule"),
    path("deadlines/<int:pk>/update/", views.deadline_update, name="deadline_update"),
    path("deadlines/<int:pk>/move_to_plan/", views.deadline_move_to_plan, name="deadline_move_to_plan"),
    path("snapshots/create/", views.create_snapshot, name="create_snapshot"),
    path("dashboard/deadline/<int:deadline_id>/react/", views.ai_deadline_react, name="ai_deadline_react"),
    path("dashboard/ai_schedule/", views.ai_study_schedule, name="ai_study_schedule"),
    path("dashboard/ai_revision/", views.ai_revision_scheduler, name="ai_revision_scheduler"),
    path("dashboard/ai_cross_forecast/", views.ai_cross_level_forecast, name="ai_cross_level_forecast"),
    path("dashboard/ai_subject_radar/", views.ai_subject_radar, name="ai_subject_radar"),
    path("dashboard/ai_forecast_hub/", views.ai_forecast_hub, name="ai_forecast_hub"),
    path("dashboard/ai_forecast_chat/", views.ai_forecast_chat, name="ai_forecast_chat"),
    path("dashboard/assistant/chat/", views.ai_assistant, name="ai_assistant"),
    path("dashboard/ai_forecast_state/", views.ai_forecast_state, name="ai_forecast_state"),
    path("dashboard/ai_voice_mentor/", views.ai_voice_mentor, name="ai_voice_mentor"),
    path("dashboard/ai_study_load/", views.ai_study_load_dashboard, name="ai_study_load_dashboard"),
    path("dashboard/ai_insights/feedback/", views.record_ai_insight_feedback, name="ai_insight_feedback"),
    path("dashboard/add_study_plan/", views.add_study_plan, name="add_study_plan"),
    path("dashboard/ai_generate_plan/", views.ai_generate_study_plan, name="ai_generate_study_plan"),
    path("dashboard/study-plan/calendar/", views.export_study_plan_calendar, name="study_plan_calendar"),
    path("weekly-goals-data/", views.weekly_goals_data, name="weekly_goals_data"),
    path("study-habits-data/", views.study_habits_data, name="study_habits_data"),
    path("ai-weekly-reflection/", views.ai_weekly_reflection, name="ai_weekly_reflection"),
    path("ai-study-schedule-week/", views.ai_study_schedule_week, name="ai_study_schedule_week"),
    path("snapshot-comparison/", views.snapshot_comparison, name="snapshot_comparison"),
    path("snapshot/comparison/", views.snapshot_comparison, name="snapshot_comparison_alias"),
    path("ai/daily-motivation/", views.ai_daily_motivation, name="ai_daily_motivation"),
    path("dashboard/target-calculator/", views.target_calculator, name="target_calculator"),
    path("tools/target-grade/", views.target_grade_calculator_page, name="target_grade_calculator_page"),
    path("energy/data/", views.study_energy_data, name="study_energy_data"),
    path("weekly-digest/", views.weekly_digest, name="weekly_digest"),
    path("generate-mock-data/", views.generate_mock_data, name="generate_mock_data"),
    path("generate/mock/", views.generate_mock_data, name="generate_mock_data_alias"),
    path("dashboard/sync/", views.sync_dashboard, name="sync_dashboard"),

    # Settings & exports
    path("settings/", views.settings_view, name="settings"),
    path("settings/view/", views.settings_view, name="settings_view"),
    path("settings/support/", views.submit_support_request, name="settings_support"),
    path("settings/update/", views.update_settings, name="update_settings"),
    path("settings/export/", views.export_data, name="export_data"),
    path("settings/export/all/", views.export_user_data, name="export_user_data"),
    path("settings/import/", views.import_user_data, name="import_user_data"),
    path("privacy/dashboard/", views.privacy_dashboard, name="privacy_dashboard"),
    path("privacy/export/", views.download_personal_data, name="download_personal_data"),
    path("toggle-theme/", views.toggle_theme, name="toggle_theme"),
    path("take_snapshot_now/", views.take_snapshot_now, name="take_snapshot_now"),
    path("export/csv/", views.export_modules_csv, name="export_modules_csv"),
    path("backup/json/", views.backup_json, name="backup_json"),
    path("backup/history/", views.backup_history, name="backup_history"),
    path("restore/json/", views.restore_backup, name="restore_backup"),
    path("export/predictions/", views.export_prediction_csv, name="export_prediction_csv"),

    # Modules
    path("modules/", views.modules_list, name="modules_list"),
    path("modules/add/", views.module_add, name="module_add"),
    path("modules/update/<int:pk>/", views.module_update, name="module_update"),
    path("modules/<int:pk>/update/", views.module_update, name="module_update_alias"),
    path("modules/delete/<int:pk>/", views.module_delete, name="module_delete"),
    path("api/modules/stats/", views.modules_stats, name="modules_stats"),

    # API surfaces
    path("api/dashboard-data/", views.dashboard_data, name="dashboard_data"),
    path("dashboard/ai_mentor_tip/", views.ai_mentor_tip, name="ai_mentor_tip"),

    # Predictions
    path("ai/predict/", views.ai_prediction, name="ai_prediction"),
    path("ai-predict/", views.ai_prediction, name="ai_prediction_alias"),
    path("predict/final/", views.predict_final_average, name="predict_final_average"),
    path("predict/targets/", views.predict_targets, name="predict_targets"),
    path("predict/save/", views.save_prediction, name="save_prediction"),
    path("predictions/", views.prediction_history, name="prediction_history"),
    path("api/predict_what_if/", views.predict_what_if, name="predict_what_if"),
    path("what-if/", views.what_if_simulation, name="what_if_simulation"),
    path("what-if/history/", views.what_if_history, name="what_if_history"),
    path("milestones/", views.achievements_center, name="milestone_history"),
    path("achievements/share/<str:token>/", views.achievement_share, name="achievement_share"),

    # Billing
    path("pricing/", views.pricing, name="pricing"),
    path("upgrade/", views.upgrade_page, name="upgrade"),
    path("payment/success/", views.payment_success, name="payment_success"),
    path("payment/cancel/", views.payment_cancel, name="payment_cancel"),
    path("manage-subscription/", views.manage_subscription, name="manage_subscription"),
    path("billing/cancel/", views.cancel_subscription, name="cancel_subscription"),
    path("upgrade/create-session/", views.create_checkout_session, name="create_checkout_session_default"),
    path("create-checkout-session/<str:plan_type>/", views.create_checkout_session, name="create_checkout_session"),
    path("create-portal-session/", views.create_portal_session, name="create_portal_session"),
]


STATIC_PAGES = {
    "college/": ("college", "core/college.html"),
    "study-suggestions/": ("study_suggestions", "core/study_suggestions.html"),
    "smart-insights/": ("smart_insights_page", "core/smart_insights.html"),
    "help/": ("help", "core/help.html"),
    "feedback/": ("feedback", "core/feedback.html"),
    "report-bug/": ("bug_report", "core/report_bug.html"),
    "how-it-works/": ("how_it_works", "core/how_it_works.html"),
    "welcome-tour/": ("welcome_tour", "core/welcome_tour.html"),
    "demo-notice/": ("demo_notice", "core/demo_notice.html"),
    "privacy/": ("privacy_policy", "core/privacy_policy.html"),
    "privacy-policy/": ("privacy_policy_alias", "core/privacy_policy.html"),
    "terms/": ("terms_of_service", "core/terms_of_service.html"),
    "terms-of-service/": ("terms_of_service_alias", "core/terms_of_service.html"),
    "legal/disclaimer/": ("prediction_disclaimer", "core/prediction_disclaimer.html"),
    "disclaimer/": ("prediction_disclaimer_alias", "core/prediction_disclaimer.html"),
    "cookies/": ("cookie_notice", "core/cookie_notice.html"),
}


for url, (name, template) in STATIC_PAGES.items():
    urlpatterns.append(path(url, TemplateView.as_view(template_name=template), name=name))

urlpatterns += [
    path("snapshot/history/", views.snapshot_history, name="snapshot_history"),
    path("what-if/basic/", views.what_if_basic, name="what_if_basic"),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
