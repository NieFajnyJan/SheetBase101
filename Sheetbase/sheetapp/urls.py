from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    # Before allauth's URLs, so these win over allauth's own login/logout pages.
    path("accounts/login/", views.login_page, name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("transactions/", views.transactions, name="transactions"),
    path("transactions/<int:pk>/correct/", views.correct_transaction, name="correct_transaction"),
    path("transactions/<int:pk>/cancel/", views.cancel_transaction, name="cancel_transaction"),
    path("summary/", views.summary, name="summary"),
]
