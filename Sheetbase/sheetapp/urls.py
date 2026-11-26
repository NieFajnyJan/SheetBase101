from django.contrib.auth.views import LogoutView
from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # No start page: the app opens on Transactions (logged out, that sends you to log in first).
    path("", RedirectView.as_view(pattern_name="transactions"), name="home"),
    # Before allauth's URLs, so these win over allauth's own login/logout pages.
    path("accounts/login/", views.login_page, name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("transactions/", views.transactions, name="transactions"),
    path("transactions/<int:pk>/correct/", views.correct_transaction, name="correct_transaction"),
    path("transactions/<int:pk>/cancel/", views.cancel_transaction, name="cancel_transaction"),
    path("planned/", views.planned, name="planned"),
    path("planned/<int:pk>/edit/", views.edit_planned, name="edit_planned"),
    path("planned/<int:pk>/delete/", views.delete_planned, name="delete_planned"),
    path("summary/", views.summary, name="summary"),
]
