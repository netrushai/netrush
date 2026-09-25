"""SMS + WhatsApp. One call, `send(kind, phone, params)`, fans out to each channel the person
has opted into, logs every attempt, and (with a dedupe_key) guarantees a reminder is sent at
most once per channel no matter how often the scheduler runs.

Each message kind has ordered params so the same data fills a plain-text SMS, an MSG91 DLT
flow, and a Meta-approved WhatsApp template (whose {{1}}, {{2}}… follow PARAMS order).
"""
import base64
import json
import sqlite3
import urllib.parse
import urllib.request

from . import clock, config, db

PARAMS = {
    "door_code": ["name", "code", "court", "valid_from", "valid_to"],
    "booking_confirmed": ["name", "ref", "court", "when", "lead"],
    "membership_reminder": ["name", "plan", "end_date", "when"],
    "academy_fee_reminder": ["name", "batch", "end_date", "when"],
    "login_otp": ["code"],
    "booking_cancelled": ["name", "ref", "court", "when", "refund"],
    "refund_issued": ["amount", "what", "days"],
    "membership_confirmed": ["name", "plan", "end_date", "access"],
}

TEXT = {
    "door_code": "Hi {name}, your entry code for {court} is {code}. Works {valid_from}-{valid_to}. "
    "Enter it on the door keypad. Enjoy your game!",
    "booking_confirmed": "Hi {name}, booking {ref} confirmed: {court}, {when}. "
    "Your door entry code comes in a separate message and opens the door from {lead} min before start.",
    "membership_reminder": "Hi {name}, your {plan} membership {when} ({end_date}). "
    "Renew online at " + config.PUBLIC_URL + "/portal or at the desk to keep your fingerprint access active.",
    "academy_fee_reminder": "Hi {name}, academy fees for {batch} are paid until {end_date} and {when}. "
    "Please renew to continue training.",
    "login_otp": "{code} is your " + config.FACILITY_NAME + " login code. Valid 5 minutes. Do not share it.",
    "booking_cancelled": "Hi {name}, booking {ref} ({court}, {when}) is cancelled. {refund}",
    "refund_issued": "{amount} has been refunded for {what}. It reaches your account in {days}.",
    "membership_confirmed": "Hi {name}, your {plan} is active until {end_date}. {access}",
}


def render(kind, params):
    return TEXT[kind].format(**params)


def e164(phone):
    p = "".join(ch for ch in phone if ch.isdigit())
    return p if len(p) > 10 else "91" + p


# ---------- providers ----------

def _post(url, data=None, json_body=None, headers=None, auth=None):
    headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = urllib.parse.urlencode(data or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def _sms(kind, phone, params, text):
    p = config.SMS_PROVIDER
    if p == "console":
        print(f"[SMS -> {phone}] {text}", flush=True)
    elif p == "twilio":
        _post(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/Messages.json",
            data={"To": "+" + e164(phone), "From": config.TWILIO_FROM, "Body": text},
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN),
        )
    elif p == "msg91":
        flow = config.MSG91_FLOWS.get(kind)
        if not flow:
            raise RuntimeError(f"no MSG91 flow configured for '{kind}'")
        _post(
            "https://control.msg91.com/api/v5/flow/",
            json_body={"template_id": flow, "short_url": "0",
                       "recipients": [{"mobiles": e164(phone), **{k: str(v) for k, v in params.items()}}]},
            headers={"authkey": config.MSG91_AUTHKEY, "accept": "application/json"},
        )
    else:
        raise RuntimeError(f"unknown SMS_PROVIDER {p}")


def _whatsapp(kind, phone, params, text):
    p = config.WHATSAPP_PROVIDER
    if p == "console":
        print(f"[WhatsApp -> {phone}] {text}", flush=True)
    elif p == "meta":
        values = [str(params[k]) for k in PARAMS[kind]]
        components = [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}]
        if kind == "login_otp":  # Meta authentication templates also need the code on the copy button
            components.append({"type": "button", "sub_type": "url", "index": "0",
                               "parameters": [{"type": "text", "text": values[0]}]})
        _post(
            f"https://graph.facebook.com/{config.WA_API_VERSION}/{config.WA_PHONE_NUMBER_ID}/messages",
            json_body={
                "messaging_product": "whatsapp",
                "to": e164(phone),
                "type": "template",
                "template": {"name": config.WA_TEMPLATES[kind], "language": {"code": config.WA_LANG},
                             "components": components},
            },
            headers={"Authorization": f"Bearer {config.WA_TOKEN}"},
        )
    else:
        raise RuntimeError(f"unknown WHATSAPP_PROVIDER {p}")


SENDERS = {"sms": _sms, "whatsapp": _whatsapp}


# ---------- public ----------

def channels_for(phone):
    """The person's opt-ins; unknown numbers (walk-in bookers) get both."""
    u = db.one("SELECT sms_opt, wa_opt FROM users WHERE phone=?", phone)
    if not u:
        return ["sms", "whatsapp"]
    return [ch for ch, on in (("sms", u["sms_opt"]), ("whatsapp", u["wa_opt"])) if on]


def send(kind, phone, params, dedupe_key=None, channels=None):
    """Returns {channel: 'sent'|'failed'|'duplicate'}. Never raises — a messaging outage
    must not break a booking or the scheduler."""
    params = {k: params[k] for k in PARAMS[kind]}
    text = render(kind, params)
    result = {}
    for ch in channels or channels_for(phone):
        key = f"{dedupe_key}:{ch}" if dedupe_key else None
        c = db.conn()
        try:
            cur = c.execute(
                "INSERT INTO notifications(dedupe_key,kind,channel,to_phone,body,status,created_at) "
                "VALUES(?,?,?,?,?,'sending',?)",
                (key, kind, ch, phone, _redact(kind, text), clock.fmt(clock.now())),
            )
        except sqlite3.IntegrityError:
            result[ch] = "duplicate"
            continue
        nid = cur.lastrowid
        try:
            SENDERS[ch](kind, phone, params, text)
            c.execute("UPDATE notifications SET status='sent' WHERE id=?", (nid,))
            result[ch] = "sent"
        except Exception as e:  # noqa: BLE001 — provider errors are logged, not fatal
            # Free the dedupe key so the next scheduler pass retries this channel.
            c.execute("UPDATE notifications SET status='failed', error=?, dedupe_key=NULL WHERE id=?",
                      (str(e)[:500], nid))
            result[ch] = "failed"
    return result


def _redact(kind, text):
    """Login codes don't belong in a log staff can read. Door codes stay — the desk needs them
    to help someone stuck at the door."""
    if kind == "login_otp" and not config.DEMO_MODE:  # demo: the "demo phone" page needs to show it
        return "(login code sent)"
    return text
