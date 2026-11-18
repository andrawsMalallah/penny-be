from django.urls import path

from finance import analytics_views as analytics
from finance import budget_views as budgets
from finance import debt_views as debts
from finance import recurring_views as recurring
from finance import transaction_views as activity
from finance import views as finance
from identity import two_factor_views as factor
from identity import views

urlpatterns = [
    path("api/v1/recurring/", recurring.ScheduleListView.as_view()),
    path("api/v1/recurring/<int:pk>/", recurring.ScheduleDetailView.as_view()),
    path("api/v1/recurring/<int:pk>/state/", recurring.ScheduleStateView.as_view()),
    path("api/v1/recurring/<int:pk>/pending/", recurring.PendingView.as_view()),
    path("api/v1/recurring/<int:pk>/confirm/", recurring.ResolveView.as_view()),
    path("api/v1/recurring/<int:pk>/skip/", recurring.ResolveView.as_view(), {"skip": True}),
    path("api/v1/budgets/", budgets.BudgetListView.as_view()),
    path("api/v1/budgets/<int:pk>/", budgets.BudgetDetailView.as_view()),
    path("api/v1/analytics/", analytics.AnalyticsView.as_view()),
    path(
        "api/v1/analytics/accounts/",
        analytics.AnalyticsBreakdownView.as_view(),
        {"section": "accounts"},
    ),
    path(
        "api/v1/analytics/groups/",
        analytics.AnalyticsBreakdownView.as_view(),
        {"section": "groups"},
    ),
    path(
        "api/v1/analytics/categories/",
        analytics.AnalyticsBreakdownView.as_view(),
        {"section": "categories"},
    ),
    path(
        "api/v1/analytics/people/",
        analytics.AnalyticsBreakdownView.as_view(),
        {"section": "people"},
    ),
    path("api/v1/debt-people/", debts.DebtPersonListView.as_view()),
    path("api/v1/debt-people/<int:pk>/", debts.DebtPersonDetailView.as_view()),
    path("api/v1/debts/", debts.DebtListView.as_view()),
    path("api/v1/debts/<int:pk>/", debts.DebtDetailView.as_view()),
    path("api/v1/debts/<int:pk>/delete/", debts.DebtDeleteView.as_view()),
    path("api/v1/debts/<int:pk>/repayments/", debts.RepaymentListView.as_view()),
    path("api/v1/debts/<int:pk>/repayments/<int:entry_id>/", debts.RepaymentDetailView.as_view()),
    path(
        "api/v1/debts/<int:pk>/repayments/<int:entry_id>/delete/",
        debts.RepaymentDeleteView.as_view(),
    ),
    path("api/v1/transactions/request-id/", activity.TransactionRequestIdView.as_view()),
    path("api/v1/transactions/", activity.TransactionListView.as_view()),
    path("api/v1/transactions/<int:pk>/", activity.TransactionDetailView.as_view()),
    path("api/v1/transactions/<int:pk>/delete/", activity.TransactionDeleteView.as_view()),
    path("api/v1/expense-categories/initialize/", finance.CategoryInitializeView.as_view()),
    path("api/v1/expense-categories/", finance.CategoryListView.as_view()),
    path("api/v1/expense-categories/<int:pk>/", finance.CategoryDetailView.as_view()),
    path("api/v1/expense-categories/<int:pk>/archive/", finance.CategoryArchiveView.as_view()),
    path("api/v1/accounts/", finance.AccountListView.as_view()),
    path("api/v1/accounts/<int:pk>/", finance.AccountDetailView.as_view()),
    path("api/v1/accounts/<int:pk>/archive/", finance.AccountArchiveView.as_view()),
    path("api/v1/account-groups/", finance.GroupListView.as_view()),
    path("api/v1/account-groups/<int:pk>/", finance.GroupDetailView.as_view()),
    path("api/v1/account-groups/<int:pk>/archive/", finance.GroupArchiveView.as_view()),
    path("api/v1/health/", views.health),
    path("api/v1/auth/csrf/", views.csrf_token),
    path("api/v1/auth/register/", views.RegisterView.as_view()),
    path("api/v1/auth/login/", views.LoginView.as_view()),
    path("api/v1/auth/verify-email/", views.VerifyEmailView.as_view()),
    path("api/v1/auth/resend-code/", views.ResendCodeView.as_view()),
    path("api/v1/auth/me/", views.CurrentUserView.as_view()),
    path("api/v1/auth/session/", views.SessionStateView.as_view()),
    path("api/v1/settings/", views.UserSettingsView.as_view()),
    path("api/v1/auth/logout/", views.LogoutView.as_view()),
    path("api/v1/auth/sessions/", views.SessionListView.as_view()),
    path("api/v1/auth/logout-all/", views.LogoutAllView.as_view()),
    path("api/v1/auth/2fa/", factor.FactorStatusView.as_view()),
    path("api/v1/auth/2fa/login/", factor.FactorLoginView.as_view()),
    path("api/v1/auth/2fa/login/cancel/", factor.FactorLoginCancelView.as_view()),
    path("api/v1/auth/2fa/setup/", factor.FactorSetupView.as_view()),
    path("api/v1/auth/2fa/setup/cancel/", factor.FactorSetupCancelView.as_view()),
    path("api/v1/auth/2fa/enable/", factor.FactorEnableView.as_view()),
    path("api/v1/auth/2fa/disable/", factor.FactorDisableView.as_view()),
    path("api/v1/auth/2fa/recovery-codes/", factor.FactorManageView.as_view()),
    path("api/v1/auth/password-reset/", views.PasswordResetRequestView.as_view()),
    path("api/v1/auth/password-reset/verify/", views.PasswordResetVerifyView.as_view()),
    path("api/v1/auth/password-reset/complete/", views.PasswordResetCompleteView.as_view()),
]
