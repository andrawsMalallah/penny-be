from django.urls import path

from . import views

urlpatterns = [
    path("people/", views.PersonList.as_view()),
    path("people/create/", views.PersonCreate.as_view()),
    path("people/<int:pk>/change/", views.PersonChange.as_view()),
    path("people/<int:person_id>/debts/", views.DebtList.as_view()),
    path("records/", views.DebtList.as_view()),
    path("records/create/", views.CreateDebt.as_view()),
    path("records/<int:pk>/", views.DebtDetail.as_view()),
    path("records/<int:pk>/events/", views.EventList.as_view()),
    path("records/<int:pk>/repay/", views.RepayDebt.as_view()),
    path("records/<int:pk>/write-off/", views.WriteOffDebt.as_view()),
    path("events/<int:pk>/reverse/", views.ReverseEvent.as_view()),
    path("summary/", views.DebtSummary.as_view()),
]
