from django.db import models
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

    def test_form_rejects_future_date(self):
        from datetime import timedelta

        from django.utils import timezone

        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.client.post(reverse("transactions"), {
            "amount": "1.00", "payer": boss.pk, "recipient": self.shop.pk,
            "description": "x", "date": tomorrow.strftime("%d/%m/%y")})
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
             "description": "Papier", "date": today().isoformat()},
        )
        self.assertEqual(response.status_code, 302)
        t = Transaction.objects.get()
        self.assertEqual((t.amount, t.payer, t.created_by), (Decimal("12.34"), payer, admin_user))
        self.assertContains(self.client.get(reverse("admin:sheetapp_transaction_changelist")), "Papier")


# --------------------------------------------------------------------------- Pages
def today():
    from django.utils import timezone

    return timezone.localdate()


class AddTransactionTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")  # an ordinary member
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("transactions")

    def post(self, **fields):
        data = {"amount": "12.49", "payer": self.jan.pk, "recipient": self.shop.pk,
                "description": "Kawa", "date": today().strftime("%d/%m/%y")}
        return self.client.post(self.url, {**data, **fields})

    def test_logged_out_goes_to_login(self):
        self.assertRedirects(self.client.get(self.url), f"{reverse('login')}?next={self.url}",
                             fetch_redirect_response=False)

    def test_defaults_are_me_and_today(self):
        self.client.force_login(self.jan)
        form = self.client.get(self.url).context["form"]
        self.assertEqual(form["payer"].value(), self.jan.pk)
        self.assertEqual(form["date"].value(), today())

    def test_member_adds_and_sees_it_in_the_list(self):
        self.client.force_login(self.jan)
        # Between members: a confirmation screen first, saved on Confirm.
        screen = self.post(payer=self.ola.pk, recipient=self.jan.pk, amount="2137")
        self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")
        self.assertFalse(Transaction.objects.exists())
        response = self.post(payer=self.ola.pk, recipient=self.jan.pk, amount="2137", confirm="",
                             plan=screen.context["plan"].signature())
        self.assertRedirects(response, self.url)
        t = Transaction.objects.get()
        self.assertEqual((t.payer, t.recipient, t.amount, t.created_by),
                         (self.ola, self.jan, Decimal("2137"), self.jan))
        self.assertContains(self.client.get(self.url), "Kawa")

    def test_adding_keeps_filters(self):
        self.client.force_login(self.jan)
        response = self.client.post(f"{self.url}?payer={self.jan.pk}", {
            "amount": "1.00", "payer": self.jan.pk, "recipient": self.shop.pk,
            "description": "x", "date": today().strftime("%d/%m/%y")})
        self.assertRedirects(response, f"{self.url}?payer={self.jan.pk}")

    def test_date_is_dd_mm_yy(self):
        self.client.force_login(self.jan)
        self.assertContains(self.client.get(self.url), f'value="{today():%d/%m/%y}"')  # default shown as dd/mm/yy
        self.post(date=f"{today():%d/%m/%Y}")  # a 4-digit year works too
        self.assertEqual(Transaction.objects.get().date, today())
        response = self.post(date=today().isoformat())  # yyyy-mm-dd is rejected
        self.assertTrue(response.context["form"].errors["date"])

    def test_comma_amount_accepted(self):
        self.client.force_login(self.jan)
        self.post(amount="12,49")
        self.assertEqual(Transaction.objects.get().amount, Decimal("12.49"))

    def test_bad_amount_keeps_input_and_shows_error(self):
        self.client.force_login(self.jan)
        for amount in ("0", "-1", "1.005", "abc"):
            response = self.post(amount=amount, description="Zostaje")
            self.assertEqual(response.status_code, 200, amount)
            self.assertTrue(response.context["form"].errors["amount"], amount)
            self.assertContains(response, "Zostaje")
        self.assertFalse(Transaction.objects.exists())


class TransactionListTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("transactions")
        self.client.force_login(self.jan)

    def make(self, amount, payer, recipient, when=None):
        return Transaction.objects.create(amount=Decimal(amount), payer=payer, recipient=recipient,
                                          created_by=payer, description=f"t{amount}", date=when or today())

    def test_total_equals_sum_of_rows(self):
        self.make("0.10", self.jan, self.shop)
        self.make("0.20", self.ola, self.shop)
        self.make("1234.50", self.ola, self.jan)
        response = self.client.get(self.url)
        rows = list(response.context["transactions"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(response.context["total"], sum(t.amount for t in rows))
        self.assertEqual(response.context["total"], Decimal("1234.80"))
        self.assertContains(response, "1\xa0234,80\xa0zł")  # shown as 1 234,80 zł

    def test_dates_shown_as_dd_mm_yy(self):
        from datetime import date

        self.make("1.00", self.jan, self.shop, date(today().year, 1, 5))
        self.assertContains(self.client.get(self.url), date(today().year, 1, 5).strftime("05/01/%y"))

    def test_shows_date_paid_and_date_added(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        t = self.make("1.00", self.jan, self.shop, date(today().year, 1, 5))
        # created_at is set automatically; pin it to check the added date is shown in Warsaw time.
        Transaction.objects.filter(pk=t.pk).update(
            created_at=datetime(today().year, 2, 3, 23, 30, tzinfo=ZoneInfo("UTC")))
        response = self.client.get(self.url)
        self.assertContains(response, "Date paid")
        self.assertContains(response, date(today().year, 1, 5).strftime("05/01/%y"))
        self.assertContains(response, date(today().year, 2, 4).strftime("04/02/%y"))  # 23:30 UTC = 00:30 in Warsaw

    def test_newest_first(self):
        from datetime import timedelta

        self.make("1.00", self.jan, self.shop, today() - timedelta(days=3))
        self.make("2.00", self.jan, self.shop)
        dates = [t.date for t in self.client.get(self.url).context["transactions"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_filters_change_list_and_total(self):
        from datetime import date

        self.make("1.00", self.jan, self.shop)
        self.make("2.00", self.ola, self.shop)
        self.make("4.00", self.ola, self.jan)
        last_year = date(today().year - 1, 6, 15)
        self.make("8.00", self.jan, self.shop, last_year)

        def total(**params):
            return self.client.get(self.url, params).context["total"]

        self.assertEqual(total(), Decimal("15.00"))
        self.assertEqual(total(payer=self.ola.pk), Decimal("6.00"))
        self.assertEqual(total(recipient=self.shop.pk), Decimal("11.00"))
        self.assertEqual(total(payer=self.ola.pk, recipient=self.jan.pk), Decimal("4.00"))
        self.assertEqual(total(year=last_year.year), Decimal("8.00"))
        self.assertEqual(total(year=last_year.year, month=6), Decimal("8.00"))
        self.assertEqual(total(year=last_year.year, month=7), 0)

    def test_empty_list_says_so(self):
        self.assertContains(self.client.get(self.url), "No transactions yet")

    def test_zl_format(self):
        from .templatetags.money import zl

        for value, text in (("0", "0,00"), ("12.5", "12,50"), ("1234.5", "1\xa0234,50"),
                            ("1234567.89", "1\xa0234\xa0567,89")):
            self.assertEqual(zl(Decimal(value)), f"{text}\xa0zł")


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
                                      "recipient": self.t.recipient.pk, "description": "a", "date": today().isoformat()})
        self.assertEqual(Transaction.objects.get().amount, Decimal("1.00"))


class ShopUserTests(TestCase):
    def test_shop_user_exists_and_cannot_log_in(self):
        shop = User.objects.get(username=SHOP_USERNAME)
        self.assertTrue(shop.is_shop)
        self.assertFalse(shop.has_usable_password())

    def test_add_shops_on_add_page(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        for name in ("Biedronka", "Żabka"):
            self.client.post(reverse("transactions"), {"add_shop": "", "name": name})
        shops = User.objects.filter(is_shop=True).exclude(username=SHOP_USERNAME)
        self.assertEqual(sorted(s.first_name for s in shops), ["Biedronka", "Żabka"])
        self.assertFalse(any(s.has_usable_password() for s in shops))
        self.assertFalse(Transaction.objects.exists())  # the transaction form wasn't touched

    def test_duplicate_shop_rejected(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        self.client.post(reverse("transactions"), {"add_shop": "", "name": "Biedronka"})
        response = self.client.post(reverse("transactions"), {"add_shop": "", "name": " biedronka "})
        self.assertContains(response, "This shop already exists.")
        self.assertEqual(User.objects.filter(first_name__iexact="biedronka").count(), 1)

    def test_paid_to_is_required(self):
        payer = User.objects.create_user("ola", "ola@example.com")
        t = Transaction(amount=Decimal("1.00"), payer=payer, created_by=payer, description="x")
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_form_rejects_done_without_paid_to(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("transactions"), {
            "amount": "1.00", "payer": boss.pk,
            "description": "x", "date": today().strftime("%d/%m/%y")})
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

    def test_form_shows_error(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("transactions"), {
            "amount": "1.00", "payer": boss.pk, "recipient": boss.pk,
            "description": "x", "date": today().strftime("%d/%m/%y")})
        self.assertContains(response, "must be different people")
        self.assertFalse(Transaction.objects.exists())

    def test_admin_shows_error(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("admin:sheetapp_transaction_add"), {
            "amount": "1.00", "payer": boss.pk, "recipient": boss.pk,
            "description": "x", "date": today().isoformat()})
        self.assertContains(response, "must be different people")
        self.assertFalse(Transaction.objects.exists())


# --------------------------------------------------------------------------- Corrections
from .models import TransactionCorrection


class CorrectionTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")  # added the transaction
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.t = Transaction.objects.create(amount=Decimal("124.90"), payer=self.jan, recipient=self.shop,
                                            created_by=self.jan, description="Kawa", date=today())
        self.list_url = reverse("transactions")
        self.correct_url = reverse("correct_transaction", args=[self.t.pk])
        self.cancel_url = reverse("cancel_transaction", args=[self.t.pk])
        self.client.force_login(self.jan)

    def correct(self, **fields):
        data = {"amount": "124.90", "payer": self.jan.pk, "recipient": self.shop.pk, "description": "Kawa",
                "date": today().strftime("%d/%m/%y"), "reason": "typo"}
        return self.client.post(self.correct_url, {**data, **fields})

    def total(self, **params):
        return self.client.get(self.list_url, params).context["total"]

    def row(self):
        return Transaction.objects.prefetch_related("corrections").get()

    def test_logged_out_goes_to_login(self):
        self.client.logout()
        for url in (self.correct_url, self.cancel_url):
            self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}", fetch_redirect_response=False)

    def test_correction_changes_row_and_total_but_not_original(self):
        self.assertRedirects(self.correct(amount="12,49"), self.list_url)
        self.assertEqual(self.total(), Decimal("12.49"))
        self.assertEqual(self.row().current.amount, Decimal("12.49"))
        self.assertEqual(Transaction.objects.get().amount, Decimal("124.90"))  # original untouched
        self.assertContains(self.client.get(self.list_url), "corrected ×1")

    def test_latest_correction_wins(self):
        self.correct(amount="12.49")
        self.correct(amount="13.00")
        self.assertEqual(self.total(), Decimal("13.00"))

    def test_any_field_can_change_and_filters_follow(self):
        from datetime import date

        last_year = date(today().year - 1, 6, 15)
        other_shop = User.objects.create(username="shop-zabka", email="zabka@sheetbase.invalid", is_shop=True)
        self.correct(payer=self.ola.pk, recipient=other_shop.pk, description="Herbata",
                     date=last_year.strftime("%d/%m/%y"))
        c = self.row().current
        self.assertEqual((c.payer, c.recipient, c.description, c.date), (self.ola, other_shop, "Herbata", last_year))
        self.assertEqual(self.total(payer=self.jan.pk), 0)
        self.assertEqual(self.total(payer=self.ola.pk), Decimal("124.90"))
        self.assertEqual(self.total(recipient=self.shop.pk), 0)
        self.assertEqual(self.total(year=today().year), 0)  # moved to the corrected year
        self.assertEqual(self.total(year=last_year.year), Decimal("124.90"))

    def test_form_prefilled_with_current_values(self):
        self.correct(amount="12.49")
        form = self.client.get(self.correct_url).context["form"]
        self.assertEqual(form["amount"].value(), Decimal("12.49"))
        self.assertEqual(form["payer"].value(), self.jan.pk)

    def test_reason_required(self):
        response = self.correct(amount="12.49", reason="")
        self.assertTrue(response.context["form"].errors["reason"])
        self.assertFalse(TransactionCorrection.objects.exists())

    def test_nothing_changed_rejected(self):
        self.assertContains(self.correct(), "Nothing was changed")
        self.assertFalse(TransactionCorrection.objects.exists())

    def test_same_validation_as_add_form(self):
        from datetime import timedelta

        too_old = today().replace(year=today().year - 3)
        for fields in ({"amount": "0"}, {"amount": "1.005"}, {"recipient": self.jan.pk},
                       {"date": too_old.strftime("%d/%m/%y")},
                       {"date": (today() + timedelta(days=1)).strftime("%d/%m/%y")}):
            self.assertEqual(self.correct(**fields).status_code, 200, fields)
        self.assertFalse(TransactionCorrection.objects.exists())

    def test_only_author_can_correct_or_cancel(self):
        self.client.force_login(self.ola)
        self.assertEqual(self.client.get(self.correct_url).status_code, 403)
        self.assertEqual(self.correct(amount="1.00").status_code, 403)
        self.assertEqual(self.client.post(self.cancel_url, {"reason": "x"}).status_code, 403)
        self.assertNotContains(self.client.get(self.list_url), self.correct_url)
        self.assertFalse(TransactionCorrection.objects.exists())
        self.client.force_login(self.jan)
        self.assertContains(self.client.get(self.list_url), self.correct_url)

    def test_cancel(self):
        self.assertEqual(self.client.post(self.cancel_url, {"reason": ""}).status_code, 200)  # reason required
        self.assertFalse(TransactionCorrection.objects.exists())

        self.assertRedirects(self.client.post(self.cancel_url, {"reason": "never happened"}), self.list_url)
        self.assertEqual(self.total(), 0)
        t = self.row()
        self.assertTrue(t.is_cancelled)
        self.assertEqual((t.current.amount, t.current.description), (Decimal("124.90"), "Kawa"))  # values kept
        page = self.client.get(self.list_url)
        self.assertContains(page, "Cancelled by jan")
        self.assertNotContains(page, self.correct_url)  # no more Correct / Cancel

    def test_hide_cancelled_filter(self):
        kept = Transaction.objects.create(amount=Decimal("5.00"), payer=self.jan, recipient=self.shop,
                                          created_by=self.jan, description="Herbata", date=today())
        self.client.post(self.cancel_url, {"reason": "never happened"})

        shown = lambda **p: [t.pk for t in self.client.get(self.list_url, p).context["transactions"]]
        self.assertCountEqual(shown(), [self.t.pk, kept.pk])  # cancelled ones show by default
        self.assertEqual(shown(hide_cancelled="on"), [kept.pk])
        self.assertContains(self.client.get(self.list_url), 'name="hide_cancelled"')

    def test_cancelled_cannot_be_corrected_or_uncancelled(self):
        self.client.post(self.cancel_url, {"reason": "never happened"})
        self.assertEqual(self.correct(amount="1.00").status_code, 403)
        self.assertEqual(self.client.post(self.cancel_url, {"reason": "again"}).status_code, 403)
        c = TransactionCorrection(original=self.t, amount=Decimal("1.00"), payer=self.jan, recipient=self.shop,
                                  description="x", date=today(), reason="undo", created_by=self.jan)
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_corrections_are_final(self):
        self.correct(amount="12.49")
        c = TransactionCorrection.objects.get()
        c.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            c.save()
        with self.assertRaises(ValueError):
            c.delete()
        self.assertEqual(TransactionCorrection.objects.get().amount, Decimal("12.49"))

    def test_history_newest_first_with_original_last(self):
        self.correct(amount="12.49", reason="first fix")
        self.correct(description="Kawa i mleko", amount="12.49", reason="second fix")
        history = self.row().history()
        self.assertEqual([e["correction"].reason for e in history], ["second fix", "first fix"])
        self.assertEqual(history[1]["changes"], [("amount", Decimal("124.90"), Decimal("12.49"))])

        page = self.client.get(self.list_url).content.decode()
        self.assertLess(page.index("second fix"), page.index("first fix"))
        self.assertLess(page.index("first fix"), page.index("Original"))
        self.assertIn("124,90\xa0zł → 12,49\xa0zł", page)
        self.assertIn("Corrected by jan", page)
        self.assertNotIn("difference", page)

    def test_admin_shows_and_adds_corrections(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        response = self.client.post(reverse("admin:sheetapp_transactioncorrection_add"), {
            "original": self.t.pk, "amount": "12.49", "payer": self.jan.pk, "recipient": self.shop.pk,
            "description": "Kawa", "date": today().isoformat(), "reason": "fixed by admin"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TransactionCorrection.objects.get().created_by, boss)
        change_url = reverse("admin:sheetapp_transaction_change", args=[self.t.pk])
        self.assertContains(self.client.get(change_url), "fixed by admin")
        # "original" is a search box: typing part of the description finds the transaction.
        found = self.client.get(reverse("admin:autocomplete"), {
            "term": "Kaw", "app_label": "sheetapp", "model_name": "transactioncorrection", "field_name": "original"})
        self.assertEqual([r["id"] for r in found.json()["results"]], [str(self.t.pk)])
        c = TransactionCorrection.objects.get()
        delete_url = reverse("admin:sheetapp_transactioncorrection_delete", args=[c.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)


# --------------------------------------------------------------------------- Yearly summary
class YearlySummaryTests(TestCase):
    def setUp(self):
        from datetime import date

        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com")  # does nothing all year
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("summary")
        self.client.force_login(self.jan)
        self.this_year = today().year
        self.d = lambda year, month=6: date(year, month, 15)

    def make(self, amount, payer, recipient, when):
        return Transaction.objects.create(amount=Decimal(amount), payer=payer, recipient=recipient,
                                          created_by=self.jan, description="x", date=when)

    def summaries(self):
        return {s.year: s for s in self.client.get(self.url).context["summaries"]}

    def row(self, summary, user):
        return next(r for r in summary.rows if r.member == user)

    def test_logged_out_goes_to_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), f"{reverse('login')}?next={self.url}",
                             fetch_redirect_response=False)

    def test_this_year_and_last_year(self):
        self.make("10.00", self.jan, self.shop, self.d(self.this_year))
        self.make("20.00", self.jan, self.shop, self.d(self.this_year - 1))
        self.make("40.00", self.jan, self.shop, self.d(self.this_year - 2))  # too old – in neither table
        s = self.summaries()
        self.assertEqual(list(s), [self.this_year, self.this_year - 1])
        self.assertEqual(s[self.this_year].purchases, Decimal("10.00"))
        self.assertEqual(s[self.this_year - 1].purchases, Decimal("20.00"))
        self.assertContains(self.client.get(self.url), "Yearly summary")

    def test_columns_per_member_and_totals(self):
        y = self.d(self.this_year)
        self.make("100.00", self.jan, self.shop, y)  # purchase
        self.make("50.00", self.ola, self.shop, y)  # purchase
        self.make("5.50", self.shop, self.jan, y)  # return
        self.make("30.00", self.ola, self.jan, y)  # ola pays jan back
        s = self.summaries()[self.this_year]
        jan, ola, ewa = self.row(s, self.jan), self.row(s, self.ola), self.row(s, self.ewa)
        self.assertEqual((jan.purchases, jan.returns, jan.paid, jan.received),
                         (Decimal("100.00"), Decimal("5.50"), 0, Decimal("30.00")))
        self.assertEqual((ola.purchases, ola.returns, ola.paid, ola.received),
                         (Decimal("50.00"), 0, Decimal("30.00"), 0))
        self.assertEqual((ewa.purchases, ewa.paid), (0, 0))  # listed even with nothing
        self.assertNotIn(self.shop, [r.member for r in s.rows])  # shops aren't members
        self.assertEqual((s.purchases, s.returns, s.paid, s.received),
                         (Decimal("150.00"), Decimal("5.50"), Decimal("30.00"), Decimal("30.00")))
        self.assertEqual(s.total, Decimal("185.50"))

    def test_shops_table(self):
        lidl = User.objects.create(username="shop-lidl", email="lidl@sheetbase.invalid", first_name="Lidl",
                                   is_shop=True)
        unused = User.objects.create(username="shop-x", email="x@sheetbase.invalid", first_name="Unused",
                                     is_shop=True)
        y = self.d(self.this_year)
        self.make("100.00", self.jan, self.shop, y)
        self.make("50.00", self.ola, lidl, y)
        self.make("20.00", self.jan, lidl, y)
        self.make("5.50", lidl, self.jan, y)  # return
        s = self.summaries()[self.this_year]
        shops = {r.shop: (r.sold, r.returned) for r in s.shop_rows}
        self.assertEqual(shops, {self.shop: (Decimal("100.00"), 0), lidl: (Decimal("70.00"), Decimal("5.50"))})
        self.assertNotIn(unused, shops)  # only shops used that year
        # Shop totals agree with the members' purchases and returns.
        self.assertEqual(sum(r.sold for r in s.shop_rows), s.purchases)
        self.assertEqual(sum(r.returned for r in s.shop_rows), s.returns)
        page = self.client.get(self.url)
        self.assertContains(page, "Bought from it")
        self.assertContains(page, f"No purchases from shops in {self.this_year - 1}")

    def test_total_matches_history_for_the_year(self):
        for year in (self.this_year, self.this_year - 1):
            self.make("100.00", self.jan, self.shop, self.d(year))
            self.make("12.34", self.ola, self.jan, self.d(year, 1))
            self.make("0.66", self.shop, self.ola, self.d(year, 3))
        for year, s in self.summaries().items():
            history = self.client.get(reverse("transactions"), {"year": year}).context["total"]
            self.assertEqual(s.total, history, year)

    def test_uses_corrected_values_and_skips_cancelled(self):
        from .models import TransactionCorrection

        fixed = self.make("124.90", self.jan, self.shop, self.d(self.this_year))
        TransactionCorrection.objects.create(original=fixed, amount=Decimal("12.49"), payer=self.ola,
                                             recipient=self.shop, description="x", date=self.d(self.this_year - 1),
                                             reason="typo", created_by=self.jan)
        gone = self.make("99.00", self.jan, self.shop, self.d(self.this_year))
        TransactionCorrection.objects.create(original=gone, amount=gone.amount, payer=self.jan, recipient=self.shop,
                                             description="x", date=gone.date, reason="never", created_by=self.jan,
                                             cancelled=True)
        s = self.summaries()
        self.assertEqual(s[self.this_year].total, 0)  # moved to last year, the other one cancelled
        self.assertEqual(self.row(s[self.this_year - 1], self.ola).purchases, Decimal("12.49"))
        self.assertEqual(self.row(s[self.this_year - 1], self.jan).purchases, 0)

    def test_all_time_per_member_from_their_first_transaction(self):
        from datetime import date
        from unittest import mock

        from .models import TransactionCorrection

        self.make("1200.00", self.jan, self.shop, date(2030, 1, 20))
        self.make("100.00", self.jan, self.shop, date(2029, 12, 5))  # jan's first: December 2029
        self.make("30.00", self.ola, self.shop, date(2030, 9, 15))  # ola's first: September 2030
        self.make("5.00", self.jan, self.ola, date(2030, 10, 1))  # a later repayment doesn't move it
        gone = self.make("999.00", self.ola, self.shop, date(2029, 1, 1))  # cancelled: doesn't count at all
        TransactionCorrection.objects.create(original=gone, amount=gone.amount, payer=self.ola, recipient=self.shop,
                                             description="x", date=gone.date, reason="never", created_by=self.jan,
                                             cancelled=True)
        self.make("40.00", self.ewa, self.ola, date(2030, 11, 9))  # the latest transaction in the app
        with mock.patch("django.utils.timezone.localdate", return_value=date(2030, 11, 9)):
            response = self.client.get(self.url)
        s = response.context["all_time"]
        jan, ola, ewa = self.row(s, self.jan), self.row(s, self.ola), self.row(s, self.ewa)
        # Months = complete months from their own first transaction to 09/11/30.
        self.assertEqual((jan.first_date, jan.months, jan.purchases), (date(2029, 12, 5), 11, Decimal("1300.00")))
        self.assertEqual((ola.first_date, ola.months, ola.purchases), (date(2030, 9, 15), 1, Decimal("30.00")))
        self.assertEqual((ewa.first_date, ewa.months), (date(2030, 11, 9), 0))
        self.assertEqual((jan.paid, ola.received), (Decimal("5.00"), Decimal("45.00")))  # 5 from jan, 40 from ewa
        # Net out of pocket: purchases − returns + paid − received.
        self.assertEqual((jan.total_spent, ola.total_spent, ewa.total_spent),
                         (Decimal("1305.00"), Decimal("-15.00"), Decimal("40.00")))
        self.assertEqual(s.total_spent, Decimal("1330.00"))  # = purchases − returns overall
        # Per month = ROUND(ABS(total spent) / months; 2), "–" before a complete month.
        self.assertEqual(jan.per_month, Decimal("118.64"))  # 1305 ÷ 11
        self.assertEqual(ola.per_month, Decimal("15.00"))  # |−15| ÷ 1
        self.assertIsNone(ewa.per_month)
        html = response.content.decode()
        headers = html[html.index("<thead>"):html.index("</thead>")]  # the all-time table comes first
        self.assertLess(headers.index("Received from members"), headers.index(">Per month<"))
        self.assertLess(headers.index(">Per month<"), headers.index(">Total spent<"))  # total spent is last
        self.assertTrue(headers.rstrip().endswith("Total spent</th></tr>"))
        self.assertContains(response, "118,64\xa0zł")
        self.assertEqual(s.total, Decimal("1375.00"))
        self.assertEqual(s.total, self.client.get(reverse("transactions")).context["total"])

    def test_complete_months_like_excel_datedif(self):
        from datetime import date

        from .summary import complete_months

        for start, end, months in ((date(2024, 10, 7), date(2024, 11, 6), 0), (date(2024, 10, 7), date(2024, 11, 7), 1),
                                   (date(2024, 1, 31), date(2024, 2, 29), 0), (date(2024, 10, 7), date(2026, 11, 9), 25),
                                   (date(2024, 10, 7), date(2024, 10, 7), 0)):
            self.assertEqual(complete_months(start, end), months, (start, end))

    def test_member_without_transactions(self):
        response = self.client.get(self.url)
        ewa = self.row(response.context["all_time"], self.ewa)
        self.assertEqual((ewa.first_date, ewa.per_month), (None, None))
        self.assertContains(response, "no transactions yet")

    def test_year_boundary(self):
        from datetime import date
        from unittest import mock

        self.make("1.00", self.jan, self.shop, date(2030, 12, 31))
        self.make("2.00", self.jan, self.shop, date(2031, 1, 1))
        with mock.patch("django.utils.timezone.localdate", return_value=date(2030, 12, 31)):
            s = self.summaries()
            self.assertEqual((s[2030].total, s[2029].total), (Decimal("1.00"), 0))
        with mock.patch("django.utils.timezone.localdate", return_value=date(2031, 1, 1)):
            s = self.summaries()  # 2030 doesn't disappear on 1 January – it's now last year
            self.assertEqual((s[2031].total, s[2030].total), (Decimal("2.00"), Decimal("1.00")))


# --------------------------------------------------------------------------- Planned transactions
from django.db import IntegrityError, transaction as db_transaction
from django.db.models import ProtectedError
from django.utils import timezone

from .models import PlannedTransaction


class PlannedTransactionTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com", first_name="Jan")
        self.ola = User.objects.create_user("ola", "ola@example.com", first_name="Ola")
        self.shop = User.objects.get(username=SHOP_USERNAME)

    def build(self, amount="307.09", debtor=None, creditor=None, **fields):
        return PlannedTransaction(amount=Decimal(amount), debtor=debtor or self.ola, creditor=creditor or self.jan,
                                  created_by=self.jan, description="Fotel", **fields)

    def make(self, *args, **kwargs):
        p = self.build(*args, **kwargs)
        p.save()
        return p

    def payment(self, amount="307.09"):
        return Transaction.objects.create(amount=Decimal(amount), payer=self.ola, recipient=self.jan,
                                          created_by=self.ola, description="Zwrot")

    def settle(self, p, payment):
        p.settled_by, p.settled_at = payment, timezone.now()
        p.save()

    def test_amount_is_stored_exactly(self):
        p = self.make("1842.47")
        self.assertEqual(PlannedTransaction.objects.get(pk=p.pk).amount, Decimal("1842.47"))

    def test_invalid_values_rejected(self):
        for p, why in [(self.build(creditor=self.ola), "owes themselves"),
                       (self.build("0.00"), "zero"), (self.build("-5.00"), "negative"),
                       (self.build("1.005"), "3 decimal places"),
                       (self.build(debtor=self.shop), "shop owes"), (self.build(creditor=self.shop), "shop owed")]:
            with self.assertRaises(ValidationError, msg=why):
                p.full_clean()

    def test_database_rejects_owing_yourself_and_non_positive_amounts(self):
        for p in (self.build(creditor=self.ola), self.build("0.00"), self.build("-1.00")):
            with self.subTest(p=p), self.assertRaises(IntegrityError), db_transaction.atomic():
                p.save()

    def test_open_and_settled(self):
        open_debt, paid_debt = self.make(), self.make()
        self.settle(paid_debt, self.payment())
        self.assertEqual(list(PlannedTransaction.objects.open()), [open_debt])
        self.assertEqual(list(PlannedTransaction.objects.settled()), [paid_debt])
        self.assertTrue(paid_debt.is_settled)
        self.assertFalse(open_debt.is_settled)

    def test_settled_needs_payment_and_time_together(self):
        p = self.build(settled_by=self.payment())  # no settled_at
        with self.assertRaises(IntegrityError):
            p.save()

    def test_settled_debt_is_final(self):
        p = self.make()
        self.settle(p, self.payment())
        p.amount = Decimal("1.00")
        with self.assertRaises(ValueError):
            p.save()
        with self.assertRaises(ValueError):
            p.delete()

    def test_open_debt_can_be_edited_and_deleted(self):
        p = self.make()
        p.amount = Decimal("1.00")
        p.save()
        p.delete()
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_user_with_debts_cannot_be_deleted(self):
        self.make()
        for user in (self.ola, self.jan):
            with self.assertRaises(ProtectedError):
                user.delete()

    def test_payment_that_settled_a_debt_is_linked(self):
        pay = self.payment()
        p = self.make()
        self.settle(p, pay)
        leftover = self.make("10.00", created_by_payment=pay)
        self.assertEqual(list(pay.settled_debts.all()), [p])
        self.assertEqual(list(pay.leftovers.all()), [leftover])

    def test_not_shown_among_done_transactions(self):
        self.make()
        self.client.force_login(self.jan)
        self.assertNotContains(self.client.get(reverse("transactions")), "Fotel")


class PlannedTransactionAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.client.force_login(self.boss)

    def add(self, **overrides):
        data = {"debtor": self.ola.pk, "creditor": self.boss.pk, "amount": "307.09",
                "description": "Fotel", **overrides}
        return self.client.post(reverse("admin:sheetapp_plannedtransaction_add"), data)

    def test_add_and_list(self):
        self.assertEqual(self.add().status_code, 302)
        p = PlannedTransaction.objects.get()
        self.assertEqual((p.debtor, p.creditor, p.amount, p.created_by), (self.ola, self.boss, Decimal("307.09"), self.boss))
        response = self.client.get(reverse("admin:sheetapp_plannedtransaction_changelist"))
        self.assertContains(response, "Fotel")
        self.assertContains(response, "Open")

    def test_invalid_values_show_a_message(self):
        for overrides, message in [({"creditor": self.ola.pk}, "must be different people"),
                                   ({"amount": "0"}, "greater than or equal to 0.01"),
                                   ({"amount": "-1"}, "greater than or equal to 0.01"),
                                   ({"amount": "1.005"}, "no more than 2 decimal places")]:
            with self.subTest(overrides=overrides):
                self.assertContains(self.add(**overrides), message)
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_shops_not_offered(self):
        response = self.client.get(reverse("admin:sheetapp_plannedtransaction_add"))
        shop = User.objects.get(username=SHOP_USERNAME)
        self.assertNotContains(response, f'<option value="{shop.pk}"')
        self.assertContains(self.add(debtor=shop.pk), "Select a valid choice")

    def test_status_filter_and_settled_is_read_only(self):
        open_debt = PlannedTransaction.objects.create(debtor=self.ola, creditor=self.boss, amount=Decimal("1.00"),
                                                      description="Kawa", created_by=self.boss)
        pay = Transaction.objects.create(amount=Decimal("2.00"), payer=self.ola, recipient=self.boss,
                                         created_by=self.boss, description="Zwrot")
        paid = PlannedTransaction.objects.create(debtor=self.ola, creditor=self.boss, amount=Decimal("2.00"),
                                                 description="Herbata", created_by=self.boss,
                                                 settled_by=pay, settled_at=timezone.now())
        changelist = reverse("admin:sheetapp_plannedtransaction_changelist")
        r = self.client.get(changelist, {"status": "open"})
        self.assertContains(r, "Kawa")
        self.assertNotContains(r, "Herbata")
        r = self.client.get(changelist, {"status": "settled"})
        self.assertContains(r, "Herbata")
        self.assertNotContains(r, "Kawa")

        # Settled: view only, no delete. Open: editable and deletable.
        self.assertNotContains(self.client.get(reverse("admin:sheetapp_plannedtransaction_change", args=[paid.pk])),
                               'name="_save"')
        self.assertEqual(self.client.get(reverse("admin:sheetapp_plannedtransaction_delete", args=[paid.pk])).status_code, 403)
        self.assertContains(self.client.get(reverse("admin:sheetapp_plannedtransaction_change", args=[open_debt.pk])),
                            'name="_save"')

        # "Delete selected" refuses when a settled one is selected, and deletes nothing.
        r = self.client.post(changelist, {"action": "delete_selected", "_selected_action": [open_debt.pk, paid.pk], "post": "yes"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(PlannedTransaction.objects.count(), 2)
        self.client.post(changelist, {"action": "delete_selected", "_selected_action": [open_debt.pk], "post": "yes"})
        self.assertEqual(list(PlannedTransaction.objects.all()), [paid])



# --------------------------------------------------------------------------- Paying debts
from .payments import plan_payment


class PayingDebtsTests(TestCase):
    """A payment between members settles the payer's debts to the person paid, oldest first."""

    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("transactions")
        self.client.force_login(self.ola)

    def debt(self, amount, debtor=None, creditor=None, description="Fotel", tag=""):
        return PlannedTransaction.objects.create(debtor=debtor or self.ola, creditor=creditor or self.jan,
                                                 amount=Decimal(amount), description=description, tag=tag,
                                                 created_by=self.jan)

    def data(self, amount, payer=None, recipient=None, **extra):
        return {"amount": amount, "payer": (payer or self.ola).pk, "recipient": (recipient or self.jan).pk,
                "description": "Zwrot", "date": today().strftime("%d/%m/%y"), **extra}

    def pay(self, amount, **kwargs):
        """Shows the confirmation screen, then clicks Confirm. Returns the saved payment."""
        screen = self.client.post(self.url, self.data(amount, **kwargs))
        self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")
        self.assertRedirects(self.client.post(self.url, self.data(
            amount, confirm="", plan=screen.context["plan"].signature(), **kwargs)), self.url)
        return Transaction.objects.latest("created_at")

    def leftover(self, payment):
        return payment.leftovers.get()

    def refresh(self, *rows):
        return [PlannedTransaction.objects.get(pk=r.pk) for r in rows]

    # --- the screen
    def test_screen_shows_what_is_covered_and_saves_nothing(self):
        self.debt("90.00", description="Krzesło")
        page = self.client.post(self.url, self.data("100,00"))
        self.assertContains(page, "This payment of 100,00\xa0zł covers:")
        self.assertContains(page, "Krzesło")
        self.assertContains(page, "Leftover: <strong>jan owes ola 10,00\xa0zł")
        self.assertFalse(Transaction.objects.exists())
        self.assertEqual(PlannedTransaction.objects.open().count(), 1)

    def test_back_returns_to_filled_form(self):
        page = self.client.post(self.url, self.data("12,34", back=""))
        self.assertTemplateUsed(page, "sheetapp/transactions.html")
        self.assertContains(page, 'value="12,34"')
        self.assertFalse(Transaction.objects.exists())

    def test_purchases_and_returns_skip_the_screen(self):
        self.debt("90.00")
        self.assertRedirects(self.client.post(self.url, self.data("5.00", recipient=self.shop)), self.url)
        self.assertRedirects(self.client.post(self.url, self.data("5.00", payer=self.shop, recipient=self.ola)),
                             self.url)
        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(PlannedTransaction.objects.open().count(), 1)  # untouched, no leftovers

    def test_debts_changed_meanwhile_shows_screen_again(self):
        self.debt("90.00")
        screen = self.client.post(self.url, self.data("90.00"))
        PlannedTransaction.objects.get().delete()  # the debt shown on the screen is deleted before Confirm
        page = self.client.post(self.url, self.data("90.00", confirm="", plan=screen.context["plan"].signature()))
        self.assertTemplateUsed(page, "sheetapp/confirm_payment.html")
        self.assertContains(page, "changed in the meantime")
        self.assertFalse(Transaction.objects.exists())

    # --- matching
    def test_exact(self):
        d = self.debt("90.00")
        pay = self.pay("90.00")
        [d] = self.refresh(d)
        self.assertEqual(d.settled_by, pay)
        self.assertIsNotNone(d.settled_at)
        self.assertFalse(pay.leftovers.exists())

    def test_overpay_creates_debt_back(self):
        d = self.debt("90.00")
        pay = self.pay("100.00")
        self.assertTrue(self.refresh(d)[0].is_settled)
        left = self.leftover(pay)
        self.assertEqual((left.debtor, left.creditor, left.amount, left.is_settled),
                         (self.jan, self.ola, Decimal("10.00"), False))

    def test_underpay_leaves_the_rest(self):
        d = self.debt("110.00", description="Krzesło", tag="krzesła")
        PlannedTransaction.objects.filter(pk=d.pk).make_group()
        pay = self.pay("100.00")
        self.assertTrue(self.refresh(d)[0].is_settled)
        left = self.leftover(pay)
        self.assertEqual((left.debtor, left.creditor, left.amount, left.description, left.tag, left.group),
                         (self.ola, self.jan, Decimal("10.00"), "Krzesło – rest", "krzesła", None))

    def test_several_debts_oldest_first(self):
        old, middle, new = self.debt("30.00"), self.debt("50.00"), self.debt("40.00")
        pay = self.pay("60.00")
        old, middle, new = self.refresh(old, middle, new)
        self.assertEqual([old.is_settled, middle.is_settled, new.is_settled], [True, True, False])
        self.assertEqual(self.leftover(pay).amount, Decimal("20.00"))  # 30 + 50 – 60, still owed by ola

    def test_nothing_owed_is_a_loan(self):
        pay = self.pay("50.00")
        left = self.leftover(pay)
        self.assertEqual((left.debtor, left.creditor, left.amount, left.description),
                         (self.jan, self.ola, Decimal("50.00"), "Zwrot"))

    def test_other_peoples_rows_untouched(self):
        mine = self.debt("30.00", tag="fotel")
        ewas = self.debt("30.00", debtor=self.ewa, tag="fotel")
        reverse_debt = self.debt("30.00", debtor=self.jan, creditor=self.ola)
        to_someone_else = self.debt("30.00", creditor=self.ewa)
        self.pay("30.00")
        mine, ewas, reverse_debt, to_someone_else = self.refresh(mine, ewas, reverse_debt, to_someone_else)
        self.assertTrue(mine.is_settled)
        self.assertFalse(any(r.is_settled for r in (ewas, reverse_debt, to_someone_else)))

    def test_plan_without_saving(self):
        d = self.debt("30.00")
        plan = plan_payment(self.ola, self.jan, Decimal("30.00"), "x")
        self.assertEqual((plan.covered, plan.leftover), ([d], None))
        self.assertFalse(PlannedTransaction.objects.settled().exists())

    # --- cancelling and correcting the payment
    def cancel(self, payment):
        return self.client.post(reverse("cancel_transaction", args=[payment.pk]), {"reason": "mistake"})

    def test_cancel_reopens_debts_and_removes_leftover(self):
        a, b = self.debt("30.00"), self.debt("50.00")
        pay = self.pay("100.00")
        self.assertRedirects(self.cancel(pay), self.url)
        self.assertEqual(set(PlannedTransaction.objects.open()), {a, b})
        self.assertFalse(PlannedTransaction.objects.settled().exists())
        self.assertFalse(pay.leftovers.exists())

    def test_cancel_refused_when_leftover_already_paid(self):
        first = self.pay("50.00")  # jan now owes ola 50
        self.client.force_login(self.jan)
        self.pay("50.00", payer=self.jan, recipient=self.ola)  # ...and pays it
        self.client.force_login(self.ola)
        self.assertContains(self.cancel(first), "Cancel that later payment first")
        self.assertFalse(first.corrections.exists())
        self.assertTrue(self.leftover(first).is_settled)

    def test_money_of_a_payment_with_debts_cant_be_corrected(self):
        self.debt("90.00")
        pay = self.pay("90.00")
        url = reverse("correct_transaction", args=[pay.pk])
        fix = {**self.data("90.00"), "reason": "typo"}
        for change in ({"amount": "80.00"}, {"recipient": self.ewa.pk}, {"payer": self.ewa.pk}):
            self.assertContains(self.client.post(url, {**fix, **change}), "Cancel it and add it again")
        self.assertFalse(pay.corrections.exists())
        self.assertRedirects(self.client.post(url, {**fix, "description": "Zwrot za fotel"}), self.url)

    def test_purchase_cant_be_corrected_into_a_payment_between_members(self):
        d = self.debt("90.00")
        self.client.post(self.url, self.data("90.00", recipient=self.shop))
        purchase = Transaction.objects.get()
        url = reverse("correct_transaction", args=[purchase.pk])
        page = self.client.post(url, {**self.data("90.00"), "reason": "it was for jan"})
        self.assertContains(page, "Cancel it and add it again")
        self.assertFalse(purchase.corrections.exists())
        self.assertFalse(self.refresh(d)[0].is_settled)

    def test_cancel_race_shows_message(self):
        from unittest import mock

        from django.core.exceptions import ValidationError as DjangoValidationError

        pay = self.pay("50.00")
        with mock.patch("sheetapp.payments.check_can_undo", side_effect=[None, DjangoValidationError("paid meanwhile")]):
            page = self.cancel(pay)
        self.assertContains(page, "paid meanwhile")
        self.assertFalse(pay.corrections.exists())
        self.assertTrue(pay.leftovers.exists())

    def test_adding_in_admin_also_pays_debts(self):
        d = self.debt("90.00")
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        self.client.post(reverse("admin:sheetapp_transaction_add"), {
            "amount": "100.00", "payer": self.ola.pk, "recipient": self.jan.pk,
            "description": "Zwrot", "date": today().isoformat()})
        pay = Transaction.objects.get()
        self.assertEqual(self.refresh(d)[0].settled_by, pay)
        self.assertEqual(self.leftover(pay).amount, Decimal("10.00"))

    def test_cancelling_in_admin_also_reopens(self):
        d = self.debt("90.00")
        pay = self.pay("90.00")
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        self.client.post(reverse("admin:sheetapp_transactioncorrection_add"), {
            "original": pay.pk, "amount": "90.00", "payer": self.ola.pk, "recipient": self.jan.pk,
            "description": "Zwrot", "date": today().isoformat(), "cancelled": "on", "reason": "mistake"})
        self.assertTrue(pay.corrections.get().cancelled)
        self.assertFalse(self.refresh(d)[0].is_settled)


class PlannedGroupTests(TestCase):
    """Rows of one shared expense share a generated group id: deleting one deletes them all."""

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com")
        self.client.force_login(self.boss)
        self.changelist = reverse("admin:sheetapp_plannedtransaction_changelist")

    def debt(self, debtor, tag="pizza", **fields):
        return PlannedTransaction.objects.create(debtor=debtor, creditor=self.boss, amount=Decimal("1.00"),
                                                 description="Pizza", tag=tag, created_by=self.boss, **fields)

    def group(self, *rows):
        PlannedTransaction.objects.filter(pk__in=[r.pk for r in rows]).make_group()

    def delete_url(self, p):
        return reverse("admin:sheetapp_plannedtransaction_delete", args=[p.pk])

    def action(self, name, *rows):
        return self.client.post(self.changelist, {"action": name, "_selected_action": [r.pk for r in rows]},
                                follow=True)

    def test_deleting_one_deletes_the_group(self):
        ola, ewa = self.debt(self.ola), self.debt(self.ewa)
        last_week_ola, last_week_ewa = self.debt(self.ola), self.debt(self.ewa)  # same tag, another group
        alone = self.debt(self.ola)
        self.group(ola, ewa)
        self.group(last_week_ola, last_week_ewa)
        self.assertContains(self.client.get(self.delete_url(ola)), "ewa owes boss")  # everyone affected is listed
        self.client.post(self.delete_url(ola), {"post": "yes"})
        self.assertEqual(set(PlannedTransaction.objects.all()), {last_week_ola, last_week_ewa, alone})

    def test_ungrouped_row_deleted_alone(self):
        alone, kept = self.debt(self.ola), self.debt(self.ewa)  # same tag, but tags don't group
        self.client.post(self.delete_url(alone), {"post": "yes"})
        self.assertEqual(list(PlannedTransaction.objects.all()), [kept])

    def test_group_with_a_paid_row_cant_be_deleted(self):
        pay = Transaction.objects.create(amount=Decimal("1.00"), payer=self.ewa, recipient=self.boss,
                                         created_by=self.boss, description="Zwrot")
        ola = self.debt(self.ola)
        ewa = self.debt(self.ewa)
        self.group(ola, ewa)
        PlannedTransaction.objects.filter(pk=ewa.pk).update(settled_by=pay, settled_at=timezone.now())
        self.assertContains(self.client.get(self.delete_url(ola)), "already paid")
        self.client.post(self.delete_url(ola), {"post": "yes"})
        self.client.post(self.changelist, {"action": "delete_selected", "_selected_action": [ola.pk], "post": "yes"})
        self.assertEqual(PlannedTransaction.objects.count(), 2)
        with self.assertRaises(ValidationError):
            PlannedTransaction.objects.filter(pk=ola.pk).delete_with_groups()

    def test_group_and_ungroup_actions(self):
        ola, ewa, other = self.debt(self.ola), self.debt(self.ewa), self.debt(self.ola)
        self.assertContains(self.action("group_selected", ola), "at least two")
        self.assertContains(self.action("group_selected", ola, ewa), "Grouped 2")
        ola, ewa, other = (PlannedTransaction.objects.get(pk=r.pk) for r in (ola, ewa, other))
        self.assertIsNotNone(ola.group)
        self.assertEqual(ola.group, ewa.group)
        self.assertIsNone(other.group)
        self.action("ungroup_selected", ewa)
        self.assertIsNone(PlannedTransaction.objects.get(pk=ewa.pk).group)

    def test_settled_rows_cant_be_grouped(self):
        pay = Transaction.objects.create(amount=Decimal("1.00"), payer=self.ewa, recipient=self.boss,
                                         created_by=self.boss, description="Zwrot")
        ola = self.debt(self.ola)
        paid = self.debt(self.ewa, settled_by=pay, settled_at=timezone.now())
        self.assertContains(self.action("group_selected", ola, paid), "can&#x27;t be grouped")
        self.assertIsNone(PlannedTransaction.objects.get(pk=ola.pk).group)


# --------------------------------------------------------------------------- Planned page
class PlannedPageTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.url = reverse("planned")
        self.client.force_login(self.jan)

    def debt(self, amount="10.00", debtor=None, creditor=None, description="Pizza", **fields):
        return PlannedTransaction.objects.create(debtor=debtor or self.ola, creditor=creditor or self.jan,
                                                 amount=Decimal(amount), description=description,
                                                 created_by=self.jan, **fields)

    def add(self, **fields):
        data = {"debtor": self.ola.pk, "creditor": self.jan.pk, "amount": "307,09", "description": "Fotel"}
        return self.client.post(self.url, {**data, **fields})

    def edit_url(self, d):
        return reverse("edit_planned", args=[d.pk])

    def delete_url(self, d):
        return reverse("delete_planned", args=[d.pk])

    def test_logged_out_goes_to_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), f"{reverse('login')}?next={self.url}",
                             fetch_redirect_response=False)

    def test_in_menu(self):
        self.assertContains(self.client.get(reverse("home")), f'href="{self.url}"')

    def test_switch_between_add_forms(self):
        self.assertContains(self.client.get(self.url), "Add a done transaction")
        self.assertContains(self.client.get(reverse("transactions")), "Add a planned transaction")

    def test_add(self):
        self.assertRedirects(self.add(), self.url)
        d = PlannedTransaction.objects.get()
        self.assertEqual((d.debtor, d.creditor, d.amount, d.created_by), (self.ola, self.jan, Decimal("307.09"), self.jan))
        self.assertContains(self.client.get(self.url), "Fotel")

    def test_form_defaults_to_me_and_hides_shops(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context["form"]["creditor"].value(), self.jan.pk)
        self.assertNotContains(page, f'<option value="{self.shop.pk}"')

    def test_bad_input_shows_message(self):
        for fields, message in [({"creditor": self.ola.pk}, "must be different people"),
                                ({"amount": "0"}, "greater than or equal to 0.01"),
                                ({"amount": "-1"}, "greater than or equal to 0.01"),
                                ({"amount": "1,005"}, "no more than 2 decimal places"),
                                ({"debtor": self.shop.pk}, "Select a valid choice")]:
            with self.subTest(fields=fields):
                self.assertContains(self.add(**fields), message)
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_lists_open_ones_with_total(self):
        self.debt("10.00", description="Kawa")
        self.debt("2.50", description="Herbata", debtor=self.ewa, creditor=self.ola)
        pay = Transaction.objects.create(amount=Decimal("5.00"), payer=self.ola, recipient=self.jan,
                                         created_by=self.jan, description="Zwrot")
        self.debt("5.00", description="Ciastko", settled_by=pay, settled_at=timezone.now())
        page = self.client.get(self.url)
        self.assertContains(page, "Kawa")
        self.assertContains(page, "Herbata")
        self.assertNotContains(page, "Ciastko")  # settled ones are hidden
        self.assertEqual(page.context["total"], Decimal("12.50"))

    def test_creditor_edits(self):
        d = self.debt()
        self.assertContains(self.client.get(self.url), self.edit_url(d))
        response = self.client.post(self.edit_url(d), {"debtor": self.ewa.pk, "creditor": self.jan.pk,
                                                       "amount": "12,50", "description": "Pizza duża"})
        self.assertRedirects(response, self.url)
        d.refresh_from_db()
        self.assertEqual((d.debtor, d.amount, d.description), (self.ewa, Decimal("12.50"), "Pizza duża"))

    def test_edit_validates(self):
        d = self.debt()
        response = self.client.post(self.edit_url(d), {"debtor": self.jan.pk, "creditor": self.jan.pk,
                                                       "amount": "1", "description": "x"})
        self.assertContains(response, "must be different people")

    def test_creditor_deletes_after_warning(self):
        d = self.debt()
        page = self.client.get(self.delete_url(d))
        self.assertContains(page, "Only delete a debt that was entered by mistake")
        self.assertTrue(PlannedTransaction.objects.exists())  # nothing deleted until confirmed
        self.assertRedirects(self.client.post(self.delete_url(d)), self.url)
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_only_creditor_can_edit_or_delete(self):
        d = self.debt()  # ola owes jan
        for who in (self.ola, self.ewa):  # the debtor and a bystander
            self.client.force_login(who)
            self.assertNotContains(self.client.get(self.url), self.edit_url(d))
            self.assertEqual(self.client.get(self.edit_url(d)).status_code, 403)
            self.assertEqual(self.client.post(self.edit_url(d), {"debtor": self.ola.pk, "creditor": self.jan.pk,
                                                                 "amount": "1", "description": "x"}).status_code, 403)
            self.assertEqual(self.client.post(self.delete_url(d)).status_code, 403)
        d.refresh_from_db()
        self.assertEqual(d.amount, Decimal("10.00"))

    def test_settled_cant_be_edited_or_deleted(self):
        pay = Transaction.objects.create(amount=Decimal("10.00"), payer=self.ola, recipient=self.jan,
                                         created_by=self.jan, description="Zwrot")
        d = self.debt(settled_by=pay, settled_at=timezone.now())
        self.assertEqual(self.client.get(self.edit_url(d)).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url(d)).status_code, 403)

    def test_delete_takes_the_group(self):
        a, b = self.debt(), self.debt(debtor=self.ewa)
        alone = self.debt(description="Kawa")
        PlannedTransaction.objects.filter(pk__in=[a.pk, b.pk]).make_group()
        self.assertContains(self.client.get(self.delete_url(a)), "all of these are deleted")
        self.client.post(self.delete_url(a))
        self.assertEqual(list(PlannedTransaction.objects.all()), [alone])

    def test_group_delete_refused_when_paid_or_owed_to_someone_else(self):
        pay = Transaction.objects.create(amount=Decimal("10.00"), payer=self.ewa, recipient=self.jan,
                                         created_by=self.jan, description="Zwrot")
        a = self.debt()
        paid = self.debt(debtor=self.ewa, settled_by=pay, settled_at=timezone.now())
        PlannedTransaction.objects.filter(pk__in=[a.pk, paid.pk]).make_group()
        self.assertContains(self.client.post(self.delete_url(a)), "ewa already paid")

        b, not_mine = self.debt(), self.debt(debtor=self.jan, creditor=self.ewa)
        PlannedTransaction.objects.filter(pk__in=[b.pk, not_mine.pk]).make_group()
        self.assertContains(self.client.post(self.delete_url(b)), "is owed to ewa, not you")
        self.assertEqual(PlannedTransaction.objects.count(), 4)
