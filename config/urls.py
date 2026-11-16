from django.urls import path

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
    path("api/v1/auth/password-reset/", views.PasswordResetRequestView.as_view()),
    path("api/v1/auth/password-reset/verify/", views.PasswordResetVerifyView.as_view()),
    path("api/v1/auth/password-reset/complete/", views.PasswordResetCompleteView.as_view()),
]
