from django.urls import path

from .mfa_views import (
    MfaConfirmView,
    MfaDisableView,
    MfaLoginView,
    MfaRegenerateView,
    MfaSetupView,
    MfaStatusView,
)
from .views import (
    CsrfView,
    DevicesView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    RegisterView,
)

urlpatterns = [
    path("mfa/", MfaStatusView.as_view(), name="auth-mfa-status"),
    path("mfa/setup/", MfaSetupView.as_view(), name="auth-mfa-setup"),
    path("mfa/confirm/", MfaConfirmView.as_view(), name="auth-mfa-confirm"),
    path("mfa/login/", MfaLoginView.as_view(), name="auth-mfa-login"),
    path("mfa/recovery-codes/", MfaRegenerateView.as_view(), name="auth-mfa-recovery"),
    path("mfa/disable/", MfaDisableView.as_view(), name="auth-mfa-disable"),
    path("csrf/", CsrfView.as_view(), name="auth-csrf"),
    path("register/", RegisterView.as_view(), name="auth-register"),
    path("login/", LoginView.as_view(), name="auth-login"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("password/", PasswordChangeView.as_view(), name="auth-password"),
    path("devices/", DevicesView.as_view(), name="auth-devices"),
]
