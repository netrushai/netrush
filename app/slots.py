"""Badminton members' daily slots, and the priority list for slots that are full.

Every badminton member (badminton or all-access plan) plays in one fixed daily slot, e.g. 19:00.
At most MEMBER_SLOT_CAPACITY members share a court in a slot. Courts fill one at a time, and a
court with members on it is closed to public booking at that hour (see bookings._blocks), so
the fewer courts members are spread over, the more stay bookable.

When every court in a slot is full, staff add the person as an enquiry: they wait in line, oldest
first. When a place frees up (a membership lapses, staff remove someone, someone moves slot), the
next in line is suggested on the admin page with a WhatsApp message already drafted. A place
offered to someone is held for them until staff mark them joined or declined.
"""
import urllib.parse
from datetime import datetime, timedelta

from . import clock, config, db


class SlotError(Exception):
    pass


def slot_times():
    return [f"{m // 60:02d}:{m % 60:02d}"
            for m in range(config.OPEN_HOUR * 60, config.CLOSE_HOUR * 60 - config.SLOT_MIN + 1, config.SLOT_MIN)]


def _end(start_time):
    return (datetime.strptime(start_time, "%H:%M") + timedelta(minutes=config.SLOT_MIN)).strftime("%H:%M")


def label(start_time):
    """'19:00' → '7–8 PM'."""
    def h(t):
        hh, mm = map(int, t.split(":"))
        return f"{(hh % 12) or 12}{':%02d' % mm if mm else ''}", "AM" if hh < 12 else "PM"
    (a, ap), (b, bp) = h(start_time), h(_end(start_time))
    return f"{a}–{b} {bp}" if ap == bp else f"{a} {ap}–{b} {bp}"


def _courts(c, start_time):
    """Badminton courts members can use at this time: not taken by an academy batch on any day."""
    end = _end(start_time)
    academy = set()
    for b in c.execute("SELECT court_ids, start_time, end_time FROM academy_batches").fetchall():
        if b["start_time"] < end and b["end_time"] > start_time:
            academy.update(b["court_ids"].split(","))
    return [r["id"] for r in c.execute("SELECT id FROM courts WHERE sport=? AND active=1 ORDER BY id",
                                       (config.MEMBER_SLOT_SPORT,)).fetchall() if r["id"] not in academy]


def _state(c, start_time, exclude_phone=None):
    courts = _courts(c, start_time)
    counts = {cid: 0 for cid in courts}
    for r in c.execute("SELECT court_id, COUNT(*) AS n FROM member_slots WHERE start_time=? AND status='active' "
                       "GROUP BY court_id", (start_time,)).fetchall():
        counts[r["court_id"]] = r["n"]
    held = c.execute("SELECT COUNT(*) FROM enquiries WHERE start_time=? AND status='offered' AND phone<>?",
                     (start_time, exclude_phone or "")).fetchone()[0]
    capacity = config.MEMBER_SLOT_CAPACITY * len(courts)
    members = sum(n for cid, n in counts.items() if cid in courts)
    return {"courts": counts, "capacity": capacity, "members": members, "held": held,
            "free": max(0, capacity - members - held)}


# ---------------------------------------------------------------- who may have a slot

def eligible(user_id, on=None):
    """A current or upcoming badminton / all-access membership (ending on or after `on`)."""
    on = (on or clock.today()).isoformat()
    return bool(db.one("SELECT 1 AS x FROM memberships m JOIN plans p ON p.id=m.plan_id WHERE m.user_id=? "
                       "AND m.status='active' AND m.end_date>=? AND (p.sport IS NULL OR p.sport=?)",
                       user_id, on, config.MEMBER_SLOT_SPORT))


def for_user(user_id):
    s = db.one("SELECT ms.id, ms.court_id, ms.start_time, c.name AS court FROM member_slots ms "
               "JOIN courts c ON c.id=ms.court_id WHERE ms.user_id=? AND ms.status='active'", user_id)
    if s:
        s["label"] = label(s["start_time"])
    return s


def assign(user_id, start_time, staff=None):
    """Give a member a daily slot (or move them to another). Returns {slot, warnings}."""
    if start_time not in slot_times():
        raise SlotError("Pick a slot time from the list")
    if not eligible(user_id):
        raise SlotError("They need an active badminton or all-access membership first")
    u = db.one("SELECT phone FROM users WHERE id=?", user_id)
    cur = for_user(user_id)
    if cur and cur["start_time"] == start_time:
        raise SlotError(f"Already in the {label(start_time)} slot")
    now = clock.fmt(clock.now())
    with db.tx() as c:
        st = _state(c, start_time, exclude_phone=u["phone"])
        if not st["courts"]:
            raise SlotError("No badminton court is free for members at that time (academy)")
        if st["free"] <= 0:
            raise SlotError(f"The {label(start_time)} slot is full "
                            f"({config.MEMBER_SLOT_CAPACITY} per court). Add them as an enquiry to put them on the priority list.")
        open_courts = [cid for cid, n in st["courts"].items() if n < config.MEMBER_SLOT_CAPACITY]
        used = [cid for cid in open_courts if st["courts"][cid] > 0]
        if used:  # fill the court already in use before opening another one
            court = max(used, key=lambda cid: st["courts"][cid])
        else:     # opening a court: pick the one with the fewest public bookings already at that hour
            court = min(open_courts, key=lambda cid: (len(_upcoming_bookings(c, cid, start_time)), cid))
        if cur:
            c.execute("UPDATE member_slots SET status='released', released_at=?, released_why=? WHERE id=?",
                      (now, f"moved to {start_time}", cur["id"]))
        sid = c.execute("INSERT INTO member_slots(user_id,court_id,start_time,created_at) VALUES(?,?,?,?)",
                        (user_id, court, start_time, now)).lastrowid
        # They were on the priority list for this slot: done.
        c.execute("UPDATE enquiries SET status='joined', closed_at=? WHERE phone=? AND start_time=? "
                  "AND status IN ('waiting','offered')", (now, u["phone"], start_time))
        clash = [] if used else _upcoming_bookings(c, court, start_time)
    warnings = [f"{b['name']} ({b['ref']}) has {court} booked on {b['start'][:10]} at {start_time}. "
                f"Move or cancel that booking." for b in clash]
    return {"slot": for_user(user_id), "warnings": warnings}


def _upcoming_bookings(c, court_id, start_time):
    """Public bookings already made on this court at this hour, from now on."""
    return [dict(r) for r in c.execute(
        "SELECT ref, name, start FROM bookings WHERE court_id=? AND status IN ('confirmed','pending') "
        "AND start>=? AND substr(start,12,5)<? AND substr(end,12,5)>? ORDER BY start",
        (court_id, clock.fmt(clock.now()), _end(start_time), start_time)).fetchall()]


def release(slot_id, why="removed by staff"):
    db.conn().execute("UPDATE member_slots SET status='released', released_at=?, released_why=? "
                      "WHERE id=? AND status='active'", (clock.fmt(clock.now()), why, slot_id))


def release_lapsed():
    """Scheduler: a member whose membership has ended gives up their slot to the next in line."""
    on = clock.today() - timedelta(days=config.MEMBER_SLOT_GRACE_DAYS)
    freed = []
    for s in db.all_("SELECT id, user_id, start_time FROM member_slots WHERE status='active'"):
        if not eligible(s["user_id"], on):
            release(s["id"], "membership ended")
            freed.append(s)
    return freed


# ---------------------------------------------------------------- enquiries / priority list

def add_enquiry(name, phone, start_time, note=None, staff=None):
    from .auth import normalize_phone, AuthError
    try:
        phone = normalize_phone(phone)
    except AuthError as e:
        raise SlotError(str(e))
    if not (name or "").strip():
        raise SlotError("Name is required")
    if start_time not in slot_times():
        raise SlotError("Pick a slot time from the list")
    if db.one("SELECT 1 AS x FROM enquiries WHERE phone=? AND start_time=? AND status IN ('waiting','offered')",
              phone, start_time):
        raise SlotError(f"They're already on the list for {label(start_time)}")
    eid = db.conn().execute("INSERT INTO enquiries(name,phone,start_time,note,created_by,created_at) "
                            "VALUES(?,?,?,?,?,?)", (name.strip()[:80], phone, start_time, (note or "").strip() or None,
                                                    staff, clock.fmt(clock.now()))).lastrowid
    e = db.one("SELECT * FROM enquiries WHERE id=?", eid)
    st = _state(db.conn(), start_time)
    position = db.one("SELECT COUNT(*) AS n FROM enquiries WHERE start_time=? AND status='waiting' AND id<=?",
                      start_time, eid)["n"]
    return {"enquiry": e, "position": position, "free": st["free"], "full": st["free"] < position}


def set_enquiry(eid, status):
    """offered (message sent: holds a place) | joined | closed (declined / no longer interested) | waiting."""
    if status not in ("offered", "joined", "closed", "waiting"):
        raise SlotError("Unknown status")
    e = db.one("SELECT * FROM enquiries WHERE id=?", eid)
    if not e:
        raise SlotError("Enquiry not found")
    now = clock.fmt(clock.now())
    db.conn().execute("UPDATE enquiries SET status=?, offered_at=CASE WHEN ?='offered' THEN ? ELSE offered_at END, "
                      "closed_at=CASE WHEN ? IN ('joined','closed') THEN ? ELSE NULL END WHERE id=?",
                      (status, status, now, status, now, eid))
    return db.one("SELECT * FROM enquiries WHERE id=?", eid)


def draft(e):
    """The WhatsApp message staff send when a place opens up."""
    text = (f"Hi {e['name'].split()[0]}, good news from {config.FACILITY_NAME}! A place has opened up in the "
            f"{label(e['start_time'])} badminton members' slot you asked about. Register now to claim it: reply "
            f"to this message or visit the front desk and we'll get your membership started. If we don't hear "
            f"back, the place goes to the next person on the list.")
    return {"text": text, "whatsapp": "https://wa.me/" + ("91" + e["phone"]) + "?text=" + urllib.parse.quote(text)}


def board():
    """Admin: every slot time with members, places left, the priority list and who to offer next."""
    c = db.conn()
    waiting = db.all_("SELECT * FROM enquiries WHERE status IN ('waiting','offered') ORDER BY created_at, id")
    members = db.all_("SELECT ms.id, ms.court_id, ms.start_time, u.id AS user_id, u.name, u.phone FROM member_slots ms "
                      "JOIN users u ON u.id=ms.user_id WHERE ms.status='active' ORDER BY ms.court_id, u.name")
    rows, suggestions = [], []
    for t in slot_times():
        st = _state(c, t)
        line = [e for e in waiting if e["start_time"] == t]
        if not st["members"] and not line:
            rows.append({"start_time": t, "label": label(t), **st, "people": [], "line": []})
            continue
        free = st["free"]
        for pos, e in enumerate([e for e in line if e["status"] == "waiting"], 1):
            e["position"] = pos
            if pos <= free:  # a place is open for them right now
                e["suggested"] = True
                suggestions.append({**e, "label": label(t), **draft(e)})
        for e in line:
            if e["status"] == "offered":
                e.update(draft(e))
        rows.append({"start_time": t, "label": label(t), **st,
                     "people": [m for m in members if m["start_time"] == t], "line": line})
    recent = db.all_("SELECT ms.start_time, ms.released_at, ms.released_why, u.name FROM member_slots ms "
                     "JOIN users u ON u.id=ms.user_id WHERE ms.status='released' AND ms.released_why NOT LIKE 'moved%' "
                     "ORDER BY ms.released_at DESC LIMIT 10")
    closed = db.all_("SELECT * FROM enquiries WHERE status IN ('joined','closed') ORDER BY closed_at DESC LIMIT 15")
    return {"capacity_per_court": config.MEMBER_SLOT_CAPACITY, "slots": rows, "suggestions": suggestions,
            "released": recent, "closed": closed}
