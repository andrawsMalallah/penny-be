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
]
