from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    # Before allauth's URLs, so these win over allauth's own login/logout pages.
    path("accounts/login/", views.login_page, name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("dev/sheet/", views.dev_sheet, name="dev_sheet"),  # TEMPORARY, staff only
]
