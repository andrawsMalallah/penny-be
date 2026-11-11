from django.urls import path

from . import views

urlpatterns = [
    path("setup/", views.SetupView.as_view()),
    path("preferences/", views.PreferencesView.as_view()),
    path("accounts/", views.AccountList.as_view()),
    path("accounts/create/", views.AccountCreateView.as_view()),
    path("accounts/<int:pk>/change/", views.AccountChangeView.as_view()),
    path("categories/", views.CategoryList.as_view()),
    path("categories/create/", views.CategoryCreateView.as_view()),
    path("categories/<int:pk>/change/", views.CategoryChangeView.as_view()),
    path("months/", views.MonthList.as_view()),
    path("rollover/", views.RolloverView.as_view()),
    path("spending-preview/", views.SpendPreviewView.as_view()),
    path("exchange-preview/", views.ExchangePreviewView.as_view()),
    path("rollover-preview/", views.RolloverPreviewView.as_view()),
    path("expenses/", views.ExpenseView.as_view()),
    path("income/", views.IncomeView.as_view()),
    path("transfers/", views.TransferView.as_view()),
    path("exchanges/", views.ExchangeView.as_view()),
    path("activity/", views.ActivityList.as_view()),
    path("activity/<int:pk>/", views.OperationDetail.as_view()),
    path("activity/<int:pk>/reverse/", views.ReverseView.as_view()),
    path("reports/", views.ReportsView.as_view()),
]
