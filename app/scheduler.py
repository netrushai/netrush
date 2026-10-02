"""One background thread, ticking every 30 s. Every job is idempotent, so a restart, a double
tick, or a missed tick never double-sends or loses anything."""
import threading
import traceback

from . import access, bookings, clock, config, db, members, payments, slots

TICK_SECONDS = 30


def tick():
    db.kv_set("last_tick", clock.fmt(clock.now()))  # heartbeat for the health page
    bookings.expire_holds()
    payments.process_refunds()      # retry any refund the gateway refused last time
    access.issue_due_codes()        # door codes going out DOOR_CODE_LEAD_MIN before each slot
    access.expire_codes()           # and removed from the lock when the slot ends
    access.sync_member_access()     # fingerprints on/off as memberships start and lapse
    slots.release_lapsed()          # lapsed badminton members free their slot for the next in line
    today = clock.today().isoformat()
    if clock.now().hour >= config.REMINDER_HOUR and db.kv_get("reminders_ran") != today:
        members.send_reminders()    # 7-day and last-day SMS + WhatsApp
        db.kv_set("reminders_ran", today)


def _loop(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001 — keep ticking; the next pass retries
            traceback.print_exc()
        stop.wait(TICK_SECONDS)


def start():
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), daemon=True, name="scheduler").start()
    return stop
