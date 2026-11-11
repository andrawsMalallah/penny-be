from django.urls import path

from . import api

urlpatterns = [
    path("jobs/", api.CreateJob.as_view()),
    path("jobs/<uuid:pk>/", api.JobStatus.as_view()),
    path("jobs/<uuid:pk>/backup/", api.DownloadBackup.as_view()),
    path("jobs/<uuid:pk>/csv/", api.DownloadCsv.as_view()),
    path("restore/preview/", api.PreviewRestore.as_view()),
    path("restore/confirm/", api.ConfirmRestore.as_view()),
]
