"""End-to-end checks of the rules that matter: no double booking, door codes only for paid
bookings and only inside their window, fingerprints follow memberships, reminders once each.

    python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta

os.environ["SEED_DEMO"] = "1"
os.environ["SEED_HISTORY"] = "0"  # tests count money exactly

from app import (access, analytics, auth, bookings, clock, config, db, health,  # noqa: E402
                 members, payments, scheduler)

T0 = datetime(2026, 9, 28, 8, 0)  # a Monday, 08:00


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clock.set_now(T0)
        access._fails.clear()
        auth._ip_hits.clear()
        self.out = io.StringIO()
        with redirect_stdout(self.out):
            db.init(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        db.conn().close()
        db._local.conn = None
        clock.set_now(None)
        self.tmp.cleanup()

    def run_quiet(self, fn, *a, **kw):
        with redirect_stdout(self.out):
            return fn(*a, **kw)

    def book_and_pay(self, court="B1", start="18:00", day="2026-09-28", slots=1, phone="9811111111"):
        out = self.run_quiet(bookings.create, court, day, start, slots, "Riya Kapoor", phone)
        return self.run_quiet(bookings.confirm_payment, out["booking"]["id"], {})

    def sent(self, kind):
        return db.all_("SELECT * FROM notifications WHERE kind=? AND status='sent'", kind)


class BookingTests(Base):
    def test_no_double_booking(self):
        self.book_and_pay("K1", "18:00")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "K1", "2026-09-28", "18:00", 1, "Other", "9822222222")
        # overlapping multi-slot too
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "K1", "2026-09-28", "17:00", 2, "Other", "9822222222")
        # other court is fine
        self.run_quiet(bookings.create, "K2", "2026-09-28", "18:00", 1, "Other", "9822222222")

    def test_unpaid_hold_lapses(self):
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "A", "9811111111")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "B", "9822222222")
        clock.set_now(T0 + timedelta(minutes=config.HOLD_MIN + 1))
        bookings.expire_holds()
        self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "B", "9822222222")

    def test_academy_batch_blocks_courts(self):
        # Seeded junior batch: Mon/Wed/Fri 17:00-19:00 on B1,B2. T0 is a Monday.
        grid = bookings.availability("badminton", date(2026, 9, 28))
        row = next(r for r in grid["slots"] if r["start"] == "17:00")
        self.assertTrue(row["cells"]["B1"].startswith("blocked"))
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-09-28", "17:00", 1, "A", "9811111111")
        # Tuesday is open
        self.run_quiet(bookings.create, "B1", "2026-09-29", "17:00", 1, "A", "9811111111")

    def test_past_and_horizon(self):
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-09-28", "07:00", 1, "A", "9811111111")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B1", "2026-12-01", "07:00", 1, "A", "9811111111")


class DoorCodeTests(Base):
    def test_code_only_inside_window_and_only_when_paid(self):
        unpaid = self.run_quiet(bookings.create, "K2", "2026-09-28", "10:00", 1, "U", "9833333333")
        b = self.book_and_pay("K1", "10:00")                               # 08:00, two hours ahead
        self.assertEqual(len(self.sent("door_code")), 2)                   # code sent with confirmation
        self.assertIsNone(db.one("SELECT 1 AS x FROM door_codes WHERE booking_id=?", unpaid["booking"]["id"]))
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        clock.set_now(datetime(2026, 9, 28, 9, 49))
        self.assertFalse(access.verify_code(code)["open"])                 # 11 min before: door stays shut
        self.run_quiet(access.issue_due_codes)
        self.assertEqual(len(self.sent("door_code")), 2)                   # no reminder yet
        clock.set_now(datetime(2026, 9, 28, 9, 50))
        self.assertEqual(self.run_quiet(access.issue_due_codes), [])       # idempotent
        self.assertEqual(len(self.sent("door_code")), 4)                   # reminder as the window opens
        self.run_quiet(access.issue_due_codes)
        self.assertEqual(len(self.sent("door_code")), 4)                   # only once

        self.assertTrue(access.verify_code(code)["open"])
        clock.set_now(datetime(2026, 9, 28, 10, 59))
        self.assertTrue(access.verify_code(code)["open"])                  # re-entry during slot
        clock.set_now(datetime(2026, 9, 28, 11, 0))
        self.assertFalse(access.verify_code(code)["open"])                 # slot over
        self.run_quiet(access.expire_codes)
        self.assertEqual(db.one("SELECT status FROM door_codes")["status"], "expired")

    def test_last_minute_booking_gets_code_immediately(self):
        clock.set_now(datetime(2026, 9, 28, 9, 57))
        self.book_and_pay("P1", "10:00")
        self.assertEqual(len(self.sent("door_code")), 2)

    def test_cancel_revokes(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.run_quiet(bookings.cancel, b["id"])
        self.assertFalse(access.verify_code(code)["open"])

    def test_keypad_lockout(self):
        for _ in range(access.MAX_FAILS):
            access.verify_code("000001", "door-1")
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.assertEqual(access.verify_code(code, "door-1")["reason"], "locked_out")
        self.assertTrue(access.verify_code(code, "door-2")["open"])

    def test_device_signature(self):
        body = b'{"code":"123456"}'
        ts = str(int(time.time()))
        sig = hmac.new(config.DEVICE_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertTrue(access.check_device_signature({"X-Timestamp": ts, "X-Signature": sig}, body))
        self.assertFalse(access.check_device_signature({"X-Timestamp": ts, "X-Signature": sig}, b'{"code":"1"}'))
        old = str(int(time.time()) - 600)
        sig_old = hmac.new(config.DEVICE_SECRET.encode(), f"{old}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertFalse(access.check_device_signature({"X-Timestamp": old, "X-Signature": sig_old}, body))


class MemberTests(Base):
    def test_fingerprint_follows_membership(self):
        self.run_quiet(access.sync_member_access)
        aarav = db.one("SELECT * FROM users WHERE phone='9000000001'")  # plan ends T0+7
        self.assertEqual(aarav["lock_enabled"], 1)
        self.assertTrue(access.verify_fingerprint("101")["open"])
        clock.set_now(T0 + timedelta(days=8))
        self.run_quiet(access.sync_member_access)
        self.assertEqual(db.one("SELECT lock_enabled FROM users WHERE id=?", aarav["id"])["lock_enabled"], 0)
        self.assertFalse(access.verify_fingerprint("101")["open"])
        self.run_quiet(members.add_membership, aarav["id"], 1)             # renews → back on
        self.assertEqual(db.one("SELECT lock_enabled FROM users WHERE id=?", aarav["id"])["lock_enabled"], 1)

    def test_early_renewal_keeps_days(self):
        aarav = db.one("SELECT id FROM users WHERE phone='9000000001'")
        m = self.run_quiet(members.add_membership, aarav["id"], 1)
        self.assertEqual(m["start_date"], "2026-10-06")                    # day after current end

    def test_reminders_7day_and_last_day_once(self):
        self.run_quiet(members.send_reminders)
        rem = self.sent("membership_reminder")
        bodies = " | ".join(r["body"] for r in rem)
        self.assertEqual(len(rem), 4)                                      # Aarav + Diya × SMS + WhatsApp
        self.assertIn("ends in 7 days", bodies)
        self.assertIn("ends today", bodies)
        self.assertEqual(len(self.sent("academy_fee_reminder")), 2)        # Ishaan, fee due in 7 days
        self.run_quiet(members.send_reminders)
        self.assertEqual(len(self.sent("membership_reminder")), 4)         # no repeats
        clock.set_now(T0 + timedelta(days=7))                              # Aarav's last day
        self.run_quiet(members.send_reminders)
        self.assertEqual(len(self.sent("membership_reminder")), 6)

    def test_renewed_member_gets_no_reminder(self):
        aarav = db.one("SELECT id FROM users WHERE phone='9000000001'")
        self.run_quiet(members.add_membership, aarav["id"], 1)
        self.run_quiet(members.send_reminders)
        self.assertNotIn("9000000001", [r["to_phone"] for r in self.sent("membership_reminder")])

    def test_channel_preference(self):
        db.conn().execute("UPDATE users SET wa_opt=0 WHERE phone='9000000002'")
        self.run_quiet(members.send_reminders)
        chans = [r["channel"] for r in self.sent("membership_reminder") if r["to_phone"] == "9000000002"]
        self.assertEqual(chans, ["sms"])


class AuthTests(Base):
    def test_otp_login(self):
        code, is_new, _ = self.run_quiet(auth.request_login_code, "+91 90000 00004")
        self.assertFalse(is_new)
        with self.assertRaises(auth.AuthError):
            auth.verify_login_code("9000000004", "000000" if code != "000000" else "111111")
        token = auth.verify_login_code("9000000004", code)
        u = auth.user_for(token)
        self.assertEqual(u["name"], "Ishaan Verma")
        s = members.status(u["id"])
        self.assertEqual(s["academy"][0]["batch"], "Junior Badminton — Evening")

    def test_login_with_member_id(self):
        ishaan = db.one("SELECT id FROM users WHERE phone='9000000004'")["id"]
        mid = auth.member_id(ishaan)
        code, _, phone = self.run_quiet(auth.request_login_code, mid.lower().replace("-", ""))
        self.assertEqual(phone, "9000000004")
        self.assertEqual(auth.user_for(auth.verify_login_code(mid, code))["id"], ishaan)

    def test_new_number_signs_up(self):
        code, is_new, _ = self.run_quiet(auth.request_login_code, "9876543210")
        self.assertTrue(is_new)
        with self.assertRaises(auth.AuthError):  # a new account needs a name
            auth.verify_login_code("9876543210", code)
        u = auth.user_for(auth.verify_login_code("9876543210", code, "Neha Joshi"))
        self.assertEqual(u["name"], "Neha Joshi")

    def test_code_request_rate_limited_per_ip(self):
        for i in range(auth.IP_MAX_REQUESTS):
            self.run_quiet(auth.request_login_code, f"98000000{i:02d}", "1.2.3.4")
        with self.assertRaises(auth.AuthError):
            self.run_quiet(auth.request_login_code, "9800000099", "1.2.3.4")

    def test_scheduler_tick_runs_clean(self):
        clock.set_now(datetime(2026, 9, 28, 10, 30))
        self.run_quiet(scheduler.tick)
        self.assertEqual(db.kv_get("reminders_ran"), "2026-09-28")


if __name__ == "__main__":
    unittest.main()
