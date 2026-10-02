"""Welcome coupons: a guest who creates an account (first login in the portal) gets one code for
WELCOME_COUPON_PCT off their next court booking, on any sport. It only works with the mobile
number it was issued to, and only once.

The coupon is taken when the booking is made (so two tabs can't spend it twice) and given back
if that booking is never paid: hold lapsed, backed out of payment, or lost the slot.
"""
import secrets

from . import clock, config, db, notify

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: codes get read out over the phone


class CouponError(Exception):
    pass


def _new_code():
    return "WELCOME-" + "".join(secrets.choice(ALPHABET) for _ in range(5))


def issue_welcome(user_id):
    """Called when someone creates their own account. One coupon per person, ever."""
    if config.WELCOME_COUPON_PCT <= 0 or db.one("SELECT 1 AS x FROM coupons WHERE user_id=?", user_id):
        return None
    u = db.one("SELECT name, phone FROM users WHERE id=?", user_id)
    code = _new_code()
    db.conn().execute("INSERT INTO coupons(code,user_id,phone,pct,created_at) VALUES(?,?,?,?,?)",
                      (code, user_id, u["phone"], config.WELCOME_COUPON_PCT, clock.fmt(clock.now())))
    notify.send("welcome_coupon", u["phone"], {"name": u["name"].split()[0], "pct": config.WELCOME_COUPON_PCT,
                                               "code": code}, dedupe_key=f"welcome:{user_id}")
    return code


def for_user(user_id):
    return db.one("SELECT c.code, c.pct, c.status, c.used_at, b.ref AS booking_ref FROM coupons c "
                  "LEFT JOIN bookings b ON b.id=c.booking_id WHERE c.user_id=?", user_id)


def check(code, phone, amount):
    """What the coupon takes off `amount`. Raises CouponError with a message for the customer."""
    code = (code or "").strip().upper()
    c = db.one("SELECT * FROM coupons WHERE code=?", code)
    if not c:
        raise CouponError("That coupon code doesn't exist")
    if c["phone"] != phone:
        raise CouponError("This coupon only works with the mobile number it was sent to")
    if c["status"] != "active":
        raise CouponError("This coupon has already been used")
    return {"code": code, "pct": c["pct"], "discount": amount * c["pct"] // 100}


def take(c, code, booking_id):
    """Inside the booking's transaction: mark the coupon used by this booking."""
    cur = c.execute("UPDATE coupons SET status='used', booking_id=?, used_at=? WHERE code=? AND status='active'",
                    (booking_id, clock.fmt(clock.now()), code))
    if cur.rowcount != 1:
        raise CouponError("This coupon has already been used")


def release_unpaid():
    """Give coupons back from bookings that never got paid for. Safe to call repeatedly."""
    db.conn().execute(
        "UPDATE coupons SET status='active', booking_id=NULL, used_at=NULL WHERE status='used' AND booking_id IN "
        "(SELECT id FROM bookings WHERE pay_status<>'paid' AND status IN ('expired','cancelled'))")
