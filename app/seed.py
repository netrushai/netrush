"""First-run data: courts, plans, the admin login, and (unless SEED_DEMO=0) a few demo people
so every screen has something on it — including a member whose plan ends in 7 days and one
ending today, to show both reminders firing."""
from datetime import timedelta

from . import clock, config


def seed(c):
    if c.execute("SELECT COUNT(*) FROM courts").fetchone()[0]:
        return
    from .auth import hash_password

    c.executemany("INSERT INTO courts(id,sport,name) VALUES(?,?,?)", config.COURTS)
    c.executemany(
        "INSERT INTO plans(name,sport,duration_days,price) VALUES(?,?,?,?)",
        [
            ("Badminton Monthly", "badminton", 30, 2500),
            ("Badminton Quarterly", "badminton", 90, 6500),
            ("Pickleball Monthly", "pickleball", 30, 3500),
            ("Padel Monthly", "padel", 30, 6000),
            ("All-Access Monthly", None, 30, 9000),
        ],
    )
    ensure_online_plan(c)  # plan 6: the membership sold in the portal
    now = clock.fmt(clock.now())
    c.execute(
        "INSERT INTO users(name,phone,is_admin,password_hash,created_at) VALUES(?,?,1,?,?)",
        ("Front Desk", config.ADMIN_PHONE, hash_password(config.ADMIN_PASSWORD), now),
    )
    if config.env("SEED_DEMO", "1") == "0":
        return

    t = clock.today()
    d = lambda n: (t + timedelta(days=n)).isoformat()
    people = [
        ("Aarav Mehta", "9000000001", "101"),
        ("Diya Sharma", "9000000002", "102"),
        ("Kabir Rao", "9000000003", None),
        ("Ishaan Verma", "9000000004", "104"),
    ]
    ids = []
    for name, phone, lock_id in people:
        cur = c.execute(
            "INSERT INTO users(name,phone,lock_user_id,created_at) VALUES(?,?,?,?)",
            (name, phone, lock_id, now),
        )
        ids.append(cur.lastrowid)
    # Aarav: ends in 7 days (7-day reminder). Diya: ends today (last-day reminder). Kabir: healthy.
    c.executemany(
        "INSERT INTO memberships(user_id,plan_id,start_date,end_date,created_at) VALUES(?,?,?,?,?)",
        [
            (ids[0], 1, d(-23), d(7), now),
            (ids[1], 5, d(-29), d(0), now),
            (ids[2], 3, d(-5), d(25), now),
        ],
    )
    cur = c.execute(
        "INSERT INTO academy_batches(name,coach,weekdays,start_time,end_time,court_ids,monthly_fee) "
        "VALUES(?,?,?,?,?,?,?)",
        ("Junior Badminton — Evening", "Coach Prakash", "0,2,4", "17:00", "19:00", "B1,B2", 3000),
    )
    c.execute(
        "INSERT INTO academy_enrollments(user_id,batch_id,level,fee_paid_until,created_at) VALUES(?,?,?,?,?)",
        (ids[3], cur.lastrowid, "Intermediate", d(7), now),
    )
    c.execute("UPDATE users SET guardian_name='Rohit Verma' WHERE id=?", (ids[3],))
    if config.env("SEED_HISTORY", "1") != "0":
        _history(c, t)
        _rich(c, t, ids)


def _rich(c, today, core_ids):
    """Dummy entries for every screen: more members (active, expiring, lapsed), three academy
    batches with players (one overdue), upcoming bookings of every kind with their door codes,
    a cash refund waiting at the desk, a court block, a month of door entries (so attendance and
    the access log fill up) and a message history."""
    import random
    import secrets as sec
    rnd = random.Random(11)
    now = clock.now()
    ts = clock.fmt(now)
    d = lambda n: (today + timedelta(days=n)).isoformat()
    at = lambda day_offset, hh, mm=0: f"{d(day_offset)} {hh:02d}:{mm:02d}"

    # ---- more members: (name, phone, lock id, plan id, start offset, end offset)
    extra = [
        ("Rohan Kapoor", "9000000011", "111", 6, -12, 17), ("Simran Kaur", "9000000012", "112", 2, -40, 49),
        ("Aditya Nair", "9000000013", "113", 1, -27, 2), ("Priya Menon", "9000000014", "114", 4, -8, 21),
        ("Farhan Sheikh", "9000000015", "115", 5, -36, -7), ("Neha Gupta", "9000000016", None, 6, -2, 27),
        ("Varun Iyer", "9000000017", "117", 3, -55, -26), ("Kavya Reddy", "9000000018", "118", 2, -70, 19),
        # evening badminton regulars (fill the 7 PM members' slot)
        ("Sanjay Kulkarni", "9000000031", "131", 1, -10, 20), ("Ritu Agarwal", "9000000032", "132", 2, -30, 60),
        ("Manish Tiwari", "9000000033", "133", 1, -15, 15), ("Lakshmi Iyer", "9000000034", "134", 1, -3, 27),
        ("Gaurav Bhatia", "9000000035", "135", 2, -60, 30), ("Harsh Vardhan", "9000000036", "136", 1, -20, 10),
        ("Pallavi Deshmukh", "9000000037", "137", 1, -6, 24),
    ]
    member_ids = list(core_ids[:3])
    for name, phone, lock, plan, s0, e0 in extra:
        uid = c.execute("INSERT INTO users(name,phone,email,lock_user_id,created_at) VALUES(?,?,?,?,?)",
                        (name, phone, name.split()[0].lower() + "@example.com", lock, ts)).lastrowid
        mid = c.execute("INSERT INTO memberships(user_id,plan_id,start_date,end_date,created_at) VALUES(?,?,?,?,?)",
                        (uid, plan, d(s0), d(e0), at(s0, 11))).lastrowid
        price = c.execute("SELECT price FROM plans WHERE id=?", (plan,)).fetchone()[0] if plan != 6 \
            else config.ONLINE_MEMBERSHIP_MONTHLY
        provider = "mock" if plan == 6 else "desk"
        c.execute("INSERT INTO payments(kind,ref_id,user_id,phone,amount,provider,payment_id,status,created_at,paid_at) "
                  "VALUES('membership',?,?,?,?,?,?,'paid',?,?)",
                  (mid, uid, phone, price, provider, f"DEMO-M{mid}", at(s0, 11), at(s0, 11)))
        member_ids.append(uid)

    # ---- badminton members' daily slots. 7 PM is full (6 + 6), with three people waiting; Farhan's
    # plan ended a week ago, so the first scheduler tick frees his place and suggests the next in line.
    # 6 AM has room: Court 2 is the morning academy's, so only Court 1 (6 places) is open to members.
    uid_of = lambda phone: c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()[0]
    for court, time, phones in [
        ("B1", "19:00", ["9000000015", "9000000001", "9000000012", "9000000013", "9000000011", "9000000018"]),
        ("B2", "19:00", ["9000000016", "9000000031", "9000000032", "9000000033", "9000000034", "9000000035"]),
        ("B1", "06:00", ["9000000002", "9000000036", "9000000037"]),
    ]:
        for phone in phones:
            c.execute("INSERT INTO member_slots(user_id,court_id,start_time,created_at) VALUES(?,?,?,?)",
                      (uid_of(phone), court, time, at(-20, 11)))
    for name, phone, time, note, ago in [
        ("Rakesh Jain", "9822200001", "19:00", "Plays with his son, wants evenings", 9),
        ("Swati Bhosale", "9822200002", "19:00", "Called twice", 6),
        ("Imran Qureshi", "9822200003", "19:00", None, 2),
        ("Anjali Nair", "9822200004", "06:00", "Prefers early mornings", 1),
    ]:
        c.execute("INSERT INTO enquiries(name,phone,start_time,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
                  (name, phone, time, note, "Front Desk", at(-ago, 12)))

    # ---- a guest who booked a court, then created an account: their welcome coupon is waiting
    sneha = c.execute("INSERT INTO users(name,phone,email,created_at) VALUES('Sneha Patil','9811100002',"
                      "'sneha@example.com',?)", (at(-1, 21),)).lastrowid
    code = "WELCOME-" + "".join(rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))
    c.execute("INSERT INTO coupons(code,user_id,phone,pct,created_at) VALUES(?,?,?,?,?)",
              (code, sneha, "9811100002", config.WELCOME_COUPON_PCT, at(-1, 21)))
    for ch in ("sms", "whatsapp"):
        c.execute("INSERT INTO notifications(kind,channel,to_phone,body,status,created_at) "
                  "VALUES('welcome_coupon',?,'9811100002',?,'sent',?)",
                  (ch, f"Welcome to {config.FACILITY_NAME}, Sneha! Here's {config.WELCOME_COUPON_PCT}% off your next "
                   f"court booking (badminton, pickleball or padel): use code {code} at {config.PUBLIC_URL} "
                   f"with this mobile number.", at(-1, 21)))

    # ---- academy: two more batches, more players
    b_morning = c.execute(
        "INSERT INTO academy_batches(name,coach,weekdays,start_time,end_time,court_ids,monthly_fee) "
        "VALUES('Adult Badminton - Early Birds','Coach Meenakshi','1,3,5','06:00','07:00','B2',2500)").lastrowid
    b_pickle = c.execute(
        "INSERT INTO academy_batches(name,coach,weekdays,start_time,end_time,court_ids,monthly_fee) "
        "VALUES('Pickleball Starters','Coach Daniel','5,6','08:00','09:00','K1',2000)").lastrowid
    junior = 1
    players = [("Anika Sharma", "9000000021", "121", junior, "Beginner", "Sunil Sharma", 19),
               ("Vihaan Joshi", "9000000022", "122", junior, "Advanced", "Pooja Joshi", 12),
               ("Myra Pillai", "9000000023", "123", junior, "Beginner", "Arun Pillai", -4),  # overdue
               ("Rajesh Kumar", "9000000024", "124", b_morning, "Intermediate", None, 23),
               ("Sunita Rao", "9000000025", "125", b_morning, "Beginner", None, 2),
               ("Tom Mathew", "9000000026", "126", b_pickle, "Beginner", None, 15)]
    academy_ids = [core_ids[3]]
    for name, phone, lock, batch, level, guardian, paid_until in players:
        uid = c.execute("INSERT INTO users(name,phone,lock_user_id,guardian_name,created_at) VALUES(?,?,?,?,?)",
                        (name, phone, lock, guardian, ts)).lastrowid
        eid = c.execute("INSERT INTO academy_enrollments(user_id,batch_id,level,fee_paid_until,created_at) "
                        "VALUES(?,?,?,?,?)", (uid, batch, level, d(paid_until), at(-40, 10))).lastrowid
        fee = c.execute("SELECT monthly_fee FROM academy_batches WHERE id=?", (batch,)).fetchone()[0]
        c.execute("INSERT INTO payments(kind,ref_id,user_id,phone,amount,provider,payment_id,status,created_at,paid_at) "
                  "VALUES('academy',?,?,?,?,'desk',?,'paid',?,?)",
                  (eid, uid, phone, fee, f"DEMO-A{eid}", at(paid_until - 30, 17), at(paid_until - 30, 17)))
        academy_ids.append(uid)
    c.execute("UPDATE users SET email='aarav@example.com' WHERE id=?", (core_ids[0],))

    # ---- upcoming bookings of every kind (with door codes already issued, no message spam)
    kinds = [  # (day, hour, court, name, phone, pay_status, source)
        (0, 20, "B1", "Rahul Desai", "9811100001", "paid", "online"),
        (0, 21, "P1", "Sneha Patil", "9811100002", "paid", "online"),
        (0, 19, "K1", "Arjun Malhotra", "9811100003", "unpaid", "phone"),
        (0, 20, "K2", "Aarav Mehta", "9000000001", "paid", "online"),
        (1, 7, "B1", "Meera Joshi", "9811100004", "paid", "desk"),
        (1, 21, "B2", "Vikram Singh", "9811100005", "unpaid", "phone"),
        (1, 19, "P1", "Ananya Bose", "9811100006", "paid", "online"),
        (1, 20, "K1", "Coach Daniel (demo class)", "9811100007", "comp", "desk"),
        (2, 21, "B1", "Karan Mehra", "9811100008", "paid", "online"),
        (2, 20, "P1", "Pooja Iyer", "9811100009", "paid", "online"),
        (3, 21, "K2", "Nikhil Rao", "9811100010", "paid", "desk"),
    ]
    for day, hour, court, name, phone, pay, source in kinds:
        start = now.replace(hour=hour, minute=0) + timedelta(days=day)
        if start <= now + timedelta(hours=1) or hour >= config.CLOSE_HOUR:
            start += timedelta(days=1)  # keep every demo booking in the future
        sport = next(s for cid, s, _ in config.COURTS if cid == court)
        amount = config.PRICES_PER_HOUR[sport]
        bid = c.execute(
            "INSERT INTO bookings(ref,court_id,name,phone,email,start,end,amount,status,created_at,pay_status,source,"
            "paid_at,payment_ref,access_key) VALUES(?,?,?,?,?,?,?,?,'confirmed',?,?,?,?,?,?)",
            (sec.token_hex(3).upper(), court, name, phone, name.split()[0].lower() + "@example.com",
             clock.fmt(start), clock.fmt(start + timedelta(hours=1)), amount, ts, pay, source,
             ts if pay == "paid" else None, f"DEMO-{source}", sec.token_urlsafe(12))).lastrowid
        if pay == "paid":
            c.execute("INSERT INTO payments(kind,ref_id,phone,amount,provider,payment_id,status,created_at,paid_at) "
                      "VALUES('booking',?,?,?,?,?,'paid',?,?)",
                      (bid, phone, amount, "mock" if source == "online" else "desk", f"DEMO-B{bid}", ts, ts))
        c.execute("INSERT INTO door_codes(booking_id,code,valid_from,valid_to,status,created_at) "
                  "VALUES(?,?,?,?,'active',?)",
                  (bid, f"{rnd.randrange(100000, 999999)}", clock.fmt(start - timedelta(minutes=config.DOOR_CODE_LEAD_MIN)),
                   clock.fmt(start + timedelta(hours=1)), ts))
    # a cancelled cash booking whose refund is waiting at the desk
    start = now.replace(minute=0) + timedelta(days=2, hours=2)
    bid = c.execute(
        "INSERT INTO bookings(ref,court_id,name,phone,start,end,amount,status,created_at,pay_status,source,"
        "cancelled_at,cancelled_by,refund_amount) VALUES(?,?,?,?,?,?,?,'cancelled',?,'paid','desk',?,'staff',?)",
        ("RFD001", "K2", "Deepak Verma", "9811100011", clock.fmt(start), clock.fmt(start + timedelta(hours=1)),
         config.PRICES_PER_HOUR["pickleball"], ts, ts, config.PRICES_PER_HOUR["pickleball"])).lastrowid
    c.execute("INSERT INTO payments(kind,ref_id,phone,amount,provider,payment_id,status,created_at,paid_at,"
              "refund_status,refund_amount) VALUES('booking',?,?,?,'desk','DEMO-CASH','paid',?,?,'manual',?)",
              (bid, "9811100011", config.PRICES_PER_HOUR["pickleball"], ts, ts, config.PRICES_PER_HOUR["pickleball"]))
    c.execute("INSERT INTO court_blocks(court_id,date,start,end,reason) VALUES('P1',?,'06:00','08:00',"
              "'Net replacement')", (d(1),))

    # ---- a month of door entries: fingerprints (members, academy) and booking codes
    fp_users = [(u, c.execute("SELECT lock_user_id FROM users WHERE id=?", (u,)).fetchone()[0])
                for u in member_ids + academy_ids]
    for back in range(30, 0, -1):
        for uid, lock in fp_users:
            if lock and rnd.random() < (.55 if uid in academy_ids else .35):
                c.execute("INSERT INTO access_events(at,device_id,method,user_id,granted,detail) "
                          "VALUES(?, 'main-door', 'fingerprint', ?, 1, 'member')",
                          (at(-back, rnd.choice([6, 7, 17, 18, 19, 20]), rnd.randint(0, 59)), uid))
    for b in c.execute("SELECT id, start FROM bookings WHERE status='confirmed' AND end < ? ORDER BY start "
                       "DESC LIMIT 120", (ts,)).fetchall():
        if rnd.random() < .85:
            s = clock.parse(b["start"]) - timedelta(minutes=rnd.randint(1, 9))
            c.execute("INSERT INTO access_events(at,device_id,method,booking_id,granted,detail) "
                      "VALUES(?, 'main-door', 'code', ?, 1, 'booking code')", (clock.fmt(s), b["id"]))
    denied = [(at(-1, 21, 14), "fingerprint", None, "unknown fingerprint id"),
              (at(-2, 19, 3), "code", None, "wrong or expired code"),
              (at(-3, 22, 41), "code", None, "keypad locked out (too many wrong codes)"),
              (at(-5, 18, 20), "fingerprint", c.execute("SELECT id FROM users WHERE phone='9000000015'").fetchone()[0],
               "membership expired")]
    for when, method, uid, detail in denied:
        c.execute("INSERT INTO access_events(at,device_id,method,user_id,granted,detail) VALUES(?,?,?,?,0,?)",
                  (when, "main-door", method, uid, detail))

    # ---- message history (what customers were sent in the last few days)
    msgs = [
        (-6, "membership_reminder", "9000000013",
         "Hi Aditya, your Badminton Monthly membership ends in 7 days. Renew online at " + config.PUBLIC_URL
         + "/portal or at the desk to keep your fingerprint access active."),
        (-7, "membership_reminder", "9000000015",
         "Hi Farhan, your All-Access Monthly membership ends today. Renew online at " + config.PUBLIC_URL
         + "/portal or at the desk to keep your fingerprint access active."),
        (-4, "academy_fee_reminder", "9000000023",
         "Hi Myra, academy fees for Junior Badminton are paid until " + d(-4) + " and ends today. "
         "Please renew to continue training."),
        (-1, "booking_confirmed", "9811100002", "Hi Sneha, booking confirmed: Padel Court. "
         "Your door entry code follows in the next message."),
        (-1, "refund_issued", "9811100012", "Rs 1800 has been refunded for your booking. "
         "It reaches your account in 5-7 working days."),
    ]
    for day, kind, phone, body in msgs:
        for ch in ("sms", "whatsapp"):
            c.execute("INSERT INTO notifications(kind,channel,to_phone,body,status,created_at) VALUES(?,?,?,?,'sent',?)",
                      (kind, ch, phone, body, at(day, 10, 0)))
    c.execute("INSERT INTO notifications(kind,channel,to_phone,body,status,error,created_at) "
              "VALUES('booking_confirmed','whatsapp','9811100013','(demo) failed message','failed',"
              "'Recipient is not on WhatsApp',?)", (at(-2, 18, 5),))
    # door codes for seeded bookings were created silently; don't send them again on startup
    for (bid,) in c.execute("SELECT booking_id FROM door_codes").fetchall():
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?, '1')", (f"door_reminded:{bid}",))


def _history(c, today):
    """30 days of past bookings and payments so analytics has something to show. Evenings busy,
    padel pricey and quieter, a few cancellations, a mix of online, desk and phone bookings."""
    import random
    rnd = random.Random(7)
    names = ["Rahul", "Sneha", "Arjun", "Meera", "Vikram", "Ananya", "Karan", "Pooja", "Nikhil", "Tara"]
    demand = {6: .5, 7: .6, 8: .3, 9: .15, 10: .1, 11: .08, 12: .05, 13: .05, 14: .06, 15: .1, 16: .2,
              17: .45, 18: .7, 19: .85, 20: .8, 21: .6, 22: .3}
    sport_boost = {"badminton": 1.0, "pickleball": .8, "padel": .55}
    n = 0
    for back in range(30, 0, -1):
        day = today - timedelta(days=back)
        for cid, sport, _ in config.COURTS:
            for hour, p in demand.items():
                if hour >= config.CLOSE_HOUR or hour < config.OPEN_HOUR:
                    continue
                if sport == "badminton" and day.weekday() in (0, 2, 4) and 17 <= hour < 19:
                    continue  # academy
                if rnd.random() > p * sport_boost[sport]:
                    continue
                n += 1
                start = f"{day.isoformat()} {hour:02d}:00"
                end = f"{day.isoformat()} {hour + 1:02d}:00"
                amount = config.PRICES_PER_HOUR[sport]
                source = rnd.choices(["online", "desk", "phone"], [70, 18, 12])[0]
                cancelled = rnd.random() < .07
                phone = f"97{rnd.randrange(10 ** 8):08d}"
                booked_at = f"{(day - timedelta(days=rnd.randint(0, 3))).isoformat()} {rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}"
                bid = c.execute(
                    "INSERT INTO bookings(ref,court_id,name,phone,start,end,amount,status,created_at,pay_status,source,"
                    "paid_at,cancelled_at,cancelled_by,refund_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"H{n:05d}", cid, rnd.choice(names), phone, start, end, amount,
                     "cancelled" if cancelled else "confirmed", booked_at, "paid", source, booked_at,
                     booked_at if cancelled else None, "customer" if cancelled else None,
                     amount if cancelled else 0)).lastrowid
                provider = "mock" if source == "online" else "desk"
                c.execute(
                    "INSERT INTO payments(kind,ref_id,phone,amount,provider,payment_id,status,created_at,paid_at,"
                    "refund_status,refund_amount,refunded_at) VALUES('booking',?,?,?,?,?,'paid',?,?,?,?,?)",
                    (bid, phone, amount, provider, f"HIST-{bid}", booked_at, booked_at,
                     "done" if cancelled else None, amount if cancelled else 0, booked_at if cancelled else None))
    # membership and academy money from the last month
    for i, (uid, amt, kind) in enumerate([(1, 2500, "membership"), (2, 9000, "membership"), (3, 3500, "membership"),
                                          (4, 3000, "academy")]):
        paid = f"{(today - timedelta(days=[23, 29, 5, 23][i])).isoformat()} 11:00"
        c.execute("INSERT INTO payments(kind,ref_id,user_id,amount,provider,payment_id,status,created_at,paid_at) "
                  "VALUES(?,?,?,?,'desk','HIST','paid',?,?)", (kind, i + 1, uid + 1, amt, paid, paid))


ONLINE_PLAN = "Monthly Membership (online)"


def ensure_online_plan(c):
    """The plan sold in the member portal. Kept in step with ONLINE_MEMBERSHIP_MONTHLY."""
    row = c.execute("SELECT id FROM plans WHERE name=?", (ONLINE_PLAN,)).fetchone()
    if row:
        c.execute("UPDATE plans SET price=? WHERE id=?", (config.ONLINE_MEMBERSHIP_MONTHLY, row[0]))
    else:
        c.execute("INSERT INTO plans(name,sport,duration_days,price) VALUES(?,NULL,30,?)",
                  (ONLINE_PLAN, config.ONLINE_MEMBERSHIP_MONTHLY))


def online_plan_id():
    from . import db
    return db.one("SELECT id FROM plans WHERE name=?", ONLINE_PLAN)["id"]
