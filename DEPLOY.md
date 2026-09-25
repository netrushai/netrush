# Putting NetRush online for the client demo

This gets you a public link like `https://netrush.onrender.com` that works end to end with **mock payments**, a
**demo phone** that shows every SMS/WhatsApp, and a **web door keypad**. It takes about 15 minutes and is free.

| Page | What the client sees |
|---|---|
| `/` | Booking site: pick sport/day/slot, enter name + mobile + email, pay, get the confirmation and door code |
| `/pay` | The (mock) payment gateway the booking redirects to |
| `/booking?...` | Confirmation page with reference, door-code status and self-cancel with automatic refund |
| `/portal` | Member & academy login (mobile or member ID + OTP), membership card, renew online at ₹2,000/month |
| `/admin` | Front desk: today, phone reservations without payment, members, analytics & revenue, health, door, messages |
| `/demo` | **Demo phone**: every SMS & WhatsApp as the customer receives it, plus the demo logins |
| `/door` | **Door keypad**: type the door code or scan a demo fingerprint |

### Demo logins

| Role | Login | Secret |
|---|---|---|
| Admin / front desk | `9999999999` | the `ADMIN_PASSWORD` you set (`admin123` locally) |
| Member (plan ends in 7 days) | `NR-0002` or `9000000001` | code shown on screen |
| Academy player | `NR-0005` or `9000000004` | code shown on screen |
| Any new customer | any 10-digit mobile | code shown on screen |

Door fingerprints to try: `101` (active member, opens), `104` (academy, opens), `115` (expired, stays locked).

> In demo mode the `/demo` page lists these logins, **including the admin password**, so anyone with the link
> can try everything. That's intended for showing the client. Use a password you don't use elsewhere, and turn
> `DEMO_MODE` off before real customers use it.

---

## Option A: Render (free, recommended for the demo)

### 1. Put `netrush/` in its own GitHub repository

The folder sits inside another project, so give it a repo of its own (private is fine). From the `netrush` folder:

```bash
git init
```
```bash
git add .
```
```bash
git commit -m "NetRush demo"
```

Create an empty private repo on GitHub named `netrush`, then:

```bash
git remote add origin https://github.com/<your-username>/netrush.git
```
```bash
git branch -M main
```
```bash
git push -u origin main
```

(`data/` and `.env` are git-ignored, so no local data or secrets are uploaded.)

### 2. Create the service

1. Sign in at <https://dashboard.render.com> with GitHub.
2. **New → Blueprint**, pick the `netrush` repo. Render reads `render.yaml`.
3. It asks for the two values marked `sync: false`:
   - `ADMIN_PASSWORD`: pick the admin password you'll give the client.
   - `PUBLIC_URL`: leave empty for now.
4. **Apply**. The first deploy takes 2–3 minutes. You get a URL like `https://netrush-xxxx.onrender.com`.

### 3. Set the public URL

Service → **Environment** → set `PUBLIC_URL` to that URL (with `https://`) → **Save, rebuild and deploy**.
It is used in messages ("Renew online at …") and to mark login cookies secure.

### 4. Keep it awake (important on the free plan)

Free services sleep after 15 minutes without visitors, and the first visit then takes about 50 seconds. While
asleep, the reminder and door-code scheduler pauses too. It catches up on wake, but it looks slow in a meeting.

- Create a free monitor at <https://uptimerobot.com>: type **HTTP(s)**, URL `https://<your-url>/healthz`,
  interval **5 minutes**. That keeps it awake.
- Or use the **Starter** plan ($7/month), which never sleeps.

### 5. What to know about data on the free plan

The free plan has no persistent disk, so **each restart or deploy wipes the database and reloads fresh demo data**.
That's handy for demos: every session starts clean. To keep data, switch to Starter and uncomment the `disk:`
block in `render.yaml` (and set `DB_PATH=/var/data/netrush.db`).

You can also reset the demo at any time: **Admin → Health → Reset demo data**.

---

## Option B: Railway (Docker, keeps data)

1. <https://railway.app> → **New project → Deploy from GitHub repo** → pick `netrush`. It builds the `Dockerfile`.
2. **Variables**: `DEMO_MODE=1`, `ADMIN_PASSWORD=…`, `DEVICE_SECRET=<random>`, `PUBLIC_URL=https://…` (after step 3).
3. **Settings → Networking → Generate domain**.
4. **Add a volume** mounted at `/data` so the database survives restarts.

Railway doesn't sleep. It costs roughly $5/month after the trial credit.

## Option C: your own server (VPS)

On any Ubuntu VPS (Hetzner, DigitalOcean, AWS Lightsail…) with a domain pointed at it:

```bash
sudo apt install -y python3 caddy
```
```bash
git clone https://github.com/<your-username>/netrush.git /opt/netrush
```

`/etc/systemd/system/netrush.service`:

```ini
[Service]
WorkingDirectory=/opt/netrush
Environment=DEMO_MODE=1 TRUST_PROXY=1 PUBLIC_URL=https://book.netrush.in ADMIN_PASSWORD=change-me DEVICE_SECRET=long-random
ExecStart=/usr/bin/python3 server.py
Restart=always
[Install]
WantedBy=multi-user.target
```

`/etc/caddy/Caddyfile` (Caddy fetches the HTTPS certificate by itself):

```
book.netrush.in {
    reverse_proxy localhost:8000
}
```

```bash
sudo systemctl enable --now netrush && sudo systemctl reload caddy
```

---

## Before the meeting (5-minute check)

1. Open `https://<url>/healthz`. It should say `{"ok": true}` (this also wakes a sleeping free instance).
2. **Admin → Health → Reset demo data** for a clean start.
3. Open `/demo` in a second tab or on a tablet next to you: the demo phone updates live.
4. Walk through: book a court → pay → the SMS and door code arrive on the demo phone → type it on `/door`
   (it opens from 10 minutes before the slot; earlier it says "opens at 18:50") → log in to `/portal` as
   `NR-0002` and renew → `/admin` for bookings, a phone reservation (no payment), analytics and health.

On **Admin → Health**, the demo correctly shows SMS, WhatsApp, payments and lock as "demo mode". That is the
honest to-do list for going live, below.

## Going live (after the demo)

| Step | What to set |
|---|---|
| Turn off demo mode | `DEMO_MODE=0` (hides the demo phone, web keypad and on-screen codes) |
| Real payments | Razorpay account → `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`; webhook `payment.captured` → `<PUBLIC_URL>/api/payments/razorpay-webhook` with `RAZORPAY_WEBHOOK_SECRET` |
| SMS | DLT registration + MSG91 (`SMS_PROVIDER=msg91`, one flow ID per message) or Twilio |
| WhatsApp | WhatsApp Business Cloud API number + approved templates (`WHATSAPP_PROVIDER=meta`) |
| Door lock | `LOCK_PROVIDER=ttlock` or `http_bridge` once the lock is chosen (see README) |
| Keep data | Paid plan + persistent disk, and a daily backup of `netrush.db` |
| Security | Strong `ADMIN_PASSWORD` and `DEVICE_SECRET` (Admin → Health turns green when both are set) |

Every value is listed in `.env.example`.
