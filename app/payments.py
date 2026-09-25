"""Money in and money back out. One `payments` row per charge, whatever it paid for.

Online: `create_order` → customer pays in Razorpay Checkout → `capture` verifies the signature
(or the webhook reports it) → the thing paid for is fulfilled. Nothing is ever fulfilled on the
browser's word alone.

Refunds are automatic: `request_refund` queues one and `process_refunds` (called at once, and
again every scheduler tick) sends it to the gateway, retrying a few times before flagging it for
staff. Cash / UPI taken at the desk can't be refunded by the gateway, so those are flagged
'manual' for the desk to hand back.

provider: mock (demo, instant) | razorpay | desk (cash/UPI at the counter)
"""
import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request

from . import clock, config, db

MAX_REFUND_ATTEMPTS = 5


class PaymentError(Exception):
    pass


def _razorpay(method, path, body=None):
    auth = base64.b64encode(f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request("https://api.razorpay.com/v1" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PaymentError(f"Razorpay {e.code}: {e.read().decode()[:300]}")


def get(payment_id):
    return db.one("SELECT * FROM payments WHERE id=?", payment_id)


def for_ref(kind, ref_id):
    """The successful payment for a booking / membership / enrollment, if any."""
    return db.one("SELECT * FROM payments WHERE kind=? AND ref_id=? AND status='paid' ORDER BY id DESC LIMIT 1",
                  kind, ref_id)


def create_order(kind, amount, receipt, phone, user_id=None, ref_id=None, meta=None):
    """Start an online payment. Returns what the browser needs to open Checkout."""
    provider = config.PAYMENT_PROVIDER
    if provider not in ("mock", "razorpay"):
        raise PaymentError(f"unknown PAYMENT_PROVIDER {provider}")
    order_id = None
    if provider == "razorpay":
        order_id = _razorpay("POST", "/orders", {"amount": amount * 100, "currency": "INR", "receipt": receipt})["id"]
    secret = secrets.token_urlsafe(16)
    pid = db.conn().execute(
        "INSERT INTO payments(kind,ref_id,user_id,phone,amount,provider,order_id,status,meta,created_at,secret) "
        "VALUES(?,?,?,?,?,?,?,'created',?,?,?)",
        (kind, ref_id, user_id, phone, amount, provider, order_id, json.dumps(meta or {}),
         clock.fmt(clock.now()), secret)).lastrowid
    # The browser is redirected to our payment page, which runs the gateway (or the mock one).
    out = {"provider": provider, "payment_id": pid, "amount": amount, "redirect": f"/pay?id={pid}&k={secret}"}
    if provider == "razorpay":
        out.update(key_id=config.RAZORPAY_KEY_ID, order_id=order_id, amount_paise=amount * 100, currency="INR")
    return out


def capture(payment_row_id, payload):
    """Verify the gateway's proof of payment and mark the row paid. Idempotent.
    Returns (row, newly_paid)."""
    p = get(payment_row_id)
    if not p:
        raise PaymentError("Payment not found")
    if p["status"] == "paid":
        return p, False
    if p["provider"] == "mock":
        gateway_id = f"MOCK-{p['id']}"
    elif p["provider"] == "razorpay":
        order_id = payload.get("razorpay_order_id", "")
        gateway_id = payload.get("razorpay_payment_id", "")
        sig = payload.get("razorpay_signature", "")
        expected = hmac.new(config.RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{gateway_id}".encode(),
                            hashlib.sha256).hexdigest()
        # The order id must be *this* row's order: a valid receipt for a ₹500 slot can't pay a ₹6000 plan.
        if order_id != p["order_id"] or not hmac.compare_digest(expected, sig):
            raise PaymentError("Payment could not be verified")
    else:
        raise PaymentError("This payment can't be captured online")
    cur = db.conn().execute("UPDATE payments SET status='paid', payment_id=?, paid_at=? WHERE id=? AND status<>'paid'",
                            (gateway_id, clock.fmt(clock.now()), p["id"]))
    return get(p["id"]), cur.rowcount == 1


def capture_from_webhook(raw_body, signature):
    """Razorpay webhook (payment.captured). Returns the payments row it settled, or None."""
    if not config.RAZORPAY_WEBHOOK_SECRET:
        raise PaymentError("Webhook secret not configured")
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise PaymentError("Bad webhook signature")
    ev = json.loads(raw_body)
    if ev.get("event") != "payment.captured":
        return None
    ent = ev["payload"]["payment"]["entity"]
    p = db.one("SELECT * FROM payments WHERE order_id=?", ent.get("order_id"))
    if not p or p["status"] == "paid":
        return None
    db.conn().execute("UPDATE payments SET status='paid', payment_id=?, paid_at=? WHERE id=? AND status<>'paid'",
                      (ent["id"], clock.fmt(clock.now()), p["id"]))
    return get(p["id"])


def record_desk(kind, ref_id, amount, phone, user_id=None, staff="desk", meta=None):
    """Cash / UPI taken at the counter."""
    now = clock.fmt(clock.now())
    pid = db.conn().execute(
        "INSERT INTO payments(kind,ref_id,user_id,phone,amount,provider,payment_id,status,meta,created_at,paid_at) "
        "VALUES(?,?,?,?,?,'desk',?,'paid',?,?,?)",
        (kind, ref_id, user_id, phone, amount, "DESK-" + staff, json.dumps(meta or {}), now, now)).lastrowid
    return get(pid)


def request_refund(payment_row_id, amount):
    p = get(payment_row_id)
    if not p or p["status"] != "paid" or amount <= 0 or p["refund_status"]:
        return None
    amount = min(amount, p["amount"])
    status = "manual" if p["provider"] == "desk" else "pending"
    db.conn().execute("UPDATE payments SET refund_status=?, refund_amount=? WHERE id=?", (status, amount, p["id"]))
    process_refunds()
    return get(p["id"])


def process_refunds():
    """Send queued refunds to the gateway. Safe to call repeatedly."""
    from . import notify
    done = []
    for p in db.all_("SELECT * FROM payments WHERE refund_status='pending'"):
        try:
            if p["provider"] == "mock":
                refund_id = f"RFND-MOCK-{p['id']}"
            else:
                refund_id = _razorpay("POST", f"/payments/{p['payment_id']}/refund",
                                      {"amount": p["refund_amount"] * 100, "speed": "normal"})["id"]
        except Exception as e:  # noqa: BLE001 — retried next tick
            attempts = p["refund_attempts"] + 1
            db.conn().execute(
                "UPDATE payments SET refund_attempts=?, refund_error=?, refund_status=? WHERE id=?",
                (attempts, str(e)[:500], "failed" if attempts >= MAX_REFUND_ATTEMPTS else "pending", p["id"]))
            continue
        db.conn().execute("UPDATE payments SET refund_status='done', refund_id=?, refunded_at=?, refund_error=NULL "
                          "WHERE id=?", (refund_id, clock.fmt(clock.now()), p["id"]))
        notify.send("refund_issued", p["phone"], {
            "amount": f"Rs {p['refund_amount']}", "what": _describe(p),
            "days": "5-7 working days" if p["provider"] == "razorpay" else "a few minutes",
        }, dedupe_key=f"refund:{p['id']}")
        done.append(p["id"])
    return done


def retry_refund(payment_row_id):
    """Staff: try a failed refund again."""
    db.conn().execute("UPDATE payments SET refund_status='pending', refund_attempts=0 WHERE id=? "
                      "AND refund_status='failed'", (payment_row_id,))
    process_refunds()
    return get(payment_row_id)


def mark_manual_refund_done(payment_row_id, staff):
    db.conn().execute("UPDATE payments SET refund_status='done', refund_id=?, refunded_at=? WHERE id=? "
                      "AND refund_status IN ('manual','failed')",
                      ("HANDED-BACK-" + staff, clock.fmt(clock.now()), payment_row_id))
    return get(payment_row_id)


def _describe(p):
    if p["kind"] == "booking":
        b = db.one("SELECT ref FROM bookings WHERE id=?", p["ref_id"])
        return f"booking {b['ref']}" if b else "your booking"
    return "your " + p["kind"]


def for_payer(payment_row_id, secret):
    """The payment behind a /pay link, if the link's secret matches."""
    p = get(payment_row_id)
    if not p or not p["secret"] or not hmac.compare_digest(p["secret"], secret or ""):
        return None
    return p
