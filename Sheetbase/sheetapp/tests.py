from django.test import TestCase
from django.urls import reverse

from django.db import models

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
        self.assertEqual(User.objects.exclude(username=SHOP_USERNAME).count(), 1)

    def test_existing_user_is_linked_by_verified_email(self):
        existing = User.objects.create_user("jan", "jan@example.com", PASSWORD)
        self.google_login(google_profile(sub="999", email="jan@example.com"))
        self.assertEqual(User.objects.exclude(username=SHOP_USERNAME).count(), 1)
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


# --------------------------------------------------------------------------- Transactions
from decimal import Decimal

from django.core.exceptions import ValidationError

from .models import SHOP_USERNAME, Transaction


class TransactionModelTests(TestCase):
    def setUp(self):
        self.ola = User.objects.create_user("ola", "ola@example.com", first_name="Ola")
        self.shop = User.objects.get(username=SHOP_USERNAME)

    def make(self, amount, **fields):
        return Transaction.objects.create(
            amount=amount, payer=self.ola, recipient=self.shop, created_by=self.ola, description="Kawa", **fields
        )

    def test_amount_is_stored_exactly(self):
        for amount in ("12.34", "0.10", "0.01", "99999999.99"):
            pk = self.make(Decimal(amount)).pk
            saved = Transaction.objects.get(pk=pk).amount
            self.assertIsInstance(saved, Decimal)
            self.assertEqual(saved, Decimal(amount))
            self.assertEqual(str(saved), amount)

    def test_sums_are_exact(self):
        # 0.1 + 0.2 != 0.3 with floats; with Decimal it must be exact.
        self.make(Decimal("0.10"))
        self.make(Decimal("0.20"))
        total = Transaction.objects.aggregate(total=models.Sum("amount"))["total"]
        self.assertEqual(total, Decimal("0.30"))

    def test_amount_must_be_positive(self):
        for amount in ("0.00", "-5.00"):
            t = Transaction(amount=Decimal(amount), payer=self.ola, recipient=self.shop, created_by=self.ola, description="x")
            with self.assertRaises(ValidationError, msg=amount):
                t.full_clean()

    def test_amount_more_than_two_decimal_places_rejected(self):
        t = Transaction(amount=Decimal("1.005"), payer=self.ola, recipient=self.shop, created_by=self.ola, description="x")
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_date_defaults_to_today(self):
        from django.utils import timezone

        self.assertEqual(self.make(Decimal("1.00")).date, timezone.localdate())

    def test_date_range(self):
        from datetime import date
        from unittest import mock

        from .models import validate_transaction_date

        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 11, 5)):
            for ok in (date(2026, 11, 5), date(2024, 11, 5)):
                validate_transaction_date(ok)  # no error
            for bad in (date(2026, 11, 6), date(2024, 11, 4)):
                with self.assertRaises(ValidationError, msg=bad):
                    validate_transaction_date(bad)
        with mock.patch("django.utils.timezone.localdate", return_value=date(2028, 2, 29)):
            validate_transaction_date(date(2026, 2, 28))  # leap day: two years back is 28 Feb
            with self.assertRaises(ValidationError):
                validate_transaction_date(date(2026, 2, 27))

    def test_dev_sheet_rejects_future_date(self):
        from datetime import timedelta

        from django.utils import timezone

        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.client.post(reverse("dev_sheet"), {
            "amount": "1.00", "payer": boss.pk, "recipient": self.shop.pk,
            "description": "x", "date": tomorrow.isoformat()})
        self.assertContains(response, "can&#x27;t be in the future")
        self.assertFalse(Transaction.objects.exists())

    def test_payer_with_transactions_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        self.make(Decimal("1.00"))
        with self.assertRaises(ProtectedError):
            self.ola.delete()


class TransactionAdminTests(TestCase):
    def test_admin_add_records_who_created_it(self):
        admin_user = User.objects.create_superuser("boss", "boss@example.com", "x")
        payer = User.objects.create_user("ola", "ola@example.com")
        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("admin:sheetapp_transaction_add"),
            {"amount": "12.34", "payer": payer.pk, "recipient": User.objects.get(username=SHOP_USERNAME).pk,
             "description": "Papier", "date": "2026-11-04"},
        )
        self.assertEqual(response.status_code, 302)
        t = Transaction.objects.get()
        self.assertEqual((t.amount, t.payer, t.created_by), (Decimal("12.34"), payer, admin_user))
        self.assertContains(self.client.get(reverse("admin:sheetapp_transaction_changelist")), "Papier")


# --------------------------------------------------------------------------- Temporary dev sheet
class DevSheetTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("dev_sheet")

    def test_staff_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)  # anonymous -> login
        self.client.force_login(self.jan)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_add_and_see_it(self):
        self.client.force_login(self.boss)
        self.client.post(self.url, {"amount": "4606.16", "payer": self.boss.pk,
                                    "recipient": self.shop.pk, "description": "3 fotele", "date": "2026-11-05"})
        self.assertEqual(Transaction.objects.get().created_by, self.boss)
        response = self.client.get(self.url)
        self.assertContains(response, "3 fotele")
        self.assertEqual(response.context["done_total"], Decimal("4606.16"))



class DoneTransactionIsFinalTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.t = Transaction.objects.create(amount=Decimal("1.00"), payer=self.boss,
                                            recipient=User.objects.get(username=SHOP_USERNAME),
                                            created_by=self.boss, description="a")

    def test_cannot_be_edited(self):
        self.t.amount = Decimal("2.00")
        with self.assertRaises(ValueError):
            self.t.save()
        self.assertEqual(Transaction.objects.get().amount, Decimal("1.00"))

    def test_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.t.delete()
        self.assertTrue(Transaction.objects.exists())

    def test_admin_shows_no_edit_or_delete(self):
        self.client.force_login(self.boss)
        delete_url = reverse("admin:sheetapp_transaction_delete", args=[self.t.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        change_url = reverse("admin:sheetapp_transaction_change", args=[self.t.pk])
        self.client.post(change_url, {"amount": "2.00", "payer": self.boss.pk,
                                      "recipient": self.t.recipient.pk, "description": "a", "date": "2026-11-05"})
        self.assertEqual(Transaction.objects.get().amount, Decimal("1.00"))


class ShopUserTests(TestCase):
    def test_shop_user_exists_and_cannot_log_in(self):
        shop = User.objects.get(username=SHOP_USERNAME)
        self.assertTrue(shop.is_shop)
        self.assertFalse(shop.has_usable_password())

    def test_add_shops_on_dev_sheet(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        for name in ("Biedronka", "Żabka"):
            self.client.post(reverse("dev_sheet"), {"add_shop": "", "name": name})
        shops = User.objects.filter(is_shop=True).exclude(username=SHOP_USERNAME)
        self.assertEqual(sorted(s.first_name for s in shops), ["Biedronka", "Żabka"])
        self.assertFalse(any(s.has_usable_password() for s in shops))
        self.assertFalse(Transaction.objects.exists())  # the transaction form wasn't touched

    def test_duplicate_shop_rejected(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        self.client.post(reverse("dev_sheet"), {"add_shop": "", "name": "Biedronka"})
        response = self.client.post(reverse("dev_sheet"), {"add_shop": "", "name": " biedronka "})
        self.assertContains(response, "This shop already exists.")
        self.assertEqual(User.objects.filter(first_name__iexact="biedronka").count(), 1)

    def test_paid_to_is_required(self):
        payer = User.objects.create_user("ola", "ola@example.com")
        t = Transaction(amount=Decimal("1.00"), payer=payer, created_by=payer, description="x")
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_dev_sheet_rejects_done_without_paid_to(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("dev_sheet"), {
            "amount": "1.00", "payer": boss.pk,
            "description": "x", "date": "2026-11-05"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Transaction.objects.exists())

    def test_shop_can_pay_for_a_return(self):
        # A return is its own transaction: the shop pays the money back to the buyer.
        jan = User.objects.create_user("jan", "jan@example.com")
        shop = User.objects.get(username=SHOP_USERNAME)
        Transaction.objects.create(amount=Decimal("49.99"), payer=jan, recipient=shop,
                                   created_by=jan, description="Czajnik")
        refund = Transaction(amount=Decimal("49.99"), payer=shop, recipient=jan,
                             created_by=jan, description="Zwrot: czajnik")
        refund.full_clean()  # no validation error
        refund.save()
        self.assertEqual(Transaction.objects.count(), 2)


class PayerIsNotRecipientTests(TestCase):
    def setUp(self):
        self.ola = User.objects.create_user("ola", "ola@example.com")

    def build(self):
        return Transaction(amount=Decimal("1.00"), payer=self.ola, recipient=self.ola,
                           created_by=self.ola, description="x")

    def test_rejected_by_validation(self):
        with self.assertRaises(ValidationError):
            self.build().full_clean()

    def test_rejected_by_database(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            self.build().save()

    def test_dev_sheet_shows_error(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("dev_sheet"), {
            "amount": "1.00", "payer": boss.pk, "recipient": boss.pk,
            "description": "x", "date": "2026-11-05"})
        self.assertContains(response, "must be different people")
        self.assertFalse(Transaction.objects.exists())

    def test_admin_shows_error(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("admin:sheetapp_transaction_add"), {
            "amount": "1.00", "payer": boss.pk, "recipient": boss.pk,
            "description": "x", "date": "2026-11-05"})
        self.assertContains(response, "must be different people")
        self.assertFalse(Transaction.objects.exists())
