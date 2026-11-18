import logging
from decimal import Decimal
from io import StringIO

from django.contrib import admin
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connections, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from sheetapp.forms import DATE_FORMATS
from sheetapp.models import PlannedTransaction, Transaction, User

from .check import chain_problems, data_problems
from .log import entry_hash, hashed_fields
from .models import Entry


class AuditTestCase(TestCase):
    databases = {"default", "audit"}

    def setUp(self):
        # Entries are printed in the server's terminal; keep them out of the test output.
        printed = logging.getLogger("audit.entries")
        self.addCleanup(setattr, printed, "handlers", printed.handlers)
        printed.handlers = [logging.NullHandler()]
        self.jan = User.objects.create_user("jan", "jan@example.com", password="secret-pass-123", first_name="Jan")
        self.ola = User.objects.create_user("ola", "ola@example.com", first_name="Ola")
        self.client.force_login(self.jan)

    def logged(self, **filters):
        return list(Entry.objects.filter(**filters))

    def debt(self, amount="10,00"):
        """Ola owes Jan, added on the Planned page."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("planned"), {"debtor": self.ola.pk, "creditor": self.jan.pk, "amount": amount,
                                                  "description": "Pizza", "tag": ""})
        return PlannedTransaction.objects.latest("pk")

    def pay(self, debt, by):
        """Mark paid, then Confirm."""
        self.client.force_login(by)
        url = f"{reverse('transactions')}?pay={debt.pk}"
        screen = self.client.get(url)
        data = {"amount": str(debt.amount), "payer": debt.debtor.pk, "recipient": debt.creditor.pk,
                "description": debt.description, "date": timezone.localdate().strftime(DATE_FORMATS[0]),
                "confirm": "", "choosing": "1", "debts": [debt.pk], "plan": screen.context["plan"].signature()}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, data)
        return Transaction.objects.latest("pk")


class ChangesAreLoggedTests(AuditTestCase):
    def test_adding_a_debt(self):
        debt = self.debt()
        entry, = self.logged(model="sheetapp.plannedtransaction")
        self.assertEqual((entry.action, entry.object_id, entry.user_id, entry.username, entry.ip),
                         (Entry.CREATE, str(debt.pk), self.jan.pk, "jan", "127.0.0.1"))
        self.assertIsNone(entry.before)
        self.assertEqual((entry.after["amount"], entry.after["debtor_id"], entry.after["description"]),
                         ("10.00", self.ola.pk, "Pizza"))
        self.assertIn("Pizza", entry.object_repr)

    def test_editing_shows_old_and_new_values(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("edit_planned", args=[debt.pk]),
                             {"debtor": self.ola.pk, "creditor": self.jan.pk, "amount": "12,50",
                              "description": "Pizza", "tag": ""})
        entry = self.logged(action=Entry.UPDATE)[-1]
        self.assertEqual(entry.changes(), {"amount": ("10.00", "12.50")})

    def test_saving_without_changes_logs_nothing(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            debt.save()
        self.assertFalse(self.logged(action=Entry.UPDATE))

    def test_deleted_debt_keeps_its_last_values(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("delete_planned", args=[debt.pk]))
        self.assertFalse(PlannedTransaction.objects.exists())
        entry, = self.logged(action=Entry.DELETE)
        self.assertEqual((entry.object_id, entry.before["amount"], entry.after), (str(debt.pk), "10.00", None))

    def test_paying_settles_the_debt_in_the_log_too(self):
        # Settling changes the debts all at once, without saving them one by one.
        debt = self.debt()
        payment = self.pay(debt, by=self.ola)
        payment_entry, = self.logged(model="sheetapp.transaction")
        self.assertEqual((payment_entry.action, payment_entry.username), (Entry.CREATE, "ola"))
        settled, = self.logged(model="sheetapp.plannedtransaction", action=Entry.UPDATE)
        self.assertEqual(settled.changes()["settled_by_id"], (None, payment.pk))
        self.assertEqual((settled.username, settled.ip), ("ola", "127.0.0.1"))

    def test_cancelling_the_payment_reopens_the_debt_in_the_log(self):
        debt = self.debt()
        payment = self.pay(debt, by=self.ola)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("cancel_transaction", args=[payment.pk]), {"reason": "wrong debt"})
        cancel, = self.logged(model="sheetapp.transactioncorrection")
        self.assertEqual((cancel.action, cancel.after["cancelled"], cancel.after["reason"]),
                         (Entry.CREATE, True, "wrong debt"))
        reopened = self.logged(model="sheetapp.plannedtransaction", action=Entry.UPDATE)[-1]
        self.assertEqual(reopened.changes()["settled_by_id"], (payment.pk, None))

    def test_grouping_is_logged(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            PlannedTransaction.objects.filter(pk=debt.pk).make_group()
        entry, = self.logged(action=Entry.UPDATE)
        self.assertEqual(list(entry.changes()), ["group"])
        self.assertEqual(entry.username, "")  # outside a request: nobody, no IP
        self.assertIsNone(entry.ip)

    def test_users_are_logged_but_never_their_password(self):
        with self.captureOnCommitCallbacks(execute=True):
            ewa = User.objects.create_user("ewa", "ewa@example.com", password="top-secret-999")
            ewa.set_password("other-secret-000")
            ewa.save()
        created, changed = self.logged(model="sheetapp.user")
        self.assertEqual((created.action, changed.action), (Entry.CREATE, Entry.UPDATE))
        self.assertEqual(list(changed.changes()), ["password"])  # it shows that the password changed...
        for entry in (created, changed):
            for value in (entry.before or {}, entry.after):
                self.assertNotIn(ewa.password, str(value))  # ...but not the password hash itself
                self.assertNotIn("secret", str(value))

    def test_a_change_that_is_rolled_back_is_not_logged(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    PlannedTransaction.objects.create(debtor=self.ola, creditor=self.jan, amount=Decimal("5"),
                                                      description="x", created_by=self.jan)
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertFalse(Entry.objects.exists())


class LoginTests(AuditTestCase):
    def test_login_and_logout(self):
        User.objects.filter(pk=self.jan.pk).update(is_staff=True)  # the admin login page is the one with a password
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:login"), {"username": "jan", "password": "secret-pass-123"})
            self.client.post(reverse("logout"))
        login, logout = self.logged(action__in=[Entry.LOGIN, Entry.LOGOUT])
        self.assertEqual((login.action, login.username, login.ip), (Entry.LOGIN, "jan", "127.0.0.1"))
        self.assertEqual((logout.action, logout.username), (Entry.LOGOUT, "jan"))
        self.assertFalse(self.logged(action=Entry.UPDATE))  # last_login changing isn't a change to log

    def test_failed_login_records_who_they_tried_not_the_password(self):
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:login"), {"username": "jan", "password": "wrong-guess-42"})
        entry, = self.logged(action=Entry.LOGIN_FAILED)
        self.assertEqual((entry.username, entry.user_id, entry.ip), ("jan", None, "127.0.0.1"))
        self.assertNotIn("wrong-guess-42", str(hashed_fields(entry)))


class EntriesAreFinalTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.debt()
        self.debt("20,00")

    def test_entries_cant_be_changed_or_deleted(self):
        entry = Entry.objects.first()
        for attempt in (lambda: Entry.objects.filter(pk=entry.pk).update(username="someone else"),
                        lambda: Entry.objects.filter(pk=entry.pk).delete()):
            with self.assertRaises(DatabaseError), transaction.atomic(using="audit"):
                attempt()
        entry.refresh_from_db()
        self.assertEqual(entry.username, "jan")

    def test_each_entry_holds_the_hash_of_the_one_before(self):
        first, second = Entry.objects.all()
        self.assertEqual(first.prev_hash, "")
        self.assertEqual(second.prev_hash, first.hash)
        for entry in (first, second):  # the hash still matches what's stored
            self.assertEqual(entry.hash, entry_hash(hashed_fields(entry)))

    def test_kept_apart_from_the_data(self):
        self.assertNotIn("audit_entry", connections["default"].introspection.table_names())
        self.assertNotIn(Entry, admin.site._registry)  # nothing in the app can show or change it

    def test_each_entry_is_printed_in_the_terminal_as_it_is_written(self):
        with self.assertLogs("audit.entries", "INFO") as printed:
            debt = self.debt("30,00")
        line, = printed.output
        self.assertIn(f"jan (127.0.0.1): added planned transaction {debt.pk}: Ola owes Jan", line)

    def test_audit_log_command(self):
        out = StringIO()
        call_command("audit_log", stdout=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("jan (127.0.0.1): added planned transaction", lines[0])
        for kind, found in (("planned transaction", 2), ("plannedtransaction", 2), ("done transaction", 0)):
            out = StringIO()
            call_command("audit_log", "--model", kind, stdout=out)
            self.assertEqual(len(out.getvalue().splitlines()), found, kind)
        out = StringIO()
        call_command("audit_log", "--user", "nobody", stdout=out)
        self.assertEqual(out.getvalue(), "")


class ReadableTests(AuditTestCase):
    """The log reads like the app: its names for things, people and payments by name, money with a comma."""

    def text(self, **filters):
        return "\n".join(line for entry in self.logged(**filters) for line in entry.lines())

    def test_a_change_reads_like_the_app(self):
        debt = self.debt()
        payment = self.pay(debt, by=self.ola)
        text = self.text(action=Entry.UPDATE)
        self.assertIn(f"ola (127.0.0.1): changed planned transaction {debt.pk}: Ola owes Jan", text)
        self.assertIn(f"    settled by payment: — → {payment}", text)  # the payment, not its id
        self.assertIn("    settled at: — → ", text)
        self.assertNotIn("sheetapp", text)
        self.assertNotIn("_id", text)

    def test_money_people_and_yes_no(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("edit_planned", args=[debt.pk]),
                             {"debtor": self.jan.pk, "creditor": self.ola.pk, "amount": "12,50",
                              "description": "Pizza", "tag": ""})
            self.ola.is_staff = True
            self.ola.save()
        text = self.text(action=Entry.UPDATE)
        self.assertIn("    amount (zł): 10,00 → 12,50", text)
        self.assertIn("    owes: Ola → Jan", text)
        self.assertIn("    owed to: Jan → Ola", text)
        self.assertIn("changed user", text)
        self.assertIn("    staff status: no → yes", text)

    def test_a_deleted_record_shows_its_last_values(self):
        debt = self.debt()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("delete_planned", args=[debt.pk]))
        text = self.text(action=Entry.DELETE)
        self.assertIn(f"deleted planned transaction {debt.pk}: Ola owes Jan", text)
        for line in ("      owes: Ola", "      owed to: Jan", "      amount (zł): 10,00", "      description: Pizza"):
            self.assertIn(line, text)

    def test_logins(self):
        User.objects.filter(pk=self.jan.pk).update(is_staff=True)
        self.client.logout()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:login"), {"username": "jan", "password": "nope-nope-1"})
            self.client.post(reverse("admin:login"), {"username": "jan", "password": "secret-pass-123"})
        failed, ok = (e.lines()[0] for e in self.logged(action__in=[Entry.LOGIN_FAILED, Entry.LOGIN]))
        self.assertTrue(failed.endswith("(127.0.0.1): failed to log in as “jan”"), failed)
        self.assertTrue(ok.endswith("jan (127.0.0.1): logged in"), ok)

    def test_a_password_change_says_only_that_it_changed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.jan.set_password("another-one-77")
            self.jan.save()
        self.assertIn("    password: changed", self.text(action=Entry.UPDATE))


class ConsistencyTests(AuditTestCase):
    """audit_check: the log is intact and agrees with the data."""

    def setUp(self):
        super().setUp()
        self.snapshot()  # jan and ola were added before anything was logged
        self.pizza = self.debt()

    def snapshot(self):
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("audit_snapshot", stdout=out)
        return out.getvalue()

    def problems(self):
        return chain_problems() + data_problems()

    def sql(self, statement, *params, using="default"):
        with connections[using].cursor() as cursor:
            cursor.execute(statement, params)

    def test_a_log_that_matches_the_data(self):
        self.pay(self.pizza, by=self.ola)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("delete_planned", args=[self.debt().pk]))
        self.assertEqual(self.problems(), [])
        out = StringIO()
        call_command("audit_check", stdout=out)
        self.assertIn("intact and agrees with the data", out.getvalue())

    def test_a_change_made_outside_the_app(self):
        self.sql("UPDATE sheetapp_plannedtransaction SET amount = '99.00' WHERE id = %s", self.pizza.pk)
        self.assertEqual(self.problems(),
                         [f"planned transaction {self.pizza.pk}: amount (zł) is 99,00, the log says 10,00"])
        with self.assertRaisesMessage(CommandError, "1 problem found"):
            call_command("audit_check", stdout=StringIO())

    def test_a_password_changed_outside_the_app(self):
        self.sql("UPDATE sheetapp_user SET password = 'x' WHERE id = %s", self.ola.pk)
        self.assertEqual(self.problems(), [f"user {self.ola.pk}: password changed without the log knowing"])

    def test_records_added_or_deleted_outside_the_app(self):
        ghost, = User.objects.bulk_create([User(username="ghost")])  # bulk_create skips the save hooks
        self.sql("DELETE FROM sheetapp_plannedtransaction WHERE id = %s", self.pizza.pk)
        self.assertEqual(self.problems(), [f"user {ghost.pk}: not in the log",
                                           f"planned transaction {self.pizza.pk}: deleted without the log knowing"])

    def test_an_entry_changed_in_the_log(self):
        entry = Entry.objects.last()
        self.sql("DROP TRIGGER audit_entry_no_update", using="audit")
        self.sql("UPDATE audit_entry SET username = 'ola' WHERE id = %s", entry.pk, using="audit")
        self.assertEqual(self.problems(), [
            "the database no longer refuses changes to entries (audit_entry_no_update removed)",
            f"#{entry.pk}: changed after it was written"])

    def test_an_entry_removed_from_the_log(self):
        self.debt("20,00")
        *_, removed, last = Entry.objects.all()
        self.sql("DROP TRIGGER audit_entry_no_delete", using="audit")
        self.sql("DELETE FROM audit_entry WHERE id = %s", removed.pk, using="audit")
        self.assertIn(f"#{last.pk}: the entry before it was changed or removed", self.problems())

    def test_snapshot_records_what_the_log_has_never_seen(self):
        ghost, = User.objects.bulk_create([User(username="ghost")])
        self.assertEqual(self.snapshot(), "Recorded 1 record.\n")
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.snapshot(), "Recorded 0 records.\n")  # only once per record
        entry = Entry.objects.last()
        self.assertEqual((entry.action, entry.object_id), (Entry.SNAPSHOT, str(ghost.pk)))
        self.assertIn(f"system: recorded user {ghost.pk}: ghost", entry.lines()[0])
        self.assertIn("      username: ghost", entry.lines())
