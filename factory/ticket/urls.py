from django.urls import path

from . import views

app_name = "tickets"

urlpatterns = [
    path("", views.ticket_list, name="list"),
    path("<int:pk>/", views.ticket_detail, name="detail"),
    path("<int:pk>/notes/", views.ticket_notes, name="notes"),
]
