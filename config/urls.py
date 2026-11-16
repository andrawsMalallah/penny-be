from django.urls import path

from identity import two_factor_views as factor
from identity import views

urlpatterns = [
    path("api/v1/health/", views.health),
    path("api/v1/auth/csrf/", views.csrf_token),
    path("api/v1/auth/register/", views.RegisterView.as_view()),
    path("api/v1/auth/login/", views.LoginView.as_view()),
    path("api/v1/auth/verify-email/", views.VerifyEmailView.as_view()),
    path("api/v1/auth/resend-code/", views.ResendCodeView.as_view()),
    path("api/v1/auth/me/", views.CurrentUserView.as_view()),
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
