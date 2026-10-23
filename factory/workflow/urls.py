from django.urls import path

from . import views

app_name = "workflow"

urlpatterns = [
    path("", views.submit, name="submit"),
]
