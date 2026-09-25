"""Members, academy players, their status page, and the expiry reminders."""
from datetime import date as Date, timedelta

import json

from . import access, clock, config, db, notify, payments, seed


class MemberError(Exception):
    pass


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- status (portal)

def _state(end_date, today):
    left = (Date.fromisoformat(end_date) - today).days
    if left < 0:
        return "expired", left
    if left <= max(config.REMINDER_DAYS):
        return "expiring", left
    return "active", left


def status(user_id):
    today = clock.today()
    u = db.one("SELECT id,name,phone,email,sms_opt,wa_opt,lock_user_id,lock_enabled,guardian_name "
               "FROM users WHERE id=?", user_id)
    ms = db.all_("SELECT m.*, p.name AS plan, p.sport FROM memberships m JOIN plans p ON p.id=m.plan_id "
                 "WHERE m.user_id=? AND m.status='active' ORDER BY m.end_date DESC", user_id)
    for m in ms:
        m["state"], m["days_left"] = _state(m["end_date"], today)
        if m["start_date"] > today.isoformat():
            m["state"] = "upcoming"
    academy = db.all_(
        "SELECT e.*, b.name AS batch, b.coach, b.weekdays, b.start_time, b.end_time, b.monthly_fee "
        "FROM academy_enrollments e JOIN academy_batches b ON b.id=e.batch_id "
        "WHERE e.user_id=? AND e.status='active'", user_id)
    since = (today - timedelta(days=30)).isoformat()
    for a in academy:
        a["fee_state"], a["fee_days_left"] = _state(a["fee_paid_until"], today)
        a["days"] = ", ".join(WEEKDAYS[int(d)] for d in a["weekdays"].split(","))
        joined = Date.fromisoformat(a["created_at"][:10])
        a["sessions_30d"] = _sessions_scheduled(a["weekdays"], max(joined, today - timedelta(days=30)), today)
    attended = db.one("SELECT COUNT(DISTINCT substr(at,1,10)) AS n FROM access_events "
                      "WHERE user_id=? AND granted=1 AND method='fingerprint' AND at>=?", user_id, since)["n"]
    from . import bookings as bk
    bookings = db.all_("SELECT b.*, c.name AS court FROM bookings b "
                       "JOIN courts c ON c.id=b.court_id WHERE b.phone=? AND b.status='confirmed' AND b.end>=? "
                       "ORDER BY b.start LIMIT 10", u["phone"], clock.fmt(clock.now()))
    for b in bookings:
        b["cancel"] = bk.refund_quote(b, "customer")
    entries = db.all_("SELECT at, method, granted FROM access_events WHERE user_id=? ORDER BY at DESC LIMIT 10",
                      user_id)
    until = access.entitlement(user_id)
    from .auth import member_id
    u["member_id"] = member_id(u["id"])
    return {
        "user": u,
        "memberships": ms,
        "academy": academy,
        "days_attended_30d": attended,
        "fingerprint": {"enrolled": bool(u["lock_user_id"]), "active": bool(until), "until": until},
        "upcoming_bookings": bookings,
        "recent_entries": entries,
        "membership_offer": {"plan": seed.ONLINE_PLAN, "monthly": config.ONLINE_MEMBERSHIP_MONTHLY,
                             "months": config.ONLINE_MEMBERSHIP_MONTH_OPTIONS,
                             "starts": _next_start(user_id).isoformat()},
        "refund_policy": bk.refund_policy_text(),
    }


def _sessions_scheduled(weekdays, frm, to):
    days = {int(d) for d in weekdays.split(",")}
    n, d = 0, frm
    while d <= to:
        n += d.weekday() in days
        d += timedelta(days=1)
    return n


# ---------------------------------------------------------------- admin operations

def upsert_person(name, phone, email=None, lock_user_id=None, guardian_name=None, sms_opt=1, wa_opt=1):
    from .auth import normalize_phone
    phone = normalize_phone(phone)
    if not (name or "").strip():
        raise MemberError("Name is required")
    existing = db.one("SELECT id FROM users WHERE phone=?", phone)
    c = db.conn()
    if existing:
        c.execute("UPDATE users SET name=?, email=?, lock_user_id=?, guardian_name=?, sms_opt=?, wa_opt=? WHERE id=?",
                  (name.strip(), email, lock_user_id or None, guardian_name, int(sms_opt), int(wa_opt),
                   existing["id"]))
        uid = existing["id"]
    else:
        uid = c.execute("INSERT INTO users(name,phone,email,lock_user_id,guardian_name,sms_opt,wa_opt,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (name.strip(), phone, email, lock_user_id or None, guardian_name, int(sms_opt), int(wa_opt),
                         clock.fmt(clock.now()))).lastrowid
    # Claim any bookings made with this phone before they were registered.
    c.execute("UPDATE bookings SET user_id=? WHERE phone=? AND user_id IS NULL", (uid, phone))
    access.sync_member_access()
    return uid


def _next_start(user_id):
    """A renewal starts the day after the current plan ends, so paying early never loses days."""
    today = clock.today()
    cur = db.one("SELECT MAX(end_date) AS e FROM memberships WHERE user_id=? AND status='active' AND end_date>=?",
                 user_id, today.isoformat())["e"]
    return Date.fromisoformat(cur) + timedelta(days=1) if cur else today


def _insert_membership(c, user_id, plan, months):
    start = _next_start(user_id)
    end = start + timedelta(days=plan["duration_days"] * months - 1)
    return c.execute("INSERT INTO memberships(user_id,plan_id,start_date,end_date,created_at) VALUES(?,?,?,?,?)",
                     (user_id, plan["id"], start.isoformat(), end.isoformat(), clock.fmt(clock.now()))).lastrowid


def add_membership(user_id, plan_id, months=1, staff=None):
    """Sold at the desk (records the cash/UPI payment) — new or renewal."""
    plan = db.one("SELECT * FROM plans WHERE id=?", plan_id)
    if not plan:
        raise MemberError("Unknown plan")
    months = int(months)
    if not 1 <= months <= 12:
        raise MemberError("1 to 12 months")
    with db.tx() as c:
        mid = _insert_membership(c, user_id, plan, months)
    u = db.one("SELECT phone FROM users WHERE id=?", user_id)
    payments.record_desk("membership", mid, plan["price"] * months, u["phone"], user_id, staff or "desk",
                         {"months": months})
    access.sync_member_access()
    return db.one("SELECT * FROM memberships WHERE id=?", mid)


def order_membership(user_id, months):
    """Portal: start an online payment for the online monthly plan."""
    months = int(months)
    if months not in config.ONLINE_MEMBERSHIP_MONTH_OPTIONS:
        raise MemberError("Pick one of the offered durations")
    u = db.one("SELECT id, phone FROM users WHERE id=?", user_id)
    return payments.create_order("membership", config.ONLINE_MEMBERSHIP_MONTHLY * months, f"MEM-{user_id}",
                                 u["phone"], user_id, meta={"months": months})


def complete_membership(p):
    """Payment for a membership has been verified (checkout or webhook): create it, exactly once."""
    plan = db.one("SELECT * FROM plans WHERE id=?", seed.online_plan_id())
    months = json.loads(p["meta"] or "{}").get("months", 1)
    with db.tx() as c:
        row = c.execute("SELECT ref_id FROM payments WHERE id=?", (p["id"],)).fetchone()
        if row["ref_id"]:
            return db.one("SELECT * FROM memberships WHERE id=?", row["ref_id"])  # already fulfilled
        mid = _insert_membership(c, p["user_id"], plan, months)
        c.execute("UPDATE payments SET ref_id=? WHERE id=?", (mid, p["id"]))
    access.sync_member_access()
    m = db.one("SELECT * FROM memberships WHERE id=?", mid)
    u = db.one("SELECT name, phone, lock_user_id FROM users WHERE id=?", p["user_id"])
    notify.send("membership_confirmed", u["phone"], {
        "name": u["name"].split()[0], "plan": plan["name"],
        "end_date": Date.fromisoformat(m["end_date"]).strftime("%d %b %Y"),
        "access": "Your fingerprint entry is active." if u["lock_user_id"] else
        "Visit the front desk once to register your fingerprint for door entry.",
    }, dedupe_key=f"memok:{mid}")
    return m


def enroll(user_id, batch_id, level=None, months=1, staff=None):
    b = db.one("SELECT id, monthly_fee FROM academy_batches WHERE id=?", batch_id)
    if not b:
        raise MemberError("Unknown batch")
    until = clock.today() + timedelta(days=30 * int(months) - 1)
    eid = db.conn().execute("INSERT INTO academy_enrollments(user_id,batch_id,level,fee_paid_until,created_at) "
                            "VALUES(?,?,?,?,?)", (user_id, batch_id, level, until.isoformat(),
                                                  clock.fmt(clock.now()))).lastrowid
    _record_fee(eid, int(months), staff)
    access.sync_member_access()
    return db.one("SELECT * FROM academy_enrollments WHERE id=?", eid)


def _record_fee(enrollment_id, months, staff):
    e = db.one("SELECT e.user_id, u.phone, b.monthly_fee FROM academy_enrollments e JOIN users u ON u.id=e.user_id "
               "JOIN academy_batches b ON b.id=e.batch_id WHERE e.id=?", enrollment_id)
    payments.record_desk("academy", enrollment_id, e["monthly_fee"] * months, e["phone"], e["user_id"],
                         staff or "desk", {"months": months})


def pay_academy_fee(enrollment_id, months=1, staff=None):
    e = db.one("SELECT * FROM academy_enrollments WHERE id=?", enrollment_id)
    if not e:
        raise MemberError("Unknown enrollment")
    base = max(Date.fromisoformat(e["fee_paid_until"]), clock.today() - timedelta(days=1))
    until = base + timedelta(days=30 * int(months))
    db.conn().execute("UPDATE academy_enrollments SET fee_paid_until=? WHERE id=?", (until.isoformat(), enrollment_id))
    _record_fee(enrollment_id, int(months), staff)
    access.sync_member_access()
    return until.isoformat()


# ---------------------------------------------------------------- reminders

def _when(days_left):
    return "ends today" if days_left == 0 else ("ends tomorrow" if days_left == 1 else f"ends in {days_left} days")


def _bucket(days_left):
    """Which reminder a plan with `days_left` is due for. With REMINDER_DAYS=7,0: 1–7 days → the
    7-day reminder, 0 → last-day. Using a range (not ==7) means a day of downtime delays a
    reminder instead of skipping it; the dedupe key still limits it to one per bucket."""
    ths = sorted(config.REMINDER_DAYS, reverse=True)
    for i, th in enumerate(ths):
        lower = ths[i + 1] if i + 1 < len(ths) else -1
        if lower < days_left <= th:
            return th
    return None


def send_reminders():
    today = clock.today()
    horizon = (today + timedelta(days=max(config.REMINDER_DAYS))).isoformat()
    sent = []
    for m in db.all_(
        "SELECT m.id, m.user_id, m.end_date, p.name AS plan, u.name, u.phone FROM memberships m "
        "JOIN plans p ON p.id=m.plan_id JOIN users u ON u.id=m.user_id "
        "WHERE m.status='active' AND m.end_date BETWEEN ? AND ? AND m.start_date<=?",
        today.isoformat(), horizon, today.isoformat()):
        # Already renewed (a later plan exists)? Then this isn't really expiring — stay quiet.
        if db.one("SELECT 1 AS x FROM memberships WHERE user_id=? AND status='active' AND end_date>?",
                  m["user_id"], m["end_date"]):
            continue
        left = (Date.fromisoformat(m["end_date"]) - today).days
        b = _bucket(left)
        if b is None:
            continue
        r = notify.send("membership_reminder", m["phone"], {
            "name": m["name"].split()[0], "plan": m["plan"],
            "end_date": Date.fromisoformat(m["end_date"]).strftime("%d %b %Y"), "when": _when(left),
        }, dedupe_key=f"mem:{m['id']}:{b}")
        sent.append(("membership", m["name"], b, r))
    for e in db.all_(
        "SELECT e.id, e.fee_paid_until, b.name AS batch, u.name, u.phone FROM academy_enrollments e "
        "JOIN academy_batches b ON b.id=e.batch_id JOIN users u ON u.id=e.user_id "
        "WHERE e.status='active' AND e.fee_paid_until BETWEEN ? AND ?", today.isoformat(), horizon):
        left = (Date.fromisoformat(e["fee_paid_until"]) - today).days
        b = _bucket(left)
        if b is None:
            continue
        # Key includes the paid-until date: after a renewal the next cycle's reminders fire again.
        r = notify.send("academy_fee_reminder", e["phone"], {
            "name": e["name"].split()[0], "batch": e["batch"],
            "end_date": Date.fromisoformat(e["fee_paid_until"]).strftime("%d %b %Y"), "when": _when(left),
        }, dedupe_key=f"acad:{e['id']}:{e['fee_paid_until']}:{b}")
        sent.append(("academy", e["name"], b, r))
    return sent
