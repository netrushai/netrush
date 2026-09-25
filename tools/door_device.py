"""Pretend to be the door controller. Sends exactly what a real keypad / fingerprint box would
send to the server, signed with DEVICE_SECRET — use it to test the lock API end to end, or as
the reference for whoever programs the real controller (ESP32, Raspberry Pi, vendor bridge).

    python tools/door_device.py code 482913
    python tools/door_device.py finger 101
    python tools/door_device.py event finger 101        # report an entry the lock allowed offline
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import config  # noqa: E402  (reads .env so the secret matches the server)

SERVER = os.environ.get("SERVER", f"http://localhost:{config.PORT}")
DEVICE_ID = os.environ.get("DEVICE_ID", "main-door")


def call(path, payload):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(config.DEVICE_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(SERVER + path, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Device-Id": DEVICE_ID, "X-Timestamp": ts, "X-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, **json.loads(e.read() or b"{}")}


def main(argv):
    if argv[:1] == ["code"] and len(argv) == 2:
        out = call("/api/lock/verify", {"code": argv[1]})
    elif argv[:1] == ["finger"] and len(argv) == 2:
        out = call("/api/lock/verify", {"fingerprint_user_id": argv[1]})
    elif argv[:2] == ["event", "finger"] and len(argv) == 3:
        out = call("/api/lock/events", {"events": [{"method": "fingerprint", "user_id": argv[2], "granted": True}]})
    else:
        print(__doc__)
        return 2
    print(json.dumps(out))
    if "open" in out:
        print(">>> RELAY ON - door unlocked" if out["open"] else ">>> stays locked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
