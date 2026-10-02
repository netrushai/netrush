"""Court availability and the booking lifecycle:

    online:  pending (slot held HOLD_MIN) --paid--> confirmed --slot ends--> (done)
                  \\--hold lapses--> expired          \\--cancel--> cancelled (+ automatic refund)
    staff:   confirmed straight away — paid at desk, unpaid (phone reservation, pay later) or complimentary

Every confirmed booking gets a door code, however it was paid.
"""
import re
import secrets
from datetime import date as Date, datetime, timedelta

from . import access, clock, config, coupons, db, notify, payments


class BookingError(Exception):
    pass


def _slot_starts(day):
    midnight = datetime.combine(day, datetime.min.time())
    t, end = midnight + timedelta(hours=config.OPEN_HOUR), midnight + timedelta(hours=config.CLOSE_HOUR)
    while t + timedelta(minutes=config.SLOT_MIN) <= end:
        yield t
        t += timedelta(minutes=config.SLOT_MIN)


def _live_bookings(c, court_ids, start, end):
    """Bookings that occupy a court in [start, end): confirmed, or unpaid but still inside their hold."""
    marks = ",".join("?" * len(court_ids))
    return c.execute(
        f"SELECT court_id, start, end, status FROM bookings WHERE court_id IN ({marks}) "
        "AND start < ? AND end > ? AND (status='confirmed' OR (status='pending' AND hold_until > ?))",
        (*court_ids, clock.fmt(end), clock.fmt(start), clock.fmt(clock.now())),
    ).fetchall()


def _blocks(c, court_ids, day):
    """Academy batches (recurring weekly), badminton members' daily slots and one-off blocks on
    that day → [(court, start, end, reason)]."""
    out = []
    for r in c.execute("SELECT DISTINCT court_id, start_time FROM member_slots WHERE status='active'").fetchall():
        if r["court_id"] in court_ids:
            end = (datetime.strptime(r["start_time"], "%H:%M") + timedelta(minutes=config.SLOT_MIN)).strftime("%H:%M")
            out.append((r["court_id"], r["start_time"], end, "Members"))
    for b in c.execute("SELECT * FROM academy_batches").fetchall():
        if str(day.weekday()) in b["weekdays"].split(","):
            for cid in b["court_ids"].split(","):
                if cid in court_ids:
                    out.append((cid, b["start_time"], b["end_time"], "Academy"))
    for b in c.execute("SELECT * FROM court_blocks WHERE date=?", (day.isoformat(),)).fetchall():
        if b["court_id"] in court_ids:
            out.append((b["court_id"], b["start"], b["end"], b["reason"] or "Blocked"))
    return out


def _blocked(blocks, court_id, s, e):
    for cid, bs, be, reason in blocks:
        if cid == court_id and bs < e.strftime("%H:%M") and be > s.strftime("%H:%M"):
            return reason
    return None


def availability(sport, day):
    """Grid for the booking page: courts × slots with state free | booked | blocked | past."""
    courts = db.all_("SELECT id, name FROM courts WHERE sport=? AND active=1 ORDER BY id", sport)
    if not courts:
        raise BookingError("Unknown sport")
    ids = [c["id"] for c in courts]
    c = db.conn()
    starts = list(_slot_starts(day))
    slot = timedelta(minutes=config.SLOT_MIN)
    taken = _live_bookings(c, ids, starts[0], starts[-1] + slot) if starts else []
    blocks = _blocks(c, ids, day)
    now = clock.now()
    grid = []
    for s in starts:
        e = s + slot
        row = {"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "cells": {}}
        for cid in ids:
            state = "free"
            if s < now:
                state = "past"
            elif any(b["court_id"] == cid and b["start"] < clock.fmt(e) and b["end"] > clock.fmt(s) for b in taken):
                state = "booked"
            elif (reason := _blocked(blocks, cid, s, e)):
                state = "blocked:" + reason
            row["cells"][cid] = state
        grid.append(row)
    return {"sport": sport, "date": day.isoformat(), "price": config.PRICES[sport],
            "price_per_hour": config.PRICES_PER_HOUR[sport],
            "slot_min": config.SLOT_MIN, "courts": courts, "slots": grid}


def _clean_email(email):
    email = (email or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise BookingError("That email address doesn't look right")
    return email or None


def _validate(court_id, day, start_hhmm, slots, name, phone):
    from .auth import normalize_phone, AuthError

    try:
        phone = normalize_phone(phone)
    except AuthError as e:
        raise BookingError(str(e))
    name = (name or "").strip()
    if not name:
        raise BookingError("Please enter a name")
    slots = int(slots)
    if not 1 <= slots <= 4:
        raise BookingError("Book between 1 and 4 slots at a time")
    court = db.one("SELECT * FROM courts WHERE id=? AND active=1", court_id)
    if not court:
        raise BookingError("Unknown court")
    day = Date.fromisoformat(day) if isinstance(day, str) else day
    if not clock.today() <= day <= clock.today() + timedelta(days=config.BOOKING_HORIZON_DAYS):
        raise BookingError(f"You can book up to {config.BOOKING_HORIZON_DAYS} days ahead")
    valid_starts = {s.strftime("%H:%M"): s for s in _slot_starts(day)}
    if start_hhmm not in valid_starts:
        raise BookingError("Pick a start time from the grid")
    start = valid_starts[start_hhmm]
    end = start + timedelta(minutes=config.SLOT_MIN * slots)
    if start < clock.now():
        raise BookingError("That slot has already started")
    if end > datetime.combine(day, datetime.min.time()) + timedelta(hours=config.CLOSE_HOUR):
        raise BookingError("That runs past closing time")
    return court, day, start, end, name, phone, config.PRICES[court["sport"]] * slots


def _insert(c, court, day, start, end, name, phone, amount, status, pay_status, source, email=None):
    if _live_bookings(c, [court["id"]], start, end):
        raise BookingError("Sorry, that slot has just been booked. Please pick another.")
    if (reason := _blocked(_blocks(c, [court["id"]], day), court["id"], start, end)):
        raise BookingError(f"That court is reserved ({reason}) at that time")
    user = c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
    hold = clock.fmt(clock.now() + timedelta(minutes=config.HOLD_MIN)) if status == "pending" else None
    return c.execute(
        "INSERT INTO bookings(ref,court_id,user_id,name,phone,start,end,amount,status,hold_until,created_at,"
        "pay_status,source,email,access_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (secrets.token_hex(3).upper(), court["id"], user and user["id"], name, phone, clock.fmt(start),
         clock.fmt(end), amount, status, hold, clock.fmt(clock.now()), pay_status, source, email,
         secrets.token_urlsafe(12))).lastrowid


def create(court_id, day, start_hhmm, slots, name, phone, email=None, coupon=None):
    """Online booking, no account needed: holds the slot and opens a payment. `coupon`: a welcome
    coupon issued to this phone, taken off the price."""
    court, day, start, end, name, phone, amount = _validate(court_id, day, start_hhmm, slots, name, phone)
    email = _clean_email(email)
    try:
        disc = coupons.check(coupon, phone, amount) if (coupon or "").strip() else None
        with db.tx() as c:
            bid = _insert(c, court, day, start, end, name, phone, amount - (disc["discount"] if disc else 0),
                          "pending", "unpaid", "online", email)
            if disc:
                c.execute("UPDATE bookings SET coupon_code=?, discount=? WHERE id=?",
                          (disc["code"], disc["discount"], bid))
                coupons.take(c, disc["code"], bid)
    except coupons.CouponError as e:
        raise BookingError(str(e))
    b = get(bid)
    pay = payments.create_order("booking", b["amount"], b["ref"], phone, b["user_id"], ref_id=bid)
    return {"booking": b, "payment": pay}


STAFF_MODES = {
    "desk_paid": ("paid", "desk"),   # cash / UPI at the counter
    "unpaid": ("unpaid", "phone"),   # reservation taken on a call, pays later
    "comp": ("comp", "desk"),        # complimentary: coaching demo, sponsor, make-good
}


def create_by_staff(court_id, day, start_hhmm, slots, name, phone, mode, staff, email=None):
    if mode not in STAFF_MODES:
        raise BookingError("Choose how this booking is paid")
    court, day, start, end, name, phone, amount = _validate(court_id, day, start_hhmm, slots, name, phone)
    email = _clean_email(email)
    pay_status, source = STAFF_MODES[mode]
    with db.tx() as c:
        bid = _insert(c, court, day, start, end, name, phone, amount, "confirmed", pay_status, source, email)
        c.execute("UPDATE bookings SET payment_ref=?, paid_at=? WHERE id=?",
                  (f"{mode.upper()}-{staff}", clock.fmt(clock.now()) if mode == "desk_paid" else None, bid))
    if mode == "desk_paid":
        payments.record_desk("booking", bid, amount, phone, staff=staff)
    return _on_confirmed(bid)


def mark_paid(booking_id, staff):
    """A phone reservation settles up at the desk."""
    b = get(booking_id)
    if not b or b["pay_status"] != "unpaid" or b["status"] != "confirmed":
        raise BookingError("Nothing to collect on this booking")
    payments.record_desk("booking", booking_id, b["amount"], b["phone"], b["user_id"], staff)
    db.conn().execute("UPDATE bookings SET pay_status='paid', paid_at=? WHERE id=?",
                      (clock.fmt(clock.now()), booking_id))
    return get(booking_id)


def get(booking_id):
    return db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
                  "WHERE b.id=?", booking_id)


def confirm_payment(booking_id, payload):
    p = db.one("SELECT * FROM payments WHERE kind='booking' AND ref_id=? ORDER BY id DESC LIMIT 1", booking_id)
    if not p:
        raise BookingError("Booking not found")
    p, _ = payments.capture(p["id"], payload)
    return fulfil(p)


def fulfil(p):
    """Payment for a booking arrived (checkout or webhook). Confirm it, or refund it if we can't."""
    b = get(p["ref_id"])
    if b["status"] == "confirmed":
        return b
    if b["status"] == "cancelled":
        payments.request_refund(p["id"], p["amount"])
        raise BookingError("This booking was cancelled; your payment is being refunded.")
    with db.tx() as c:
        # Paid after the hold lapsed: still fine unless someone else has confirmed the slot since.
        # (An unpaid hold by someone else loses to money in hand.)
        taken = c.execute(
            "SELECT 1 FROM bookings WHERE court_id=? AND id<>? AND status='confirmed' AND start<? AND end>?",
            (b["court_id"], b["id"], b["end"], b["start"])).fetchone()
        if taken:
            c.execute("UPDATE bookings SET status='cancelled', cancelled_at=?, cancelled_by='system', "
                      "refund_amount=?, hold_until=NULL WHERE id=?",
                      (clock.fmt(clock.now()), p["amount"], b["id"]))
        else:
            c.execute("UPDATE bookings SET status='confirmed', pay_status='paid', payment_ref=?, paid_at=?, "
                      "hold_until=NULL WHERE id=?", (p["payment_id"], clock.fmt(clock.now()), b["id"]))
            if b["coupon_code"]:  # hold lapsed and gave the coupon back before the money arrived: take it again
                c.execute("UPDATE coupons SET status='used', booking_id=?, used_at=? WHERE code=? AND status='active'",
                          (b["id"], clock.fmt(clock.now()), b["coupon_code"]))
    if taken:
        payments.request_refund(p["id"], p["amount"])
        coupons.release_unpaid()
        raise BookingError("Payment received, but the slot was taken after your 10-minute hold ran out. "
                           "Your money is being refunded automatically.")
    return _on_confirmed(b["id"])


def _on_confirmed(booking_id):
    b = get(booking_id)
    notify.send("booking_confirmed", b["phone"], {
        "name": b["name"].split()[0], "ref": b["ref"], "court": b["court_name"],
        "when": _when(b), "lead": config.DOOR_CODE_LEAD_MIN,
    }, dedupe_key=f"booking:{b['id']}")
    access.issue_due_codes()  # booked at the last minute? the code goes out right now
    return get(booking_id)


def _when(b):
    return clock.parse(b["start"]).strftime("%a %d %b, %H:%M") + "-" + b["end"][11:]


# ---------------------------------------------------------------- cancellation + refunds

def refund_quote(b, by="customer"):
    """What a cancellation right now would refund: {amount, pct, can_cancel, note}."""
    started = clock.parse(b["start"]) <= clock.now()
    if b["status"] not in ("confirmed", "pending"):
        return {"can_cancel": False, "amount": 0, "pct": 0, "note": "Already " + b["status"]}
    if by == "customer" and started:
        return {"can_cancel": False, "amount": 0, "pct": 0, "note": "The slot has already started"}
    if b["pay_status"] != "paid":
        return {"can_cancel": True, "amount": 0, "pct": 0, "note": "Nothing was charged"}
    if by == "staff":
        pct = 100
    else:
        hours = (clock.parse(b["start"]) - clock.now()).total_seconds() / 3600
        pct = 100 if hours >= config.REFUND_FULL_HOURS else (
            config.REFUND_PARTIAL_PCT if hours >= config.REFUND_PARTIAL_HOURS else 0)
    amount = b["amount"] * pct // 100
    note = (f"Full refund of Rs {amount}" if pct == 100 else
            f"{pct}% refund: Rs {amount} (cancelled under {config.REFUND_FULL_HOURS}h before)" if pct else
            f"No refund (under {config.REFUND_PARTIAL_HOURS}h before start)")
    return {"can_cancel": True, "amount": amount, "pct": pct, "note": note}


def refund_policy_text():
    return (f"Free cancellation up to {config.REFUND_FULL_HOURS} hours before. "
            f"{config.REFUND_PARTIAL_PCT}% back if cancelled {config.REFUND_PARTIAL_HOURS}–{config.REFUND_FULL_HOURS} "
            f"hours before. No refund after that.")


def cancel(booking_id, by="customer"):
    """by: customer (refund per policy) | staff (always full refund)."""
    b = get(booking_id)
    if not b:
        raise BookingError("Booking not found")
    q = refund_quote(b, by)
    if not q["can_cancel"]:
        raise BookingError(f"This booking can't be cancelled: {q['note'].lower()}")
    with db.tx() as c:
        cur = c.execute("UPDATE bookings SET status='cancelled', cancelled_at=?, cancelled_by=?, refund_amount=?, "
                        "hold_until=NULL WHERE id=? AND status IN ('confirmed','pending')",
                        (clock.fmt(clock.now()), by, q["amount"], booking_id))
        if cur.rowcount != 1:
            raise BookingError("This booking was already cancelled")
    access.revoke_for_booking(booking_id, f"cancelled by {by}")
    coupons.release_unpaid()  # cancelled before paying: the coupon can be used again
    p = payments.for_ref("booking", booking_id)
    if p and q["amount"]:
        p = payments.request_refund(p["id"], q["amount"])
    if b["status"] == "confirmed":
        if q["amount"] and p and p["provider"] == "desk":
            refund = f"Rs {q['amount']} will be handed back to you at the front desk."
        elif q["amount"]:
            refund = f"Rs {q['amount']} is being refunded to your original payment method."
        elif b["pay_status"] == "paid":
            refund = q["note"] + "."
        else:
            refund = "Nothing was charged."
        notify.send("booking_cancelled", b["phone"], {
            "name": b["name"].split()[0], "ref": b["ref"], "court": b["court_name"], "when": _when(b),
            "refund": refund}, dedupe_key=f"cancel:{booking_id}")
    return {"booking": get(booking_id), "refund": q}


def expire_holds():
    db.conn().execute("UPDATE bookings SET status='expired' WHERE status='pending' AND hold_until <= ?",
                      (clock.fmt(clock.now()),))
    coupons.release_unpaid()


def lookup(ref, phone):
    from .auth import normalize_phone
    return db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
                  "WHERE b.ref=? AND b.phone=?", (ref or "").upper().strip(), normalize_phone(phone))


def release_hold(booking_id):
    """Customer backed out on the payment page: free the slot straight away."""
    db.conn().execute("UPDATE bookings SET status='expired', hold_until=NULL WHERE id=? AND status='pending'",
                      (booking_id,))
    coupons.release_unpaid()


def by_key(ref, key):
    """The booking behind a confirmation link."""
    import hmac
    b = db.one("SELECT b.*, c.name AS court_name, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
               "WHERE b.ref=?", (ref or "").upper())
    if not b or not b["access_key"] or not hmac.compare_digest(b["access_key"], key or ""):
        return None
    return b
