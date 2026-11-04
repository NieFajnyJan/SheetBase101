from django.test import TestCase
from django.urls import reverse

from .models import User

PASSWORD = "Sup3r-secret-pw!"


class NoLocalAccountsTests(TestCase):
    """Accounts come only from Google / Dex – no password signup or login on the site."""

    def test_signup_pages_are_gone(self):
        for url in ("/accounts/signup/", "/accounts/password/reset/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_password_login_is_rejected(self):
        User.objects.create_user("jan", "jan@example.com", PASSWORD)
        response = self.client.post(reverse("login"), {"username": "jan", "password": PASSWORD})
        self.assertEqual(response.status_code, 405)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_has_no_password_form(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'type="password"')

    def test_logged_in_user_redirected_away_from_login(self):
        self.client.force_login(User.objects.create_user("jan", "jan@example.com"))
        self.assertRedirects(self.client.get(reverse("login")), reverse("home"))

    def test_logout(self):
        self.client.force_login(User.objects.create_user("jan", "jan@example.com", first_name="Jan"))
        self.assertContains(self.client.get(reverse("home")), "Hi, Jan!")
        self.assertRedirects(self.client.post(reverse("logout")), reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)


# --------------------------------------------------------------------------- Google login
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import override_settings

GOOGLE_CONFIGURED = {
    "google": {
        "SCOPE": ["profile", "email"],
        "APPS": [{"client_id": "test-client-id", "secret": "test-secret"}],
    }
}
GOOGLE_NOT_CONFIGURED = {"google": {"APPS": []}}


def google_profile(**overrides):
    """What Google's ID token says about the person logging in."""
    return {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "ola.google@example.com",
        "email_verified": True,
        "given_name": "Ola",
        "family_name": "Google",
        "name": "Ola Google",
        **overrides,
    }


@override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE_CONFIGURED)
class GoogleLoginTests(TestCase):
    """Runs the real allauth login + callback views; only the calls to Google are faked."""

    def google_login(self, profile):
        # 1. User clicks "Continue with Google" -> we redirect them to Google.
        response = self.client.post("/accounts/google/login/")
        self.assertEqual(response.status_code, 302)
        google_url = urlparse(response["Location"])
        self.assertEqual(google_url.netloc, "accounts.google.com")
        params = parse_qs(google_url.query)
        self.assertEqual(params["client_id"], ["test-client-id"])
        self.assertEqual(
            params["redirect_uri"], ["http://testserver/accounts/google/login/callback/"]
        )
        # 2. Google sends them back with a code; we exchange it for their profile.
        with mock.patch(
            "allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token",
            return_value={"access_token": "token", "id_token": "jwt", "expires_in": 3600},
        ), mock.patch(
            "allauth.socialaccount.providers.google.views._verify_and_decode",
            return_value=profile,
        ):
            return self.client.get(
                "/accounts/google/login/callback/",
                {"code": "auth-code", "state": params["state"][0]},
            )

    def test_button_shown_when_configured(self):
        self.assertContains(self.client.get(reverse("login")), "Continue with Google")

    @override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE_NOT_CONFIGURED)
    def test_button_hidden_without_credentials(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Continue with Google")

    def test_new_google_user_gets_an_account(self):
        response = self.google_login(google_profile())
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        user = User.objects.get(email="ola.google@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Ola", "Google"))
        self.assertTrue(user.username)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertContains(self.client.get(reverse("home")), "Hi, Ola!")

    def test_returning_google_user_reuses_account(self):
        self.google_login(google_profile())
        self.client.logout()
        self.google_login(google_profile())
        self.assertEqual(User.objects.count(), 1)

    def test_existing_user_is_linked_by_verified_email(self):
        existing = User.objects.create_user("jan", "jan@example.com", PASSWORD)
        self.google_login(google_profile(sub="999", email="jan@example.com"))
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(int(self.client.session["_auth_user_id"]), existing.pk)
        self.assertTrue(existing.socialaccount_set.filter(provider="google").exists())

    def test_unverified_google_email_does_not_take_over_account(self):
        existing = User.objects.create_user("jan", "jan@example.com", PASSWORD)
        self.google_login(google_profile(sub="999", email="jan@example.com", email_verified=False))
        self.assertNotEqual(self.client.session.get("_auth_user_id"), str(existing.pk))
        self.assertFalse(existing.socialaccount_set.exists())

    def test_cancelled_login_page_renders(self):
        response = self.client.get("/accounts/google/login/callback/", {"error": "access_denied"})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "SheetBase", status_code=response.status_code)


# --------------------------------------------------------------------------- Dex login
DEX_CONFIGURED = {
    "openid_connect": {
        "APPS": [
            {
                "provider_id": "dex",
                "name": "Dex",
                "client_id": "sheetbase",
                "secret": "test-secret",
                "settings": {"server_url": "http://dex.test/dex"},
            }
        ]
    }
}
DEX_DISCOVERY = {
    "issuer": "http://dex.test/dex",
    "authorization_endpoint": "http://dex.test/dex/auth",
    "token_endpoint": "http://dex.test/dex/token",
    "userinfo_endpoint": "http://dex.test/dex/userinfo",
    "jwks_uri": "http://dex.test/dex/keys",
}


@override_settings(SOCIALACCOUNT_PROVIDERS=DEX_CONFIGURED)
class DexLoginTests(TestCase):
    def test_button_shown_when_configured(self):
        self.assertContains(self.client.get(reverse("login")), "Continue with Dex")

    def test_redirects_to_dex(self):
        fake = mock.Mock(status_code=200)
        fake.json.return_value = DEX_DISCOVERY
        with mock.patch("allauth.socialaccount.providers.openid_connect.views.get_adapter") as ga:
            ga.return_value.get_requests_session.return_value.__enter__.return_value.get.return_value = fake
            response = self.client.post("/accounts/oidc/dex/login/")
        self.assertEqual(response.status_code, 302)
        url = urlparse(response["Location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "http://dex.test/dex/auth")
        params = parse_qs(url.query)
        self.assertEqual(params["client_id"], ["sheetbase"])
        self.assertEqual(
            params["redirect_uri"], ["http://testserver/accounts/oidc/dex/login/callback/"]
        )
