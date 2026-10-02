"""Logins. Customers, members and academy players sign in with their phone + a one-time code
(no passwords to forget; a first login creates the account). Staff use phone + password."""
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import timedelta

from . import clock, config, db, notify

SESSION_DAYS = 30
OTP_TTL_MIN = 5
OTP_MAX_ATTEMPTS = 5


class AuthError(Exception):
    pass


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_password(pw, stored):
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(pw, salt), stored)


def normalize_phone(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10:
        raise AuthError("Enter a 10-digit mobile number")
    return digits


def member_id(user_id):
    """Human-friendly ID printed on the member card and portal, e.g. NR-0002."""
    return f"NR-{int(user_id):04d}"


def phone_for(identifier):
    """Login accepts a mobile number or a member ID (NR-0002 / nr2)."""
    m = re.fullmatch(r"\s*nr-?\s*0*(\d+)\s*", identifier or "", re.I)
    if m:
        u = db.one("SELECT phone FROM users WHERE id=? AND is_admin=0", int(m.group(1)))
        if not u:
            raise AuthError("No member with that ID")
        return u["phone"]
    return normalize_phone(identifier)


def _code_hash(phone, code):
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


# Codes cost money to send and anyone can ask for one, so cap requests per client IP too.
IP_MAX_REQUESTS, IP_WINDOW = (30 if config.DEMO_MODE else 5), 15 * 60  # demo: many people, one office IP
_ip_hits = {}
_ip_lock = threading.Lock()


def _ip_allowed(ip):
    if not ip:
        return True
    t = time.time()
    with _ip_lock:
        hits = [x for x in _ip_hits.get(ip, []) if t - x < IP_WINDOW]
        if len(hits) >= IP_MAX_REQUESTS:
            _ip_hits[ip] = hits
            return False
        _ip_hits[ip] = hits + [t]
        return True


def request_login_code(phone, ip=None):
    """Send a one-time code. Works for any number: new people sign up by logging in.
    Returns (code, is_new, phone). The HTTP layer shows the code only in DEMO_MODE."""
    phone = phone_for(phone)
    prev = db.one("SELECT expires_at FROM login_otps WHERE phone=?", phone)
    if prev and clock.parse(prev["expires_at"]) - timedelta(minutes=OTP_TTL_MIN - 1) >= clock.now():
        raise AuthError("A code was just sent. Please wait a minute before asking again.")
    if not _ip_allowed(ip):
        raise AuthError("Too many codes requested. Please try again in a few minutes.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.conn().execute(
        "INSERT INTO login_otps(phone,code_hash,expires_at,attempts) VALUES(?,?,?,0) "
        "ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at, attempts=0",
        (phone, _code_hash(phone, code), clock.fmt(clock.now() + timedelta(minutes=OTP_TTL_MIN))),
    )
    notify.send("login_otp", phone, {"code": code})
    return code, not db.one("SELECT 1 AS x FROM users WHERE phone=?", phone), phone


def consume_code(phone, code):
    """Check a one-time code and use it up. Proves the caller holds this phone."""
    phone = normalize_phone(phone)
    row = db.one("SELECT * FROM login_otps WHERE phone=?", phone)
    if not row or clock.parse(row["expires_at"]) < clock.now():
        raise AuthError("Code expired. Request a new one.")
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        raise AuthError("Too many wrong attempts. Request a new code.")
    if not hmac.compare_digest(row["code_hash"], _code_hash(phone, (code or "").strip())):
        db.conn().execute("UPDATE login_otps SET attempts=attempts+1 WHERE phone=?", (phone,))
        raise AuthError("Wrong code")
    db.conn().execute("DELETE FROM login_otps WHERE phone=?", (phone,))
    return phone


def verify_login_code(phone, code, name=None):
    phone = phone_for(phone)
    user = db.one("SELECT id FROM users WHERE phone=?", phone)
    if not user and not (name or "").strip():
        raise AuthError("Please tell us your name")
    consume_code(phone, code)
    if not user:
        uid = db.conn().execute("INSERT INTO users(name,phone,created_at) VALUES(?,?,?)",
                                (name.strip()[:80], phone, clock.fmt(clock.now()))).lastrowid
        db.conn().execute("UPDATE bookings SET user_id=? WHERE phone=? AND user_id IS NULL", (uid, phone))
        from . import coupons
        coupons.issue_welcome(uid)  # registering for the first time: % off their next court booking
        return new_session(uid)
    return new_session(user["id"])


def admin_login(phone, password):
    phone = normalize_phone(phone)
    u = db.one("SELECT id, password_hash FROM users WHERE phone=? AND is_admin=1", phone)
    if not u or not check_password(password or "", u["password_hash"]):
        raise AuthError("Wrong phone or password")
    return new_session(u["id"])


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    db.conn().execute(
        "INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
        (token, user_id, clock.fmt(clock.now() + timedelta(days=SESSION_DAYS))),
    )
    return token


def user_for(token):
    if not token:
        return None
    s = db.one("SELECT user_id, expires_at FROM sessions WHERE token=?", token)
    if not s or clock.parse(s["expires_at"]) < clock.now():
        return None
    return db.one("SELECT id,name,phone,email,is_admin,sms_opt,wa_opt,lock_user_id,guardian_name "
                  "FROM users WHERE id=?", s["user_id"])


def logout(token):
    db.conn().execute("DELETE FROM sessions WHERE token=?", (token,))
