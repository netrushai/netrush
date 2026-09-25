"""Business numbers for the owner: money, court use, members, and plain-language findings on
what is working and what isn't. Revenue counts money actually collected (the payments table),
so unpaid phone reservations and complimentary bookings never inflate it."""
from collections import defaultdict
from datetime import timedelta

from . import clock, config, db, seed


def report(days=30):
    days = max(1, min(int(days), 366))
    today = clock.today()
    first = today - timedelta(days=days - 1)
    lo, hi = first.isoformat(), (today + timedelta(days=1)).isoformat()   # [lo, hi) on 'YYYY-MM-DD HH:MM'
    dates = [(first + timedelta(days=i)).isoformat() for i in range(days)]

    # ---------------- money
    collected = db.all_("SELECT kind, provider, SUM(amount) AS amt, COUNT(*) AS n FROM payments "
                        "WHERE status='paid' AND paid_at>=? AND paid_at<? GROUP BY kind, provider", lo, hi)
    by_kind = defaultdict(int)
    online = desk = 0
    for r in collected:
        by_kind[r["kind"]] += r["amt"]
        if r["provider"] == "desk":
            desk += r["amt"]
        else:
            online += r["amt"]
    refunded = db.one("SELECT COALESCE(SUM(refund_amount),0) AS r FROM payments WHERE refund_status='done' "
                      "AND refunded_at>=? AND refunded_at<?", lo, hi)["r"]
    gross = online + desk
    daily_in = {r["d"]: r["amt"] for r in db.all_(
        "SELECT substr(paid_at,1,10) AS d, SUM(amount) AS amt FROM payments WHERE status='paid' "
        "AND paid_at>=? AND paid_at<? GROUP BY d", lo, hi)}
    daily_out = {r["d"]: r["amt"] for r in db.all_(
        "SELECT substr(refunded_at,1,10) AS d, SUM(refund_amount) AS amt FROM payments WHERE refund_status='done' "
        "AND refunded_at>=? AND refunded_at<? GROUP BY d", lo, hi)}
    daily = [{"date": d, "collected": daily_in.get(d, 0), "refunded": daily_out.get(d, 0)} for d in dates]
    sport_rev = {r["sport"]: r["amt"] for r in db.all_(
        "SELECT c.sport, SUM(p.amount) AS amt FROM payments p JOIN bookings b ON b.id=p.ref_id AND p.kind='booking' "
        "JOIN courts c ON c.id=b.court_id WHERE p.status='paid' AND p.paid_at>=? AND p.paid_at<? GROUP BY c.sport",
        lo, hi)}
    outstanding = db.one("SELECT COALESCE(SUM(amount),0) AS a, COUNT(*) AS n FROM bookings "
                         "WHERE status='confirmed' AND pay_status='unpaid'")
    comp = db.one("SELECT COALESCE(SUM(amount),0) AS a, COUNT(*) AS n FROM bookings WHERE status='confirmed' "
                  "AND pay_status='comp' AND start>=? AND start<?", lo, hi)
    refunds_open = db.one("SELECT COALESCE(SUM(refund_amount),0) AS a, COUNT(*) AS n FROM payments "
                          "WHERE refund_status IN ('pending','failed','manual')")

    # ---------------- bookings & court use
    bk = db.all_("SELECT b.*, c.sport FROM bookings b JOIN courts c ON c.id=b.court_id "
                 "WHERE b.start>=? AND b.start<? AND b.status IN ('confirmed','cancelled')", lo, hi)
    confirmed = [b for b in bk if b["status"] == "confirmed"]
    cancelled = [b for b in bk if b["status"] == "cancelled"]
    courts = db.all_("SELECT id, name, sport FROM courts WHERE active=1 ORDER BY id")
    open_hours = config.CLOSE_HOUR - config.OPEN_HOUR
    booked_h = defaultdict(float)
    hour_use = defaultdict(float)
    for b in confirmed:
        s, e = clock.parse(b["start"]), clock.parse(b["end"])
        booked_h[b["court_id"]] += (e - s).total_seconds() / 3600
        t = s
        while t < e:
            hour_use[t.hour] += 1
            t += timedelta(hours=1)
    academy_h = _academy_hours(first, today)
    court_rows = []
    for c in courts:
        avail = days * open_hours
        court_rows.append({
            "court": c["name"], "sport": c["sport"], "booked_hours": round(booked_h[c["id"]], 1),
            "academy_hours": academy_h[c["id"]], "open_hours": avail,
            "utilization": round(100 * (booked_h[c["id"]] + academy_h[c["id"]]) / avail) if avail else 0,
            "booked_pct": round(100 * booked_h[c["id"]] / avail) if avail else 0,
        })
    capacity = days * len(courts)
    by_hour = [{"hour": h, "pct": round(100 * hour_use[h] / capacity) if capacity else 0}
               for h in range(config.OPEN_HOUR, config.CLOSE_HOUR)]
    sources = defaultdict(int)
    for b in confirmed:
        sources[b["source"]] += 1
    by_sport = []
    for sport in sorted({c["sport"] for c in courts}):
        sc = [c for c in court_rows if c["sport"] == sport]
        hours = sum(c["booked_hours"] for c in sc)
        by_sport.append({"sport": sport, "courts": len(sc), "booked_hours": hours,
                         "revenue": sport_rev.get(sport, 0),
                         "utilization": round(sum(c["booked_pct"] for c in sc) / len(sc)) if sc else 0,
                         "per_court_hour": round(sport_rev.get(sport, 0) / hours) if hours else 0})

    # ---------------- members & academy
    t = today.isoformat()
    active_members = db.one("SELECT COUNT(DISTINCT user_id) AS n FROM memberships WHERE status='active' "
                            "AND start_date<=? AND end_date>=?", t, t)["n"]
    new_m = db.all_("SELECT m.user_id, m.id, p.provider FROM memberships m LEFT JOIN payments p "
                    "ON p.kind='membership' AND p.ref_id=m.id WHERE m.created_at>=? AND m.created_at<?", lo, hi)
    renewals = sum(1 for m in new_m if db.one(
        "SELECT 1 AS x FROM memberships WHERE user_id=? AND id<?", m["user_id"], m["id"]))
    lapsed = db.one(
        "SELECT COUNT(*) AS n FROM memberships m WHERE m.status='active' AND m.end_date>=? AND m.end_date<? "
        "AND NOT EXISTS (SELECT 1 FROM memberships m2 WHERE m2.user_id=m.user_id AND m2.status='active' "
        "AND m2.end_date>m.end_date)", lo, t)["n"]
    expiring = db.one(
        "SELECT COUNT(*) AS n FROM memberships m WHERE m.status='active' AND m.end_date>=? AND m.end_date<=? "
        "AND NOT EXISTS (SELECT 1 FROM memberships m2 WHERE m2.user_id=m.user_id AND m2.status='active' "
        "AND m2.end_date>m.end_date)", t, (today + timedelta(days=7)).isoformat())["n"]
    players = db.one("SELECT COUNT(*) AS n FROM academy_enrollments WHERE status='active'")["n"]
    overdue = db.one("SELECT COUNT(*) AS n FROM academy_enrollments WHERE status='active' AND fee_paid_until<?",
                     t)["n"]
    online_plan = seed.online_plan_id()
    members = {"active": active_members, "new": len(new_m), "renewals": renewals,
               "new_online": sum(1 for m in new_m if m["provider"] in ("mock", "razorpay")),
               "lapsed": lapsed, "expiring_7d": expiring, "academy_players": players, "fees_overdue": overdue,
               "online_plan_id": online_plan}

    out = {
        "range": {"from": lo, "to": t, "days": days},
        "money": {"gross": gross, "online": online, "desk": desk, "refunded": refunded, "net": gross - refunded,
                  "courts": by_kind["booking"], "memberships": by_kind["membership"], "academy": by_kind["academy"],
                  "unpaid_reservations": outstanding["a"], "unpaid_count": outstanding["n"],
                  "complimentary": comp["a"], "complimentary_count": comp["n"],
                  "refunds_open": refunds_open["a"], "refunds_open_count": refunds_open["n"]},
        "daily": daily,
        "bookings": {"confirmed": len(confirmed), "cancelled": len(cancelled),
                     "cancel_rate": round(100 * len(cancelled) / len(bk)) if bk else 0,
                     "by_source": dict(sources),
                     "cancelled_by": {k: sum(1 for b in cancelled if b["cancelled_by"] == k)
                                      for k in ("customer", "staff", "system")}},
        "courts": court_rows, "by_hour": by_hour, "by_sport": by_sport, "members": members,
    }
    out["insights"] = _insights(out)
    return out


def _academy_hours(first, last):
    hrs = defaultdict(float)
    batches = db.all_("SELECT * FROM academy_batches")
    d = first
    while d <= last:
        for b in batches:
            if str(d.weekday()) in b["weekdays"].split(","):
                sh, sm = map(int, b["start_time"].split(":"))
                eh, em = map(int, b["end_time"].split(":"))
                for cid in b["court_ids"].split(","):
                    hrs[cid] += (eh * 60 + em - sh * 60 - sm) / 60
        d += timedelta(days=1)
    return hrs


def inr(n):
    """₹ with Indian digit grouping: 335000 → ₹3,35,000."""
    n = int(round(n))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def _insights(r):
    """Plain-language findings. tone: good | warn | bad."""
    out = []
    add = lambda tone, text: out.append({"tone": tone, "text": text})
    m, b = r["money"], r["bookings"]
    if not b["confirmed"] and not m["gross"]:
        add("warn", "No bookings or payments in this period yet.")
        return out
    sports = [s for s in r["by_sport"] if s["booked_hours"]]
    if sports:
        best = max(sports, key=lambda s: s["revenue"])
        add("good", f"{best['sport'].title()} earns the most from courts: {inr(best['revenue'])} "
                    f"({best['utilization']}% of its court time booked).")
        low = min(r["by_sport"], key=lambda s: s["utilization"])
        if low["utilization"] < 25:
            add("warn", f"{low['sport'].title()} courts are only {low['utilization']}% booked. "
                        "Consider off-peak pricing, a league night, or bundling it into memberships.")
    hours = [h for h in r["by_hour"]]
    if hours:
        peak = sorted(hours, key=lambda h: -h["pct"])[:3]
        if peak[0]["pct"]:
            add("good", "Busiest hours: " + ", ".join(f"{h['hour']:02d}:00 ({h['pct']}%)" for h in peak) + ".")
        dead = [h for h in hours if h["pct"] < 10]
        if len(dead) >= 3:
            examples = ", ".join("%02d:00" % h["hour"] for h in dead[:4])
            add("warn", f"{len(dead)} hours of the day are under 10% booked (e.g. {examples}). "
                        "A cheaper off-peak rate could fill them.")
    if b["cancel_rate"] >= 15:
        add("bad", f"{b['cancel_rate']}% of bookings were cancelled. Check whether the refund window is too generous.")
    elif b["confirmed"]:
        add("good", f"Cancellations are low ({b['cancel_rate']}%).")
    total = sum(b["by_source"].values())
    if total:
        share = round(100 * b["by_source"].get("online", 0) / total)
        add("good" if share >= 50 else "warn",
            f"{share}% of bookings were made online without staff."
            + ("" if share >= 50 else " Share the booking link on WhatsApp and Google Maps to push this up."))
    if m["unpaid_count"]:
        add("warn", f"{inr(m['unpaid_reservations'])} is still owed on {m['unpaid_count']} unpaid phone "
                    "reservation(s). Collect it or mark it paid on the Today tab.")
    if m["refunds_open_count"]:
        add("bad", f"{m['refunds_open_count']} refund(s) ({inr(m['refunds_open'])}) are waiting: cash to hand back "
                   "or a gateway refund that failed. See the Today tab.")
    mem = r["members"]
    if mem["lapsed"]:
        add("warn", f"{mem['lapsed']} membership(s) ran out in this period without renewing. "
                    "Worth a personal call or a comeback offer.")
    if mem["renewals"]:
        add("good", f"{mem['renewals']} member(s) renewed in this period.")
    if mem["fees_overdue"]:
        add("warn", f"{mem['fees_overdue']} academy player(s) have overdue fees and their fingerprint entry is off.")
    return out
