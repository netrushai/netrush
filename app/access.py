"""Unmanned door access.

Two ways in:

1. Members / academy players — fingerprint. They enrol once at the device (the lock gives
   them a user id; staff store it as `lock_user_id`). We keep the lock in step with the
   business: fingerprint works while a membership (or paid-up academy fee) is current and is
   switched off the day it lapses — `sync_member_access()`.

2. Online bookers — a one-time door code. For each *paid* booking, DOOR_CODE_LEAD_MIN minutes
   before start we generate a code valid until the slot ends, send it by SMS + WhatsApp, and
   hand it to the lock. Unpaid, cancelled or finished bookings never have a working code.

Locks come in two flavours, both supported:
  • push  — the lock (or its cloud) stores time-limited passcodes and checks them offline
            (TTLock-family keypads, most ZKTeco/eSSL controllers via an on-site bridge).
            We push each code with its validity window and delete it afterwards.
  • pull  — a keypad controller (e.g. ESP32 + relay) asks us on every entry:
            POST /api/lock/verify {code}. We answer open / don't open.
Every entry the device reports (POST /api/lock/events) lands in `access_events`, which also
gives academy attendance for free.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import timedelta

from . import clock, config, db, notify


# ---------------------------------------------------------------- lock adapters

class ConsoleLock:
    """No hardware: prints what would be sent. Use for demos and to test the flow end to end."""

    def add_code(self, code, valid_from, valid_to, label):
        print(f"[LOCK] add passcode {code} for {label}: {clock.fmt(valid_from)} -> {clock.fmt(valid_to)}", flush=True)
        return "console-" + code

    def remove_code(self, lock_ref, code):
        print(f"[LOCK] remove passcode {code} ({lock_ref})", flush=True)

    def set_fingerprint_enabled(self, lock_user_id, enabled, valid_until):
        print(f"[LOCK] fingerprint user {lock_user_id}: {'ENABLE until ' + str(valid_until) if enabled else 'DISABLE'}",
              flush=True)


class HttpBridgeLock:
    """For controllers on the local network (ZKTeco, eSSL, Hikvision, Suprema…) that only speak
    their vendor SDK. A small bridge process on-site exposes three JSON endpoints and translates;
    we sign every request so nobody else on the network can open the door."""

    def _call(self, path, payload):
        body = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = hmac.new(config.LOCK_BRIDGE_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(config.LOCK_BRIDGE_URL.rstrip("/") + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Timestamp": ts,
                                              "X-Signature": sig})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def add_code(self, code, valid_from, valid_to, label):
        r = self._call("/codes", {"code": code, "valid_from": clock.fmt(valid_from),
                                  "valid_to": clock.fmt(valid_to), "label": label})
        return str(r.get("id", code))

    def remove_code(self, lock_ref, code):
        self._call("/codes/delete", {"id": lock_ref, "code": code})

    def set_fingerprint_enabled(self, lock_user_id, enabled, valid_until):
        self._call("/users", {"user_id": lock_user_id, "enabled": enabled,
                              "valid_until": valid_until and str(valid_until)})


class TTLock:
    """TTLock Open Platform (the cloud behind many Wi-Fi fingerprint + keypad locks sold in India).
    Needs a G2 gateway so changes reach the lock remotely. `lock_user_id` = the fingerprintId.
    Check endpoint names against your TTLock developer account before going live."""

    @staticmethod
    def _ms(dt):
        return int((dt - timedelta(minutes=config.TZ_OFFSET_MIN)).timestamp() * 1000) if dt else 0

    def _call(self, path, params):
        params = {"clientId": config.TTLOCK_CLIENT_ID, "accessToken": config.TTLOCK_ACCESS_TOKEN,
                  "lockId": config.TTLOCK_LOCK_ID, "date": int(time.time() * 1000), **params}
        req = urllib.request.Request(config.TTLOCK_API + path, data=urllib.parse.urlencode(params).encode(),
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read())
        if out.get("errcode", 0) != 0:
            raise RuntimeError(f"TTLock {path}: {out}")
        return out

    def add_code(self, code, valid_from, valid_to, label):
        out = self._call("/v3/keyboardPwd/add", {
            "keyboardPwd": code, "keyboardPwdName": label[:30], "addType": 2,
            "startDate": self._ms(valid_from), "endDate": self._ms(valid_to)})
        return str(out["keyboardPwdId"])

    def remove_code(self, lock_ref, code):
        self._call("/v3/keyboardPwd/delete", {"keyboardPwdId": lock_ref, "deleteType": 2})

    def set_fingerprint_enabled(self, lock_user_id, enabled, valid_until):
        from datetime import datetime
        now = clock.now()
        end = (datetime.combine(valid_until, datetime.max.time().replace(microsecond=0))
               if enabled else now - timedelta(minutes=1))
        self._call("/v3/fingerprint/changePeriod", {
            "fingerprintId": lock_user_id, "changeType": 2,
            "startDate": self._ms(now - timedelta(days=1)), "endDate": self._ms(end)})


def lock():
    return {"console": ConsoleLock, "http_bridge": HttpBridgeLock, "ttlock": TTLock}[config.LOCK_PROVIDER]()


# ---------------------------------------------------------------- door codes (bookings)

def _new_code(c):
    """Random code not currently active — two groups on court never share a code."""
    active = {r[0] for r in c.execute("SELECT code FROM door_codes WHERE status='active'")}
    while True:
        code = f"{secrets.randbelow(10 ** config.DOOR_CODE_DIGITS):0{config.DOOR_CODE_DIGITS}d}"
        if code not in active and len(set(code)) > 1:  # skip 000000, 111111…
            return code


def issue_due_codes():
    """Create + send codes for confirmed bookings: straight away if DOOR_CODE_AT_BOOKING, else once
    the window opens. The code only opens the door from DOOR_CODE_LEAD_MIN before start. Safe to
    call any time; UNIQUE(booking_id) on door_codes makes it idempotent."""
    now = clock.now()
    lead = timedelta(minutes=config.DOOR_CODE_LEAD_MIN)
    horizon = now + (timedelta(days=config.BOOKING_HORIZON_DAYS + 1) if config.DOOR_CODE_AT_BOOKING else lead)
    due = db.all_(
        "SELECT b.*, c.name AS court_name FROM bookings b JOIN courts c ON c.id=b.court_id "
        "LEFT JOIN door_codes d ON d.booking_id=b.id "
        "WHERE b.status='confirmed' AND d.id IS NULL AND b.start <= ? AND b.end > ?",
        clock.fmt(horizon), clock.fmt(now))
    issued = []
    for b in due:
        start, end = clock.parse(b["start"]), clock.parse(b["end"])
        valid_from = start - lead if start - lead > now else now
        valid_to = end + timedelta(minutes=config.DOOR_CODE_GRACE_MIN)
        with db.tx() as c:
            if c.execute("SELECT 1 FROM door_codes WHERE booking_id=?", (b["id"],)).fetchone():
                continue  # another thread got here first
            code = _new_code(c)
            cur = c.execute(
                "INSERT INTO door_codes(booking_id,code,valid_from,valid_to,status,created_at) "
                "VALUES(?,?,?,?,'active',?)",
                (b["id"], code, clock.fmt(valid_from), clock.fmt(valid_to), clock.fmt(now)))
            dc_id = cur.lastrowid
        try:
            ref = lock().add_code(code, valid_from, valid_to, f"{b['ref']} {b['name']}")
            db.conn().execute("UPDATE door_codes SET lock_ref=? WHERE id=?", (ref, dc_id))
        except Exception as e:  # noqa: BLE001
            # Pull-mode devices still work (they ask /api/lock/verify). Flag it for the desk.
            _log_event(None, "system", None, b["id"], False, f"lock push failed: {e}")
        notify.send("door_code", b["phone"], _door_params(b, code, valid_from, valid_to),
                    dedupe_key=f"door:{b['id']}")
        if valid_from <= now:  # already inside the window: that message was the reminder too
            db.kv_set(f"door_reminded:{b['id']}", "1")
        issued.append({"booking_id": b["id"], "code": code})
    send_door_reminders()
    return issued


def _door_params(b, code, valid_from, valid_to):
    return {"name": b["name"].split()[0], "code": code, "court": b["court_name"],
            "valid_from": valid_from.strftime("%a %d %b %H:%M"), "valid_to": valid_to.strftime("%H:%M")}


def send_door_reminders():
    """Codes sent days ahead get re-sent when the window opens, so nobody hunts for an old SMS."""
    now = clock.now()
    for d in db.all_(
        "SELECT d.*, b.name, b.phone, c.name AS court_name FROM door_codes d JOIN bookings b ON b.id=d.booking_id "
        "JOIN courts c ON c.id=b.court_id WHERE d.status='active' AND d.valid_from <= ? AND d.valid_to > ?",
        clock.fmt(now), clock.fmt(now)):
        if db.kv_get(f"door_reminded:{d['booking_id']}"):
            continue
        db.kv_set(f"door_reminded:{d['booking_id']}", "1")
        notify.send("door_code", d["phone"], _door_params(d, d["code"], clock.parse(d["valid_from"]),
                                                          clock.parse(d["valid_to"])),
                    dedupe_key=f"door-reminder:{d['booking_id']}")


def expire_codes():
    now = clock.fmt(clock.now())
    for d in db.all_("SELECT * FROM door_codes WHERE status='active' AND valid_to <= ?", now):
        _retire(d, "expired")


def revoke_for_booking(booking_id, reason):
    d = db.one("SELECT * FROM door_codes WHERE booking_id=? AND status='active'", booking_id)
    if d:
        _retire(d, "revoked")
        _log_event(None, "system", None, booking_id, False, f"code revoked: {reason}")


def _retire(d, status):
    db.conn().execute("UPDATE door_codes SET status=? WHERE id=?", (status, d["id"]))
    try:
        lock().remove_code(d["lock_ref"], d["code"])
    except Exception as e:  # noqa: BLE001 — the code dies at valid_to on the lock anyway
        _log_event(None, "system", None, d["booking_id"], False, f"lock delete failed: {e}")


# Brute-force guard for keypad devices: MAX_FAILS wrong codes in FAIL_WINDOW → keypad ignored
# for LOCKOUT. Fingerprints are unaffected.
MAX_FAILS, FAIL_WINDOW, LOCKOUT = 5, 300, 300
_fails = {}
_fails_lock = threading.Lock()


def verify_code(code, device_id="door-1"):
    """Pull-mode check. Returns {'open': bool, 'reason': str}."""
    t = time.time()
    with _fails_lock:
        recent = [x for x in _fails.get(device_id, []) if t - x < FAIL_WINDOW + LOCKOUT]
        _fails[device_id] = recent
        if len([x for x in recent if t - x < FAIL_WINDOW]) >= MAX_FAILS and t - recent[-1] < LOCKOUT:
            _log_event(device_id, "code", None, None, False, "keypad locked out (too many wrong codes)")
            return {"open": False, "reason": "locked_out"}
    now = clock.fmt(clock.now())
    d = db.one("SELECT d.*, b.user_id FROM door_codes d JOIN bookings b ON b.id=d.booking_id "
               "WHERE d.code=? AND d.status='active' AND d.valid_from <= ? AND d.valid_to > ?",
               (code or "").strip(), now, now)
    if not d:
        early = db.one("SELECT booking_id, valid_from FROM door_codes WHERE code=? AND status='active' AND valid_from > ?",
                       (code or "").strip(), now)
        if early:  # a real code, just too soon: tell them when, and don't count it as a guess
            _log_event(device_id, "code", None, early["booking_id"], False, "code not valid yet")
            return {"open": False, "reason": "too_early", "valid_from": early["valid_from"]}
        with _fails_lock:
            _fails.setdefault(device_id, []).append(t)
        _log_event(device_id, "code", None, None, False, "wrong or expired code")
        return {"open": False, "reason": "invalid"}
    db.conn().execute("UPDATE door_codes SET uses=uses+1 WHERE id=?", (d["id"],))
    _log_event(device_id, "code", d["user_id"], d["booking_id"], True, "booking code")
    return {"open": True, "reason": "booking", "valid_to": d["valid_to"]}


# ---------------------------------------------------------------- fingerprints (members)

def entitlement(user_id, day=None):
    """Last day this person may enter by fingerprint, or None. Membership or paid academy fees."""
    day = (day or clock.today()).isoformat()
    r = db.one(
        "SELECT MAX(d) AS until FROM ("
        " SELECT end_date AS d FROM memberships WHERE user_id=? AND status='active' AND start_date<=? AND end_date>=?"
        " UNION ALL"
        " SELECT fee_paid_until FROM academy_enrollments WHERE user_id=? AND status='active' AND fee_paid_until>=?)",
        user_id, day, day, user_id, day)
    return r["until"] if r else None


def sync_member_access():
    """Make the lock's enabled/disabled state match who has paid. Runs every scheduler tick;
    only touches the lock when something changed."""
    from datetime import date
    changes = []
    for u in db.all_("SELECT id, name, lock_user_id, lock_enabled FROM users WHERE lock_user_id IS NOT NULL "
                     "AND lock_user_id <> ''"):
        until = entitlement(u["id"])
        want = 1 if until else 0
        key = f"lock_until:{u['id']}"
        if want == u["lock_enabled"] and (not want or db.kv_get(key) == until):
            continue
        try:
            lock().set_fingerprint_enabled(u["lock_user_id"], bool(want), until and date.fromisoformat(until))
            db.conn().execute("UPDATE users SET lock_enabled=? WHERE id=?", (want, u["id"]))
            db.kv_set(key, until or "")
            changes.append((u["name"], bool(want), until))
        except Exception as e:  # noqa: BLE001 — retried next tick
            _log_event(None, "system", u["id"], None, False, f"lock sync failed: {e}")
    return changes


def verify_fingerprint(lock_user_id, device_id="door-1"):
    """Pull-mode for controllers that ask before opening on a fingerprint match."""
    u = db.one("SELECT id FROM users WHERE lock_user_id=?", str(lock_user_id))
    ok = bool(u and entitlement(u["id"]))
    _log_event(device_id, "fingerprint", u and u["id"], None, ok,
               "member" if ok else ("unknown fingerprint id" if not u else "membership expired"))
    return {"open": ok, "reason": "member" if ok else "not_entitled"}


def record_device_event(ev, device_id):
    """Push-mode devices report entries after the fact. ev: {method, user_id|code, granted, at?}."""
    user_id = booking_id = None
    if ev.get("method") == "fingerprint" and ev.get("user_id") is not None:
        u = db.one("SELECT id FROM users WHERE lock_user_id=?", str(ev["user_id"]))
        user_id = u and u["id"]
    elif ev.get("code"):
        d = db.one("SELECT booking_id FROM door_codes WHERE code=? ORDER BY id DESC LIMIT 1", str(ev["code"]))
        booking_id = d and d["booking_id"]
    _log_event(device_id, ev.get("method", "unknown"), user_id, booking_id, bool(ev.get("granted", True)),
               "reported by device", at=ev.get("at"))


def _log_event(device_id, method, user_id, booking_id, granted, detail, at=None):
    db.conn().execute(
        "INSERT INTO access_events(at,device_id,method,user_id,booking_id,granted,detail) VALUES(?,?,?,?,?,?,?)",
        (at or clock.fmt(clock.now()), device_id, method, user_id, booking_id, int(granted), detail))


# ---------------------------------------------------------------- device authentication

def check_device_signature(headers, body, max_skew=120):
    """Door devices sign requests: X-Signature = hex(HMAC-SHA256(DEVICE_SECRET, f"{X-Timestamp}." + body)).
    The timestamp window stops a captured 'open' request being replayed later."""
    ts, sig = headers.get("X-Timestamp", ""), headers.get("X-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > max_skew:
        return False
    expected = hmac.new(config.DEVICE_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
