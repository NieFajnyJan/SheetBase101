import importlib
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction as db_transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from audit.models import Entry

from .models import (
    SHOP_USERNAME,
    AlreadyAdded,
    PlannedTransaction,
    Transaction,
    TransactionCorrection,
    User,
    earliest_allowed_date,
    save_once,
    validate_transaction_date,
)
from .payments import plan_payment, record_payment
from .splits import divide, plan_split
from .summary import complete_months
from .templatetags.money import zl


PASSWORD = "Sup3r-secret-pw!"
NBSP = "\u00a0"  # the zł filter never lets an amount wrap


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
        self.assertEqual(self.make(Decimal("1.00")).date, timezone.localdate())

    def test_date_range(self):
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
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(boss)
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.client.post(reverse("transactions"), {
            "amount": "1.00", "payer": boss.pk, "recipient": self.shop.pk,
            "description": "x", "date": tomorrow.strftime("%d/%m/%y")})
        self.assertContains(response, "can&#x27;t be in the future")
        self.assertFalse(Transaction.objects.exists())

    def test_payer_with_transactions_cannot_be_deleted(self):
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
    return timezone.localdate()


def edit(debt, **fields):
    """Someone else changes a debt, as the Planned page's edit does."""
    debt = PlannedTransaction.objects.get(pk=debt.pk)
    for name, value in fields.items():
        setattr(debt, name, value)
    debt.save()


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

    def test_date_is_dd_mm_yyyy(self):
        self.client.force_login(self.jan)
        page = self.client.get(self.url)
        self.assertContains(page, f'value="{today():%d.%m.%Y}"')  # default shown as dd.mm.yyyy
        self.assertContains(page, "dd.mm.yyyy, e.g. 20.11.2026")  # the format, next to the box for good
        # Typed: dots or slashes, a four- or two-digit year.
        for typed in (f"{today():%d.%m.%Y}", f"{today():%d/%m/%Y}", f"{today():%d.%m.%y}", f"{today():%d/%m/%y}",
                      f"{today().day}.{today().month}.{today().year}"):  # no leading zeros
            response = self.post(date=typed)
            self.assertEqual(response.status_code, 302, typed)
        self.assertEqual(set(Transaction.objects.values_list("date", flat=True)), {today()})
        # Refused: yyyy-mm-dd, mixed separators, and a day that doesn't exist (not moved to the next month).
        for typed in (today().isoformat(), f"{today():%d.%m/%Y}", f"31.02.{today().year}"):
            response = self.post(date=typed)
            self.assertTrue(response.context["form"].errors["date"], typed)
            self.assertContains(response, f'value="{typed}"')  # what was typed is kept, to fix
        self.assertEqual(Transaction.objects.count(), 5)

    def test_calendar_gets_the_servers_limits(self):
        self.client.force_login(self.jan)
        page = self.client.get(self.url)
        self.assertContains(page, f'data-min="{earliest_allowed_date(today()).isoformat()}"')
        self.assertContains(page, f'data-max="{today().isoformat()}"')

    def test_grouped_amount_accepted(self):
        self.client.force_login(self.jan)
        for typed in ("1 234,50", "1\xa0234,50", "1 234.50"):  # space, non-breaking, thin
            self.post(amount=typed)
        self.assertEqual(list(Transaction.objects.values_list("amount", flat=True)), [Decimal("1234.50")] * 3)

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

    def test_dates_shown_as_dd_mm_yyyy(self):
        paid = date(today().year, 1, 5)
        self.make("1.00", self.jan, self.shop, paid)
        # Readable by people and, without guessing the order, by software.
        self.assertContains(self.client.get(self.url), f'<time datetime="{paid.isoformat()}">05.01.{paid.year}</time>',
                            html=True)

    def test_shows_date_paid_and_date_added(self):
        t = self.make("1.00", self.jan, self.shop, date(today().year, 1, 5))
        # created_at is set automatically; pin it to check the added date is shown in Warsaw time.
        Transaction.objects.filter(pk=t.pk).update(
            created_at=datetime(today().year, 2, 3, 23, 30, tzinfo=ZoneInfo("UTC")))
        response = self.client.get(self.url)
        self.assertContains(response, "Date paid")
        self.assertContains(response, f"05.01.{today().year}")
        self.assertContains(response, f"04.02.{today().year}")  # 23:30 UTC = 00:30 in Warsaw

    def test_newest_first(self):
        self.make("1.00", self.jan, self.shop, today() - timedelta(days=3))
        self.make("2.00", self.jan, self.shop)
        dates = [t.date for t in self.client.get(self.url).context["transactions"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_filters_change_list_and_total(self):
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


class BetweenShopsTests(TestCase):
    """A done transaction has a member on at least one side: between two shops it's neither a purchase
    nor a return, and no summary could count it."""

    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.other_shop = User.objects.create(username="shop-ikea", email="shop-ikea@sheetbase.invalid",
                                              first_name="Ikea", is_shop=True)
        self.client.force_login(self.jan)

    def old_one(self, amount="11.00"):
        """One recorded before this was refused (saved without the checks)."""
        return Transaction.objects.create(amount=Decimal(amount), payer=self.shop, recipient=self.other_shop,
                                          description="Przelew", date=today(), created_by=self.jan)

    def test_refused_when_adding(self):
        page = self.client.post(reverse("transactions"), {
            "amount": "11.00", "payer": self.shop.pk, "recipient": self.other_shop.pk, "description": "Przelew",
            "date": today().strftime("%d/%m/%y")})
        self.assertContains(page, "One side has to be a member")
        self.assertFalse(Transaction.objects.exists())

    def test_refused_in_the_admin_too(self):
        t = Transaction(amount=Decimal("1.00"), payer=self.shop, recipient=self.other_shop, description="x",
                        created_by=self.jan)
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_refused_when_correcting(self):
        t = Transaction.objects.create(amount=Decimal("11.00"), payer=self.jan, recipient=self.shop,
                                       description="Kawa", date=today(), created_by=self.jan)
        page = self.client.post(reverse("correct_transaction", args=[t.pk]), {
            "amount": "11.00", "payer": self.shop.pk, "recipient": self.other_shop.pk, "description": "Kawa",
            "date": today().strftime("%d/%m/%y"), "reason": "x"})
        self.assertContains(page, "One side has to be a member")
        self.assertFalse(TransactionCorrection.objects.exists())

    def test_an_old_one_can_be_corrected_into_a_purchase_or_cancelled(self):
        t = self.old_one()
        url = reverse("correct_transaction", args=[t.pk])
        fix = {"amount": "11.00", "recipient": self.other_shop.pk, "description": "Przelew",
               "date": today().strftime("%d/%m/%y"), "reason": "it was me who paid"}
        self.assertContains(self.client.post(url, {**fix, "payer": self.shop.pk, "description": "Inny"}),
                            "One side has to be a member")  # still between shops: not even a description fix
        self.assertRedirects(self.client.post(url, {**fix, "payer": self.jan.pk}), reverse("transactions"))
        other = self.old_one()
        self.assertRedirects(self.client.post(reverse("cancel_transaction", args=[other.pk]), {"reason": "mistake"}),
                             reverse("transactions"))
        self.assertTrue(Transaction.objects.get(pk=other.pk).is_cancelled)

    def test_an_old_one_is_shown_in_the_summary_and_marked_in_the_history(self):
        self.old_one("11.00")
        summary = self.client.get(reverse("summary"))
        self.assertEqual(summary.context["all_time"].between_shops, Decimal("11.00"))
        self.assertEqual(summary.context["summaries"][0].between_shops, Decimal("11.00"))
        self.assertContains(summary, "was paid between two shops", count=2)  # all time and this year
        self.assertContains(self.client.get(reverse("transactions")), "Between two shops:", count=1)

    def test_nothing_shown_without_one(self):
        Transaction.objects.create(amount=Decimal("11.00"), payer=self.jan, recipient=self.shop,
                                   description="Kawa", date=today(), created_by=self.jan)
        self.assertNotContains(self.client.get(reverse("summary")), "between two shops")
        self.assertNotContains(self.client.get(reverse("transactions")), "Between two shops")

    def test_the_form_says_which_combinations_work(self):
        page = self.client.get(reverse("transactions"))
        self.assertContains(page, "A member – or the shop, when it pays money back (a return).")
        self.assertContains(page, "The shop, for a purchase – or the member paid.")


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
        self.assertEqual([c.version for c in self.row().corrections.all()], [2, 1])

    def correct_meanwhile(self, data):
        """Someone else saves a correction of the same version after this form was checked."""
        TransactionCorrection.objects.create(original=self.t, amount=Decimal("1.00"), payer=self.jan,
                                             recipient=self.shop, description="Kawa", date=today(),
                                             reason="theirs", created_by=self.ola)

    def test_correction_sent_at_the_same_moment_as_another_is_refused(self):
        # check_loan runs last in clean(): after the check that the page is up to date, before saving.
        with mock.patch("sheetapp.forms.CorrectionForm.check_loan", autospec=True,
                        side_effect=lambda form, data: self.correct_meanwhile(data)):
            page = self.correct(amount="12.49")
        self.assertContains(page, "corrected by someone else at the same moment")
        self.assertEqual(self.row().current.amount, Decimal("1.00"))  # theirs stays, ours isn't saved
        self.assertEqual(self.row().corrections.count(), 1)

    def test_corrections_of_the_same_version_cant_both_be_saved(self):
        first, second = (TransactionCorrection(original=self.t, version=1, amount=Decimal(a), payer=self.jan,
                                               recipient=self.shop, description="Kawa", date=today(),
                                               reason="typo", created_by=self.jan) for a in ("1.00", "2.00"))
        first.save()
        with self.assertRaisesMessage(ValidationError, "at the same moment"):
            second.save()
        self.assertEqual(self.row().current.amount, Decimal("1.00"))

    def test_any_field_can_change_and_filters_follow(self):
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

    def shown(self, url):
        """What a page opened now remembers having shown."""
        return self.client.get(url).context["form"]["shown"].value()

    def test_correction_from_an_old_page_keeps_the_newer_values(self):
        old = self.shown(self.correct_url)  # a second tab, opened at 124,90 zł
        self.assertRedirects(self.correct(amount="12,49", shown=self.shown(self.correct_url)), self.list_url)
        # The old tab only fixes the description, but still holds the old amount.
        page = self.correct(description="Kawa i ciastko", shown=old)
        self.assertContains(page, "Changed after you opened this page: amount (now 12,49\xa0zł). Nothing was saved")
        self.assertEqual(TransactionCorrection.objects.count(), 1)
        form = page.context["form"]
        self.assertEqual(Decimal(form["amount"].value()), Decimal("12.49"))  # the newer amount, not the old one
        self.assertEqual(form["description"].value(), "Kawa i ciastko")  # what was typed is kept
        # Sent again as it's now shown, it saves – without undoing the first correction.
        again = {f: form[f].value() for f in ("amount", "payer", "recipient", "description", "reason", "shown")}
        self.assertRedirects(self.client.post(self.correct_url, {**again, "date": today().strftime("%d/%m/%y")}),
                             self.list_url)
        current = self.row().current
        self.assertEqual((current.amount, current.description), (Decimal("12.49"), "Kawa i ciastko"))

    def test_correction_from_an_old_page_flags_a_field_changed_on_both(self):
        old = self.shown(self.correct_url)
        self.correct(amount="12,49", shown=self.shown(self.correct_url))
        page = self.correct(amount="99,00", shown=old)
        self.assertContains(page, "Changed meanwhile to 12,49\xa0zł. Save again to replace it")
        self.assertEqual(page.context["form"]["amount"].value(), "99,00")  # what was typed is kept
        self.assertEqual(TransactionCorrection.objects.count(), 1)

    def test_cancel_from_an_old_page(self):
        old = self.shown(self.cancel_url)
        self.correct(amount="12,49", shown=self.shown(self.correct_url))
        page = self.client.post(self.cancel_url, {"reason": "never happened", "shown": old})
        self.assertContains(page, "Changed after you opened this page: amount (now 12,49\xa0zł)")
        self.assertFalse(self.row().is_cancelled)
        self.assertRedirects(self.client.post(self.cancel_url, {"reason": "never happened",
                                                                "shown": page.context["form"]["shown"].value()}),
                             self.list_url)
        self.assertTrue(self.row().is_cancelled)

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
        self.make("1.00", self.jan, self.shop, date(2030, 12, 31))
        self.make("2.00", self.jan, self.shop, date(2031, 1, 1))
        with mock.patch("django.utils.timezone.localdate", return_value=date(2030, 12, 31)):
            s = self.summaries()
            self.assertEqual((s[2030].total, s[2029].total), (Decimal("1.00"), 0))
        with mock.patch("django.utils.timezone.localdate", return_value=date(2031, 1, 1)):
            s = self.summaries()  # 2030 doesn't disappear on 1 January – it's now last year
            self.assertEqual((s[2031].total, s[2030].total), (Decimal("2.00"), Decimal("1.00")))


# --------------------------------------------------------------------------- Planned transactions
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
        self.assertContains(page, "What is this payment of 100,00\xa0zł for?")
        self.assertContains(page, "Krzesło")
        self.assertContains(page, 'type="checkbox" name="debts"')
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
        self.assertContains(page, "please check it again")
        self.assertFalse(Transaction.objects.exists())

    def confirm_after(self, change, amount):
        """The confirmation screen, then `change` to the debts, then Confirm: the page that comes back."""
        screen = self.client.post(self.url, self.data(amount))
        change()
        return self.client.post(self.url, self.data(amount, confirm="", plan=screen.context["plan"].signature()))

    def test_debt_description_changed_meanwhile_is_shown_again(self):
        d, other = self.debt("90.00", description="Krzesło"), self.debt("20.00", description="Kawa")
        page = self.confirm_after(
            lambda: edit(d, description="Biurko"), "90.00")
        self.assertContains(page, "please check it again")
        self.assertFalse(Transaction.objects.exists())
        self.assertContains(page, "Changed – check it", count=1)
        self.assertEqual(page.context["changed_debts"], {d.pk})
        # Confirmed again from that screen, it's saved.
        self.assertRedirects(self.client.post(self.url, self.data(
            "90.00", confirm="", plan=page.context["plan"].signature())), self.url)
        self.assertEqual(self.refresh(d)[0].settled_by, Transaction.objects.get())

    def test_amounts_changed_meanwhile_are_shown_again_even_when_they_add_up_the_same(self):
        a, b = self.debt("30.00"), self.debt("50.00")

        def change():  # still 80 together, so the same debts are paid exactly
            edit(a, amount=Decimal("40.00"))
            edit(b, amount=Decimal("40.00"))

        page = self.confirm_after(change, "80.00")
        self.assertContains(page, "please check it again")
        self.assertEqual(page.context["changed_debts"], {a.pk, b.pk})
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
        d = self.refresh(d)[0]
        self.assertTrue(d.is_settled)
        left = self.leftover(pay)
        self.assertEqual((left.debtor, left.creditor, left.amount, left.description, left.tag, left.group),
                         (self.ola, self.jan, Decimal("10.00"), "Krzesło – rest", "krzesła", d.group))

    def split_shares(self):
        """ola and ewa owe jan 30 zł each, from one split."""
        shares = [self.debt("30.00", debtor=who, description="Pizza") for who in (self.ola, self.ewa)]
        PlannedTransaction.objects.filter(pk__in=[s.pk for s in shares]).make_group()
        return self.refresh(*shares)

    def test_rest_of_a_paid_share_cant_be_deleted_on_its_own(self):
        ola_share, ewa_share = self.split_shares()
        rest = self.leftover(self.pay("10.00"))  # ola pays 10 of her 30: 20 still owed
        self.assertEqual((rest.amount, rest.group), (Decimal("20.00"), ola_share.group))
        url = reverse("delete_planned", args=[rest.pk])
        page = self.client.get(url)
        self.assertContains(page, "already paid “Pizza”")  # her share is shown with the rest
        self.assertContains(page, "Pizza – rest")
        self.client.post(url)
        self.assertTrue(PlannedTransaction.objects.filter(pk=rest.pk).exists())
        self.assertTrue(PlannedTransaction.objects.filter(pk=ewa_share.pk).exists())

    def ticked(self, amount):
        """Which debts the confirmation screen ticks by itself for a payment of this amount."""
        screen = self.client.post(self.url, self.data(amount))
        return [d for d, ticked, _ in screen.context["plan"].rows() if ticked]

    def test_oldest_debts_are_ticked(self):
        old, middle, new = self.debt("30.00"), self.debt("50.00"), self.debt("40.00")
        self.assertEqual(self.ticked("60.00"), [old, middle])
        pay = self.pay("60.00")
        old, middle, new = self.refresh(old, middle, new)
        self.assertEqual([old.is_settled, middle.is_settled, new.is_settled], [True, True, False])
        self.assertEqual(self.leftover(pay).amount, Decimal("20.00"))  # 30 + 50 – 60, still owed by ola

    def test_debts_paid_in_full_are_all_ticked(self):
        # Jan Kowal owed 100 + 100 + 98 and paid 298.
        a, b, c = self.debt("100.00"), self.debt("100.00"), self.debt("98.00")
        self.assertEqual(self.ticked("298.00"), [a, b, c])
        self.assertEqual(self.ticked("200.00"), [a, b])
        self.assertEqual(self.ticked("500.00"), [a, b, c])  # overpaid: everything owed is ticked

    def test_an_equal_newer_debt_does_not_jump_the_queue(self):
        old, new = self.debt("100.00"), self.debt("890.00")
        self.assertEqual(self.ticked("890.00"), [old, new])

    def test_small_payment_towards_a_big_debt_ticks_it(self):
        big = self.debt("1000.00")
        self.assertEqual(self.ticked("10.00"), [big])

    def test_debts_added_at_the_same_moment_go_in_saving_order(self):
        first, second, third = self.debt("30.00"), self.debt("30.00"), self.debt("30.00")
        PlannedTransaction.objects.update(created_at=first.created_at)
        self.assertEqual(self.ticked("60.00"), [first, second])
        self.assertEqual(self.ticked("60.00"), [first, second])  # and every time

    def test_nothing_owed_ticks_nothing(self):
        self.assertEqual(self.ticked("50.00"), [])

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

    # --- choosing what a payment is for
    def choose(self, amount, debts, button="confirm", **kwargs):
        """Posts the confirmation screen with these debts ticked."""
        plan = plan_payment(self.ola, self.jan, Decimal(amount.replace(",", ".")), "Zwrot",
                            chosen={d.pk for d in debts})
        return self.client.post(self.url, self.data(amount, choosing="1", debts=[d.pk for d in debts],
                                                    plan=plan.signature(), **{button: ""}, **kwargs))

    def test_newer_debt_can_be_ticked_instead(self):
        # Jan's case: he owed 100 + 98 + 890 and paid 890 for the tort, so he ticks just the tort.
        old, older = self.debt("100.00", description="za cokolwiek"), self.debt("98.00", description="retoryka")
        tort = self.debt("890.00", description="tort")
        self.assertRedirects(self.choose("890.00", [tort]), self.url)
        old, older, tort = self.refresh(old, older, tort)
        self.assertEqual([old.is_settled, older.is_settled, tort.is_settled], [False, False, True])
        self.assertFalse(Transaction.objects.get().leftovers.exists())

    def test_pick_which_debts(self):
        a, b, c = self.debt("30.00"), self.debt("50.00"), self.debt("40.00")
        self.assertRedirects(self.choose("90.00", [a, c]), self.url)
        a, b, c = self.refresh(a, b, c)
        self.assertEqual([a.is_settled, b.is_settled, c.is_settled], [True, False, True])
        left = PlannedTransaction.objects.get(created_by_payment__isnull=False)
        self.assertEqual((left.debtor, left.amount), (self.jan, Decimal("20.00")))  # overpaid by 20

    def test_picked_debts_more_than_paid(self):
        a, b, c = self.debt("30.00"), self.debt("50.00"), self.debt("40.00")
        page = self.choose("60.00", [a, b, c], button="recalculate")
        statuses = [status for _, _, status in page.context["plan"].rows()]
        self.assertEqual(statuses, ["paid", "partly", "ran_out"])
        self.assertContains(page, "paid, but 20,00\xa0zł of it stays owed")
        self.assertFalse(Transaction.objects.exists())  # Update doesn't save

    def test_nothing_ticked_is_a_loan(self):
        d = self.debt("30.00")
        self.assertRedirects(self.choose("30.00", []), self.url)
        self.assertFalse(self.refresh(d)[0].is_settled)
        left = PlannedTransaction.objects.get(created_by_payment__isnull=False)
        self.assertEqual((left.debtor, left.creditor, left.amount), (self.jan, self.ola, Decimal("30.00")))

    def test_other_peoples_debts_cant_be_ticked(self):
        mine, ewas = self.debt("30.00"), self.debt("30.00", debtor=self.ewa)
        self.choose("30.00", [mine, ewas])
        self.assertTrue(self.refresh(mine)[0].is_settled)
        self.assertFalse(self.refresh(ewas)[0].is_settled)

    def test_ticks_changed_without_update_shows_screen_again(self):
        a, b = self.debt("30.00"), self.debt("50.00")
        screen = self.client.post(self.url, self.data("30.00"))  # a is picked (oldest)
        page = self.client.post(self.url, self.data("30.00", choosing="1", debts=[b.pk], confirm="",
                                                    plan=screen.context["plan"].signature()))
        self.assertContains(page, "please check it again")
        self.assertFalse(Transaction.objects.exists())

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

    def test_loan_follows_a_correction(self):
        pay = self.pay("100.00")  # owed nothing: jan now owes ola 100
        url = reverse("correct_transaction", args=[pay.pk])
        fix = {**self.data("100.00"), "reason": "typo"}
        self.assertRedirects(self.client.post(url, {**fix, "amount": "120.00", "description": "Pożyczka"}), self.url)
        loan = self.leftover(pay)
        self.assertEqual((loan.debtor, loan.creditor, loan.amount, loan.description),
                         (self.jan, self.ola, Decimal("120.00"), "Pożyczka"))
        self.assertRedirects(self.client.post(url, {**fix, "amount": "120.00", "description": "Pożyczka",
                                                    "recipient": self.ewa.pk}), self.url)
        self.assertEqual(self.leftover(pay).debtor, self.ewa)

    def edit_loan(self, pay, **fields):
        """Changes the loan a payment made, as the Planned page does."""
        PlannedTransaction.objects.filter(pk=self.leftover(pay).pk).update(**fields)

    def test_loan_keeps_its_own_amount_when_only_the_description_is_corrected(self):
        pay = self.pay("100.00")  # owed nothing: jan now owes ola 100
        self.edit_loan(pay, amount=Decimal("70.00"))
        url = reverse("correct_transaction", args=[pay.pk])
        self.assertRedirects(self.client.post(url, {**self.data("100.00"), "description": "Pożyczka",
                                                    "reason": "typo"}), self.url)
        loan = self.leftover(pay)
        self.assertEqual((loan.amount, loan.description), (Decimal("70.00"), "Pożyczka"))

    def test_correcting_what_was_also_changed_on_the_loan_asks_which_to_keep(self):
        pay = self.pay("100.00")
        self.edit_loan(pay, amount=Decimal("70.00"))
        url = reverse("correct_transaction", args=[pay.pk])
        self.assertContains(self.client.get(url), "now jan owes ola 70,00\xa0zł for “Zwrot”")
        fix = {**self.data("120.00"), "reason": "typo"}
        page = self.client.post(url, fix)
        self.assertContains(page, "amount: the loan says 70,00\xa0zł, the correction 120,00\xa0zł")
        self.assertFalse(pay.corrections.exists())

        self.assertRedirects(self.client.post(url, {**fix, "loan": "keep"}), self.url)
        self.assertEqual(Transaction.objects.get(pk=pay.pk).current.amount, Decimal("120.00"))
        self.assertEqual(self.leftover(pay).amount, Decimal("70.00"))

        self.assertRedirects(self.client.post(url, {**self.data("130.00"), "reason": "typo", "loan": "follow"}),
                             self.url)
        self.assertEqual(self.leftover(pay).amount, Decimal("130.00"))

    def test_loan_choice_only_when_the_loan_was_changed(self):
        pay = self.pay("100.00")
        url = reverse("correct_transaction", args=[pay.pk])
        self.assertNotContains(self.client.get(url), 'name="loan"')
        self.edit_loan(pay, description="<b>Taxi</b>")
        page = self.client.get(url)
        self.assertContains(page, 'name="loan"')
        self.assertContains(page, "“&lt;b&gt;Taxi&lt;/b&gt;”")  # typed by a user: escaped

    def test_loan_correction_keeps_edits_to_the_loan_when_only_the_date_changes(self):
        pay = self.pay("100.00")
        PlannedTransaction.objects.filter(pk=self.leftover(pay).pk).update(description="Na taksówkę")
        url = reverse("correct_transaction", args=[pay.pk])
        yesterday = (today() - timedelta(days=1)).strftime("%d/%m/%y")
        self.assertRedirects(self.client.post(url, {**self.data("100.00"), "date": yesterday, "reason": "x"}), self.url)
        self.assertEqual(self.leftover(pay).description, "Na taksówkę")

    def test_loan_cant_be_corrected_once_paid_back_or_into_a_purchase(self):
        pay = self.pay("50.00")
        url = reverse("correct_transaction", args=[pay.pk])
        fix = {**self.data("50.00"), "reason": "typo"}
        self.assertContains(self.client.post(url, {**fix, "recipient": self.shop.pk}), "into a purchase")
        self.client.force_login(self.jan)
        self.pay("50.00", payer=self.jan, recipient=self.ola)  # jan pays the loan back
        self.client.force_login(self.ola)
        self.assertContains(self.client.post(url, {**fix, "amount": "60.00"}), "Cancel that later payment first")
        self.assertFalse(pay.corrections.exists())

    def test_description_of_a_paid_back_loan_can_still_be_corrected(self):
        pay = self.pay("50.00")  # jan now owes ola 50
        self.client.force_login(self.jan)
        self.pay("50.00", payer=self.jan, recipient=self.ola)  # ...and pays it back
        self.client.force_login(self.ola)
        url = reverse("correct_transaction", args=[pay.pk])
        fix = {**self.data("50.00"), "description": "Pożyczka na obiad", "reason": "typo"}
        self.assertRedirects(self.client.post(url, fix), self.url)
        self.assertEqual(Transaction.objects.get(pk=pay.pk).current.description, "Pożyczka na obiad")
        loan = self.leftover(pay)  # paid back, so it's history and stays as it was
        self.assertTrue(loan.is_settled)
        self.assertEqual(loan.description, "Zwrot")

    def test_correction_explains_when_the_loan_was_paid_back_meanwhile(self):
        pay = self.pay("50.00")
        url = reverse("correct_transaction", args=[pay.pk])
        self.client.force_login(self.jan)
        self.pay("50.00", payer=self.jan, recipient=self.ola)
        self.client.force_login(self.ola)
        # Skip the form's check, as if the loan was paid back between checking the form and saving it.
        with mock.patch.object(TransactionCorrection, "clean"):
            page = self.client.post(url, {**self.data("50.00"), "amount": "60.00", "reason": "typo"})
        self.assertContains(page, "already been paid back")
        self.assertFalse(pay.corrections.exists())
        self.assertEqual(self.leftover(pay).amount, Decimal("50.00"))

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
        pay = self.pay("50.00")
        with mock.patch("sheetapp.payments.check_can_undo", side_effect=[None, ValidationError("paid meanwhile")]):
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


    # --- What a payment did, and what cancelling it would do

    def test_history_shows_what_a_partial_payment_covered(self):
        self.debt("30.00", description="Krzesło")
        self.pay("10.00")
        self.assertContains(self.client.get(self.url), "Krzesło 10,00\xa0zł of 30,00\xa0zł – 20,00\xa0zł still owed")

    def test_cancel_page_lists_what_changes(self):
        self.debt("30.00", description="Krzesło")
        pay = self.pay("100.00")
        page = self.client.get(reverse("cancel_transaction", args=[pay.pk]))
        self.assertContains(page, "Owed again – it paid them")
        self.assertContains(page, "ola owes jan 30,00\xa0zł – Krzesło")
        self.assertContains(page, "Deleted – it made them")
        self.assertContains(page, "jan owes ola 70,00\xa0zł")

    def test_cancel_page_names_the_payment_that_blocks_it(self):
        self.debt("30.00")
        pay = self.pay("100.00")  # Jan now owes Ola 70
        back = Transaction(payer=self.jan, recipient=self.ola, amount=Decimal("70.00"), description="Oddaję",
                           created_by=self.jan)
        record_payment(back)
        page = self.client.get(reverse("cancel_transaction", args=[pay.pk]))
        self.assertContains(page, "It can't be cancelled yet.")
        self.assertContains(page, f'href="{self.url}#t-{back.pk}"')
        self.assertContains(page, "added by jan")

    # --- The confirmation screen
class GroupRestsMigrationTests(TestCase):
    """Rests added before they kept their debt's group join it when migrating, written to the audit log."""

    databases = {"default", "audit"}

    def test_rests_added_before_join_their_group(self):
        # Models as the migration sees them: saving them doesn't reach the audit log's hooks.
        apps = MigrationExecutor(connection).loader.project_state(("sheetapp", "0011_submission")).apps

        jan, ola = (User.objects.create_user(n, f"{n}@example.com") for n in ("jan", "ola"))
        shares = [PlannedTransaction.objects.create(debtor=ola, creditor=jan, amount=Decimal("30.00"),
                                                    description="Pizza", created_by=jan) for _ in range(2)]
        PlannedTransaction.objects.filter(pk__in=[s.pk for s in shares]).make_group()
        group = PlannedTransaction.objects.get(pk=shares[0].pk).group

        def pay(payer, recipient, amount):
            payment = Transaction(amount=Decimal(amount), payer=payer, recipient=recipient, description="Zwrot",
                                  created_by=payer)
            record_payment(payment, chosen={shares[0].pk})
            return payment.leftovers.get()

        rest, overpaid = pay(ola, jan, "10.00"), pay(jan, ola, "5.00")  # the second is owed the other way
        PlannedTransaction.objects.filter(pk__in=[rest.pk, overpaid.pk]).update(group=None)  # as they were added
        migration = importlib.import_module("sheetapp.migrations.0012_group_rests")
        with self.captureOnCommitCallbacks(execute=True):
            migration.group_rests(apps, mock.Mock(connection=mock.Mock(alias="default")))
        rest, overpaid = (PlannedTransaction.objects.get(pk=r.pk) for r in (rest, overpaid))
        self.assertEqual((rest.group, overpaid.group), (group, None))
        entries = Entry.objects.filter(model="sheetapp.plannedtransaction", object_id=str(rest.pk))
        self.assertEqual([(e.action, e.after["group"], e.object_repr) for e in entries],
                         [("update", str(group), "ola owes jan 20.00 zł – Pizza – rest")])

class SentTwiceTests(TestCase):
    """The same add form sent twice (Add or Confirm clicked twice, the browser sending it again) adds it once."""

    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.client.force_login(self.jan)
        self.submission = str(uuid.uuid4())

    def transaction(self, **fields):
        data = {"amount": "12.49", "payer": self.jan.pk, "recipient": self.shop.pk, "description": "Kawa",
                "date": today().strftime("%d/%m/%y"), "submission": self.submission}
        return self.client.post(reverse("transactions"), {**data, **fields}, follow=True)

    def confirm_twice(self):
        """A payment from jan to ola: the confirmation screen, then Confirm clicked twice."""
        screen = self.transaction(recipient=self.ola.pk)
        self.assertContains(screen, f'value="{self.submission}"')  # the screen keeps the form's id
        confirm = {"recipient": self.ola.pk, "confirm": "", "plan": screen.context["plan"].signature()}
        self.transaction(**confirm)
        return self.transaction(**confirm)

    def test_purchase(self):
        self.transaction()
        page = self.transaction()
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertContains(page, "Already added: ")

    def test_planned_transaction(self):
        data = {"debtor": self.ola.pk, "creditor": self.jan.pk, "amount": "30,00", "description": "Pizza",
                "submission": self.submission}
        self.client.post(reverse("planned"), data)
        page = self.client.post(reverse("planned"), data, follow=True)
        self.assertEqual(PlannedTransaction.objects.count(), 1)
        self.assertContains(page, "Already added: ")

    def test_loan(self):  # jan owes ola nothing, so the payment is a loan: ola owes jan
        page = self.confirm_twice()
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(PlannedTransaction.objects.count(), 1)
        self.assertContains(page, "Already added: ")

    def test_payment_of_a_debt(self):
        PlannedTransaction.objects.create(debtor=self.jan, creditor=self.ola, amount=Decimal("12.49"),
                                          description="Pizza", created_by=self.ola)
        page = self.confirm_twice()
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertFalse(PlannedTransaction.objects.open().exists())  # paid, and no loan from the second click
        self.assertContains(page, "Already added: ")
        self.assertNotContains(page, "has changed")

    def test_each_new_form_gets_a_new_id(self):
        ids = {self.client.get(reverse(page)).context["form"]["submission"].value()
               for page in ("transactions", "transactions", "planned")}
        self.assertEqual(len(ids), 3)
        page = self.client.get(reverse("transactions"))
        self.assertContains(page, 'name="submission"')
        self.assertContains(page, "data-new-id")  # the page renews it when it comes back

    def test_the_database_refuses_the_same_id_twice(self):
        fields = {"amount": Decimal("1.00"), "payer": self.jan, "recipient": self.shop, "description": "x",
                  "created_by": self.jan, "submission": self.submission}
        Transaction.objects.create(**fields)
        with self.assertRaises(IntegrityError), db_transaction.atomic():
            Transaction.objects.create(**fields)

    def test_both_arriving_at_once(self):
        first = Transaction.objects.create(amount=Decimal("1.00"), payer=self.jan, recipient=self.shop,
                                           description="x", created_by=self.jan, submission=self.submission)
        second = Transaction(amount=Decimal("1.00"), payer=self.jan, recipient=self.shop, description="x",
                             created_by=self.jan, submission=self.submission)
        # Nothing is looked up first: the database refuses the second, and only then is the first found.
        with CaptureQueriesContext(connection) as queries, self.assertRaises(AlreadyAdded) as added:
            save_once(Transaction, self.submission, second.save)
        self.assertEqual(added.exception.record, first)
        self.assertEqual(Transaction.objects.count(), 1)
        lookups = [i for i, q in enumerate(queries) if '"submission" =' in q["sql"]]
        inserts = [i for i, q in enumerate(queries) if q["sql"].startswith("INSERT")]
        self.assertEqual(len(lookups), 1)
        self.assertLess(inserts[0], lookups[0])

    def test_saving_looks_nothing_up(self):
        t = Transaction(amount=Decimal("1.00"), payer=self.jan, recipient=self.shop, description="x",
                        created_by=self.jan, submission=self.submission)
        with CaptureQueriesContext(connection) as queries:
            save_once(Transaction, self.submission, t.save)
        self.assertFalse([q for q in queries if '"submission" =' in q["sql"]])


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
        self.assertContains(self.client.get(self.url), "Done transactions →")
        self.assertContains(self.client.get(reverse("transactions")), "Planned transactions →")

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

    def test_edit_sent_at_the_same_moment_as_another_is_refused(self):
        d = self.debt()
        mine, theirs = PlannedTransaction.objects.get(pk=d.pk), PlannedTransaction.objects.get(pk=d.pk)
        theirs.amount = Decimal("20.00")
        theirs.save()
        mine.amount = Decimal("30.00")
        with self.assertRaisesMessage(ValidationError, "at the same moment"):
            mine.save()
        d.refresh_from_db()
        self.assertEqual((d.amount, d.version), (Decimal("20.00"), 1))

    def test_edit_page_refuses_a_save_from_an_older_version(self):
        d = self.debt()
        with mock.patch("sheetapp.forms.EditPlannedForm.check_up_to_date", autospec=True,
                        side_effect=lambda form, data: edit(d, amount=Decimal("20.00"))):
            page = self.client.post(self.edit_url(d), {"debtor": self.ola.pk, "creditor": self.jan.pk,
                                                       "amount": "30,00", "description": "Pizza"})
        self.assertContains(page, "at the same moment")
        d.refresh_from_db()
        self.assertEqual(d.amount, Decimal("20.00"))

    def test_edit_validates(self):
        d = self.debt()
        response = self.client.post(self.edit_url(d), {"debtor": self.jan.pk, "creditor": self.jan.pk,
                                                       "amount": "1", "description": "x"})
        self.assertContains(response, "must be different people")

    def test_edit_from_an_old_page_keeps_someone_elses_change(self):
        d = self.debt()  # ola owes jan 10,00 for Pizza
        shown = lambda: self.client.get(self.edit_url(d)).context["form"]["shown"].value()
        old = shown()
        fields = {"debtor": self.ola.pk, "creditor": self.jan.pk, "amount": "10,00", "description": "Pizza", "tag": ""}
        self.client.force_login(self.ewa)  # someone else raises the amount
        self.assertRedirects(self.client.post(self.edit_url(d), {**fields, "amount": "40,00", "shown": shown()}),
                             self.url)
        self.client.force_login(self.jan)  # jan's old page only fixes the description
        page = self.client.post(self.edit_url(d), {**fields, "description": "Pizza duża", "shown": old})
        self.assertContains(page, "Changed after you opened this page: amount (now 40,00\xa0zł). Nothing was saved")
        d.refresh_from_db()
        self.assertEqual((d.amount, d.description), (Decimal("40.00"), "Pizza"))
        form = page.context["form"]
        self.assertEqual(Decimal(form["amount"].value()), Decimal("40.00"))
        self.assertEqual(form["description"].value(), "Pizza duża")
        again = {f: form[f].value() for f in ("debtor", "creditor", "amount", "description", "tag", "shown")}
        self.assertRedirects(self.client.post(self.edit_url(d), again), self.url)
        d.refresh_from_db()
        self.assertEqual((d.amount, d.description), (Decimal("40.00"), "Pizza duża"))

    def test_creditor_deletes_after_warning(self):
        d = self.debt()
        page = self.client.get(self.delete_url(d))
        self.assertContains(page, "Only delete a debt that was entered by mistake")
        self.assertTrue(PlannedTransaction.objects.exists())  # nothing deleted until confirmed
        self.assertRedirects(self.client.post(self.delete_url(d)), self.url)
        self.assertFalse(PlannedTransaction.objects.exists())

    def shown_rows(self, d):
        """What a Delete page opened now says it deletes."""
        return self.client.get(self.delete_url(d)).context["signature"]

    def test_delete_refused_when_a_row_changed_after_opening_the_page(self):
        a, b = self.debt("100.00"), self.debt("100.00", debtor=self.ewa)
        PlannedTransaction.objects.filter(pk__in=[a.pk, b.pk]).make_group()
        shown = self.shown_rows(a)
        edit(b, amount=Decimal("400.00"))  # someone else edits it
        page = self.client.post(self.delete_url(a), {"rows": shown})
        self.assertContains(page, "changed after you opened this page")
        self.assertContains(page, "changed or added since, check it", count=1)
        self.assertContains(page, "400,00")
        self.assertEqual(PlannedTransaction.objects.count(), 2)
        # Delete clicked on the page as it's shown now: deleted.
        self.assertRedirects(self.client.post(self.delete_url(a), {"rows": page.context["signature"]}), self.url)
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_delete_refused_when_the_group_grew_after_opening_the_page(self):
        a, other = self.debt(), self.debt(debtor=self.ewa, description="Kawa")
        shown = self.shown_rows(a)  # a alone
        PlannedTransaction.objects.filter(pk__in=[a.pk, other.pk]).make_group()  # grouped in the admin
        page = self.client.post(self.delete_url(a), {"rows": shown})
        self.assertContains(page, "Nothing was deleted")
        self.assertEqual(page.context["changed"], {other.pk})
        self.assertEqual(PlannedTransaction.objects.count(), 2)

    def test_any_member_can_edit_or_delete(self):
        for who in (self.ola, self.ewa):  # the debtor and a bystander
            d = self.debt()  # ola owes jan
            self.client.force_login(who)
            self.assertContains(self.client.get(self.url), self.edit_url(d))
            self.client.post(self.edit_url(d), {"debtor": self.ola.pk, "creditor": self.jan.pk,
                                                "amount": "1", "description": "x"})
            d.refresh_from_db()
            self.assertEqual(d.amount, Decimal("1.00"))
            self.client.post(self.delete_url(d))
            self.assertFalse(PlannedTransaction.objects.filter(pk=d.pk).exists())

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

    def test_group_delete_refused_when_someone_paid(self):
        pay = Transaction.objects.create(amount=Decimal("10.00"), payer=self.ewa, recipient=self.jan,
                                         created_by=self.jan, description="Zwrot")
        a = self.debt()
        paid = self.debt(debtor=self.ewa, settled_by=pay, settled_at=timezone.now())
        PlannedTransaction.objects.filter(pk__in=[a.pk, paid.pk]).make_group()
        self.assertContains(self.client.post(self.delete_url(a)), "ewa already paid")
        self.assertEqual(PlannedTransaction.objects.count(), 2)

    def test_group_owed_to_different_people_can_be_deleted(self):
        b, other = self.debt(), self.debt(debtor=self.jan, creditor=self.ewa)
        PlannedTransaction.objects.filter(pk__in=[b.pk, other.pk]).make_group()
        self.client.post(self.delete_url(b))
        self.assertFalse(PlannedTransaction.objects.exists())


# --------------------------------------------------------------------------- Mark paid
class MarkPaidTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com")
        self.ola = User.objects.create_user("ola", "ola@example.com")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com")
        self.url = reverse("transactions")

    def debt(self, amount="890.00", debtor=None, creditor=None, description="Tort"):
        return PlannedTransaction.objects.create(debtor=debtor or self.ola, creditor=creditor or self.jan,
                                                 amount=Decimal(amount), description=description, created_by=self.jan)

    def pay_url(self, d):
        return f"{self.url}?pay={d.pk}"

    def data(self, d, **extra):
        return {"amount": str(d.amount), "payer": d.debtor.pk, "recipient": d.creditor.pk,
                "description": d.description, "date": today().strftime("%d/%m/%y"), **extra}

    def mark_paid(self, d):
        """Opens Mark paid, submits the filled-in form, then confirms."""
        screen = self.client.post(self.pay_url(d), self.data(d))
        self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")
        return self.client.post(self.pay_url(d), self.data(d, confirm="", plan=screen.context["plan"].signature()))

    def test_button_shown_to_debtor_and_creditor_only(self):
        d = self.debt()
        for who, shown in ((self.ola, True), (self.jan, True), (self.ewa, False)):
            self.client.force_login(who)
            page = self.client.get(reverse("planned"))
            (self.assertContains if shown else self.assertNotContains)(page, self.pay_url(d))

    def test_opens_straight_on_the_confirmation_screen(self):
        older = self.debt(description="Inny tort")
        d = self.debt()
        self.client.force_login(self.ola)
        screen = self.client.get(self.pay_url(d))
        self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")
        payment = screen.context["payment"]
        self.assertEqual((payment.payer, payment.recipient, payment.amount, payment.description, payment.date),
                         (self.ola, self.jan, Decimal("890.00"), "Tort", today()))
        self.assertEqual([(r.pk, ticked) for r, ticked, _ in screen.context["plan"].rows()],
                         [(older.pk, False), (d.pk, True)])  # only the debt clicked
        self.assertFalse(Transaction.objects.exists())  # nothing saved yet
        # Confirm from there records it.
        response = self.client.post(self.pay_url(d), self.data(d, confirm="", choosing="1", debts=[d.pk],
                                                               plan=screen.context["plan"].signature()))
        self.assertRedirects(response, reverse("planned"))
        d.refresh_from_db()
        self.assertEqual(d.settled_by, Transaction.objects.get())

    def test_change_details_opens_the_filled_in_form(self):
        d = self.debt()
        self.client.force_login(self.ola)
        self.assertContains(self.client.get(self.pay_url(d)), "Change details")
        page = self.client.post(self.pay_url(d), self.data(d, back="", choosing="1", debts=[d.pk]))
        self.assertTemplateUsed(page, "sheetapp/transactions.html")
        form = page.context["form"]
        # Who paid whom comes from the debt (fixed), the rest from what was sent.
        self.assertEqual((form["payer"].value(), form["recipient"].value(), form["amount"].value(),
                          form["description"].value()), (self.ola.pk, self.jan.pk, "890.00", "Tort"))
        self.assertContains(page, "Marking as paid")
        self.assertFalse(Transaction.objects.exists())

    def test_who_paid_whom_cant_be_changed(self):
        d = self.debt()  # ola owes jan
        shop = User.objects.get(username=SHOP_USERNAME)
        self.client.force_login(self.ola)
        form = self.client.post(self.pay_url(d), self.data(d, back="")).context["form"]
        self.assertTrue(form.fields["payer"].disabled and form.fields["recipient"].disabled)
        # Paid to a shop and Add: before, that saved a purchase straight away and left the debt open.
        for change in ({"recipient": shop.pk}, {"payer": self.ewa.pk}):
            screen = self.client.post(self.pay_url(d), self.data(d, **change))
            self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")
            payment = screen.context["payment"]
            self.assertEqual((payment.payer, payment.recipient), (self.ola, self.jan), change)
        self.assertFalse(Transaction.objects.exists())

    def test_cant_be_split(self):
        d = self.debt()
        self.client.force_login(self.ola)
        screen = self.client.post(self.pay_url(d), self.data(d, split="on", participants=[self.ola.pk, self.jan.pk]))
        self.assertTemplateUsed(screen, "sheetapp/confirm_payment.html")

    def test_the_debt_stays_ticked(self):
        other = self.debt(description="Inny tort")  # same amount, older
        d = self.debt()
        self.client.force_login(self.ola)
        screen = self.client.get(self.pay_url(d))
        self.assertContains(screen, '<input type="checkbox" checked disabled>', count=1)
        self.assertContains(screen, f'<input type="hidden" name="debts" value="{d.pk}">')
        self.assertContains(screen, f'<input type="checkbox" name="debts" value="{other.pk}">')
        # Sent without it, ticking the other one instead: before, that paid the other one.
        response = self.client.post(self.pay_url(d), self.data(d, confirm="", choosing="1", debts=[other.pk],
                                                               plan=screen.context["plan"].signature()))
        self.assertContains(response, "Tick the debt this payment is for: “Tort”")
        self.assertFalse(Transaction.objects.exists())

    def test_the_money_must_reach_the_debt(self):
        other = self.debt(description="Inny tort")  # older, so it's paid first when both are ticked
        d = self.debt()
        self.client.force_login(self.ola)
        both = self.data(d, choosing="1", debts=[other.pk, d.pk])
        screen = self.client.post(self.pay_url(d), both)
        response = self.client.post(self.pay_url(d), {**both, "confirm": "",
                                                       "plan": screen.context["plan"].signature()})
        self.assertContains(response, "The money runs out before “Tort”")
        self.assertFalse(Transaction.objects.exists())
        # Enough for both: fine, it pays them both.
        both["amount"] = "1780.00"
        screen = self.client.post(self.pay_url(d), both)
        self.assertRedirects(self.client.post(self.pay_url(d), {**both, "confirm": "",
                                                                "plan": screen.context["plan"].signature()}),
                             reverse("planned"))
        self.assertFalse(PlannedTransaction.objects.open().exists())

    def test_confirm_with_nothing_ticked_is_refused(self):
        d = self.debt()
        self.client.force_login(self.ola)
        screen = self.client.post(self.pay_url(d), self.data(d, choosing="1"))  # the debt unticked
        response = self.client.post(self.pay_url(d), self.data(d, confirm="", choosing="1",
                                                               plan=screen.context["plan"].signature()))
        self.assertTemplateUsed(response, "sheetapp/confirm_payment.html")
        self.assertContains(response, "Tick the debt this payment is for")
        self.assertFalse(Transaction.objects.exists())
        self.assertEqual(list(PlannedTransaction.objects.all()), [d])  # no loan the other way

    def test_marks_that_debt_paid_and_returns_to_planned(self):
        # Another debt of the same amount is older, but the one clicked is the one that gets paid.
        older = self.debt(description="Inny tort")
        d = self.debt()
        self.client.force_login(self.jan)  # the creditor records it
        screen = self.client.post(self.pay_url(d), self.data(d))
        self.assertEqual([(r.pk, ticked) for r, ticked, _ in screen.context["plan"].rows()],
                         [(older.pk, False), (d.pk, True)])
        self.assertRedirects(self.mark_paid(d), reverse("planned"))
        pay = Transaction.objects.get()
        d.refresh_from_db()
        older.refresh_from_db()
        self.assertEqual((d.settled_by, older.is_settled), (pay, False))
        self.assertEqual((pay.payer, pay.recipient, pay.amount, pay.created_by),
                         (self.ola, self.jan, Decimal("890.00"), self.jan))
        self.assertNotContains(self.client.get(reverse("planned")), self.pay_url(d))

    def test_clicking_confirm_twice_records_one_payment(self):
        d = self.debt()
        self.client.force_login(self.ola)
        screen = self.client.post(self.pay_url(d), self.data(d))
        confirm = self.data(d, confirm="", plan=screen.context["plan"].signature())
        self.client.post(self.pay_url(d), confirm)
        response = self.client.post(self.pay_url(d), confirm)
        self.assertRedirects(response, reverse("planned"))
        self.assertEqual(Transaction.objects.count(), 1)

    def test_others_cant_mark_paid(self):
        d = self.debt()
        self.client.force_login(self.ewa)
        self.assertEqual(self.client.get(self.pay_url(d)).status_code, 403)
        self.assertEqual(self.client.post(self.pay_url(d), self.data(d)).status_code, 403)
        self.assertEqual(self.client.get(f"{self.url}?pay=abc").status_code, 404)
        self.assertFalse(Transaction.objects.exists())

    def test_admin_marks_selected_paid_one_payment_per_pair(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        a = self.debt("10.00", description="Kawa")
        b = self.debt("5.50", description="Ciastko")
        c = self.debt("7.00", debtor=self.ewa, description="Kawa")
        untouched = self.debt("1.00", description="Woda")
        self.client.force_login(boss)
        changelist = reverse("admin:sheetapp_plannedtransaction_changelist")
        self.client.post(changelist, {"action": "mark_selected_paid", "_selected_action": [a.pk, b.pk, c.pk]})
        payments = {(p.payer, p.amount, p.description) for p in Transaction.objects.all()}
        self.assertEqual(payments, {(self.ola, Decimal("15.50"), "Kawa, Ciastko"), (self.ewa, Decimal("7.00"), "Kawa")})
        self.assertEqual(set(PlannedTransaction.objects.open()), {untouched})
        self.assertFalse(PlannedTransaction.objects.filter(created_by_payment__isnull=False).exists())  # exact

    def test_admin_refuses_when_some_already_paid(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        pay = Transaction.objects.create(amount=Decimal("1.00"), payer=self.ola, recipient=self.jan,
                                         created_by=boss, description="Zwrot")
        a = self.debt("10.00")
        paid = self.debt("1.00")
        PlannedTransaction.objects.filter(pk=paid.pk).update(settled_by=pay, settled_at=timezone.now())
        self.client.force_login(boss)
        page = self.client.post(reverse("admin:sheetapp_plannedtransaction_changelist"),
                                {"action": "mark_selected_paid", "_selected_action": [a.pk, paid.pk]}, follow=True)
        self.assertContains(page, "already paid; nothing was recorded")
        self.assertEqual(Transaction.objects.count(), 1)


# --------------------------------------------------------------------------- Splitting a purchase between people
class DivideTests(TestCase):
    def test_shares_add_up_exactly_and_differ_by_at_most_a_grosz(self):
        for amount, people in [("10.00", 3), ("1535.39", 5), ("0.05", 5), ("100.00", 4), ("0.07", 3)]:
            shares = divide(Decimal(amount), people, uuid.uuid4())
            self.assertEqual(sum(shares), Decimal(amount), amount)
            self.assertLessEqual(max(shares) - min(shares), Decimal("0.01"), amount)

    def test_same_seed_same_shares(self):
        seed = uuid.uuid4()
        self.assertEqual(divide(Decimal("10.00"), 3, seed), divide(Decimal("10.00"), 3, seed))

    def test_leftover_grosz_goes_to_different_people(self):
        lucky = {divide(Decimal("10.00"), 3, uuid.UUID(int=i)).index(Decimal("3.34")) for i in range(30)}
        self.assertEqual(lucky, {0, 1, 2})


class SplitTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com", first_name="Jan")
        self.ola = User.objects.create_user("ola", "ola@example.com", first_name="Ola")
        self.ewa = User.objects.create_user("ewa", "ewa@example.com", first_name="Ewa")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.done_url = reverse("transactions")
        self.planned_url = reverse("planned")
        self.client.force_login(self.jan)
        self.seed = str(uuid.uuid4())

    def purchase(self, people=None, **fields):
        data = {"amount": "30.00", "payer": self.jan.pk, "recipient": self.shop.pk, "description": "Fotele",
                "date": today().strftime("%d/%m/%y"), "split": "on", "split_seed": self.seed,
                "participants": [p.pk for p in (people or [self.jan, self.ola, self.ewa])]}
        return self.client.post(self.done_url, {**data, **fields})

    def owed(self):
        return {(d.debtor, d.creditor): d.amount for d in PlannedTransaction.objects.all()}

    def test_members_all_ticked_shops_not_offered(self):
        page = self.client.get(self.done_url)
        form = page.context["form"]
        self.assertEqual(set(form.fields["participants"].queryset), {self.jan, self.ola, self.ewa})
        self.assertEqual(set(form["participants"].value()), {self.jan.pk, self.ola.pk, self.ewa.pk})
        self.assertContains(page, "Tick everyone")
        self.assertContains(page, "only one of them is for everyone")  # the “?” next to Amount to split
        self.assertContains(self.client.get(self.planned_url), "Split between people")

    def test_purchase_split_between_everyone(self):
        screen = self.purchase()
        self.assertTemplateUsed(screen, "sheetapp/confirm_split.html")
        self.assertContains(screen, f"10,00{NBSP}zł each")
        self.assertContains(screen, "Jan’s own share")
        self.assertFalse(Transaction.objects.exists())  # nothing saved before Confirm
        self.assertRedirects(self.purchase(confirm=""), self.done_url)
        purchase = Transaction.objects.get()
        self.assertEqual((purchase.amount, purchase.recipient), (Decimal("30.00"), self.shop))
        self.assertEqual(self.owed(), {(self.ola, self.jan): Decimal("10.00"), (self.ewa, self.jan): Decimal("10.00")})
        shares = PlannedTransaction.objects.all()
        self.assertEqual({(s.source, s.description, s.created_by) for s in shares}, {(purchase, "Fotele", self.jan)})
        self.assertEqual(len({s.group for s in shares}), 1)  # deleting one deletes them all

    def test_unticked_person_gets_nothing(self):
        self.purchase(people=[self.jan, self.ola], confirm="")
        self.assertEqual(self.owed(), {(self.ola, self.jan): Decimal("15.00")})

    def test_payer_not_taking_part(self):
        self.purchase(people=[self.ola, self.ewa], confirm="")
        self.assertEqual(self.owed(), {(self.ola, self.jan): Decimal("15.00"), (self.ewa, self.jan): Decimal("15.00")})

    def test_split_only_part_of_the_purchase(self):
        users = [self.jan, self.ola, self.ewa] + [User.objects.create_user(f"u{i}", f"u{i}@example.com") for i in (4, 5)]
        screen = self.purchase(people=users, amount="4606,16", split_amount="1535,39")
        self.assertContains(screen, f"1{NBSP}535,39{NBSP}zł ÷ 5 people")
        self.assertContains(screen, f"307,07{NBSP}zł–307,08{NBSP}zł each")
        self.purchase(people=users, amount="4606,16", split_amount="1535,39", confirm="")
        self.assertEqual(Transaction.objects.get().amount, Decimal("4606.16"))
        shares = PlannedTransaction.objects.all()
        self.assertEqual(len(shares), 4)
        jan_share = Decimal("1535.39") - sum(s.amount for s in shares)
        self.assertIn(jan_share, {Decimal("307.07"), Decimal("307.08")})  # the shares add up exactly

    def test_preview_and_saved_shares_match(self):
        users = [self.jan, self.ola, self.ewa]
        plan = self.purchase(people=users, amount="10.00").context["plan"]
        self.purchase(people=users, amount="10.00", confirm="")
        self.assertEqual(self.owed(), {(s.member, self.jan): s.amount for s in plan.owed})

    def test_shares_dont_depend_on_the_order_people_are_listed(self):
        seed = uuid.uuid4()
        people = [self.jan, self.ola, self.ewa]
        shares = [{s.member: s.amount for s in plan_split(self.jan, Decimal("10.00"), order, "x", seed).shares}
                  for order in (people, people[::-1])]
        self.assertEqual(shares[0], shares[1])

    def test_confirm_twice_adds_once(self):
        self.purchase(confirm="")
        self.purchase(confirm="")
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(PlannedTransaction.objects.count(), 2)

    def test_back_keeps_the_ticks(self):
        response = self.purchase(people=[self.jan, self.ola], back="")
        self.assertTemplateUsed(response, "sheetapp/transactions.html")
        form = response.context["form"]
        self.assertEqual({int(pk) for pk in form["participants"].value()}, {self.jan.pk, self.ola.pk})
        self.assertFalse(Transaction.objects.exists())

    def test_refused(self):
        cases = [
            ({"recipient": self.ola.pk}, "split"),  # a payment between members, not a purchase
            ({"payer": self.shop.pk, "recipient": self.jan.pk}, "split"),  # a return
            ({"participants": [self.jan.pk]}, "participants"),  # nobody besides the payer
            ({"participants": []}, "participants"),
            ({"split_amount": "31.00"}, "split_amount"),  # more than was paid
            ({"split_amount": "0"}, "split_amount"),  # not “the whole amount”
            ({"split_amount": "-5"}, "split_amount"),
            ({"amount": "0.02"}, "amount"),  # less than a grosz each
        ]
        for fields, error in cases:
            response = self.purchase(**fields)
            self.assertTrue(response.context["form"].errors.get(error), fields)
        self.assertFalse(Transaction.objects.exists())
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_without_split_nothing_changes(self):
        self.purchase(split="")
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertFalse(PlannedTransaction.objects.exists())

    def test_unticking_split_ignores_what_was_typed_in_it(self):
        # Typed into the split section, then unticked it: the hidden section can't block Add.
        for split_amount in ("0", "abc", "31.00"):
            response = self.purchase(split="", split_amount=split_amount, participants=[self.jan.pk])
            self.assertRedirects(response, self.done_url, msg_prefix=split_amount)
        self.assertEqual(Transaction.objects.count(), 3)
        self.assertFalse(PlannedTransaction.objects.exists())

    # --- from the Planned page
    def planned(self, **fields):
        data = {"creditor": self.ola.pk, "amount": "10,00", "description": "Pizza", "tag": "pizza",
                "split": "on", "split_seed": self.seed, "participants": [self.jan.pk, self.ola.pk, self.ewa.pk]}
        return self.client.post(self.planned_url, {**data, **fields})

    def test_split_from_the_planned_form(self):
        self.assertTemplateUsed(self.planned(), "sheetapp/confirm_split.html")
        self.assertFalse(PlannedTransaction.objects.exists())
        self.assertRedirects(self.planned(confirm=""), self.planned_url)
        shares = PlannedTransaction.objects.all()
        self.assertEqual({s.debtor for s in shares}, {self.jan, self.ewa})  # owed to Ola, so not her
        self.assertEqual({(s.creditor, s.tag, s.source) for s in shares}, {(self.ola, "pizza", None)})
        self.assertIn(sum(s.amount for s in shares), {Decimal("6.66"), Decimal("6.67")})
        self.assertFalse(Transaction.objects.exists())

    def test_planned_split_ignores_owes(self):
        self.planned(debtor=self.ola.pk, confirm="")  # same as “owed to”, but not used
        self.assertEqual(PlannedTransaction.objects.count(), 2)

    def test_planned_without_split_needs_owes(self):
        response = self.planned(split="")
        self.assertTrue(response.context["form"].errors["debtor"])
        self.planned(split="", debtor=self.ewa.pk)
        self.assertEqual(self.owed(), {(self.ewa, self.ola): Decimal("10.00")})

    def test_planned_unticking_split_ignores_what_was_typed_in_it(self):
        response = self.planned(split="", debtor=self.ewa.pk, participants=[self.ola.pk], split_seed="nonsense")
        self.assertRedirects(response, self.planned_url)
        self.assertEqual(self.owed(), {(self.ewa, self.ola): Decimal("10.00")})

    # --- cancelling and correcting the purchase
    def split_purchase(self):
        self.purchase(confirm="")
        return Transaction.objects.get()

    def test_cancelling_the_purchase_cancels_the_shares(self):
        purchase = self.split_purchase()
        other = PlannedTransaction.objects.create(debtor=self.ola, creditor=self.jan, amount=Decimal("5.00"),
                                                  description="Kawa", created_by=self.jan)
        self.client.post(reverse("cancel_transaction", args=[purchase.pk]), {"reason": "Zwrócone"})
        self.assertTrue(Transaction.objects.get().is_cancelled)
        self.assertEqual(list(PlannedTransaction.objects.all()), [other])

    def test_cant_cancel_once_a_share_is_paid(self):
        purchase = self.split_purchase()
        pay = Transaction(amount=Decimal("10.00"), payer=self.ola, recipient=self.jan, description="Fotel",
                          created_by=self.ola)
        record_payment(pay)
        response = self.client.post(reverse("cancel_transaction", args=[purchase.pk]), {"reason": "Zwrócone"})
        self.assertContains(response, "Ola has already paid their share")
        self.assertFalse(Transaction.objects.get(pk=purchase.pk).is_cancelled)
        self.assertEqual(PlannedTransaction.objects.count(), 2)

    def test_split_purchase_amount_cant_be_corrected(self):
        purchase = self.split_purchase()
        url = reverse("correct_transaction", args=[purchase.pk])
        fields = {"amount": "30.00", "payer": self.jan.pk, "recipient": self.shop.pk, "description": "Fotele",
                  "date": today().strftime("%d/%m/%y"), "reason": "x"}
        response = self.client.post(url, {**fields, "amount": "33.00"})
        self.assertContains(response, "split between people")
        self.client.post(url, {**fields, "description": "Fotele biurowe"})  # the description still can be
        self.assertEqual(Transaction.objects.get().current.description, "Fotele biurowe")


# --------------------------------------------------------------------------- Debts linked to their purchase
class PurchaseLinkTests(TestCase):
    def setUp(self):
        self.jan = User.objects.create_user("jan", "jan@example.com", first_name="Jan")
        self.ola = User.objects.create_user("ola", "ola@example.com", first_name="Ola")
        self.shop = User.objects.get(username=SHOP_USERNAME)
        self.client.force_login(self.jan)
        self.client.post(reverse("transactions"), {
            "amount": "4606,16", "payer": self.jan.pk, "recipient": self.shop.pk, "description": "3 fotele",
            "date": today().strftime("%d/%m/%y"), "split": "on", "split_seed": str(uuid.uuid4()),
            "split_amount": "20.00", "participants": [self.jan.pk, self.ola.pk], "confirm": ""})
        self.purchase = Transaction.objects.get()
        self.share = PlannedTransaction.objects.get()  # Ola owes Jan 10,00

    def pay(self, amount, description="Oddaję"):
        payment = Transaction(amount=Decimal(amount), payer=self.ola, recipient=self.jan, description=description,
                              created_by=self.ola)
        record_payment(payment, chosen={self.share.pk})
        return payment

    def test_planned_list_shows_the_purchase(self):
        page = self.client.get(reverse("planned"))
        # The description itself links to the purchase; hovering shows the purchase's full amount.
        self.assertContains(page, f'<a class="src" href="{reverse("transactions")}#t-{self.purchase.pk}" '
                                  f'title="From the purchase: 3 fotele, 4{NBSP}606,16{NBSP}zł">3 fotele</a>')
        self.assertContains(self.client.get(reverse("transactions")), f'id="t-{self.purchase.pk}"')

    def test_shows_the_purchase_as_corrected(self):
        TransactionCorrection.objects.create(
            original=self.purchase, amount=self.purchase.amount, payer=self.jan, recipient=self.shop,
            description="Trzy fotele", date=self.purchase.date, reason="literówka", created_by=self.jan)
        self.assertContains(self.client.get(reverse("planned")), 'title="From the purchase: Trzy fotele')

    def test_hand_added_debt_has_no_link(self):
        PlannedTransaction.objects.create(debtor=self.ola, creditor=self.jan, amount=Decimal("5.00"),
                                          description="Kawa", created_by=self.jan)
        self.assertContains(self.client.get(reverse("planned")), 'class="src"', count=1)

    def test_repayment_says_what_it_paid_for(self):
        self.pay("10.00")
        page = self.client.get(reverse("transactions"))
        self.assertContains(page, "Paid for:")
        self.assertContains(page, f'<a href="{reverse("transactions")}#t-{self.purchase.pk}">3 fotele</a> 10,00{NBSP}zł')

    def test_paid_for_left_out_when_it_only_repeats_the_payment(self):
        self.pay("10.00", description="3 fotele")  # as Mark paid fills it in
        self.assertNotContains(self.client.get(reverse("transactions")), "Paid for:")

    def test_rest_of_a_share_keeps_the_link(self):
        self.pay("4.00")
        rest = PlannedTransaction.objects.open().get()
        self.assertEqual((rest.description, rest.amount, rest.source), ("3 fotele – rest", Decimal("6.00"), self.purchase))

    def test_history_queries_dont_grow_with_payments(self):
        self.pay("10.00")
        self.client.get(reverse("transactions"))  # warm up (sessions, shop list...)
        with CaptureQueriesContext(connection) as one:
            self.client.get(reverse("transactions"))
        for i in range(3):
            self.share = PlannedTransaction.objects.create(debtor=self.ola, creditor=self.jan, amount=Decimal("1.00"),
                                                           description=f"Kawa {i}", source=self.purchase,
                                                           created_by=self.jan)
            self.pay("1.00")
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("transactions"))
        self.assertEqual(len(one), len(many))
