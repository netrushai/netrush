const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || "Request failed"); e.status = r.status; throw e; }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer;
function toast(msg, err) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
  t.textContent = msg; t.classList.toggle("err", !!err); t.classList.remove("hidden");
  t.style.animation = "none"; t.offsetHeight; t.style.animation = "";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

const rupees = n => "₹" + Number(n || 0).toLocaleString("en-IN");
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function niceDate(iso) {   // "2026-09-28" → "Mon 28 Sep"
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `${DAYS[dt.getDay()]} ${d} ${MONTHS[m - 1]}`;
}
const hhmm = s => s.slice(11, 16);
const SPORT_LABEL = { badminton: "Badminton", padel: "Padel", pickleball: "Pickleball" };

// Simple line icons, drawn in currentColor.
const SPORT_ICON = {
  badminton: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M9 23l-3 3"/><circle cx="10.5" cy="21.5" r="2.5"/><path d="M12.3 19.7L20 6l6 6-13.7 7.7"/><path d="M16 11l5 5M18 8.5l5.5 5.5"/></svg>`,
  padel: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <ellipse cx="18" cy="12" rx="8" ry="9" transform="rotate(35 18 12)"/><path d="M12.5 19.5L6 26"/>
    <circle cx="16" cy="10" r=".8" fill="currentColor"/><circle cx="19.5" cy="12.5" r=".8" fill="currentColor"/><circle cx="17" cy="14.5" r=".8" fill="currentColor"/>
    <circle cx="26" cy="25" r="2.5"/></svg>`,
  pickleball: `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="16" cy="16" r="10"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="11" r="1.3" fill="currentColor"/>
    <circle cx="20" cy="18.5" r="1.3" fill="currentColor"/><circle cx="13" cy="19.5" r="1.3" fill="currentColor"/><circle cx="16" cy="15.5" r="1.3" fill="currentColor"/></svg>`,
};
const ICON = {
  today: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>`,
  people: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><path d="M16 4.5a3.5 3.5 0 010 7M18 14.8c1.8.7 3 2.5 3.5 5.2"/></svg>`,
  chart: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>`,
  pulse: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h4l3-7 5 14 3-7h5"/></svg>`,
  door: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 21V4a1 1 0 011-1h12a1 1 0 011 1v17M3 21h18"/><circle cx="15" cy="12" r="1" fill="currentColor"/></svg>`,
  chat: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z"/></svg>`,
};

// Demo mode: a lime strip under the header linking the demo phone, web door keypad and demo logins.
function demoBar(cfg) {
  if (!cfg || !cfg.demo || $(".demo-bar")) return;
  const bar = document.createElement("div");
  bar.className = "demo-bar";
  bar.innerHTML = `<div class="wrap"><b>DEMO</b><span>No real SMS, payments or lock. Everything else is live.</span>
    <span class="spacer"></span><a href="/demo" target="_blank">📱 Demo phone</a>
    <a href="/door" target="_blank">🚪 Door keypad</a><a href="/demo#logins" target="_blank">🔑 Demo logins</a></div>`;
  const header = $("header.top");
  header ? header.after(bar) : document.body.prepend(bar);
}
