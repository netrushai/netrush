"""NetRush — booking, memberships, academy and unmanned door access for a multi-sport facility.

    python server.py            → http://localhost:8000  (booking)  /portal  (members)  /admin  (staff)
"""
import json
import re
import sys
import traceback
from datetime import date as Date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import (access, analytics, auth, bookings, clock, config, coupons, db, health, members, payments,
                 scheduler, slots)
from app.auth import AuthError
from app.bookings import BookingError
from app.coupons import CouponError
from app.members import MemberError
from app.payments import PaymentError
from app.slots import SlotError

STATIC = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/portal": "portal.html", "/admin": "admin.html", "/pay": "pay.html",
         "/booking": "booking.html", "/demo": "demo.html", "/door": "door.html"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
COOKIE = "nr_session"

ROUTES = []


def route(method, pattern, who="public"):
    """who: public | user (logged-in member/player) | admin | device (signed by the door)."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), who, fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


# ------------------------------------------------------------------ public

@route("GET", "/api/config")
def get_config(req):
    courts = db.all_("SELECT id, sport, name FROM courts WHERE active=1 ORDER BY id")
    sports = {}
    for c in courts:
        sports.setdefault(c["sport"], {"sport": c["sport"], "price": config.PRICES[c["sport"]],
                                       "price_per_hour": config.PRICES_PER_HOUR[c["sport"]], "courts": []})
        sports[c["sport"]]["courts"].append(c)
    return {"facility": config.FACILITY_NAME, "sports": list(sports.values()), "today": clock.today().isoformat(),
            "horizon_days": config.BOOKING_HORIZON_DAYS, "slot_min": config.SLOT_MIN,
            "door_code_lead_min": config.DOOR_CODE_LEAD_MIN, "hold_min": config.HOLD_MIN,
            "payment_provider": config.PAYMENT_PROVIDER, "refund_policy": bookings.refund_policy_text(),
            "demo": config.DEMO_MODE, "membership_months": config.ONLINE_MEMBERSHIP_MONTH_OPTIONS,
            "membership_monthly": config.ONLINE_MEMBERSHIP_MONTHLY, "welcome_coupon_pct": config.WELCOME_COUPON_PCT}


@route("GET", "/api/availability")
def get_availability(req):
    q = req.query
    try:
        day = Date.fromisoformat(q.get("date") or clock.today().isoformat())
    except ValueError:
        raise HttpError(400, "Bad date")
    return bookings.availability(q.get("sport", "badminton"), day)


@route("POST", "/api/bookings")
def post_booking(req):
    """No account needed: name, mobile, email. Returns where to send the browser to pay."""
    b = req.body
    out = bookings.create(b.get("court_id"), b.get("date"), b.get("start"), b.get("slots", 1),
                          b.get("name"), b.get("phone"), b.get("email"), b.get("coupon"))
    return {"booking": _public_booking(out["booking"]), "redirect": out["payment"]["redirect"]}


@route("GET", "/api/coupon")
def check_coupon(req):
    """Booking form: is this welcome coupon good for this mobile? (The price is worked out again on booking.)"""
    try:
        phone = auth.normalize_phone(req.query.get("phone"))
    except AuthError:
        raise HttpError(400, "Enter your mobile number first: the coupon is tied to it")
    c = coupons.check(req.query.get("code"), phone, 0)
    return {"code": c["code"], "pct": c["pct"]}


@route("GET", "/api/booking")
def get_booking(req):
    """The confirmation page (/booking?ref=…&k=…): the link itself is the proof of ownership."""
    b = bookings.by_key(req.query.get("ref"), req.query.get("k"))
    if not b:
        raise HttpError(404, "Booking not found")
    return {"booking": _public_booking(b, with_quote=True), "refund_policy": bookings.refund_policy_text()}


@route("POST", "/api/booking/cancel")
def cancel_by_link(req):
    b = bookings.by_key(req.body.get("ref"), req.body.get("k"))
    if not b:
        raise HttpError(404, "Booking not found")
    out = bookings.cancel(b["id"], "customer")
    return {"booking": _public_booking(out["booking"], with_quote=True), "refund": out["refund"]}


@route("GET", "/api/bookings/lookup")
def lookup_booking(req):
    b = bookings.lookup(req.query.get("ref"), req.query.get("phone"))
    if not b:
        raise HttpError(404, "No booking with that reference and phone number")
    return {"booking": _public_booking(b, with_quote=True)}


@route("POST", "/api/bookings/cancel")
def customer_cancel(req):
    """From 'Find my booking': reference + phone + a one-time code sent to that phone."""
    phone = auth.consume_code(req.body.get("phone"), req.body.get("code"))
    b = bookings.lookup(req.body.get("ref"), phone)
    if not b:
        raise HttpError(404, "No booking with that reference and phone number")
    out = bookings.cancel(b["id"], "customer")
    return {"booking": _public_booking(out["booking"]), "refund": out["refund"]}


# ------------------------------------------------------------------ payment page (/pay?id=…&k=…)

def _payment(req, src):
    p = payments.for_payer(int(src.get("id") or 0), src.get("k"))
    if not p:
        raise HttpError(404, "Payment link not found")
    return p


@route("GET", "/api/pay")
def pay_info(req):
    p = _payment(req, req.query)
    out = {"id": p["id"], "amount": p["amount"], "kind": p["kind"], "status": p["status"],
           "provider": p["provider"], "facility": config.FACILITY_NAME, "phone": p["phone"]}
    if p["kind"] == "booking":
        b = bookings.get(p["ref_id"])
        out["title"] = f"{b['court_name']} booking"
        out["detail"] = f"{clock.parse(b['start']).strftime('%a %d %b, %H:%M')}-{b['end'][11:]} · {b['name']}"
        if b["discount"]:
            out["detail"] += f" · welcome coupon -Rs {b['discount']}"
        out["hold_seconds"] = max(0, int((clock.parse(b["hold_until"]) - clock.now()).total_seconds()))             if b["hold_until"] else None
        out["booking_status"] = b["status"]
    else:
        months = json.loads(p["meta"] or "{}").get("months", 1)
        out["title"] = "Membership"
        out["detail"] = f"{months} month{'s' if months > 1 else ''} · all courts · fingerprint entry"
    if p["provider"] == "razorpay":
        out.update(key_id=config.RAZORPAY_KEY_ID, order_id=p["order_id"])
    return out


@route("POST", "/api/pay/complete")
def pay_complete(req):
    """The gateway says it's paid. Verify, fulfil, and tell the browser where to go next."""
    p = _payment(req, req.body)
    p, _ = payments.capture(p["id"], req.body)
    try:
        _fulfil(p)
    except BookingError:
        pass  # slot lost after the hold ran out: already refunded; the booking page explains
    return {"redirect": _after_payment(p)}


@route("POST", "/api/pay/cancel")
def pay_cancel(req):
    p = _payment(req, req.body)
    if p["status"] != "paid" and p["kind"] == "booking":
        bookings.release_hold(p["ref_id"])
        return {"redirect": "/?cancelled=1"}
    return {"redirect": _after_payment(p) if p["status"] == "paid" else "/portal"}


def _after_payment(p):
    if p["kind"] == "booking":
        b = bookings.get(p["ref_id"])
        return f"/booking?ref={b['ref']}&k={b['access_key']}"
    return "/portal?paid=1"


@route("POST", "/api/payments/razorpay-webhook")
def razorpay_webhook(req):
    """Settles payments whose browser closed before Checkout reported back."""
    p = payments.capture_from_webhook(req.raw, req.headers.get("X-Razorpay-Signature"))
    if p:
        try:
            _fulfil(p)
        except BookingError:
            pass  # slot lost; refunded automatically
    return {"ok": True}


def _fulfil(p):
    if p["kind"] == "booking":
        return bookings.fulfil(p)
    if p["kind"] == "membership":
        return members.complete_membership(p)


def _public_booking(b, with_quote=False):
    keep = ("id", "ref", "court_id", "court_name", "sport", "name", "start", "end", "amount", "status", "hold_until",
            "pay_status", "refund_amount", "cancelled_by", "phone", "email", "discount", "coupon_code")
    out = {k: b.get(k) for k in keep}
    out["phone"] = out["phone"] and "******" + out["phone"][-4:]
    if with_quote:
        out["cancel"] = bookings.refund_quote(b, "customer")
    d = db.one("SELECT valid_from, valid_to, status FROM door_codes WHERE booking_id=?", b["id"])
    # The code itself only travels by SMS/WhatsApp to the booker's phone — never in a web response.
    out["door_code"] = d and {"sent": True, "valid_from": d["valid_from"], "valid_to": d["valid_to"],
                              "status": d["status"]}
    p = payments.for_ref("booking", b["id"])
    out["refund_status"] = p and p["refund_status"]
    # A guest without an account is invited to sign up for the welcome coupon.
    out["has_account"] = bool(db.one("SELECT 1 AS x FROM users WHERE phone=?", b["phone"]))
    return out


# ------------------------------------------------------------------ auth

@route("POST", "/api/auth/request-code")
def request_code(req):
    code, is_new, _ = auth.request_login_code(req.body.get("phone"), req.ip)
    out = {"ok": True, "new": is_new}
    if config.DEMO_MODE:
        out["demo_code"] = code  # demo only: no real SMS goes out, so show it
    return out


@route("POST", "/api/auth/verify")
def verify_code(req):
    token = auth.verify_login_code(req.body.get("phone"), req.body.get("code"), req.body.get("name"))
    req.set_cookie = token
    return {"ok": True}


@route("POST", "/api/auth/admin")
def admin_login(req):
    req.set_cookie = auth.admin_login(req.body.get("phone"), req.body.get("password"))
    return {"ok": True}


@route("POST", "/api/auth/logout")
def logout(req):
    auth.logout(req.token)
    req.set_cookie = ""
    return {"ok": True}


@route("GET", "/api/me", "user")
def me(req):
    return members.status(req.user["id"])


@route("POST", "/api/me/prefs", "user")
def me_prefs(req):
    sms, wa = int(bool(req.body.get("sms_opt"))), int(bool(req.body.get("wa_opt")))
    if not (sms or wa):
        raise HttpError(400, "Keep at least one channel on so you get your reminders")
    db.conn().execute("UPDATE users SET sms_opt=?, wa_opt=? WHERE id=?", (sms, wa, req.user["id"]))
    return {"ok": True}


@route("POST", r"/api/me/bookings/(\d+)/cancel", "user")
def me_cancel(req, bid):
    b = bookings.get(int(bid))
    if not b or b["phone"] != req.user["phone"]:
        raise HttpError(404, "Booking not found")
    return bookings.cancel(int(bid), "customer")


@route("POST", "/api/me/membership/order", "user")
def me_membership_order(req):
    """Buy / renew online. Returns the payment page to redirect to."""
    pay = members.order_membership(req.user["id"], req.body.get("months", 1))
    return {"redirect": pay["redirect"], "amount": pay["amount"]}


# ------------------------------------------------------------------ demo helpers (DEMO_MODE only)

def _demo_only():
    if not config.DEMO_MODE:
        raise HttpError(404, "Not found")


@route("GET", "/api/demo/info")
def demo_info(req):
    _demo_only()
    return {"logins": [
        {"role": "Admin / front desk", "login": config.ADMIN_PHONE, "secret": "password: " + config.ADMIN_PASSWORD,
         "where": "/admin"},
        {"role": "Member (plan ends in 7 days)", "login": auth.member_id(2) + "  or  9000000001",
         "secret": "code shown on screen", "where": "/portal"},
        {"role": "Academy player (junior)", "login": auth.member_id(5) + "  or  9000000004",
         "secret": "code shown on screen", "where": "/portal"},
        {"role": "New sign-up with a 10% welcome coupon", "login": "9811100002",
         "secret": "code shown on screen", "where": "/portal"},
    ], "fingerprints": [{"id": "101", "who": "Aarav Mehta (member, active)"},
                        {"id": "104", "who": "Ishaan Verma (academy, fees paid)"},
                        {"id": "115", "who": "Farhan Sheikh (membership expired)"}]}


@route("GET", "/api/demo/messages")
def demo_messages(req):
    _demo_only()
    phone = "".join(ch for ch in req.query.get("phone", "") if ch.isdigit())[-10:]
    rows = db.all_("SELECT id, created_at, kind, channel, to_phone, body, status FROM notifications "
                   + ("WHERE to_phone=? " if phone else "") + "ORDER BY id DESC LIMIT 80", *([phone] if phone else []))
    return {"messages": rows}


@route("POST", "/api/demo/door")
def demo_door(req):
    """The web keypad: exactly what the door hardware would ask the server."""
    _demo_only()
    b = req.body
    if b.get("fingerprint_user_id"):
        return access.verify_fingerprint(str(b["fingerprint_user_id"]), "web-keypad")
    return access.verify_code(b.get("code"), "web-keypad")


@route("POST", "/api/admin/demo-reset", "admin")
def demo_reset(req):
    _demo_only()
    db.reset()
    access.sync_member_access()
    return {"ok": True}


# ------------------------------------------------------------------ door device

@route("POST", "/api/lock/verify", "device")
def lock_verify(req):
    b, dev = req.body, req.headers.get("X-Device-Id", "door-1")
    if b.get("fingerprint_user_id") is not None:
        return access.verify_fingerprint(b["fingerprint_user_id"], dev)
    return access.verify_code(b.get("code"), dev)


@route("POST", "/api/lock/events", "device")
def lock_events(req):
    dev = req.headers.get("X-Device-Id", "door-1")
    for ev in req.body.get("events", []):
        access.record_device_event(ev, dev)
    return {"ok": True}


# ------------------------------------------------------------------ admin

@route("GET", "/api/admin/overview", "admin")
def overview(req):
    day = req.query.get("date") or clock.today().isoformat()
    todays = db.all_(
        "SELECT b.id,b.ref,b.court_id,c.name AS court,b.name,b.phone,b.email,b.start,b.end,b.amount,b.status,b.payment_ref,"
        " b.discount, b.coupon_code,"
        " b.pay_status,b.source, d.code, d.status AS code_status, d.valid_from, d.uses FROM bookings b "
        "JOIN courts c ON c.id=b.court_id LEFT JOIN door_codes d ON d.booking_id=b.id "
        "WHERE substr(b.start,1,10)=? AND b.status IN ('confirmed','pending') ORDER BY b.start, b.court_id", day)
    soon = (clock.today() + timedelta(days=max(config.REMINDER_DAYS))).isoformat()
    today = clock.today().isoformat()
    expiring = db.all_(
        "SELECT u.id AS user_id, u.name, u.phone, p.name AS plan, m.end_date FROM memberships m "
        "JOIN users u ON u.id=m.user_id JOIN plans p ON p.id=m.plan_id "
        "WHERE m.status='active' AND m.end_date BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM memberships m2 WHERE m2.user_id=m.user_id AND m2.status='active' "
        "AND m2.end_date>m.end_date) ORDER BY m.end_date", today, soon)
    fees_due = db.all_(
        "SELECT e.id, u.name, u.phone, b.name AS batch, e.fee_paid_until FROM academy_enrollments e "
        "JOIN users u ON u.id=e.user_id JOIN academy_batches b ON b.id=e.batch_id "
        "WHERE e.status='active' AND e.fee_paid_until <= ? ORDER BY e.fee_paid_until", soon)
    collected = db.one("SELECT COALESCE(SUM(amount),0) AS r FROM payments WHERE status='paid' "
                       "AND substr(paid_at,1,10)=?", day)["r"]
    unpaid = db.all_("SELECT b.id,b.ref,b.name,b.phone,b.start,b.end,b.amount,c.name AS court FROM bookings b "
                     "JOIN courts c ON c.id=b.court_id WHERE b.status='confirmed' AND b.pay_status='unpaid' "
                     "ORDER BY b.start")
    refunds = db.all_(
        "SELECT p.id, p.kind, p.phone, p.provider, p.refund_amount, p.refund_status, p.refund_error, b.ref, b.name "
        "FROM payments p LEFT JOIN bookings b ON p.kind='booking' AND b.id=p.ref_id "
        "WHERE p.refund_status IN ('manual','failed','pending') ORDER BY p.id")
    return {
        "date": day, "now": clock.fmt(clock.now()), "bookings": todays, "collected": collected,
        "expiring": expiring, "fees_due": fees_due, "unpaid": unpaid, "refunds": refunds,
        "slot_openings": slots.board()["suggestions"],
        "notifications": db.all_("SELECT created_at,kind,channel,to_phone,body,status,error FROM notifications "
                                 "ORDER BY id DESC LIMIT 60"),
        "access": db.all_("SELECT a.at,a.device_id,a.method,a.granted,a.detail,u.name,b.ref FROM access_events a "
                          "LEFT JOIN users u ON u.id=a.user_id LEFT JOIN bookings b ON b.id=a.booking_id "
                          "ORDER BY a.id DESC LIMIT 40"),
        "providers": {"sms": config.SMS_PROVIDER, "whatsapp": config.WHATSAPP_PROVIDER,
                      "lock": config.LOCK_PROVIDER, "payment": config.PAYMENT_PROVIDER},
    }


@route("GET", "/api/admin/analytics", "admin")
def get_analytics(req):
    return analytics.report(int(req.query.get("days") or 30))


@route("GET", "/api/admin/health", "admin")
def get_health(req):
    return {"checks": health.checks(), "now": clock.fmt(clock.now())}


@route("GET", "/api/admin/catalog", "admin")
def catalog(req):
    return {"plans": db.all_("SELECT * FROM plans ORDER BY id"),
            "batches": db.all_("SELECT * FROM academy_batches ORDER BY id"),
            "courts": db.all_("SELECT * FROM courts ORDER BY id")}


@route("GET", "/api/admin/people", "admin")
def people(req):
    q = "%" + (req.query.get("q") or "") + "%"
    rows = db.all_("SELECT id,name,phone,email,lock_user_id,lock_enabled,guardian_name,sms_opt,wa_opt FROM users "
                   "WHERE is_admin=0 AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 200", q, q)
    for r in rows:
        s = members.status(r["id"])
        r["memberships"], r["academy"], r["access_until"] = s["memberships"], s["academy"], s["fingerprint"]["until"]
        r["slot"], r["slot_ok"] = s["slot"], slots.eligible(r["id"])
        r["member_id"] = auth.member_id(r["id"])
    return {"people": rows}


@route("POST", "/api/admin/people", "admin")
def save_person(req):
    b = req.body
    new = not db.one("SELECT 1 AS x FROM users WHERE phone=?", auth.normalize_phone(b.get("phone")))
    uid = members.upsert_person(b.get("name"), b.get("phone"), b.get("email") or None, b.get("lock_user_id"),
                                b.get("guardian_name") or None, b.get("sms_opt", 1), b.get("wa_opt", 1))
    c = coupons.for_user(uid) if new else None
    return {"id": uid, "welcome_coupon": c and c["code"]}


@route("POST", "/api/admin/memberships", "admin")
def add_membership(req):
    return {"membership": members.add_membership(int(req.body["user_id"]), int(req.body["plan_id"]),
                                                 int(req.body.get("months", 1)), req.user["name"])}


@route("GET", "/api/admin/slots", "admin")
def slot_board(req):
    return {**slots.board(), "times": [{"start_time": t, "label": slots.label(t)} for t in slots.slot_times()]}


@route("POST", "/api/admin/slots", "admin")
def assign_slot(req):
    return slots.assign(int(req.body["user_id"]), req.body.get("start_time"), req.user["name"])


@route("POST", r"/api/admin/slots/(\d+)/release", "admin")
def release_slot(req, sid):
    slots.release(int(sid), "removed by staff")
    return {"ok": True}


@route("POST", "/api/admin/enquiries", "admin")
def add_enquiry(req):
    b = req.body
    return slots.add_enquiry(b.get("name"), b.get("phone"), b.get("start_time"), b.get("note"), req.user["name"])


@route("POST", r"/api/admin/enquiries/(\d+)", "admin")
def update_enquiry(req, eid):
    return {"enquiry": slots.set_enquiry(int(eid), req.body.get("status"))}


@route("POST", "/api/admin/enroll", "admin")
def enroll(req):
    b = req.body
    return {"enrollment": members.enroll(int(b["user_id"]), int(b["batch_id"]), b.get("level"),
                                         int(b.get("months", 1)), req.user["name"])}


@route("POST", "/api/admin/academy-fee", "admin")
def academy_fee(req):
    return {"fee_paid_until": members.pay_academy_fee(int(req.body["enrollment_id"]),
                                                      int(req.body.get("months", 1)), req.user["name"])}


@route("POST", "/api/admin/bookings", "admin")
def desk_booking(req):
    """Booking made by staff. mode: desk_paid (cash/UPI now) | unpaid (phone reservation, pays later)
    | comp (free). All of them get a door code like any other booking."""
    b = req.body
    return {"booking": bookings.create_by_staff(b.get("court_id"), b.get("date"), b.get("start"), b.get("slots", 1),
                                                b.get("name"), b.get("phone"), b.get("mode"), req.user["name"],
                                                b.get("email"))}


@route("POST", r"/api/admin/bookings/(\d+)/mark-paid", "admin")
def mark_paid(req, bid):
    return {"booking": bookings.mark_paid(int(bid), req.user["name"])}


@route("POST", r"/api/admin/bookings/(\d+)/cancel", "admin")
def cancel_booking(req, bid):
    return bookings.cancel(int(bid), "staff")


@route("POST", r"/api/admin/refunds/(\d+)/retry", "admin")
def retry_refund(req, pid):
    return {"payment": payments.retry_refund(int(pid))}


@route("POST", r"/api/admin/refunds/(\d+)/done", "admin")
def refund_done(req, pid):
    return {"payment": payments.mark_manual_refund_done(int(pid), req.user["name"])}


@route("POST", "/api/admin/blocks", "admin")
def add_block(req):
    b = req.body
    db.conn().execute("INSERT INTO court_blocks(court_id,date,start,end,reason) VALUES(?,?,?,?,?)",
                      (b["court_id"], b["date"], b["start"], b["end"], b.get("reason") or "Maintenance"))
    return {"ok": True}


@route("POST", "/api/admin/run-reminders", "admin")
def run_reminders(req):
    return {"sent": [{"type": t, "name": n, "bucket": b, "result": r} for t, n, b, r in members.send_reminders()]}


@route("POST", "/api/admin/tick", "admin")
def run_tick(req):
    scheduler.tick()
    return {"ok": True}


@route("POST", "/api/admin/simulate-door", "admin")
def simulate_door(req):
    """Try the door without hardware: behaves exactly like the keypad / fingerprint reader calling in."""
    b = req.body
    if b.get("fingerprint_user_id"):
        return access.verify_fingerprint(b["fingerprint_user_id"], "simulator")
    return access.verify_code(b.get("code"), "simulator")


# ------------------------------------------------------------------ plumbing

class Req:
    def __init__(self, handler, query, body_raw):
        self.headers = handler.headers
        self.query = {k: v[0] for k, v in parse_qs(query).items()}
        self.raw = body_raw
        try:
            self.body = json.loads(body_raw or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "Body must be JSON")
        jar = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
        self.token = jar[COOKIE].value if COOKIE in jar else None
        self.user = None
        self.set_cookie = None
        self.ip = handler.client_address[0]
        if config.TRUST_PROXY:
            # The proxy appends the address it saw, so the LAST entry is the one a client can't forge.
            fwd = [p.strip() for p in handler.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            self.ip = fwd[-1] if fwd else self.ip


class Handler(BaseHTTPRequestHandler):
    server_version = "NetRush"

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            sys.stderr.write("%s %s\n" % (clock.fmt(clock.now()), fmt % args))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def _handle(self, method):
        url = urlparse(self.path)
        if url.path == "/healthz":  # for the host's uptime check
            return self._json(200, {"ok": True})
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            for m, rx, who, fn in ROUTES:
                match = rx.match(url.path)
                if m != method or not match:
                    continue
                req = Req(self, url.query, raw)
                if who == "device":
                    if not access.check_device_signature(self.headers, raw):
                        raise HttpError(401, "Bad device signature")
                elif who in ("user", "admin"):
                    req.user = auth.user_for(req.token)
                    if not req.user:
                        raise HttpError(401, "Please log in")
                    if who == "admin" and not req.user["is_admin"]:
                        raise HttpError(403, "Staff only")
                return self._json(200, fn(req, *match.groups()), req.set_cookie)
            raise HttpError(404, "Not found")
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except (BookingError, AuthError, MemberError, PaymentError, SlotError, CouponError) as e:
            self._json(400, {"error": str(e)})
        except (KeyError, ValueError) as e:
            self._json(400, {"error": f"Bad request: {e}"})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong"})

    def _json(self, status, obj, set_cookie=None):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            age = 60 * 60 * 24 * auth.SESSION_DAYS if set_cookie else 0
            secure = "; Secure" if config.PUBLIC_URL.startswith("https://") else ""
            self.send_header("Set-Cookie", f"{COOKIE}={set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        name = PAGES.get(path, path.lstrip("/"))
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    db.init()
    scheduler.start()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    srv.daemon_threads = True
    print(f"{config.FACILITY_NAME} running on http://localhost:{config.PORT}\n"
          f"  booking  /      members  /portal      staff  /admin  (login {config.ADMIN_PHONE} / "
          f"{'<ADMIN_PASSWORD>' if config.ADMIN_PASSWORD != 'admin123' else 'admin123'})\n"
          f"  sms={config.SMS_PROVIDER} whatsapp={config.WHATSAPP_PROVIDER} lock={config.LOCK_PROVIDER} "
          f"payment={config.PAYMENT_PROVIDER}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
