import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,       -- 10-digit, no country code
    email         TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,                       -- admins only
    sms_opt       INTEGER NOT NULL DEFAULT 1,
    wa_opt        INTEGER NOT NULL DEFAULT 1,
    lock_user_id  TEXT,                       -- fingerprint user id enrolled on the lock
    lock_enabled  INTEGER NOT NULL DEFAULT 0, -- what we last told the lock
    guardian_name TEXT,                       -- academy juniors
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courts (
    id    TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    name  TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    sport         TEXT,                        -- NULL = all sports
    duration_days INTEGER NOT NULL,
    price         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    plan_id    INTEGER NOT NULL REFERENCES plans(id),
    start_date TEXT NOT NULL,                  -- YYYY-MM-DD
    end_date   TEXT NOT NULL,                  -- last valid day, inclusive
    status     TEXT NOT NULL DEFAULT 'active', -- active | cancelled
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS academy_batches (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    coach       TEXT,
    weekdays    TEXT NOT NULL,     -- '0,2,4' = Mon,Wed,Fri
    start_time  TEXT NOT NULL,     -- HH:MM
    end_time    TEXT NOT NULL,
    court_ids   TEXT NOT NULL,     -- 'B1,B2' — blocked from public booking at those times
    monthly_fee INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS academy_enrollments (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    batch_id       INTEGER NOT NULL REFERENCES academy_batches(id),
    level          TEXT,
    fee_paid_until TEXT NOT NULL,  -- YYYY-MM-DD inclusive
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS court_blocks (
    id       INTEGER PRIMARY KEY,
    court_id TEXT NOT NULL,
    date     TEXT NOT NULL,        -- one-off block (maintenance, tournament)
    start    TEXT NOT NULL,        -- HH:MM
    end      TEXT NOT NULL,
    reason   TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY,
    ref          TEXT NOT NULL UNIQUE,          -- short public reference
    court_id     TEXT NOT NULL REFERENCES courts(id),
    user_id      INTEGER REFERENCES users(id),
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    start        TEXT NOT NULL,                 -- YYYY-MM-DD HH:MM
    end          TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL,                 -- pending | confirmed | cancelled | expired
    hold_until   TEXT,
    payment_ref  TEXT,
    paid_at      TEXT,
    created_at   TEXT NOT NULL,
    pay_status   TEXT NOT NULL DEFAULT 'unpaid', -- unpaid | paid | comp (complimentary)
    source       TEXT NOT NULL DEFAULT 'online', -- online | desk | phone
    cancelled_at TEXT,
    cancelled_by TEXT,                          -- customer | staff | system
    refund_amount INTEGER NOT NULL DEFAULT 0,
    email        TEXT,
    access_key   TEXT                           -- secret in the confirmation link (no login needed)
);
CREATE INDEX IF NOT EXISTS ix_bookings_court_start ON bookings(court_id, start);

-- Every rupee in (and back out). Bookings, memberships and academy fees, online or at the desk.
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,     -- booking | membership | academy
    ref_id          INTEGER,           -- booking / membership / enrollment id (membership: set once fulfilled)
    user_id         INTEGER,
    phone           TEXT,
    amount          INTEGER NOT NULL,  -- rupees
    provider        TEXT NOT NULL,     -- mock | razorpay | desk
    order_id        TEXT,              -- gateway order id
    payment_id      TEXT,              -- gateway payment id
    status          TEXT NOT NULL,     -- created | paid
    meta            TEXT,              -- JSON, e.g. {"months": 3}
    created_at      TEXT NOT NULL,
    paid_at         TEXT,
    refund_status   TEXT,              -- NULL | pending | done | failed | manual (cash to hand back)
    refund_amount   INTEGER NOT NULL DEFAULT 0,
    refund_id       TEXT,
    refund_error    TEXT,
    refund_attempts INTEGER NOT NULL DEFAULT 0,
    refunded_at     TEXT,
    secret          TEXT               -- in the payment page link, so only the payer can open it
);
CREATE INDEX IF NOT EXISTS ix_payments_ref ON payments(kind, ref_id);

CREATE TABLE IF NOT EXISTS door_codes (
    id          INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    code        TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    status      TEXT NOT NULL,     -- active | revoked | expired
    lock_ref    TEXT,              -- id the lock vendor gave the pushed passcode
    uses        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_events (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    device_id  TEXT,
    method     TEXT NOT NULL,      -- code | fingerprint | manual
    user_id    INTEGER,
    booking_id INTEGER,
    granted    INTEGER NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE,        -- stops a reminder going out twice
    kind       TEXT NOT NULL,
    channel    TEXT NOT NULL,      -- sms | whatsapp
    to_phone   TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL,      -- sent | failed
    error      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_otps (
    phone      TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_local = threading.local()
_path = None


def init(path=None):
    """Open (and create) the database. Call once at startup; tests pass ':memory:'-like temp paths."""
    global _path
    _path = path or config.DB_PATH
    if _path != ":memory:":
        Path(_path).parent.mkdir(parents=True, exist_ok=True)
    _local.conn = None
    c = conn()
    _upgrade(c)
    c.executescript(SCHEMA)
    from . import seed
    c.execute("BEGIN")  # one transaction: seeding thousands of demo rows one commit at a time is slow
    seed.seed(c)
    seed.ensure_online_plan(c)
    c.execute("COMMIT")


# Columns added after the first release, for databases created before them.
_ADDED = {
    "bookings": [
        ("pay_status", "TEXT NOT NULL DEFAULT 'unpaid'"), ("source", "TEXT NOT NULL DEFAULT 'online'"),
        ("cancelled_at", "TEXT"), ("cancelled_by", "TEXT"), ("refund_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("email", "TEXT"), ("access_key", "TEXT"),
    ],
    "payments": [("secret", "TEXT")],
}


def _upgrade(c):
    for table, cols in _ADDED.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # fresh database; SCHEMA creates it complete
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if (table, name) == ("bookings", "pay_status"):
                    c.execute("UPDATE bookings SET pay_status='paid' WHERE paid_at IS NOT NULL")


def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _path:
        c = sqlite3.connect(_path, timeout=10, isolation_level=None)  # autocommit; explicit BEGINs
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        _local.conn = c
        _local.path = _path
    return c


class tx:
    """`with tx() as c:` — BEGIN IMMEDIATE so two people can't grab the same slot."""

    def __enter__(self):
        self.c = conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, *_):
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def one(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def all_(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", k)
    return r["v"] if r else default


def kv_set(k, v):
    conn().execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


TABLES = ["sessions", "login_otps", "notifications", "access_events", "door_codes", "payments", "bookings",
          "court_blocks", "academy_enrollments", "academy_batches", "memberships", "plans", "courts", "users", "kv"]


def reset():
    """Demo only: wipe everything and seed fresh demo data."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    for t in TABLES:
        c.execute(f"DELETE FROM {t}")
    from . import seed
    seed.seed(c)
    seed.ensure_online_plan(c)
    c.execute("COMMIT")
