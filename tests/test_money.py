"""Cancellations, refunds, staff reservations, online memberships, analytics and health.

    python -m unittest discover -s tests -v
"""
from datetime import datetime, timedelta

from app import access, analytics, auth, bookings, clock, config, db, health, members, payments
from tests.test_core import T0, Base


class RefundTests(Base):
    def paid(self, b):
        return db.one("SELECT * FROM payments WHERE kind='booking' AND ref_id=?", b["id"])

    def test_customer_refund_tiers(self):
        # T0 = Mon 08:00. Tue 10:00 is 26h away, Mon 20:00 is 12h, Mon 11:00 is 3h.
        full = self.book_and_pay("P1", "10:00", "2026-09-29")
        half = self.book_and_pay("P1", "20:00")
        none = self.book_and_pay("K1", "11:00")
        self.assertEqual(self.run_quiet(bookings.cancel, full["id"])["refund"]["amount"], config.PRICES["padel"])
        self.assertEqual(self.run_quiet(bookings.cancel, half["id"])["refund"]["amount"],
                         config.PRICES["padel"] * config.REFUND_PARTIAL_PCT // 100)
        self.assertEqual(self.run_quiet(bookings.cancel, none["id"])["refund"]["amount"], 0)
        self.assertEqual(self.paid(full)["refund_status"], "done")             # mock gateway refunds instantly
        self.assertIsNone(self.paid(none)["refund_status"])
        self.assertEqual(len(self.sent("refund_issued")), 4)                   # 2 refunds × SMS + WhatsApp
        self.assertEqual(len(self.sent("booking_cancelled")), 6)
        self.run_quiet(bookings.create, "P1", "2026-09-29", "10:00", 1, "X", "9844444444")  # slot free again

    def test_customer_cannot_cancel_after_start_staff_can_with_full_refund(self):
        b = self.book_and_pay("K2", "09:00")
        clock.set_now(datetime(2026, 9, 28, 9, 5))
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.cancel, b["id"], "customer")
        self.assertEqual(self.run_quiet(bookings.cancel, b["id"], "staff")["refund"]["amount"],
                         config.PRICES["pickleball"])

    def test_cancel_kills_door_code(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.book_and_pay("P1", "10:00")
        code = db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"])["code"]
        self.run_quiet(bookings.cancel, b["id"])
        self.assertFalse(access.verify_code(code)["open"])

    def test_phone_reservation_unpaid_gets_door_code_then_pays(self):
        clock.set_now(datetime(2026, 9, 28, 9, 55))
        b = self.run_quiet(bookings.create_by_staff, "B1", "2026-09-28", "10:00", 1, "Caller", "9855555555",
                           "unpaid", "Desk")
        self.assertEqual((b["status"], b["pay_status"], b["source"]), ("confirmed", "unpaid", "phone"))
        self.assertIsNotNone(db.one("SELECT code FROM door_codes WHERE booking_id=?", b["id"]))
        self.assertEqual(analytics.report(1)["money"]["gross"], 0)            # nothing collected yet
        self.assertEqual(analytics.report(1)["money"]["unpaid_reservations"], config.PRICES["badminton"])
        self.run_quiet(bookings.mark_paid, b["id"], "Desk")
        self.assertEqual(analytics.report(1)["money"]["gross"], config.PRICES["badminton"])

    def test_desk_cash_refund_is_manual(self):
        b = self.run_quiet(bookings.create_by_staff, "B2", "2026-09-29", "09:00", 1, "Cash", "9866666666",
                           "desk_paid", "Desk")
        self.run_quiet(bookings.cancel, b["id"], "staff")
        self.assertEqual(self.paid(b)["refund_status"], "manual")
        payments.mark_manual_refund_done(self.paid(b)["id"], "Desk")
        self.assertEqual(self.paid(b)["refund_status"], "done")

    def test_complimentary_booking(self):
        b = self.run_quiet(bookings.create_by_staff, "K1", "2026-09-29", "09:00", 1, "Coach demo", "9877777777",
                           "comp", "Desk")
        self.assertEqual(b["pay_status"], "comp")
        self.assertEqual(self.run_quiet(bookings.cancel, b["id"], "staff")["refund"]["amount"], 0)

    def test_paid_after_hold_lost_slot_is_refunded(self):
        slow = self.run_quiet(bookings.create, "P1", "2026-09-28", "20:00", 1, "Slow", "9811111111")
        clock.set_now(T0 + timedelta(minutes=config.HOLD_MIN + 1))
        bookings.expire_holds()
        self.book_and_pay("P1", "20:00", phone="9822222222")
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.confirm_payment, slow["booking"]["id"], {})
        self.assertEqual(bookings.get(slow["booking"]["id"])["status"], "cancelled")
        self.assertEqual(self.paid(slow["booking"])["refund_status"], "done")

    def test_failed_refund_retries_then_flags(self):
        b = self.book_and_pay("P1", "10:00", "2026-09-29")
        db.conn().execute("UPDATE payments SET provider='razorpay' WHERE kind='booking' AND ref_id=?", (b["id"],))
        real = payments._razorpay

        def down(*a, **k):
            raise RuntimeError("gateway down")
        payments._razorpay = down
        try:
            self.run_quiet(bookings.cancel, b["id"])
            self.assertEqual(self.paid(b)["refund_status"], "pending")
            for _ in range(payments.MAX_REFUND_ATTEMPTS):
                self.run_quiet(payments.process_refunds)
            self.assertEqual(self.paid(b)["refund_status"], "failed")
            self.assertIn("Refunds", [c["area"] for c in health.checks() if c["status"] == "bad"])
        finally:
            payments._razorpay = real


class OnlineMembershipTests(Base):
    def test_buy_and_renew_online(self):
        kabir = db.one("SELECT id FROM users WHERE phone='9000000003'")  # current plan ends T0+25
        pay = self.run_quiet(members.order_membership, kabir["id"], 3)
        self.assertEqual(pay["amount"], 3 * config.ONLINE_MEMBERSHIP_MONTHLY)
        p, _ = payments.capture(pay["payment_id"], {})
        m = self.run_quiet(members.complete_membership, p)
        self.assertEqual(m["start_date"], "2026-10-24")                    # continues after current plan
        self.assertEqual(m["end_date"], "2027-01-21")                      # 90 days
        again = self.run_quiet(members.complete_membership, payments.get(p["id"]))
        self.assertEqual(again["id"], m["id"])                             # checkout + webhook → fulfilled once
        self.assertEqual(len(self.sent("membership_confirmed")), 2)
        with self.assertRaises(members.MemberError):
            members.order_membership(kabir["id"], 5)                       # not an offered duration

    def test_new_signup_buys_membership(self):
        code, _, _ = self.run_quiet(auth.request_login_code, "9812345678")
        uid = auth.user_for(auth.verify_login_code("9812345678", code, "New Member"))["id"]
        p, _ = payments.capture(self.run_quiet(members.order_membership, uid, 1)["payment_id"], {})
        self.run_quiet(members.complete_membership, p)
        self.assertEqual(access.entitlement(uid), "2026-10-27")
        self.assertIn("front desk", self.sent("membership_confirmed")[0]["body"])  # no fingerprint yet


class ReportTests(Base):
    def test_analytics_and_health(self):
        self.book_and_pay("B1", "20:00")
        self.run_quiet(bookings.create_by_staff, "K1", "2026-09-28", "19:00", 1, "Walk", "9888888888",
                       "desk_paid", "Desk")
        r = analytics.report(7)
        self.assertEqual(r["money"]["gross"], config.PRICES["badminton"] + config.PRICES["pickleball"])
        self.assertEqual(r["money"]["online"], config.PRICES["badminton"])
        self.assertEqual(r["bookings"]["by_source"], {"online": 1, "desk": 1})
        self.assertTrue(r["insights"])
        status = {c["area"]: c["status"] for c in health.checks()}
        self.assertEqual(status["Security settings"], "bad")               # demo password still set
        self.assertEqual(status["Automation (scheduler)"], "bad")          # never ticked in this test

    def test_prices(self):
        self.assertEqual(config.PRICES_PER_HOUR, {"badminton": 1000, "padel": 1800, "pickleball": 500})
        self.assertEqual(config.ONLINE_MEMBERSHIP_MONTHLY, 2000)
