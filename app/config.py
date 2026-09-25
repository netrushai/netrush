"""All settings come from environment variables (or a .env file next to server.py).

Every external integration defaults to "console" so the whole system runs and can be
demoed with no accounts: messages print to the terminal and lock calls are logged.
Swap a provider by setting its env var — nothing else changes.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name, default=""):
    return os.environ.get(name, default)


def env_int(name, default):
    return int(os.environ.get(name, default))


FACILITY_NAME = env("FACILITY_NAME", "NetRush")
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000")
PORT = env_int("PORT", 8000)
DB_PATH = env("DB_PATH", str(ROOT / "data" / "netrush.db"))

# Facility clock. India has no DST, so a fixed offset avoids needing tzdata on Windows.
TZ_OFFSET_MIN = env_int("TZ_OFFSET_MIN", 330)

OPEN_HOUR = env_int("OPEN_HOUR", 6)       # first slot starts
CLOSE_HOUR = env_int("CLOSE_HOUR", 23)    # last slot ends
SLOT_MIN = env_int("SLOT_MIN", 60)
BOOKING_HORIZON_DAYS = env_int("BOOKING_HORIZON_DAYS", 14)
HOLD_MIN = env_int("HOLD_MIN", 10)        # unpaid booking holds the slot this long

# Door code: works from this many minutes before the slot until the slot ends.
DOOR_CODE_LEAD_MIN = env_int("DOOR_CODE_LEAD_MIN", 10)
# 1 = send the code with the booking confirmation (it still only opens the door inside its window),
# plus a reminder DOOR_CODE_LEAD_MIN before start. 0 = send it only DOOR_CODE_LEAD_MIN before start.
DOOR_CODE_AT_BOOKING = env("DOOR_CODE_AT_BOOKING", "1") == "1"
DOOR_CODE_GRACE_MIN = env_int("DOOR_CODE_GRACE_MIN", 0)  # extra minutes after slot end
DOOR_CODE_DIGITS = env_int("DOOR_CODE_DIGITS", 6)

# Membership / academy-fee reminders: days-before-expiry that trigger a message.
REMINDER_DAYS = [int(x) for x in env("REMINDER_DAYS", "7,0").split(",")]
REMINDER_HOUR = env_int("REMINDER_HOUR", 10)  # local hour the daily reminder run happens

# Providers
SMS_PROVIDER = env("SMS_PROVIDER", "console")            # console | twilio | msg91
WHATSAPP_PROVIDER = env("WHATSAPP_PROVIDER", "console")  # console | meta
LOCK_PROVIDER = env("LOCK_PROVIDER", "console")          # console | http_bridge | ttlock
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "mock")       # mock | razorpay

TWILIO_SID = env("TWILIO_SID")
TWILIO_TOKEN = env("TWILIO_TOKEN")
TWILIO_FROM = env("TWILIO_FROM")

MSG91_AUTHKEY = env("MSG91_AUTHKEY")
MSG91_SENDER = env("MSG91_SENDER")
# India DLT: every SMS must match a registered template. One MSG91 flow id per message kind.
MSG91_FLOWS = {
    "door_code": env("MSG91_FLOW_DOOR_CODE"),
    "booking_confirmed": env("MSG91_FLOW_BOOKING"),
    "membership_reminder": env("MSG91_FLOW_MEMBERSHIP"),
    "academy_fee_reminder": env("MSG91_FLOW_ACADEMY"),
    "login_otp": env("MSG91_FLOW_LOGIN"),
    "booking_cancelled": env("MSG91_FLOW_CANCELLED"),
    "refund_issued": env("MSG91_FLOW_REFUND"),
    "membership_confirmed": env("MSG91_FLOW_MEMBERSHIP_OK"),
}

WA_TOKEN = env("WA_TOKEN")
WA_PHONE_NUMBER_ID = env("WA_PHONE_NUMBER_ID")
WA_API_VERSION = env("WA_API_VERSION", "v20.0")
WA_LANG = env("WA_LANG", "en")
# Business-initiated WhatsApp messages must use Meta-approved templates.
WA_TEMPLATES = {
    "door_code": env("WA_TPL_DOOR_CODE", "door_code"),
    "booking_confirmed": env("WA_TPL_BOOKING", "booking_confirmed"),
    "membership_reminder": env("WA_TPL_MEMBERSHIP", "membership_reminder"),
    "academy_fee_reminder": env("WA_TPL_ACADEMY", "academy_fee_reminder"),
    "login_otp": env("WA_TPL_LOGIN", "login_otp"),
    "booking_cancelled": env("WA_TPL_CANCELLED", "booking_cancelled"),
    "refund_issued": env("WA_TPL_REFUND", "refund_issued"),
    "membership_confirmed": env("WA_TPL_MEMBERSHIP_OK", "membership_confirmed"),
}

LOCK_BRIDGE_URL = env("LOCK_BRIDGE_URL")        # http_bridge: your on-site bridge service
LOCK_BRIDGE_SECRET = env("LOCK_BRIDGE_SECRET")
TTLOCK_API = env("TTLOCK_API", "https://euapi.ttlock.com")
TTLOCK_CLIENT_ID = env("TTLOCK_CLIENT_ID")
TTLOCK_ACCESS_TOKEN = env("TTLOCK_ACCESS_TOKEN")
TTLOCK_LOCK_ID = env("TTLOCK_LOCK_ID")

# Shared secret the door device uses to sign /api/lock/* calls (see access.py).
DEVICE_SECRET = env("DEVICE_SECRET", "change-me-device-secret")

RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET")  # catches payments whose browser closed early

# Demo mode, for showing the system without real SMS / payments / lock: login codes appear on
# screen, a "demo phone" page shows every SMS & WhatsApp sent, and a web keypad opens the door.
# On by default while SMS is in console mode. NEVER leave on in production.
DEMO_MODE = env("DEMO_MODE", "1" if SMS_PROVIDER == "console" else "0") == "1"

# Behind a hosting proxy (Render, Railway, nginx…) the client's IP is the last X-Forwarded-For entry.
TRUST_PROXY = env("TRUST_PROXY", "0") == "1"

ADMIN_PHONE = env("ADMIN_PHONE", "9999999999")
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "admin123")

# Courts are seeded once; edit here before first run (or in the DB afterwards).
COURTS = [
    ("B1", "badminton", "Badminton Court 1"),
    ("B2", "badminton", "Badminton Court 2"),
    ("P1", "padel", "Padel Court"),
    ("K1", "pickleball", "Pickleball Court 1"),
    ("K2", "pickleball", "Pickleball Court 2"),
]
# Price per court per hour, in rupees (a slot of SLOT_MIN minutes is charged pro rata).
PRICES_PER_HOUR = {
    "badminton": env_int("PRICE_BADMINTON", 1000),
    "padel": env_int("PRICE_PADEL", 1800),
    "pickleball": env_int("PRICE_PICKLEBALL", 500),
}
PRICES = {sport: p * SLOT_MIN // 60 for sport, p in PRICES_PER_HOUR.items()}

# Membership bought online in the member portal: all courts, fingerprint entry.
ONLINE_MEMBERSHIP_MONTHLY = env_int("ONLINE_MEMBERSHIP_MONTHLY", 2000)
ONLINE_MEMBERSHIP_MONTH_OPTIONS = [int(x) for x in env("ONLINE_MEMBERSHIP_MONTHS", "1,3,6").split(",")]

# Customer cancellation refunds: full refund if cancelled this many hours ahead, partial
# (REFUND_PARTIAL_PCT) if at least REFUND_PARTIAL_HOURS ahead, nothing after that.
# Cancelling is allowed until the slot starts. A cancellation by staff always refunds in full.
REFUND_FULL_HOURS = env_int("REFUND_FULL_HOURS", 24)
REFUND_PARTIAL_HOURS = env_int("REFUND_PARTIAL_HOURS", 6)
REFUND_PARTIAL_PCT = env_int("REFUND_PARTIAL_PCT", 50)
