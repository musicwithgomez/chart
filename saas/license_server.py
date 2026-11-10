"""Goldmine Pro: license + subscription server.

YOU host this once (Render, Railway, Fly.io, a VPS...). Your customers never run it.
It does four things:
  1. Accounts: sign up / log in for your customers.
  2. Subscriptions: Stripe Checkout + Stripe webhooks (or a built-in TEST MODE without Stripe keys).
  3. License keys: one key per customer, bound to max N bridge PCs (plan dependent).
  4. License checks: every customer bridge asks /api/license/verify; the answer is signed (Ed25519),
     so a bridge cannot fake an active subscription.
It also serves the customer web app (terminal_test.html + the terminal) and a personalised bridge download.

Run locally (test mode, no Stripe needed):
    pip install -r saas/requirements.txt
    python -m uvicorn saas.license_server:app --port 8800      (from the backend folder)
    open http://127.0.0.1:8800

Settings (environment variables, all optional in test mode): see saas/.env.example
"""
from __future__ import annotations

import calendar
import contextvars
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent          # backend/saas
SITE = HERE.parent                              # backend (terminal, journal, bridge files)
sys.path.insert(0, str(SITE))
import gp_sign  # noqa: E402


def env(k: str, d: str = "") -> str:
    return os.getenv(k, d).strip()


DATA = Path(env("GP_DATA_DIR") or HERE / "data")
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "licenses.db"
PUBLIC_URL = env("GP_PUBLIC_URL").rstrip("/")  # empty = use the address the request came in on
ADMIN_TOKEN = env("GP_ADMIN_TOKEN")
TRIAL_DAYS = int(env("GP_TRIAL_DAYS", "7") or 7)
GRACE_HOURS = int(env("GP_GRACE_HOURS", "72") or 72)   # a bridge keeps working this long without reaching the server
STRIPE_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WHSEC = env("STRIPE_WEBHOOK_SECRET")
TEST_MODE = not STRIPE_KEY


def _secret_file(name: str, make) -> bytes:
    """Read a secret from the environment (hex) or from data/<name>; create it on first run."""
    v = env("GP_" + name.upper())
    if v:
        return bytes.fromhex(v)
    p = DATA / name
    if not p.exists():
        p.write_text(make().hex())
    return bytes.fromhex(p.read_text().strip())


SIGNING_SEED = _secret_file("signing_seed", gp_sign.new_seed)       # NEVER share; signs license answers
SESSION_SECRET = _secret_file("session_secret", lambda: os.urandom(32))
PUBKEY = gp_sign.public_key(SIGNING_SEED)
PUBKEY_HEX = PUBKEY.hex()

# ------------------------------------------------------------------ plans
# Prices are what the web app shows. In Stripe you create one Price per plan + interval and put its id in the env.
# Pro is the main offer. Starter is not on the pricing page: it is only offered when someone wants to cancel (downsell).
PLANS = {
    "starter": {"name": "Starter", "month": 9, "year": 90, "accounts": 1, "machines": 1, "hidden": True,
                "features": ["terminal", "journal", "alerts", "drawings"],
                "blurb": "One MT5 account, the full terminal and the journal."},
    # "lifetime" = one-time payment for lifetime access (limited to LIFETIME_SEATS)
    # lifetime3 = Lifetime paid in 3 monthly instalments (payment-plan downsell)
    "pro": {"name": "Pro", "month": 29, "year": 197, "lifetime": 497, "lifetime3": 179, "accounts": 5, "machines": 2,
            "features": ["terminal", "journal", "alerts", "drawings", "layers", "basket", "manage", "paper", "copy"],
            "blurb": "Everything to size, manage and protect your trades. Five MT5 accounts."},
    "elite": {"name": "Elite", "month": 59, "year": 590, "accounts": 25, "machines": 3,
              "features": ["terminal", "journal", "alerts", "drawings", "layers", "basket", "manage", "paper", "copy", "priority"],
              "blurb": "For many accounts and small teams: 25 accounts, 3 PCs, priority support."},
}
TRIAL_PLAN = "pro"
# Founding members: the first GP_FOUNDING_SEATS Pro customers pay these prices for as long as they stay subscribed.
# Real scarcity: the seat count is live, and it closes at GP_FOUNDING_END (YYYY-MM-DD) or when the seats are gone.
FOUNDING = {"plan": "pro", "month": 19, "year": 147, "seats": int(env("GP_FOUNDING_SEATS", "100") or 100), "ends": env("GP_FOUNDING_END", "")}
LIFETIME_SEATS = int(env("GP_LIFETIME_SEATS", "50") or 50)
GUARANTEE_DAYS = 30
BOOKING_URL = env("GP_BOOKING_URL", "")            # e.g. your Calendly link for the founding setup call
REF_TRIAL_DAYS = int(env("GP_REF_TRIAL_DAYS", "30") or 30)  # free trial for someone who signs up with a friend's link
# Referral: the friend gets REF_TRIAL_DAYS free right away; the referrer gets 30 days when the friend becomes a paying customer
# (paying on the result, not on a sign-up: fake sign-ups with your own link earn nothing).
TRIAL_CARD = env("GP_TRIAL_CARD", "0") == "1"      # 1 = the 7-day trial starts in Stripe checkout with a card
# One-time add-ons (classic upsells after the purchase). Only sell what you really deliver; remove what you don't.
ADDONS = {
    "vps": {"name": "Done-for-you VPS setup", "price": 49,
            "blurb": "We set up a Windows VPS with MT5 + Goldmine for you, so your targets and limits run 24/7 with your PC off."},
    "review": {"name": "1-on-1 strategy review (45 min)", "price": 99,
               "blurb": "We go through your journal together: what to cut, what to repeat, and your risk plan for next month."},
}


def price_id(plan: str, interval: str, founding: bool = False) -> str:
    return env(f"STRIPE_PRICE_{plan.upper()}_{'FOUNDING_' if founding else ''}{IVL_NAME.get(interval, 'MONTHLY')}")


IVL_NAME = {"month": "MONTHLY", "year": "YEARLY", "lifetime": "LIFETIME", "lifetime3": "LIFETIME3"}
LIFETIME_END = 4102444800  # 2100-01-01: lifetime access


def plan_from_price(pid: str) -> Optional[tuple]:
    for p in PLANS:
        for iv in ("month", "year", "lifetime3"):
            if pid and pid in (price_id(p, iv), price_id(p, iv, True)):
                return p, iv
    return None


# ------------------------------------------------------------------ database
LOCK = threading.RLock()
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, pw TEXT NOT NULL, created REAL NOT NULL,
  plan TEXT, interval TEXT, status TEXT, period_end REAL, cancel_at_end INTEGER DEFAULT 0,
  stripe_customer TEXT, stripe_sub TEXT, license TEXT UNIQUE, machines TEXT DEFAULT '{}', last_seen REAL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, user_id INTEGER, ts REAL, amount REAL, currency TEXT, descr TEXT, ref TEXT);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, created REAL, source TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY, ts REAL, admin TEXT, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, name TEXT, start REAL, end REAL, balance REAL, leverage INTEGER,
  daily_loss REAL, max_dd REAL, min_trades INTEGER, prizes TEXT);
CREATE TABLE IF NOT EXISTS challenge_entries(id TEXT PRIMARY KEY, challenge_id TEXT, user_id INTEGER, nickname TEXT, joined REAL,
  login INTEGER, status TEXT, fail TEXT, equity REAL, return_pct REAL, trades INTEGER, wins INTEGER, win_rate REAL,
  profit_factor REAL, best_trade REAL, max_dd_pct REAL, updated REAL, prize TEXT);
""")
for _col in ("bridge_key TEXT", "is_admin INTEGER DEFAULT 0", "founding INTEGER DEFAULT 0", "ref_code TEXT",
             "referred_by INTEGER", "ref_rewarded INTEGER DEFAULT 0", "addons TEXT DEFAULT '{}'", "paused_until REAL DEFAULT 0",
             "banned INTEGER DEFAULT 0", "ban_reason TEXT", "notes TEXT"):  # upgrade older databases
    try:
        DB.execute("ALTER TABLE users ADD COLUMN " + _col)
    except sqlite3.OperationalError:
        pass
DB.commit()


def q(sql: str, *a, one=False):
    with LOCK:
        cur = DB.execute(sql, a)
        DB.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


# ------------------------------------------------------------------ helpers
def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex(), h)
    except Exception:
        return False


def new_license() -> str:
    a = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "GP-" + "-".join("".join(secrets.choice(a) for _ in range(5)) for _ in range(4))


def make_session(uid: int, days: int = 30) -> str:
    exp = int(time.time() + days * 86400)
    msg = f"{uid}.{exp}"
    sig = hmac.new(SESSION_SECRET, msg.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{msg}.{sig}"


def user_from(auth: Optional[str]):
    tok = (auth or "").removeprefix("Bearer ").strip()
    try:
        uid, exp, sig = tok.split(".")
        good = hmac.new(SESSION_SECRET, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            raise ValueError
    except ValueError:
        raise HTTPException(401, "Please log in again.")
    u = q("SELECT * FROM users WHERE id=?", int(uid), one=True)
    if not u:
        raise HTTPException(401, "Account not found.")
    if u["banned"]:
        raise HTTPException(403, "This account is blocked. Contact support.")
    return u


def effective(u) -> dict:
    """The one place that decides if a customer may use the app."""
    now = time.time()
    st, end = u["status"] or "none", u["period_end"] or 0
    if st == "trialing" and end < now:
        st = "expired"
    if st in ("active", "canceled") and end and end < now:
        st = "expired"
    valid = st in ("trialing", "active", "past_due") or (st == "canceled" and end > now)
    if u["banned"]:
        st, valid = "banned", False
    plan = PLANS.get(u["plan"] or "", None)
    return {"status": st, "valid": valid, "plan": u["plan"], "interval": u["interval"], "period_end": end,
            "cancel_at_end": bool(u["cancel_at_end"]), "days_left": max(0, -int(-(end - now) // 86400)) if end else 0,
            "limits": {"accounts": plan["accounts"], "machines": plan["machines"]} if plan else {"accounts": 0, "machines": 1},
            "features": plan["features"] if plan else []}


def founding_state() -> dict:
    """Live founding seats: real scarcity, shown on the website."""
    taken = q("SELECT COUNT(*) n FROM users WHERE founding=1", one=True)["n"]
    left = max(0, FOUNDING["seats"] - taken)
    ended = bool(FOUNDING["ends"]) and time.strftime("%Y-%m-%d") > FOUNDING["ends"]
    lt = q("SELECT COUNT(*) n FROM users WHERE interval='lifetime'", one=True)["n"]
    return {**FOUNDING, "left": left, "open": left > 0 and not ended, "lifetime_left": max(0, LIFETIME_SEATS - lt), "lifetime_seats": LIFETIME_SEATS}


def ref_code(u) -> str:
    """Each customer's referral code (made on first use)."""
    if u["ref_code"]:
        return u["ref_code"]
    a = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(20):
        c = "".join(secrets.choice(a) for _ in range(6))
        if not q("SELECT id FROM users WHERE ref_code=?", c, one=True):
            q("UPDATE users SET ref_code=? WHERE id=?", c, u["id"])
            return c
    return ""


def reward_referral(u):
    """'Give a month, get a month': the first time someone you referred pays, you both get a month."""
    if not u["referred_by"] or u["ref_rewarded"]:
        return
    q("UPDATE users SET ref_rewarded=1 WHERE id=?", u["id"])
    r = q("SELECT * FROM users WHERE id=?", u["referred_by"], one=True)
    if not r:
        return
    month = PLANS["pro"]["month"]
    if not TEST_MODE and r["stripe_customer"]:  # a credit on the referrer's next Stripe invoice
        try:
            stripe(f"customers/{r['stripe_customer']}/balance_transactions",
                   {"amount": -int(month * 100), "currency": "eur", "description": f"Referral reward: {u['email']} joined"})
        except HTTPException as e:
            print("[referral] credit failed:", e.detail)
    elif r["period_end"]:  # no card on file yet (or test mode): add 30 days of access instead
        q("UPDATE users SET period_end=MAX(period_end, ?) + 30*86400 WHERE id=?", time.time(), r["id"])


def owner_emails() -> set:
    """Owner accounts (admin automatically when they log in): GP_OWNER_EMAILS and/or data/owners.txt (one email per line).
    data/ is never uploaded or zipped, so your email stays private."""
    s = {e.strip().lower() for e in env("GP_OWNER_EMAILS").split(",") if e.strip()}
    f = DATA / "owners.txt"
    if f.is_file():
        s |= {ln.strip().lower() for ln in f.read_text(encoding="utf-8").splitlines() if "@" in ln and not ln.strip().startswith("#")}
    return s


def ensure_owner(u):
    if u and not u["is_admin"] and (u["email"] or "").lower() in owner_emails():
        q("UPDATE users SET is_admin=1 WHERE id=?", u["id"])
        return q("SELECT * FROM users WHERE id=?", u["id"], one=True)
    return u


def me(u) -> dict:
    u = ensure_owner(u)
    refs = q("SELECT COUNT(*) n, COALESCE(SUM(ref_rewarded),0) paid FROM users WHERE referred_by=?", u["id"], one=True)
    return {"user": {"id": u["id"], "email": u["email"], "name": u["name"], "created": u["created"], "is_admin": bool(u["is_admin"])},
            "sub": effective(u), "license": u["license"], "machines": json.loads(u["machines"] or "{}"),
            "payments": [dict(r) for r in q("SELECT ts,amount,currency,descr FROM payments WHERE user_id=? ORDER BY ts DESC LIMIT 50", u["id"])],
            "server": public_url(), "pubkey": PUBKEY_HEX, "test_mode": TEST_MODE, "bridge_key": u["bridge_key"],
            "founding": bool(u["founding"]),
            # setup call: founding members, lifetime and yearly (continuity bonus)
            "booking_url": BOOKING_URL if u["founding"] or u["interval"] in ("lifetime", "lifetime3", "year") else "",
            "addons": json.loads(u["addons"] or "{}"), "paused_until": u["paused_until"] or 0,
            "ref": {"code": ref_code(u), "joined": refs["n"], "rewarded": refs["paid"]}}


def stripe(path: str, data: dict) -> dict:
    """Minimal Stripe API call (form-encoded, like the official libraries). No extra package needed."""
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://api.stripe.com/v1/" + path, data=body,
                                 headers={"Authorization": "Bearer " + STRIPE_KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = json.loads(e.read() or b"{}").get("error", {}).get("message", str(e))
        raise HTTPException(502, "Stripe: " + msg)


# ------------------------------------------------------------------ app
app = FastAPI(title="Goldmine Pro License Server", version="1.0.0")
_BASE_URL: contextvars.ContextVar = contextvars.ContextVar("base_url", default="http://127.0.0.1:8800")


def public_url() -> str:
    """GP_PUBLIC_URL when set (production); otherwise the address this request came in on (local testing)."""
    return PUBLIC_URL or _BASE_URL.get()


@app.middleware("http")
async def _remember_base(request: Request, call_next):
    # include the mount path (/saas when this runs inside your bridge)
    _BASE_URL.set(f"{request.url.scheme}://{request.url.netloc}{request.scope.get('root_path', '')}".rstrip("/"))
    resp = await call_next(request)
    if request.headers.get("access-control-request-private-network", "").lower() == "true":
        resp.headers["Access-Control-Allow-Private-Network"] = "true"  # an https site (e.g. GitHub Pages) calling a local test server
    return resp


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
                   allow_headers=["Content-Type", "Authorization", "X-Admin-Token"])

FAILS: dict = {}


def throttle(request: Request, key: str):
    ip = (request.client.host if request.client else "?") + key
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
    if len(FAILS[ip]) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    return ip


class SignupIn(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=100)
    ref: str = Field(default="", max_length=12)


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(b: SignupIn, request: Request):
    throttle(request, "signup")
    email = b.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address.")
    if q("SELECT id FROM users WHERE email=?", email, one=True):
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    now = time.time()
    by = q("SELECT id FROM users WHERE ref_code=?", b.ref.strip().upper(), one=True) if b.ref.strip() else None
    # card trial: the account starts without access; the 7-day trial starts in Stripe checkout
    st, end = ("none", 0) if TRIAL_CARD else ("trialing", now + (REF_TRIAL_DAYS if by else TRIAL_DAYS) * 86400)
    q("INSERT INTO users(email,name,pw,created,plan,interval,status,period_end,license,bridge_key,referred_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      email, b.name.strip(), hash_pw(b.password), now, TRIAL_PLAN, "month", st, end, new_license(),
      secrets.token_hex(24), by["id"] if by else None)
    q("UPDATE leads SET source=source || ' → signup' WHERE email=? AND source NOT LIKE '%signup%'", email)
    u = q("SELECT * FROM users WHERE email=?", email, one=True)
    return {"token": make_session(u["id"]), **me(u)}


@app.post("/api/auth/login")
def login(b: LoginIn, request: Request):
    ip = throttle(request, "login")
    u = q("SELECT * FROM users WHERE email=?", b.email.strip().lower(), one=True)
    if not u or not check_pw(b.password, u["pw"]):
        FAILS.setdefault(ip, []).append(time.time())
        raise HTTPException(401, "Wrong email or password.")
    if u["banned"]:
        raise HTTPException(403, "This account is blocked. Contact support.")
    return {"token": make_session(u["id"]), **me(u)}


@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(default=None)):
    return me(user_from(authorization))


@app.get("/api/plans")
def plans():
    return {"plans": PLANS, "trial_days": TRIAL_DAYS, "trial_plan": TRIAL_PLAN, "test_mode": TEST_MODE, "currency": "EUR",
            "founding": founding_state(), "guarantee_days": GUARANTEE_DAYS, "ref_trial_days": REF_TRIAL_DAYS, "trial_card": TRIAL_CARD, "setup_call": bool(BOOKING_URL),
            "addons": ADDONS, "challenge_on": CH["on"]}


# ------------------------------------------------------------------ leads (the free Risk Rulebook)
class LeadIn(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    source: str = Field(default="", max_length=40)
    data: dict = Field(default_factory=dict)


LEAD_HITS: dict = {}


@app.post("/api/leads")
def add_lead(b: LeadIn, request: Request):
    ip, now = (request.client.host if request.client else "?"), time.time()
    LEAD_HITS[ip] = [t for t in LEAD_HITS.get(ip, []) if now - t < 3600] + [now]
    if len(LEAD_HITS[ip]) > 20:
        raise HTTPException(429, "Too many requests. Try again later.")
    email = b.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address.")
    data = json.dumps({k: str(v)[:60] for k, v in list(b.data.items())[:12]})
    if q("SELECT id FROM leads WHERE email=?", email, one=True):
        q("UPDATE leads SET data=? WHERE email=?", data, email)
    else:
        q("INSERT INTO leads(email,created,source,data) VALUES(?,?,?,?)", email, now, re.sub(r"[^\w -]", "", b.source) or "site", data)
    return {"ok": True}


# ------------------------------------------------------------------ billing
class CheckoutIn(BaseModel):
    plan: str
    interval: str = "month"
    trial: bool = False  # card trial (GP_TRIAL_CARD=1): start the 7-day Pro trial in checkout


@app.post("/api/billing/checkout")
def checkout(b: CheckoutIn, authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    if b.plan not in PLANS or b.interval not in ("month", "year", "lifetime", "lifetime3") or not PLANS[b.plan].get(b.interval):
        raise HTTPException(400, "Unknown plan.")
    fs = founding_state()
    if b.interval in ("lifetime", "lifetime3") and fs["lifetime_left"] <= 0 and u["interval"] not in ("lifetime", "lifetime3"):
        raise HTTPException(400, "Lifetime is sold out. Pro yearly is the best deal now.")
    found = b.plan == FOUNDING["plan"] and b.interval in ("month", "year") and (bool(u["founding"]) or fs["open"])
    trial = b.trial and TRIAL_CARD and u["status"] in ("none", None) and b.interval != "lifetime"
    if TEST_MODE:  # simulated payment: activates the plan immediately so you can test the whole flow
        now, P = time.time(), PLANS[b.plan]
        amount = FOUNDING[b.interval] if found else P[b.interval]
        if trial:
            q("UPDATE users SET plan=?,interval=?,status='trialing',period_end=?,cancel_at_end=0,founding=? WHERE id=?",
              b.plan, b.interval, now + (REF_TRIAL_DAYS if u["referred_by"] else TRIAL_DAYS) * 86400, 1 if found else u["founding"], u["id"])
            return {"url": None, "test": True, **me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))}
        # test mode: a 3-instalment Lifetime is simulated as fully paid (all 3 payments recorded)
        end = LIFETIME_END if b.interval in ("lifetime", "lifetime3") else now + (365 if b.interval == "year" else 30) * 86400
        q("UPDATE users SET plan=?,interval=?,status='active',period_end=?,cancel_at_end=0,founding=? WHERE id=?",
          b.plan, "lifetime" if b.interval == "lifetime3" else b.interval, end, 1 if found else u["founding"], u["id"])
        for k in range(3 if b.interval == "lifetime3" else 1):
            q("INSERT INTO payments(user_id,ts,amount,currency,descr,ref) VALUES(?,?,?,?,?,?)", u["id"], now + k, amount, "EUR",
              f"{P['name']}{' founding' if found else ''} ({ {'year': 'yearly', 'lifetime': 'lifetime, one-time', 'lifetime3': f'lifetime, instalment {k + 1} of 3'}.get(b.interval, 'monthly')}) · TEST", "test")
        reward_referral(q("SELECT * FROM users WHERE id=?", u["id"], one=True))
        return {"url": None, "test": True, **me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))}
    pid = price_id(b.plan, b.interval, found)
    if not pid:
        raise HTTPException(500, f"Set STRIPE_PRICE_{b.plan.upper()}_{'FOUNDING_' if found else ''}{IVL_NAME[b.interval]} on the server.")
    once = b.interval == "lifetime"  # one-time payment instead of a subscription
    # lifetime3 = a monthly subscription that the webhook ends after the 3rd paid invoice (then: lifetime access)
    if b.interval == "lifetime3":
        trial = False
    data = {"mode": "payment" if once else "subscription", "line_items[0][price]": pid, "line_items[0][quantity]": 1,
            "client_reference_id": str(u["id"]), "metadata[plan]": b.plan, "metadata[interval]": b.interval,
            "metadata[founding]": "1" if found else "0",
            **({} if once else {"subscription_data[metadata][user_id]": str(u["id"])}),
            **({"subscription_data[trial_period_days]": REF_TRIAL_DAYS if u["referred_by"] else TRIAL_DAYS} if trial else {}),
            "success_url": public_url() + "/#welcome?paid=1", "cancel_url": public_url() + "/#pricing",
            "allow_promotion_codes": "true"}
    if b.interval == "lifetime3":
        data["subscription_data[metadata][lifetime3]"] = "1"
    if u["stripe_customer"]:
        data["customer"] = u["stripe_customer"]
    else:
        data["customer_email"] = u["email"]
    return {"url": stripe("checkout/sessions", data)["url"]}


class AddonIn(BaseModel):
    addon: str = Field(max_length=32)


def _grant_addon(uid: int, key: str, amount: float, ref: str, test: bool = False):
    u = q("SELECT * FROM users WHERE id=?", uid, one=True)
    if not u or key not in ADDONS:
        return
    have = json.loads(u["addons"] or "{}")
    have[key] = time.time()
    q("UPDATE users SET addons=? WHERE id=?", json.dumps(have), uid)
    q("INSERT INTO payments(user_id,ts,amount,currency,descr,ref) VALUES(?,?,?,?,?,?)", uid, time.time(), amount, "EUR",
      ADDONS[key]["name"] + (" · TEST" if test else ""), ref)


@app.post("/api/billing/addon")
def buy_addon(b: AddonIn, authorization: Optional[str] = Header(default=None)):
    """One-time add-on (classic upsell after the main purchase)."""
    u = user_from(authorization)
    a = ADDONS.get(b.addon)
    if not a:
        raise HTTPException(400, "Unknown add-on.")
    if b.addon in json.loads(u["addons"] or "{}"):
        raise HTTPException(400, "You already have this add-on.")
    if TEST_MODE:
        _grant_addon(u["id"], b.addon, a["price"], "test", True)
        return {"url": None, "test": True, **me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))}
    pid = env(f"STRIPE_PRICE_ADDON_{b.addon.upper()}")
    if not pid:
        raise HTTPException(500, f"Set STRIPE_PRICE_ADDON_{b.addon.upper()} on the server.")
    data = {"mode": "payment", "line_items[0][price]": pid, "line_items[0][quantity]": 1, "client_reference_id": str(u["id"]),
            "metadata[addon]": b.addon, "success_url": public_url() + "/#welcome?addon=1", "cancel_url": public_url() + "/#welcome"}
    if u["stripe_customer"]:
        data["customer"] = u["stripe_customer"]
    else:
        data["customer_email"] = u["email"]
    return {"url": stripe("checkout/sessions", data)["url"]}


@app.post("/api/billing/pause")
def pause(authorization: Optional[str] = Header(default=None)):
    """Downsell before cancelling: pause billing for 30 days, access stays."""
    u = user_from(authorization)
    s = effective(u)
    if s["status"] not in ("active", "past_due") or u["interval"] in ("lifetime", "lifetime3"):
        raise HTTPException(400, "Only an active monthly or yearly subscription can be paused.")
    until = time.time() + 30 * 86400
    if TEST_MODE or not u["stripe_sub"]:
        q("UPDATE users SET paused_until=?, period_end=period_end + 30*86400, cancel_at_end=0 WHERE id=?", until, u["id"])
    else:
        stripe(f"subscriptions/{u['stripe_sub']}", {"pause_collection[behavior]": "void", "pause_collection[resumes_at]": int(until)})
        q("UPDATE users SET paused_until=?, cancel_at_end=0 WHERE id=?", until, u["id"])
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


@app.post("/api/billing/portal")
def portal(authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    if TEST_MODE or not u["stripe_customer"]:
        return {"url": None, "test": TEST_MODE}
    return {"url": stripe("billing_portal/sessions", {"customer": u["stripe_customer"], "return_url": public_url() + "/#dashboard"})["url"]}


@app.post("/api/billing/cancel")
def cancel(authorization: Optional[str] = Header(default=None)):
    """Test mode only (with Stripe, customers cancel in the Stripe portal and the webhook updates us)."""
    u = user_from(authorization)
    if not TEST_MODE:
        raise HTTPException(400, "Use 'Manage billing' to cancel.")
    q("UPDATE users SET cancel_at_end=1, status=CASE WHEN status='trialing' THEN 'canceled' ELSE status END WHERE id=?", u["id"])
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


@app.post("/api/billing/resume")
def resume(authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    if not TEST_MODE:
        raise HTTPException(400, "Use 'Manage billing'.")
    q("UPDATE users SET cancel_at_end=0, paused_until=0 WHERE id=?", u["id"])
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


class TestStateIn(BaseModel):
    state: str


@app.post("/api/test/state")
def test_state(b: TestStateIn, authorization: Optional[str] = Header(default=None)):
    """TEST MODE ONLY: jump your own account to any situation (trial ending, expired, payment failed...)
    so you can see what customers see. Disappears as soon as Stripe keys are set."""
    if not TEST_MODE:
        raise HTTPException(404, "Not found.")
    u, now = user_from(authorization), time.time()
    plan = u["plan"] if u["plan"] in PLANS else TRIAL_PLAN
    states = {
        "trial": ("trialing", TRIAL_PLAN, now + TRIAL_DAYS * 86400, 0),
        "trial_last_day": ("trialing", TRIAL_PLAN, now + 3600 * 20, 0),
        "trial_expired": ("trialing", TRIAL_PLAN, now - 60, 0),
        "active": ("active", plan, now + 30 * 86400, 0),
        "canceling": ("active", plan, now + 12 * 86400, 1),
        "past_due": ("past_due", plan, now + 20 * 86400, 0),
        "ended": ("canceled", plan, now - 60, 0),
    }
    if b.state == "unlink_pcs":
        q("UPDATE users SET machines='{}' WHERE id=?", u["id"])
    elif b.state in states:
        st, p, end, cae = states[b.state]
        q("UPDATE users SET status=?,plan=?,period_end=?,cancel_at_end=? WHERE id=?", st, p, end, cae, u["id"])
    else:
        raise HTTPException(400, "Unknown test state.")
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


def _stripe_signature_ok(payload: bytes, header: str) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        t = int(parts["t"])
        sigs = [p.split("=", 1)[1] for p in header.split(",") if p.startswith("v1=")]
    except Exception:
        return False
    if abs(time.time() - t) > 300:
        return False
    good = hmac.new(STRIPE_WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(good, s) for s in sigs)


def _apply_sub(sub: dict, uid: Optional[int] = None):
    uid = uid or int((sub.get("metadata") or {}).get("user_id") or 0)
    u = (uid and q("SELECT * FROM users WHERE id=?", uid, one=True)) \
        or q("SELECT * FROM users WHERE stripe_sub=?", sub.get("id"), one=True) \
        or q("SELECT * FROM users WHERE stripe_customer=? ORDER BY id DESC", sub.get("customer"), one=True)
    if not u or u["interval"] == "lifetime":  # lifetime (incl. a finished 3-instalment plan) is never downgraded by a subscription event
        return
    items = (sub.get("items") or {}).get("data") or []
    pi = plan_from_price(items[0]["price"]["id"]) if items else None
    end = sub.get("current_period_end") or (items[0].get("current_period_end") if items else None) or u["period_end"]
    q("UPDATE users SET status=?,period_end=?,cancel_at_end=?,stripe_sub=?,stripe_customer=?,plan=?,interval=? WHERE id=?",
      sub.get("status", "active"), end, 1 if sub.get("cancel_at_period_end") else 0, sub.get("id"), sub.get("customer"),
      pi[0] if pi else u["plan"], pi[1] if pi else u["interval"], u["id"])


@app.post("/api/billing/webhook")
async def webhook(request: Request):
    payload = await request.body()
    if not STRIPE_WHSEC or not _stripe_signature_ok(payload, request.headers.get("stripe-signature", "")):
        raise HTTPException(400, "Bad signature.")
    ev = json.loads(payload)
    if q("SELECT id FROM events WHERE id=?", ev["id"], one=True):
        return {"ok": True, "duplicate": True}
    q("INSERT INTO events(id,ts) VALUES(?,?)", ev["id"], time.time())
    obj, typ = ev["data"]["object"], ev["type"]
    if typ == "checkout.session.completed" and (obj.get("metadata") or {}).get("interval") == "lifetime":
        uid = int(obj.get("client_reference_id") or 0)  # lifetime deal: one-time payment, access until 2100
        q("UPDATE users SET stripe_customer=?, plan=?, interval='lifetime', status='active', period_end=?, cancel_at_end=0 WHERE id=?",
          obj.get("customer"), (obj.get("metadata") or {}).get("plan", "pro"), LIFETIME_END, uid)
        q("INSERT INTO payments(user_id,ts,amount,currency,descr,ref) VALUES(?,?,?,?,?,?)", uid, time.time(),
          (obj.get("amount_total") or 0) / 100, (obj.get("currency") or "eur").upper(), "Lifetime deal", obj.get("id"))
        u = q("SELECT * FROM users WHERE id=?", uid, one=True)
        if u:
            reward_referral(u)
    elif typ == "checkout.session.completed" and (obj.get("metadata") or {}).get("addon"):
        uid = int(obj.get("client_reference_id") or 0)  # one-time add-on: record it, the plan does not change
        if obj.get("customer"):
            q("UPDATE users SET stripe_customer=COALESCE(stripe_customer, ?) WHERE id=?", obj.get("customer"), uid)
        _grant_addon(uid, obj["metadata"]["addon"], (obj.get("amount_total") or 0) / 100, obj.get("id"))
    elif typ == "checkout.session.completed":
        uid = int(obj.get("client_reference_id") or 0)
        q("UPDATE users SET stripe_customer=?, stripe_sub=?, status='active', cancel_at_end=0 WHERE id=?",
          obj.get("customer"), obj.get("subscription"), uid)
        if (obj.get("metadata") or {}).get("founding") == "1":
            q("UPDATE users SET founding=1 WHERE id=?", uid)
        u = q("SELECT * FROM users WHERE id=?", uid, one=True)
        if u:
            reward_referral(u)
        if obj.get("subscription"):
            try:
                with urllib.request.urlopen(urllib.request.Request("https://api.stripe.com/v1/subscriptions/" + obj["subscription"],
                                                                   headers={"Authorization": "Bearer " + STRIPE_KEY}), timeout=20) as r:
                    _apply_sub(json.loads(r.read()), uid)
            except Exception as e:  # the subscription.* events below fix it anyway
                print("[webhook] could not load subscription:", e)
    elif typ in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        if typ.endswith("deleted"):
            obj["status"] = "canceled"
        _apply_sub(obj)
    elif typ == "invoice.paid":
        u = q("SELECT * FROM users WHERE stripe_customer=?", obj.get("customer"), one=True)
        if u:
            inst = u["interval"] == "lifetime3" and (obj.get("amount_paid") or 0) > 0
            q("INSERT INTO payments(user_id,ts,amount,currency,descr,ref) VALUES(?,?,?,?,?,?)", u["id"], time.time(),
              (obj.get("amount_paid") or 0) / 100, (obj.get("currency") or "eur").upper(),
              "Lifetime instalment" if inst else "Subscription payment", obj.get("id"))
            if inst and q("SELECT COUNT(*) n FROM payments WHERE user_id=? AND descr='Lifetime instalment'", u["id"], one=True)["n"] >= 3:
                # 3rd instalment paid: lifetime access, end the instalment subscription
                q("UPDATE users SET interval='lifetime', status='active', period_end=?, cancel_at_end=0 WHERE id=?", LIFETIME_END, u["id"])
                sid = obj.get("subscription") or u["stripe_sub"]
                if sid:
                    try:
                        urllib.request.urlopen(urllib.request.Request("https://api.stripe.com/v1/subscriptions/" + sid, method="DELETE",
                                                                      headers={"Authorization": "Bearer " + STRIPE_KEY}), timeout=20).read()
                    except Exception as e:
                        print("[webhook] could not end the instalment subscription:", e)
    elif typ == "invoice.payment_failed":
        q("UPDATE users SET status='past_due' WHERE stripe_customer=?", obj.get("customer"))
    return {"ok": True}


# ------------------------------------------------------------------ licenses (called by customer bridges)
class VerifyIn(BaseModel):
    key: str = Field(max_length=64)
    machine: str = Field(min_length=8, max_length=80)
    name: str = Field(default="", max_length=80)
    version: str = Field(default="", max_length=20)


@app.post("/api/license/verify")
def verify(b: VerifyIn, request: Request):
    ip = throttle(request, "verify")
    u = q("SELECT * FROM users WHERE license=?", b.key.strip().upper(), one=True)
    now = time.time()
    if not u:
        FAILS.setdefault(ip, []).append(now)
        raise HTTPException(404, "Unknown license key. Copy it again from your dashboard.")
    s = effective(u)
    machines = json.loads(u["machines"] or "{}")
    reason = "" if s["valid"] else {"expired": "Your subscription has ended.", "past_due": "Payment failed.",
                                     "canceled": "Your subscription was cancelled.", "banned": "This account is blocked. Contact support."}.get(s["status"], "No active subscription.")
    if s["valid"] and b.machine not in machines and len(machines) >= s["limits"]["machines"]:
        reason = (f"This license is already used on {len(machines)} PC(s), the maximum for your plan. "
                  "Remove a PC in your dashboard, or upgrade.")
    if not reason or b.machine in machines:
        machines.setdefault(b.machine, {"first": now})
        machines[b.machine].update({"name": b.name[:80], "last": now, "version": b.version})
    q("UPDATE users SET machines=?, last_seen=? WHERE id=?", json.dumps(machines), now, u["id"])
    payload = {"key": u["license"], "machine": b.machine, "valid": not reason, "reason": reason, "status": s["status"],
               "plan": s["plan"], "features": s["features"], "limits": s["limits"], "email": u["email"],
               "period_end": s["period_end"], "iat": int(now), "exp": int(now + GRACE_HOURS * 3600)}
    return {"token": gp_sign.sign_token(payload, SIGNING_SEED), **payload}


@app.get("/api/license/pubkey")
def pubkey():
    return {"pubkey": PUBKEY_HEX}


@app.post("/api/license/rotate")
def rotate(authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    q("UPDATE users SET license=?, machines='{}' WHERE id=?", new_license(), u["id"])
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


class MachineIn(BaseModel):
    machine: str


@app.post("/api/license/machines/remove")
def machine_remove(b: MachineIn, authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    m = json.loads(u["machines"] or "{}")
    m.pop(b.machine, None)
    q("UPDATE users SET machines=? WHERE id=?", json.dumps(m), u["id"])
    return me(q("SELECT * FROM users WHERE id=?", u["id"], one=True))


# ------------------------------------------------------------------ bridge download (personalised zip)
# The customer only unzips and double-clicks "START GOLDMINE.bat". Everything else is already filled in.
BRIDGE_FILES = {"app.py": "app.py", "paper.py": "paper.py", "licensing.py": "licensing.py", "gp_sign.py": "gp_sign.py",
                "requirements.txt": "requirements.txt", "run_bridge.py": "run_bridge.py",
                # customers get a plain .bat + Python launcher: no PowerShell script (Windows flags downloaded .ps1 files)
                "start_customer.bat": "START GOLDMINE.bat", "terminal_pro.html": "terminal_pro.html",
                "journal.html": "journal.html", "gp-shared.js": "gp-shared.js", "gp-theme.css": "gp-theme.css"}
# NEVER in the zip: YOUR config.json, accounts_extra.json, paper_accounts.json, copy_settings.json, targets.json, private keys, saas/


def ensure_bridge_key(u) -> str:
    if u["bridge_key"]:
        return u["bridge_key"]
    k = secrets.token_hex(24)
    q("UPDATE users SET bridge_key=? WHERE id=?", k, u["id"])
    return k


@app.get("/api/download/bridge")
def download(site: str = "", authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    if not effective(u)["valid"]:
        raise HTTPException(402, "Start a plan or trial first.")
    site = site.rstrip("/") if re.fullmatch(r"https://[\w.\-/]+|http://(127\.0\.0\.1|localhost)(:\d+)?[\w.\-/]*", site or "") else public_url()
    key = ensure_bridge_key(u)
    config = {  # ready to run: random bridge key (the website knows it, so the customer never types it),
        "mode": "demo",           # accounts are detected from the MT5 terminal that is logged in
        "live_trading": False,    # set to true to also allow real-money accounts
        "api_key": key,
        "allowed_origins": [re.match(r"https?://[^/]+", site).group(0)],
        "terminals": [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for src, dst in BRIDGE_FILES.items():
            p = SITE / src
            if p.is_file():
                z.write(p, "GoldmineBridge/" + dst)
        z.writestr("GoldmineBridge/config.json", json.dumps(config, indent=2))
        z.writestr("GoldmineBridge/license.json", json.dumps(
            {"server": public_url(), "site": site, "pubkey": PUBKEY_HEX, "key": u["license"]}, indent=2))
        z.writestr("GoldmineBridge/README.txt", BRIDGE_README.format(url=site))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="GoldmineBridge.zip"'})


BRIDGE_README = """GOLDMINE PRO BRIDGE
====================
Before you unzip: right-click GoldmineBridge.zip > Properties > tick "Unblock" > OK.
(Windows then trusts the files inside and won't warn about them.)

1. Open MetaTrader 5 and log in to your trading account.
2. Double-click "START GOLDMINE.bat" and keep that window open.
   (The first time it installs what it needs; this takes 1-2 minutes.)
3. Your browser opens {url} and connects by itself. Done.

Real-money account? Open config.json and change  "live_trading": false  into  "live_trading": true , then start again.
Trading from your phone or another PC (optional): see Setup > "Trade from anywhere" on the website.
license.json contains YOUR license key. Do not share it: it works on a limited number of PCs.
"""

# ------------------------------------------------------------------ admin (you)
def _admin_check(tok: Optional[str], auth: Optional[str] = None):
    """Admin = the admin token, OR a logged-in owner/admin account (see owners.txt and /api/admin/claim)."""
    if ADMIN_TOKEN and tok and hmac.compare_digest(tok, ADMIN_TOKEN):
        return
    if auth:
        try:
            if ensure_owner(user_from(auth))["is_admin"]:
                return
        except HTTPException:
            pass
    raise HTTPException(401, "Admin only: log in with your admin account, or enter the admin token once.")


@app.post("/api/admin/claim")
def admin_claim(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Logged in + correct admin token once -> this account is admin for good: the Admin tab then works
    on every device and address where you log in, without the token."""
    if not (ADMIN_TOKEN and x_admin_token and hmac.compare_digest(x_admin_token, ADMIN_TOKEN)):
        raise HTTPException(401, "Wrong admin token.")
    u = user_from(authorization)
    q("UPDATE users SET is_admin=1 WHERE id=?", u["id"])
    return {"ok": True, "message": f"{u['email']} is now an admin account."}


@app.post("/api/admin/delete_user")
def admin_delete_user(body: dict, x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Remove a customer for good: stops their Stripe subscription, revokes the license (their bridge stops
    within 30 minutes) and deletes the account and its payment rows."""
    _admin_check(x_admin_token, authorization)
    email = str(body.get("email") or "").strip().lower()
    u = q("SELECT * FROM users WHERE lower(email)=?", email, one=True)
    if not u:
        raise HTTPException(404, "No user with that email.")
    stripe_note = ""
    if u["stripe_sub"] and STRIPE_KEY:
        try:  # cancel now, so a deleted customer is never charged again
            req = urllib.request.Request("https://api.stripe.com/v1/subscriptions/" + u["stripe_sub"], method="DELETE",
                                         headers={"Authorization": "Bearer " + STRIPE_KEY})
            urllib.request.urlopen(req, timeout=20).read()
            stripe_note = " Stripe subscription cancelled."
        except Exception as e:
            if body.get("force") is not True:
                raise HTTPException(502, f"Could not cancel the Stripe subscription ({e}). Cancel it in Stripe first, or delete anyway.")
            stripe_note = " Stripe subscription NOT cancelled: check Stripe."
    q("DELETE FROM payments WHERE user_id=?", u["id"])
    q("DELETE FROM users WHERE id=?", u["id"])
    return {"ok": True, "message": f"{u['email']} deleted.{stripe_note}"}


@app.get("/api/admin/overview")
def admin(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    _admin_check(x_admin_token, authorization)
    rows, now, out = q("SELECT * FROM users ORDER BY created DESC"), time.time(), []
    mrr = 0.0
    counts = {"active": 0, "trialing": 0, "past_due": 0, "expired": 0, "canceled": 0, "none": 0, "banned": 0}
    for u in rows:
        s = effective(u)
        counts[s["status"]] = counts.get(s["status"], 0) + 1
        if s["status"] in ("active", "past_due") and s["plan"] in PLANS and not s["cancel_at_end"]:
            P = FOUNDING if u["founding"] and s["plan"] == FOUNDING["plan"] else PLANS[s["plan"]]
            mrr += 0 if s["interval"] == "lifetime" else P["year"] / 12 if s["interval"] == "year" else P.get("lifetime3", 0) if s["interval"] == "lifetime3" else P["month"]
        out.append(_cust_row(u))
    rev30 = q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE ts>?", now - 30 * 86400, one=True)["s"]
    leads = q("SELECT COUNT(*) n, COALESCE(SUM(created>?),0) m FROM leads", now - 30 * 86400, one=True)
    return {"mrr": round(mrr, 2), "arr": round(mrr * 12, 2), "revenue_30d": rev30, "counts": counts, "customers": out,
            "signups_30d": sum(1 for u in rows if u["created"] > now - 30 * 86400), "test_mode": TEST_MODE,
            "leads": leads["n"], "leads_30d": leads["m"], "founding": founding_state()}


# ------------------------------------------------------------------ admin: manage people
def _admin_who(tok, auth) -> str:
    """Who did it (for the audit log)."""
    if auth:
        try:
            return user_from(auth)["email"]
        except HTTPException:
            pass
    return "admin token"


def _log(who: str, action: str, target: str, detail: str = ""):
    q("INSERT INTO admin_log(ts,admin,action,target,detail) VALUES(?,?,?,?,?)", time.time(), who, action, target, detail[:500])


def _cust_row(u) -> dict:
    s = effective(u)
    return {"email": u["email"], "name": u["name"], "created": u["created"], "status": s["status"], "plan": s["plan"],
            "interval": s["interval"], "period_end": s["period_end"], "cancel_at_end": s["cancel_at_end"],
            "machines": len(json.loads(u["machines"] or "{}")), "last_seen": u["last_seen"], "founding": bool(u["founding"]),
            "banned": bool(u["banned"]), "is_admin": bool(u["is_admin"]), "notes": bool((u["notes"] or "").strip()),
            "addons": list(json.loads(u["addons"] or "{}")), "stripe": bool(u["stripe_sub"]), "referred_by": u["referred_by"],
            "paid": q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE user_id=?", u["id"], one=True)["s"]}


@app.get("/api/admin/user")
def admin_user(email: str, x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Everything about one customer."""
    _admin_check(x_admin_token, authorization)
    u = q("SELECT * FROM users WHERE lower(email)=?", email.strip().lower(), one=True)
    if not u:
        raise HTTPException(404, "No user with that email.")
    by = q("SELECT email FROM users WHERE id=?", u["referred_by"], one=True) if u["referred_by"] else None
    lead = q("SELECT * FROM leads WHERE email=?", u["email"], one=True)
    return {**_cust_row(u), "license": u["license"], "ref_code": u["ref_code"], "machines_list": json.loads(u["machines"] or "{}"),
            "addons_full": json.loads(u["addons"] or "{}"), "ban_reason": u["ban_reason"] or "", "notes_text": u["notes"] or "",
            "stripe_customer": u["stripe_customer"] or "", "stripe_sub": u["stripe_sub"] or "", "paused_until": u["paused_until"] or 0,
            "referred_by_email": by["email"] if by else "", "lead": dict(lead) if lead else None,
            "referrals": [dict(r) for r in q("SELECT email, created, ref_rewarded FROM users WHERE referred_by=? ORDER BY created DESC", u["id"])],
            "payments": [dict(r) for r in q("SELECT id,ts,amount,currency,descr,ref FROM payments WHERE user_id=? ORDER BY ts DESC", u["id"])],
            "log": [dict(r) for r in q("SELECT ts,admin,action,detail FROM admin_log WHERE target=? ORDER BY ts DESC LIMIT 50", u["email"])],
            "addons_catalog": ADDONS, "plans_catalog": {k: v["name"] for k, v in PLANS.items()}}


class AdminActIn(BaseModel):
    email: str = Field(max_length=200)
    action: str = Field(max_length=32)
    plan: str = Field(default="", max_length=20)
    interval: str = Field(default="", max_length=20)
    days: int = Field(default=0, ge=0, le=36500)
    amount: float = Field(default=0, ge=-100000, le=100000)
    text: str = Field(default="", max_length=2000)
    key: str = Field(default="", max_length=80)
    name: str = Field(default="", max_length=100)


@app.post("/api/admin/action")
def admin_action(b: AdminActIn, x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """One admin action on one customer. Every action is written to the audit log.
    Manual plan changes don't touch Stripe: for a Stripe subscriber, change or cancel it in Stripe too."""
    _admin_check(x_admin_token, authorization)
    who, now, a = _admin_who(x_admin_token, authorization), time.time(), b.action
    email = b.email.strip().lower()
    if a == "create":  # add a customer by hand (e.g. paid by bank transfer, a partner, a friend)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(400, "Enter a valid email address.")
        if q("SELECT id FROM users WHERE email=?", email, one=True):
            raise HTTPException(409, "That email already has an account.")
        pw = secrets.token_urlsafe(9)
        plan = b.plan if b.plan in PLANS else TRIAL_PLAN
        days = b.days or TRIAL_DAYS
        q("INSERT INTO users(email,name,pw,created,plan,interval,status,period_end,license,bridge_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
          email, b.name.strip(), hash_pw(pw), now, plan, "month", "active" if b.plan else "trialing", now + days * 86400,
          new_license(), secrets.token_hex(24))
        _log(who, "create", email, f"{plan}, {days} days")
        return {"ok": True, "message": f"Account created. Temporary password: {pw}", "password": pw}
    u = q("SELECT * FROM users WHERE lower(email)=?", email, one=True)
    if not u:
        raise HTTPException(404, "No user with that email.")
    uid, msg, detail = u["id"], "", ""
    warn = " Note: they also have a Stripe subscription; change or cancel it in Stripe too." if u["stripe_sub"] and a in ("set_plan", "end") else ""
    if a == "set_plan":  # manual upgrade / downgrade / comp
        if b.plan not in PLANS or b.interval not in ("month", "year", "lifetime"):
            raise HTTPException(400, "Pick a plan and a billing period.")
        end = LIFETIME_END if b.interval == "lifetime" else now + (b.days or (365 if b.interval == "year" else 30)) * 86400
        q("UPDATE users SET plan=?,interval=?,status='active',period_end=?,cancel_at_end=0 WHERE id=?", b.plan, b.interval, end, uid)
        detail, msg = f"{b.plan} {b.interval}" + ("" if b.interval == "lifetime" else f" until {time.strftime('%Y-%m-%d', time.gmtime(end))}"), f"Plan set to {PLANS[b.plan]['name']} ({b.interval})."
    elif a == "extend":  # add days of access (goodwill, bug compensation, referral by hand)
        if not b.days:
            raise HTTPException(400, "How many days?")
        base = max(u["period_end"] or 0, now)
        st = u["status"] if u["status"] in ("trialing", "active", "past_due") else "active"
        q("UPDATE users SET period_end=?, status=? WHERE id=?", base + b.days * 86400, st, uid)
        detail, msg = f"+{b.days} days", f"Added {b.days} days."
    elif a == "trial":  # (re)start a free trial
        q("UPDATE users SET status='trialing', plan=?, period_end=?, cancel_at_end=0 WHERE id=?",
          b.plan if b.plan in PLANS else TRIAL_PLAN, now + (b.days or TRIAL_DAYS) * 86400, uid)
        detail, msg = f"{b.days or TRIAL_DAYS} days", "Trial started."
    elif a == "end":  # stop access now
        q("UPDATE users SET status='canceled', period_end=?, cancel_at_end=0 WHERE id=?", now - 1, uid)
        msg = "Access ended."
    elif a == "founding":
        q("UPDATE users SET founding=? WHERE id=?", 0 if u["founding"] else 1, uid)
        detail = "on" if not u["founding"] else "off"; msg = f"Founding member {detail}."
    elif a in ("ban", "unban"):  # blocklist: no login, no sign-up with this email, bridge stops at its next check
        if a == "ban" and (u["email"].lower() in owner_emails() or u["email"] == who):
            raise HTTPException(400, "You can't block an owner account or yourself.")
        q("UPDATE users SET banned=?, ban_reason=? WHERE id=?", 1 if a == "ban" else 0, b.text.strip() if a == "ban" else "", uid)
        detail, msg = b.text.strip(), "Blocked: they can't log in and their bridge stops at its next check." if a == "ban" else "Unblocked."
    elif a == "note":
        q("UPDATE users SET notes=? WHERE id=?", b.text, uid)
        msg = "Note saved."
    elif a in ("make_admin", "remove_admin"):
        if a == "remove_admin" and u["email"].lower() in owner_emails():
            raise HTTPException(400, "This is an owner account (owners.txt): remove it there first.")
        q("UPDATE users SET is_admin=? WHERE id=?", 1 if a == "make_admin" else 0, uid)
        msg = "Admin rights " + ("given." if a == "make_admin" else "removed.")
    elif a == "new_license":
        q("UPDATE users SET license=?, machines='{}' WHERE id=?", new_license(), uid)
        msg = "New license key made; every linked PC was removed."
    elif a == "remove_pc":
        m = json.loads(u["machines"] or "{}"); m.pop(b.key, None)
        q("UPDATE users SET machines=? WHERE id=?", json.dumps(m), uid)
        detail, msg = b.key[:8], "PC removed."
    elif a == "reset_password":
        pw = secrets.token_urlsafe(9)
        q("UPDATE users SET pw=? WHERE id=?", hash_pw(pw), uid)
        _log(who, a, u["email"])
        return {"ok": True, "message": f"New temporary password: {pw} (send it to them, they can change it after logging in)", "password": pw}
    elif a in ("addon_grant", "addon_remove"):
        if b.key not in ADDONS:
            raise HTTPException(400, "Unknown add-on.")
        have = json.loads(u["addons"] or "{}")
        if a == "addon_grant":
            have[b.key] = now
        else:
            have.pop(b.key, None)
        q("UPDATE users SET addons=? WHERE id=?", json.dumps(have), uid)
        detail, msg = b.key, ("Add-on given (no payment recorded)." if a == "addon_grant" else "Add-on removed.")
    elif a == "payment":  # record a payment made outside Stripe (bank transfer, cash) or a refund (negative amount)
        if not b.amount:
            raise HTTPException(400, "Enter an amount (negative for a refund).")
        q("INSERT INTO payments(user_id,ts,amount,currency,descr,ref) VALUES(?,?,?,?,?,?)", uid, now, b.amount, "EUR",
          (b.text.strip() or ("Refund" if b.amount < 0 else "Manual payment")) + " · manual", "admin")
        detail, msg = f"{b.amount:+.2f} EUR {b.text.strip()}", ("Refund recorded." if b.amount < 0 else "Payment recorded.") + \
            (" Do the actual refund in Stripe." if b.amount < 0 and u["stripe_customer"] else "")
    elif a == "cancel_stripe":  # cancel the Stripe subscription at the end of the paid period
        if TEST_MODE or not u["stripe_sub"]:
            q("UPDATE users SET cancel_at_end=1 WHERE id=?", uid)
        else:
            stripe(f"subscriptions/{u['stripe_sub']}", {"cancel_at_period_end": "true"})
            q("UPDATE users SET cancel_at_end=1 WHERE id=?", uid)
        msg = "Subscription cancels at the end of the paid period."
    elif a == "rename":
        q("UPDATE users SET name=? WHERE id=?", b.name.strip(), uid)
        msg = "Name saved."
    else:
        raise HTTPException(400, "Unknown action.")
    _log(who, a, u["email"], detail)
    return {"ok": True, "message": msg + warn}


@app.get("/api/admin/activity")
def admin_activity(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Recent payments, the admin audit log and the latest leads (for the Admin tabs)."""
    _admin_check(x_admin_token, authorization)
    pays = q("SELECT p.ts,p.amount,p.currency,p.descr,u.email FROM payments p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.ts DESC LIMIT 200")
    leads = q("SELECT l.email,l.created,l.source,l.data,(SELECT COUNT(*) FROM users u WHERE u.email=l.email) c FROM leads l ORDER BY created DESC LIMIT 300")
    return {"payments": [dict(r) for r in pays], "log": [dict(r) for r in q("SELECT * FROM admin_log ORDER BY ts DESC LIMIT 200")],
            "leads": [{**dict(r), "customer": bool(r["c"])} for r in leads]}


@app.get("/api/admin/customers.csv")
def admin_customers_csv(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    _admin_check(x_admin_token, authorization)
    import csv
    buf = io.StringIO(); w = csv.writer(buf)
    cols = ["email", "name", "status", "plan", "interval", "period_end", "founding", "banned", "paid", "machines", "created", "last_seen"]
    w.writerow(cols)
    for u in q("SELECT * FROM users ORDER BY created DESC"):
        r = _cust_row(u)
        for k in ("period_end", "created", "last_seen"):
            r[k] = time.strftime("%Y-%m-%d", time.gmtime(r[k])) if r[k] else ""
        w.writerow([r[k] for k in cols])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=goldmine-customers.csv"})


# ------------------------------------------------------------------ monthly challenge
# Every month a free paper-trading challenge: same start balance and rules for everyone, ranked by % return.
# Results come from the trader's bridge (paper account with fixed rules). The top places win Pro.
# Results are produced on the trader's own PC: check the winners' trades before you give the prize (Admin > Challenge).
CH = {"on": env("GP_CHALLENGE_ON", "1") == "1", "balance": float(env("GP_CHALLENGE_BALANCE", "10000") or 10000),
      "leverage": int(env("GP_CHALLENGE_LEVERAGE", "100") or 100), "daily_loss": float(env("GP_CHALLENGE_DAILY_LOSS", "5") or 5),
      "max_dd": float(env("GP_CHALLENGE_MAX_DD", "10") or 10), "min_trades": int(env("GP_CHALLENGE_MIN_TRADES", "15") or 15),
      # "label:days of Pro|…" for 1st, 2nd, 3rd place
      "prizes": [(x.split(":")[0], int(x.split(":")[1])) for x in
                 (env("GP_CHALLENGE_PRIZES", "Pro for 12 months:365|Pro for 6 months:182|Pro for 3 months:91")).split("|") if ":" in x]}


def _month_bounds(ts: float):
    t = time.gmtime(ts)
    start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))  # 1st of the month 00:00 UTC
    ny, nm = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    end = calendar.timegm((ny, nm, 1, 0, 0, 0, 0, 0, 0)) - 1
    return int(start), int(end), time.strftime("%B %Y", t)


def current_challenge():
    """This month's challenge (made on first use)."""
    start, end, label = _month_bounds(time.time())
    cid = time.strftime("%Y-%m", time.gmtime())
    row = q("SELECT * FROM challenges WHERE id=?", cid, one=True)
    if not row:
        q("INSERT OR IGNORE INTO challenges(id,name,start,end,balance,leverage,daily_loss,max_dd,min_trades,prizes) VALUES(?,?,?,?,?,?,?,?,?,?)",
          cid, f"{label} Challenge", start, end, CH["balance"], CH["leverage"], CH["daily_loss"], CH["max_dd"], CH["min_trades"],
          json.dumps(CH["prizes"]))
        row = q("SELECT * FROM challenges WHERE id=?", cid, one=True)
    return row


def _ch_public(ch) -> dict:
    return {"id": ch["id"], "name": ch["name"], "start": ch["start"], "end": ch["end"], "balance": ch["balance"],
            "leverage": ch["leverage"], "daily_loss": ch["daily_loss"], "max_dd": ch["max_dd"], "min_trades": ch["min_trades"],
            "prizes": [{"place": i + 1, "label": x[0], "days": x[1]} for i, x in enumerate(json.loads(ch["prizes"] or "[]"))],
            "entries": q("SELECT COUNT(*) n FROM challenge_entries WHERE challenge_id=?", ch["id"], one=True)["n"]}


def _ranked(cid: str):
    """Ranking: qualified traders by % return, then those with too few trades, then failed / disqualified."""
    ch = q("SELECT * FROM challenges WHERE id=?", cid, one=True)
    rows = q("SELECT * FROM challenge_entries WHERE challenge_id=?", cid)
    def key(r):
        out = r["status"] in ("failed", "disqualified")
        qual = (r["trades"] or 0) >= (ch["min_trades"] if ch else 0)
        return (out, not qual, -(r["return_pct"] or 0), -(r["trades"] or 0))
    return ch, sorted(rows, key=key)


def _board_row(r, rank, ch) -> dict:
    qual = (r["trades"] or 0) >= ch["min_trades"] and r["status"] not in ("failed", "disqualified")
    return {"rank": rank if qual else None, "nickname": r["nickname"], "return_pct": r["return_pct"] or 0, "equity": r["equity"] or ch["balance"],
            "trades": r["trades"] or 0, "win_rate": r["win_rate"] or 0, "max_dd_pct": r["max_dd_pct"] or 0, "status": r["status"],
            "qualified": qual, "needs": max(0, ch["min_trades"] - (r["trades"] or 0)), "updated": r["updated"], "prize": r["prize"] or ""}


def _entry_for_bridge(e, ch) -> dict:
    return {"id": e["id"], "challenge_id": ch["id"], "name": ch["name"], "nickname": e["nickname"], "balance": ch["balance"],
            "leverage": ch["leverage"], "daily_loss": ch["daily_loss"], "max_dd": ch["max_dd"], "min_trades": ch["min_trades"],
            "start": ch["start"], "end": ch["end"]}


@app.get("/api/challenge")
def challenge_board(authorization: Optional[str] = Header(default=None)):
    """Public: this month's challenge, the leaderboard (nicknames only) and last month's winners. With a login: your entry too."""
    if not CH["on"]:
        return {"on": False}
    ch = current_challenge()
    _, rows = _ranked(ch["id"])
    board, n = [], 0
    for r in rows[:200]:
        qual = (r["trades"] or 0) >= ch["min_trades"] and r["status"] not in ("failed", "disqualified")
        n += 1 if qual else 0
        board.append(_board_row(r, n, ch))
    prev = q("SELECT * FROM challenges WHERE id<? ORDER BY id DESC LIMIT 1", ch["id"], one=True)
    winners = []
    if prev:
        pch, prows = _ranked(prev["id"])
        k = 0
        for r in prows:
            if (r["trades"] or 0) >= pch["min_trades"] and r["status"] not in ("failed", "disqualified") and k < 3:
                k += 1; winners.append(_board_row(r, k, pch))
    me = None
    if authorization:
        try:
            u = user_from(authorization)
            e = q("SELECT * FROM challenge_entries WHERE challenge_id=? AND user_id=?", ch["id"], u["id"], one=True)
            if e:
                rank = next((b["rank"] for b in board if b["nickname"] == e["nickname"]), None)
                me = {**_entry_for_bridge(e, ch), "status": e["status"], "rank": rank, "return_pct": e["return_pct"] or 0,
                      "trades": e["trades"] or 0, "fail": e["fail"] or "", "linked": bool(e["login"]), "prize": e["prize"] or ""}
        except HTTPException:
            pass
    return {"on": True, "challenge": _ch_public(ch), "board": board, "previous": {"name": prev["name"], "winners": winners} if prev else None, "me": me}


class ChallengeJoinIn(BaseModel):
    nickname: str = Field(min_length=3, max_length=20)


@app.post("/api/challenge/join")
def challenge_join(b: ChallengeJoinIn, authorization: Optional[str] = Header(default=None)):
    u = user_from(authorization)
    if not CH["on"]:
        raise HTTPException(400, "There is no challenge running right now.")
    if not effective(u)["valid"]:
        raise HTTPException(402, "Start your free trial first: the challenge is free for every member.")
    ch = current_challenge()
    e = q("SELECT * FROM challenge_entries WHERE challenge_id=? AND user_id=?", ch["id"], u["id"], one=True)
    if e:
        return {"entry": _entry_for_bridge(e, ch)}
    nick = re.sub(r"[^\w .\-]", "", b.nickname).strip()
    if len(nick) < 3:
        raise HTTPException(400, "Pick a nickname of at least 3 letters or numbers.")
    if q("SELECT id FROM challenge_entries WHERE challenge_id=? AND lower(nickname)=?", ch["id"], nick.lower(), one=True):
        raise HTTPException(409, "That nickname is taken this month. Pick another one.")
    eid = secrets.token_hex(8)
    q("INSERT INTO challenge_entries(id,challenge_id,user_id,nickname,joined,status,equity,return_pct,trades) VALUES(?,?,?,?,?,?,?,?,?)",
      eid, ch["id"], u["id"], nick, time.time(), "active", ch["balance"], 0, 0)
    return {"entry": _entry_for_bridge(q("SELECT * FROM challenge_entries WHERE id=?", eid, one=True), ch)}


class ChallengePushIn(BaseModel):
    key: str = Field(default="", max_length=64)
    machine: str = Field(default="", max_length=80)
    stats: list = Field(default_factory=list, max_length=20)


@app.post("/api/challenge/push")
def challenge_push(b: ChallengePushIn, authorization: Optional[str] = Header(default=None)):
    """Results from a trader's bridge: sent by the bridge itself (license key) or relayed by the website (login)."""
    if authorization:
        u = user_from(authorization)
    else:
        u = q("SELECT * FROM users WHERE license=?", b.key.strip().upper(), one=True)
        if not u or b.machine not in json.loads(u["machines"] or "{}"):
            raise HTTPException(401, "Unknown license or PC.")
    n = 0
    for s in b.stats[:20]:
        if not isinstance(s, dict):
            continue
        e = q("SELECT * FROM challenge_entries WHERE id=? AND user_id=?", str(s.get("entry", ""))[:32], u["id"], one=True)
        if not e or e["status"] == "disqualified":
            continue
        ch = q("SELECT * FROM challenges WHERE id=?", e["challenge_id"], one=True)
        if not ch or time.time() > ch["end"] + 3 * 86400:  # results are frozen 3 days after the end
            continue
        try:
            ret = max(-100.0, min(100000.0, float(s.get("return_pct") or 0)))
            st = s.get("status") if s.get("status") in ("active", "failed", "finished") else "active"
            if e["status"] == "failed":
                st = "failed"  # a failed challenge never comes back
            q("""UPDATE challenge_entries SET login=?, status=?, fail=?, equity=?, return_pct=?, trades=?, wins=?, win_rate=?,
                 profit_factor=?, best_trade=?, max_dd_pct=?, updated=? WHERE id=?""",
              int(s.get("login") or 0), st, str(s.get("fail") or "")[:200], float(s.get("equity") or 0), round(ret, 2),
              int(s.get("trades") or 0), int(s.get("wins") or 0), float(s.get("win_rate") or 0), float(s.get("profit_factor") or 0),
              float(s.get("best_trade") or 0), float(s.get("max_dd_pct") or 0), time.time(), e["id"])
            n += 1
        except (TypeError, ValueError):
            continue
    return {"ok": True, "updated": n}


@app.get("/api/admin/challenge")
def admin_challenge(id: str = "", x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Every entry with email and full stats, for checking and awarding."""
    _admin_check(x_admin_token, authorization)
    ch = q("SELECT * FROM challenges WHERE id=?", id, one=True) if id else current_challenge()
    if not ch:
        raise HTTPException(404, "No such challenge.")
    _, rows = _ranked(ch["id"])
    out, n = [], 0
    for r in rows:
        qual = (r["trades"] or 0) >= ch["min_trades"] and r["status"] not in ("failed", "disqualified")
        n += 1 if qual else 0
        u = q("SELECT email FROM users WHERE id=?", r["user_id"], one=True)
        out.append({**dict(r), "email": u["email"] if u else "(deleted)", "rank": n if qual else None, "qualified": qual})
    return {"challenge": _ch_public(ch), "entries": out, "all": [dict(x) for x in q("SELECT id,name,start,end FROM challenges ORDER BY id DESC")]}


class ChallengeActIn(BaseModel):
    entry: str = Field(max_length=32)
    action: str = Field(max_length=20)
    text: str = Field(default="", max_length=200)
    days: int = Field(default=0, ge=0, le=3650)


@app.post("/api/admin/challenge/action")
def admin_challenge_action(b: ChallengeActIn, x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    _admin_check(x_admin_token, authorization)
    who = _admin_who(x_admin_token, authorization)
    e = q("SELECT * FROM challenge_entries WHERE id=?", b.entry, one=True)
    if not e:
        raise HTTPException(404, "Entry not found.")
    u = q("SELECT * FROM users WHERE id=?", e["user_id"], one=True)
    email = u["email"] if u else "(deleted)"
    if b.action == "disqualify":
        q("UPDATE challenge_entries SET status='disqualified', fail=? WHERE id=?", b.text or "Disqualified by the organiser", e["id"])
        msg = "Disqualified."
    elif b.action == "reinstate":
        q("UPDATE challenge_entries SET status='active', fail='' WHERE id=?", e["id"])
        msg = "Back in the challenge."
    elif b.action == "award":  # give the prize: days of Pro on top of what they have
        if not u:
            raise HTTPException(400, "This account no longer exists.")
        days = b.days or 365
        base = LIFETIME_END if u["interval"] == "lifetime" else max(u["period_end"] or 0, time.time())
        if u["interval"] != "lifetime":
            q("UPDATE users SET plan='pro', status='active', period_end=?, cancel_at_end=0 WHERE id=?", base + days * 86400, u["id"])
        q("UPDATE challenge_entries SET prize=? WHERE id=?", b.text or f"{days} days of Pro", e["id"])
        msg = f"Prize given: {b.text or f'{days} days of Pro'} added to {email}."
    else:
        raise HTTPException(400, "Unknown action.")
    _log(who, "challenge_" + b.action, email, f"{e['challenge_id']} · {e['nickname']} {b.text}".strip())
    return {"ok": True, "message": msg}


@app.get("/api/admin/leads")
def admin_leads(x_admin_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None), token: str = ""):
    """CSV of everyone who asked for the Risk Rulebook: import it into MailerLite / Brevo for the follow-up emails."""
    _admin_check(x_admin_token or token or None, authorization)
    import csv
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["email", "created", "source", "is_customer", "data"])
    for r in q("SELECT l.*, (SELECT COUNT(*) FROM users u WHERE u.email=l.email) c FROM leads l ORDER BY created DESC"):
        w.writerow([r["email"], time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created"])), r["source"], "yes" if r["c"] else "no", r["data"]])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=goldmine-leads.csv"})


# ------------------------------------------------------------------ web app files
WEB_FILES = {"terminal_test.html", "rulebook.html", "terminal_pro.html", "journal.html", "gp-shared.js", "gp-theme.css"}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".jpg": "image/jpeg"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(SITE / "terminal_test.html", media_type=MIME[".html"], headers={"Cache-Control": "no-cache"})


@app.get("/{fname}", include_in_schema=False)
def web_file(fname: str):
    p = SITE / fname
    if (fname in WEB_FILES or re.fullmatch(r"lp-[a-z0-9-]+\.jpg", fname)) and p.is_file():  # lp-*.jpg = landing page screenshots
        return FileResponse(p, media_type=MIME.get(p.suffix), headers={"Cache-Control": "no-cache"})
    raise HTTPException(404, "Not found.")


print(f"License server ready - {'TEST MODE (no Stripe keys: payments are simulated)' if TEST_MODE else 'Stripe LIVE/TEST keys loaded'}")
print(f"Public key for bridges: {PUBKEY_HEX}")
