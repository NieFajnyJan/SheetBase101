from django.test import TestCase
from django.urls import reverse

from .models import User

PASSWORD = "Sup3r-secret-pw!"


def signup_data(**overrides):
    return {
        "username": "ola",
        "email": "ola@example.com",
        "first_name": "Ola",
        "last_name": "Nowak",
        "password1": PASSWORD,
        "password2": PASSWORD,
        **overrides,
    }


class SignUpTests(TestCase):
    def test_page_loads(self):
        response = self.client.get(reverse("signup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create an account")

    def test_creates_user_and_logs_in(self):
        response = self.client.post(reverse("signup"), signup_data())
        self.assertRedirects(response, reverse("home"))
        user = User.objects.get(username="ola")
        self.assertEqual(user.email, "ola@example.com")
        self.assertEqual(str(user), "Ola Nowak")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_duplicate_email_rejected(self):
        User.objects.create_user("jan", "ola@example.com", PASSWORD)
        response = self.client.post(reverse("signup"), signup_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertFalse(User.objects.filter(username="ola").exists())

    def test_password_mismatch_rejected(self):
        response = self.client.post(reverse("signup"), signup_data(password2="different-pw-1!"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_weak_password_rejected(self):
        response = self.client.post(reverse("signup"), signup_data(password1="12345678", password2="12345678"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_name_required(self):
        response = self.client.post(reverse("signup"), signup_data(first_name="", last_name=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_cannot_make_yourself_admin(self):
        self.client.post(reverse("signup"), signup_data(is_staff="on", is_superuser="on"))
        user = User.objects.get(username="ola")
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_logged_in_user_redirected_away(self):
        self.client.force_login(User.objects.create_user("jan", "jan@example.com", PASSWORD))
        self.assertRedirects(self.client.get(reverse("signup")), reverse("home"))


class LoginLogoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("jan", "jan@example.com", PASSWORD, first_name="Jan")

    def test_login_and_logout(self):
        response = self.client.post(reverse("login"), {"username": "jan", "password": PASSWORD})
        self.assertRedirects(response, reverse("home"))
        self.assertContains(self.client.get(reverse("home")), "Hi, Jan!")
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("home"))
        self.assertContains(self.client.get(reverse("home")), "Create an account")

    def test_wrong_password(self):
        response = self.client.post(reverse("login"), {"username": "jan", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
