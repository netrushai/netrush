# NetRush: court booking, memberships, academy and unmanned door access

For an arena with **2 badminton courts (₹1,000/h), 1 padel court (₹1,800/h) and 2 pickleball courts (₹500/h)**.

| Who | What they get |
|---|---|
| **Anyone** (no signup) | Picks sport, day and slot. Enters name, mobile and email. Redirected to payment. Gets the **confirmation and door code by SMS + WhatsApp**. The code opens the door from 10 minutes before the slot until it ends. Can cancel from the confirmation link, with an **automatic refund** (100% ≥24h before, 50% ≥6h, none after). |
| **Members** | Fingerprint entry while their plan is active. Portal login with mobile or member ID (`NR-0002`) + OTP. **Buy or renew online at ₹2,000/month** (1/3/6 months, renewals start the day after the current plan ends). Reminders 7 days before expiry and on the last day, by SMS and/or WhatsApp. |
| **Academy players** | Fingerprint entry while fees are paid. Batch, coach, schedule, fee status and attendance in the portal. Fee reminders at 7 days and on the last day. |
| **Front desk** (`/admin`) | Today's bookings with door codes. **Phone reservations without payment** (confirmed, door code sent, "Mark paid" later), paid-at-desk and complimentary bookings. Members, renewals, academy fees, court blocks, refunds to hand back. **Analytics & revenue** and a **health page showing what's working and what isn't**. |

Python standard library + SQLite. No dependencies, no build step.

## Run it

```bash
python server.py
```

Open <http://localhost:8000>. **Demo mode** is on by default: nothing real is sent or charged, and:

- `/demo` is a **demo phone** showing every SMS and WhatsApp, plus the demo logins
- `/door` is a **web door keypad** (type a door code, or scan a demo fingerprint)
- login codes appear on screen

| Role | Login |
|---|---|
| Admin | `9999999999` / `admin123` |
| Member | `NR-0002` (or `9000000001`), code shown on screen |
| Academy player | `NR-0005` (or `9000000004`), code shown on screen |

The first run seeds a month of realistic dummy data: 18 members and players, 3 academy batches, ~700 past bookings,
upcoming bookings of every kind, door entries, messages and a cash refund waiting. Reset it any time from
**Admin → Health → Reset demo data**.

```bash
python -m unittest discover -s tests -v
```

That runs 31 tests: double booking, door-code windows, reminders, refunds, phone reservations, online membership, analytics.

**Hosting it for the client:** see [DEPLOY.md](DEPLOY.md) (Render free tier in about 15 minutes, or Railway / your own VPS).

## How the door works

```
paid (or staff-confirmed) booking → 6-digit code by SMS + WhatsApp right away, pushed to the lock
                                    opens the door from start − 10 min until the slot ends; resent when it becomes valid
cancelled → code deleted at once        slot over → code deleted

membership / academy fees active → fingerprint enabled on the lock;  lapsed → disabled;  renewed → enabled again
```

- 5 wrong codes in 5 minutes locks the keypad for 5 minutes. A real code typed too early says when it opens.
- Every entry goes into the access log. For academy players, that log is their attendance.

**Choosing the lock** is the one hardware decision. Three adapters exist in `app/access.py`:

| Lock type | `LOCK_PROVIDER` | How |
|---|---|---|
| Wi-Fi fingerprint + keypad lock on the **TTLock** platform (G2 gateway) | `ttlock` | Time-limited passcodes and fingerprint validity pushed to TTLock cloud |
| **Biometric controller** (ZKTeco, eSSL, Hikvision) + electric lock | `http_bridge` | Small on-site bridge speaks the vendor SDK; signed JSON calls |
| **Custom keypad + relay** (ESP32 / Pi) | `console` | Device asks `POST /api/lock/verify` on each entry (see `tools/door_device.py`) |

Buy a lock that can **create time-limited codes remotely** and **disable a fingerprint remotely**. Keep a mechanical
key override and a UPS.

Device API (HMAC-signed: `X-Timestamp`, `X-Signature = hex(HMAC-SHA256(DEVICE_SECRET, ts + "." + body))`):

```
POST /api/lock/verify   {"code":"482913"}             → {"open":true,"reason":"booking"}
POST /api/lock/verify   {"fingerprint_user_id":"101"} → {"open":false,"reason":"not_entitled"}
POST /api/lock/events   {"events":[{"method":"fingerprint","user_id":"101","granted":true}]}
```

## Money

Every rupee in and out is a row in `payments`: online (mock / Razorpay) and at the desk, for bookings, memberships
and academy fees.

- **Payment redirect:** booking → `/pay?id&k` (mock gateway, or Razorpay Checkout) → verified server-side →
  `/booking?ref&k`. A Razorpay webhook settles payments whose browser closed early.
- **Automatic refunds:** customer or staff cancels → refund queued → sent to the gateway (retried up to 5 times,
  then flagged on the Health page). Cash taken at the desk is flagged "hand back" on the Today page.
- **Paid after the hold expired but someone else took the slot:** refunded automatically.
- **Analytics** count money actually collected. Unpaid phone reservations and complimentary bookings are shown
  separately, never as revenue.

## Messages

| Message | When |
|---|---|
| Booking confirmed + door code | on payment / staff confirmation (code resent when it becomes valid) |
| Booking cancelled (+ refund amount) | on cancellation |
| Refund issued | when the gateway confirms |
| Membership ending | 7 days before and on the last day (not if already renewed) |
| Academy fees due | 7 days before and on the last day |
| Membership confirmed | after online purchase |
| Login code | on request |

Going live in India needs **DLT-registered SMS templates** (MSG91 flow ID per message) and **Meta-approved WhatsApp
templates**. Their names are in `.env.example`, and parameter order follows `PARAMS` in `app/notify.py`.

## Layout

```
server.py            routes: public booking, payment page, portal, admin, door device, demo helpers
app/bookings.py      availability, holds, staff bookings, confirmation, cancellation + refund policy
app/payments.py      orders, verification, webhook, desk payments, automatic refunds with retries
app/access.py        door codes, fingerprint sync, lock adapters, device auth, keypad lockout
app/members.py       portal status, online/desk memberships, academy fees, reminders
app/analytics.py     revenue, utilisation, peak hours, members, plain-language insights
app/health.py        "what's working": scheduler, SMS, WhatsApp, payments, refunds, lock, door, security
app/notify.py        SMS (Twilio, MSG91) + WhatsApp (Meta Cloud API), logging and dedupe
app/auth.py          OTP login (mobile or member ID), staff password login, rate limits
app/seed.py          courts, plans, demo people and a month of dummy history
static/              index (booking), pay, booking (confirmation), portal, admin, demo (phone), door (keypad)
tools/door_device.py reference door-controller client
```
