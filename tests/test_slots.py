"""Badminton members' slots (6 per court), the priority list, and welcome coupons.

    python -m unittest discover -s tests -v
"""
from datetime import date, timedelta

from app import auth, bookings, clock, config, coupons, db, members, slots
from tests.test_core import T0, Base


class SlotTests(Base):
    def member(self, n, plan=1):
        uid = self.run_quiet(members.upsert_person, f"Player {n}", f"97000000{n:02d}")
        self.run_quiet(members.add_membership, uid, plan)
        return uid

    def fill(self, time="19:00", n=12):
        return [slots.assign(self.member(i), time)["slot"] for i in range(n)]

    def test_six_per_court_courts_fill_in_order(self):
        got = self.fill()
        self.assertEqual([s["court_id"] for s in got], ["B1"] * 6 + ["B2"] * 6)
        with self.assertRaises(slots.SlotError):
            slots.assign(self.member(50), "19:00")                         # 13th: full
        grid = bookings.availability("badminton", date(2026, 9, 29))
        row = next(r for r in grid["slots"] if r["start"] == "19:00")
        self.assertEqual(row["cells"], {"B1": "blocked:Members", "B2": "blocked:Members"})
        with self.assertRaises(bookings.BookingError):
            self.run_quiet(bookings.create, "B2", "2026-09-29", "19:00", 1, "Guest", "9811111111")

    def test_second_court_stays_bookable_until_needed(self):
        slots.assign(self.member(1), "20:00")
        row = next(r for r in bookings.availability("badminton", date(2026, 9, 29))["slots"] if r["start"] == "20:00")
        self.assertEqual(row["cells"]["B2"], "free")
        self.run_quiet(bookings.create, "B2", "2026-09-29", "20:00", 1, "Guest", "9811111111")

    def test_rules(self):
        kabir = db.one("SELECT id FROM users WHERE phone='9000000003'")["id"]  # pickleball plan
        with self.assertRaises(slots.SlotError):
            slots.assign(kabir, "19:00")
        with self.assertRaises(slots.SlotError):
            slots.assign(self.member(1), "17:00")                          # both courts: junior academy
        diya = db.one("SELECT id FROM users WHERE phone='9000000002'")["id"]   # all-access counts
        self.assertEqual(slots.assign(diya, "06:00")["slot"]["court_id"], "B1")

    def test_warns_about_public_bookings_already_made(self):
        self.book_and_pay("B1", "21:00", "2026-09-30")
        r = slots.assign(self.member(1), "21:00")
        self.assertEqual(r["slot"]["court_id"], "B2")                      # picks the court with no clash
        self.assertEqual(r["warnings"], [])

    def test_priority_list_and_next_in_line(self):
        got = self.fill()
        a = slots.add_enquiry("Asha Rao", "9822200001", "19:00")
        b = slots.add_enquiry("Bala Iyer", "9822200002", "19:00")
        self.assertEqual((a["position"], a["full"], b["position"]), (1, True, 2))
        with self.assertRaises(slots.SlotError):
            slots.add_enquiry("Asha Rao", "9822200001", "19:00")           # already waiting
        self.assertEqual(slots.board()["suggestions"], [])

        slots.release(got[3]["id"])                                        # someone leaves
        sugg = slots.board()["suggestions"]
        self.assertEqual([s["name"] for s in sugg], ["Asha Rao"])          # only the first in line
        self.assertIn("7–8 PM", sugg[0]["text"])
        self.assertTrue(sugg[0]["whatsapp"].startswith("https://wa.me/919822200001?text="))

        slots.set_enquiry(a["enquiry"]["id"], "offered")                   # place held for Asha
        self.assertEqual(slots.board()["suggestions"], [])
        with self.assertRaises(slots.SlotError):
            slots.assign(self.member(60), "19:00")                         # can't jump the queue
        slots.set_enquiry(a["enquiry"]["id"], "closed")                    # Asha declined
        self.assertEqual([s["name"] for s in slots.board()["suggestions"]], ["Bala Iyer"])

        bala = self.run_quiet(members.upsert_person, "Bala Iyer", "9822200002")
        self.run_quiet(members.add_membership, bala, 1)
        self.assertEqual(slots.assign(bala, "19:00")["slot"]["court_id"], "B1")
        self.assertEqual(db.one("SELECT status FROM enquiries WHERE id=?", b["enquiry"]["id"])["status"], "joined")

    def test_offered_person_can_take_their_held_place(self):
        got = self.fill()
        e = slots.add_enquiry("Asha Rao", "9822200001", "19:00")
        slots.release(got[0]["id"])
        slots.set_enquiry(e["enquiry"]["id"], "offered")
        asha = self.run_quiet(members.upsert_person, "Asha Rao", "9822200001")
        self.run_quiet(members.add_membership, asha, 1)
        slots.assign(asha, "19:00")

    def test_lapsed_membership_frees_the_slot(self):
        diya = db.one("SELECT id FROM users WHERE phone='9000000002'")["id"]   # plan ends today
        slots.assign(diya, "19:00")
        self.assertEqual(slots.release_lapsed(), [])
        clock.set_now(T0 + timedelta(days=1))
        self.assertEqual(len(slots.release_lapsed()), 1)
        self.assertIsNone(slots.for_user(diya))
        self.assertEqual(slots.board()["released"][0]["released_why"], "membership ended")

    def test_moving_slot(self):
        uid = self.member(1)
        slots.assign(uid, "19:00")
        slots.assign(uid, "20:00")
        self.assertEqual(slots.for_user(uid)["start_time"], "20:00")
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM member_slots WHERE status='active'")["n"], 1)


class CouponTests(Base):
    def signup(self, phone="9812345678"):
        code, _, _ = self.run_quiet(auth.request_login_code, phone)
        uid = auth.user_for(self.run_quiet(auth.verify_login_code, phone, code, "Nisha Shah"))["id"]
        return uid, coupons.for_user(uid)

    def test_signup_gets_coupon_for_any_sport(self):
        uid, c = self.signup()
        self.assertEqual((c["pct"], c["status"]), (config.WELCOME_COUPON_PCT, "active"))
        self.assertIn(c["code"], self.sent("welcome_coupon")[0]["body"])
        with self.assertRaises(bookings.BookingError):                     # someone else's number
            self.run_quiet(bookings.create, "P1", "2026-09-29", "10:00", 1, "X", "9822222222", None, c["code"])
        out = self.run_quiet(bookings.create, "P1", "2026-09-29", "10:00", 1, "Nisha", "9812345678", None,
                             c["code"].lower())
        price = config.PRICES["padel"]
        self.assertEqual(out["booking"]["amount"], price - price * 10 // 100)
        self.assertEqual(out["payment"]["amount"], out["booking"]["amount"])
        with self.assertRaises(bookings.BookingError):                     # one use only
            self.run_quiet(bookings.create, "K1", "2026-09-29", "10:00", 1, "Nisha", "9812345678", None, c["code"])

    def test_unpaid_booking_gives_coupon_back(self):
        _, c = self.signup()
        self.run_quiet(bookings.create, "K1", "2026-09-28", "20:00", 1, "Nisha", "9812345678", None, c["code"])
        clock.set_now(T0 + timedelta(minutes=config.HOLD_MIN + 1))
        bookings.expire_holds()
        self.assertEqual(coupons.for_user(_)["status"], "active")
        b = self.run_quiet(bookings.create, "B1", "2026-09-28", "20:00", 1, "Nisha", "9812345678", None, c["code"])
        self.run_quiet(bookings.confirm_payment, b["booking"]["id"], {})
        self.assertEqual(coupons.for_user(_)["status"], "used")

    def test_new_people_only_staff_added_or_self_signup(self):
        code, _, _ = self.run_quiet(auth.request_login_code, "9000000001")  # existing member logging in
        self.run_quiet(auth.verify_login_code, "9000000001", code)
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM coupons")["n"], 0)
        uid = self.run_quiet(members.upsert_person, "Desk Added", "9833333333")  # new person added by staff
        self.assertEqual(coupons.for_user(uid)["status"], "active")
        self.assertEqual(len(self.sent("welcome_coupon")), 2)              # SMS + WhatsApp
        self.run_quiet(members.upsert_person, "Desk Added Edited", "9833333333")  # editing them later: no second one
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM coupons")["n"], 1)
