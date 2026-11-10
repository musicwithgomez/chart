"""Goldmine Pro self-test: plays a whole customer journey end-to-end, in about 30 seconds.

- starts a temporary license server (own empty database, test mode, nothing touches your real data)
- signs up a customer, downloads their bridge zip, unzips it and starts THAT bridge exactly like a customer would
  (with a fake MetaTrader 5, so no terminal, broker or money is involved)
- walks through trial -> paid -> payment failed -> cancelled -> ended, forged licenses, extra PCs, Stripe webhooks
- finally checks that YOUR own bridge (port 8765) still runs in owner mode

Run:  saas\\TEST_EVERYTHING.bat     (or: python saas/selftest/selftest.py from the backend folder)
"""
import hashlib, hmac, io, json, os, socket, sys, tempfile, threading, time, urllib.request, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
TMP = Path(tempfile.mkdtemp(prefix="goldmine_selftest_"))
os.environ.update(GP_DATA_DIR=str(TMP / "server"), GP_ADMIN_TOKEN="selftest-admin", GP_PUBLIC_URL="")
for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
    os.environ.pop(k, None)
(TMP / "server").mkdir()

RESULTS = []


def check(cond, msg):
    RESULTS.append((bool(cond), msg))
    print(("  ✔ " if cond else "  ✘ ") + msg, flush=True)
    return cond


def section(t):
    print("\n" + t, flush=True)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


# ------------------------------------------------------------------ 1. temporary license server
sys.path.insert(0, str(BACKEND))
from saas import license_server as ls  # noqa: E402
import uvicorn  # noqa: E402

PORT = free_port()
SRV = f"http://127.0.0.1:{PORT}"
server = uvicorn.Server(uvicorn.Config(ls.app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(.05)


def call(path, body=None, token=None, admin=None, raw=False):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    if admin: h["X-Admin-Token"] = admin
    req = urllib.request.Request(SRV + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = r.read(); return r.status, (d if raw else json.loads(d))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print(f"Goldmine Pro self-test · temporary server {SRV} · temp folder {TMP}")

section("1. A new customer signs up")
s, j = call("/api/plans")
check(s == 200 and j["test_mode"], "website + license server answer (test mode, no real payments)")
s, j = call("/api/auth/signup", {"email": "customer@example.com", "password": "testpass123"})
check(s == 200 and j["sub"]["status"] == "trialing" and j["sub"]["days_left"] == ls.TRIAL_DAYS,
      f"sign-up with only email + password -> {ls.TRIAL_DAYS}-day Pro trial, no card")
TOK, KEY, BKEY = j["token"], j["license"], j["bridge_key"]
check(BKEY and len(BKEY) == 48, "a private random bridge key was created for this customer")

section("1b. Launch offer: free lead magnet, founding seats, referrals")
s, j = call("/api/plans")
check(j["plans"]["pro"]["month"] == 29 and j["founding"]["month"] == 19 and j["founding"]["open"] and j["guarantee_days"] == 30,
      f"Pro EUR 29/mo · founding EUR 19/mo open · {j['founding']['left']} seats left · 30-day guarantee")
s, _ = call("/api/leads", {"email": "lead@example.com", "source": "calculator", "data": {"risk": "1"}})
check(s == 200, "free Risk Rulebook: email saved as a lead (no account needed)")
check(call("/api/leads", {"email": "not-an-email"})[0] in (400, 422), "an invalid email is refused")
s, me1 = call("/api/me", token=TOK)
REF = me1["ref"]["code"]
check(len(REF) == 6, f"every customer gets a referral code ({REF})")
s, j2 = call("/api/auth/signup", {"email": "friend@example.com", "password": "testpass123", "ref": REF})
check(s == 200 and j2["sub"]["status"] == "trialing" and j2["sub"]["days_left"] == ls.REF_TRIAL_DAYS, f"a friend signs up with that referral link -> {ls.REF_TRIAL_DAYS}-day free trial (instead of {ls.TRIAL_DAYS})")
check(call("/api/me", token=TOK)[1]["ref"]["rewarded"] == 0, "the referrer gets nothing yet for a sign-up (only when the friend pays)")
before = call("/api/me", token=TOK)[1]["sub"]["period_end"]
s, j3 = call("/api/billing/checkout", {"plan": "pro", "interval": "month"}, token=j2["token"])
check(j3["founding"] and j3["payments"][0]["amount"] == 19, "the friend buys Pro at the founding price (EUR 19) and becomes a founding member")
after = call("/api/me", token=TOK)[1]
check(after["sub"]["period_end"] - before >= 29 * 86400 and after["ref"]["rewarded"] == 1, "the friend pays -> the referrer gets 30 extra days")
check(call("/api/plans")[1]["founding"]["left"] == j["founding"]["left"] - 1, "one founding seat fewer on the website")
s, ja = call("/api/billing/addon", {"addon": "vps"}, token=j2["token"])
check(s == 200 and "vps" in ja["addons"] and any("VPS" in p["descr"] for p in ja["payments"]), "upsell after buying: VPS setup add-on bought (EUR 49 payment recorded)")
check(call("/api/billing/addon", {"addon": "vps"}, token=j2["token"])[0] == 400, "the same add-on can't be bought twice")
end0 = ja["sub"]["period_end"]
s, jp = call("/api/billing/pause", {}, token=j2["token"])
check(s == 200 and jp["paused_until"] > time.time() and jp["sub"]["period_end"] - end0 >= 29 * 86400 and jp["sub"]["valid"],
      "downsell instead of cancelling: pause 1 month (access kept, billing moved 30 days)")
s, j4 = call("/api/auth/signup", {"email": "lifeplan@example.com", "password": "testpass123"})
s, j5 = call("/api/billing/checkout", {"plan": "pro", "interval": "lifetime3"}, token=j4["token"])
check(s == 200 and j5["sub"]["interval"] == "lifetime" and sum(1 for p in j5["payments"] if "instalment" in p["descr"]) == 3,
      "payment-plan downsell: Lifetime in 3 x EUR 179 -> lifetime access")
check(call("/api/billing/pause", {}, token=j4["token"])[0] == 400, "a lifetime plan can't be paused")

section("2. The customer downloads the bridge")
s, z = call(f"/api/download/bridge?site={SRV}", token=TOK, raw=True)
zf = zipfile.ZipFile(io.BytesIO(z))
names = [n.split("/", 1)[1] for n in zf.namelist()]
cfg = json.loads(zf.read("GoldmineBridge/config.json"))
lic = json.loads(zf.read("GoldmineBridge/license.json"))
check("START GOLDMINE.bat" in names and "run_bridge.py" in names and not any(n.endswith(".ps1") for n in names), "zip has one thing to double-click: START GOLDMINE.bat (no PowerShell script for Windows to flag)")
check(cfg["api_key"] == BKEY and cfg["terminals"] == [] and cfg["live_trading"] is False,
      "config.json is ready: random api_key filled in, accounts detected automatically, demo-only by default")
check("config.example.json" not in names, "no example file to copy or edit")
check(lic["key"] == KEY and lic["site"] == SRV, "license.json has the license key and the website address")
secret = [n for n in names if any(x in n for x in ("accounts_extra", "paper_accounts", "copy_settings", "private", "saas", "secret", ".db"))]
check(not secret, "zip contains none of YOUR files or secrets")
CUST = TMP / "customer_pc"
zf.extractall(CUST)
CUST = CUST / "GoldmineBridge"

section("3. Customer opens MT5 and double-clicks START GOLDMINE (bridge starts)")
sys.path.insert(0, str(CUST))
sys.path.insert(0, str(HERE / "fakemt5"))
import MetaTrader5 as fake  # noqa: E402
os.environ["GOMEZ_PAPER_FILE"] = str(CUST / "paper_accounts.json")
os.environ["GOMEZ_ACCOUNTS_FILE"] = str(CUST / "accounts_extra.json")
import app  # noqa: E402  (the bridge from the customer's zip, not your own)
check(Path(app.__file__).resolve().parent == CUST.resolve(), "running the bridge code from the customer's zip")
from fastapi.testclient import TestClient  # noqa: E402
c = TestClient(app.app)
H = {"X-Gomez-Key": BKEY, "Origin": SRV}
check(app.TERMINALS and app.TERMINALS[0]["login"] == 51234567, "logged-in MT5 account detected automatically (no login/password typed)")
for _ in range(60):
    if app.LICENSE.ok():
        break
    time.sleep(.2)
check(app.LICENSE.ok(), "license activated by itself on first start (no button, no key to paste)")

section("4. The website connects to the bridge by itself")
r = c.get("/api/health", headers=H)
check(r.status_code == 200 and r.headers.get("access-control-allow-origin"), "website reaches the bridge with the stored key (CORS ok)")
acc = c.get("/api/accounts", headers=H).json()["accounts"]
check(any(a["login"] == 51234567 for a in acc), f"account list: {acc[0]['name']} ({acc[0]['login']})")
live = c.get("/api/live?login=51234567&symbol=XAUUSD&timeframe=M5", headers=H)
check(live.status_code == 200 and live.json()["bid"] == 2650.0, "live prices and the account come through (terminal works)")
check(c.get("/api/accounts", headers={"X-Gomez-Key": "wrong" * 10}).status_code == 401, "a wrong key is refused (other websites can't use the bridge)")
app.TERMINALS.clear(); fake.S["logged_in"] = False
check(c.get("/api/accounts", headers=H).json()["accounts"] == [], "MT5 not logged in yet -> bridge keeps running and waits")
fake.S["logged_in"] = True
check(any(a["login"] == 51234567 for a in c.get("/api/accounts", headers=H).json()["accounts"]), "customer logs in to MT5 later -> picked up automatically")

section("5. Subscription life cycle (what the bridge does in every situation)")
for state, want in (("trial_last_day", 200), ("trial_expired", 402), ("active", 200), ("past_due", 200),
                    ("canceling", 200), ("ended", 402), ("active", 200)):
    call("/api/test/state", {"state": state}, token=TOK); app.LICENSE.refresh()
    r = c.get("/api/accounts", headers=H)
    check(r.status_code == want, f"{state:<15} -> terminal {'works' if want == 200 else 'locked: ' + r.json().get('detail', '')[21:70]}")
s, j = call("/api/billing/checkout", {"plan": "starter", "interval": "month"}, token=TOK); app.LICENSE.refresh()
check(c.post("/api/paper/create", json={"name": "x", "balance": 1000}, headers=H).status_code == 402, "Starter plan: Pro-only features are locked")
s, j = call("/api/billing/checkout", {"plan": "pro", "interval": "year"}, token=TOK); app.LICENSE.refresh()
check(app.LICENSE.ok() and j["payments"], f"upgrade to Pro yearly (test payment {j['payments'][0]['amount']} EUR) -> unlocked")
call("/api/billing/checkout", {"plan": "starter", "interval": "month"}, token=TOK); app.LICENSE.refresh()
call("/api/billing/checkout", {"plan": "elite", "interval": "month"}, token=TOK)  # upgrade on the website; the bridge is NOT told
app.LICENSE.checked = 0  # (its last check was "long ago")
r = c.post("/api/paper/create", json={"name": "after upgrade", "balance": 1000}, headers=H)
check(r.status_code == 200 and app.LICENSE.data.get("plan") == "elite", "upgrade to Elite -> paper accounts work on the bridge right away (it re-checks before refusing)")
check(c.post("/api/license/refresh", headers=H).status_code == 200 and c.post("/api/license/refresh", headers={"X-Gomez-Key": "x" * 48}).status_code == 401,
      "the website can tell the bridge to re-check the plan (with the private key only)")
call("/api/billing/checkout", {"plan": "pro", "interval": "year"}, token=TOK); app.LICENSE.refresh()  # back to Pro for the next checks

section("5b. Price targets on a real MT5 account (kept by the bridge, page closed)")
LG = 51234567
def tgt(**b):
    return c.post("/api/targets/add", json={"login": LG, **b}, headers=H)
fake.POS[:] = [dict(ticket=1, symbol="XAUUSD", type=0, volume=0.10, price_open=2640.0, sl=0.0, tp=0.0),
               dict(ticket=2, symbol="XAUUSD", type=0, volume=0.10, price_open=2645.0, sl=0.0, tp=0.0),
               dict(ticket=3, symbol="XAUUSD", type=0, volume=0.20, price_open=2648.0, sl=0.0, tp=0.0)]
fake.S["price"] = 2650.0
r = tgt(kind="partial", ticket=2, price=2660, percent=50, be=True)
check(r.status_code == 200 and r.json()["target"]["dir"] == 1, "partial TP: close 50% of #2 at 2660 saved in the bridge")
check(tgt(kind="partial", ticket=2, price=2650, percent=50).status_code == 400, "a target at the current price is refused")
time.sleep(1.3)
check(fake.POS[1]["volume"] == 0.10, "price 2650: nothing happens yet")
fake.S["price"] = 2661.0
for _ in range(30):
    if fake.POS[1]["volume"] < 0.1:
        break
    time.sleep(.2)
check(abs(fake.POS[1]["volume"] - 0.05) < 1e-9, "price 2661 -> background checker closed 0.05 of #2 (no page needed)")
check(fake.POS[1]["sl"] == 2645.0, "runner #2 stop moved to break-even")
lv = c.get(f"/api/live?login={LG}&symbol=XAUUSD&timeframe=M5", headers=H).json()
check(lv["targets"] == [] and any("Partial TP" in e["text"] for e in lv["events"]), "app gets the message: " + lv["events"][-1]["text"])
r = tgt(kind="keepbest", symbol="XAUUSD", side="BUY", price=2670, percent=60)
check(r.status_code == 200, "keep-best BUY at 2670 (close 60% of the best entry) saved")
fake.S["price"] = 2671.0
lv = c.get(f"/api/live?login={LG}&symbol=XAUUSD&timeframe=M5", headers=H).json()
left = [p for p in fake.POS if p["volume"] > 1e-9]
check(len(left) == 1 and left[0]["ticket"] == 1 and abs(left[0]["volume"] - 0.04) < 1e-9,
      "price 2671 -> #2 and #3 closed, best entry #1 (2640) kept with a 0.04 runner")
fake.POS[:] = [dict(ticket=7, symbol="XAUUSD", type=1, volume=0.10, price_open=2672.0, sl=0.0, tp=0.0),
               dict(ticket=8, symbol="XAUUSD", type=1, volume=0.10, price_open=2675.0, sl=0.0, tp=0.0)]
r = tgt(kind="keepbest", symbol="XAUUSD", side="SELL", price=0, percent=0)
left = [p for p in fake.POS if p["volume"] > 1e-9]
check(r.status_code == 200 and [p["ticket"] for p in left] == [8], "keep-best SELL now: best entry (highest, 2675) kept, other closed -> " + r.json()["message"])
r = tgt(kind="partial", ticket=8, price=2600, percent=50)
tid = r.json()["target"]["id"]
check(c.post("/api/targets/remove", json={"login": LG, "id": tid}, headers=H).status_code == 200, "target removed again with ×")
fake.POS.clear()

section("5c. Monthly challenge (same paper account + rules for everyone, leaderboard)")
fake.S["price"] = 2650.0
s, j = call("/api/challenge")
check(s == 200 and j["on"] and j["challenge"]["balance"] == 10000 and len(j["challenge"]["prizes"]) == 3,
      f"public challenge page: {j['challenge']['name']}, $10,000, prizes for top 3")
s, j = call("/api/challenge/join", {"nickname": "GoldKing"}, token=TOK)
ENT = j.get("entry") or {}
check(s == 200 and ENT.get("id") and ENT["daily_loss"] == 5 and ENT["max_dd"] == 10, "member joins with a nickname -> gets the challenge entry")
check(call("/api/challenge/join", {"nickname": "GoldKing"}, token=TOK)[1]["entry"]["id"] == ENT["id"], "joining twice gives the same entry")
_, rj = call("/api/auth/signup", {"email": "rival@example.com", "password": "testpass123"})
check(call("/api/challenge/join", {"nickname": "goldking"}, token=rj["token"])[0] == 409, "a nickname can only be used once per month")
r = c.post("/api/challenge/start", json={"entry": ENT}, headers=H)
CL = r.json()["account"]["login"] if r.status_code == 200 else 0
check(r.status_code == 200 and c.post("/api/challenge/start", json={"entry": ENT}, headers=H).json()["account"]["login"] == CL,
      "the bridge makes the challenge paper account once (same account when started again)")
check(any(a["login"] == CL and a.get("challenge") for a in c.get("/api/accounts", headers=H).json()["accounts"]), "challenge account shows in the account list (flagged)")
check(c.post("/api/paper/deposit", json={"login": CL, "amount": 5000}, headers=H).status_code == 400 and
      c.post("/api/paper/reset", json={"login": CL}, headers=H).status_code == 400 and
      c.post("/api/paper/leverage", json={"login": CL, "leverage": 500}, headers=H).status_code == 400 and
      c.post("/api/paper/delete", json={"login": CL}, headers=H).status_code == 400,
      "no deposits, resets, leverage changes or deleting during the challenge")
r = c.post("/api/order", json={"login": CL, "symbol": "XAUUSD", "side": "buy", "volume": 0.2}, headers=H)
check(r.status_code == 200, "trade on the challenge account (buy 0.2 XAUUSD at 2650)")
fake.S["price"] = 2700.0
st = c.get(f"/api/challenge/stats?entry={ENT['id']}", headers=H).json()["stats"][0]
check(st["status"] == "active" and abs(st["return_pct"] - 10) < 0.5, f"price 2700 -> challenge return {st['return_pct']}%")
app.challenge_push()
_, j = call("/api/challenge", token=TOK)
row = next((b for b in j["board"] if b["nickname"] == "GoldKing"), None)
check(row and abs(row["return_pct"] - 10) < 0.5 and row["rank"] is None and row["needs"] == 15 and j["me"]["linked"],
      "bridge sends results -> leaderboard shows GoldKing +10% (not ranked yet: needs 15 closed trades)")
check(call("/api/challenge/push", {"key": KEY, "machine": "someone-else", "stats": [st]})[0] == 401, "results from an unknown PC are refused")
fake.S["price"] = 2620.0
st = c.get(f"/api/challenge/stats?entry={ENT['id']}", headers=H).json()["stats"][0]
check(st["status"] == "failed" and "daily loss" in st["fail"] and st["open_positions"] == 0, "equity below the 5% daily loss limit -> challenge failed, trades closed")
check(c.post("/api/order", json={"login": CL, "symbol": "XAUUSD", "side": "buy", "volume": 0.1}, headers=H).status_code == 400, "a failed challenge account can't trade anymore")
app.challenge_push()
_, j = call("/api/challenge")
check(next(b for b in j["board"] if b["nickname"] == "GoldKing")["status"] == "failed", "leaderboard shows the failed entry (listed last)")
_, j = call("/api/admin/challenge", admin="selftest-admin")
ae = next(e for e in j["entries"] if e["nickname"] == "GoldKing")
check(ae["email"] == "customer@example.com" and ae["login"] == CL, "admin sees every entry with email, account and stats")
before = ls.q("SELECT period_end FROM users WHERE email=?", "customer@example.com", one=True)["period_end"]
s, j = call("/api/admin/challenge/action", {"entry": ae["id"], "action": "award", "days": 365, "text": "1st place: Pro for 12 months"}, admin="selftest-admin")
after = ls.q("SELECT period_end FROM users WHERE email=?", "customer@example.com", one=True)["period_end"]
check(s == 200 and abs((after - before) / 86400 - 365) < 1, "admin gives the prize: +365 days of Pro on the winner's account")
check(call("/api/admin/challenge/action", {"entry": ae["id"], "action": "disqualify", "text": "test"}, admin="selftest-admin")[0] == 200 and
      call("/api/admin/challenge/action", {"entry": ae["id"], "action": "reinstate"}, admin="selftest-admin")[0] == 200, "admin can disqualify and reinstate")
fake.S["price"] = 2650.0

section("6. Security")
import gp_sign  # noqa: E402
cache = json.loads((CUST / "license_cache.json").read_text())
body = json.loads(gp_sign.unb64(cache["token"].split(".")[0])); body.update(plan="elite", exp=9999999999)
forged = gp_sign.b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()) + "." + cache["token"].split(".")[1]
try:
    gp_sign.verify_token(forged, bytes.fromhex(lic["pubkey"])); check(False, "forged license rejected")
except ValueError:
    check(True, "an edited / forged license answer is rejected (digital signature)")
from licensing import License  # noqa: E402
for i, want in ((2, True), (3, False)):
    L = License(Path(tempfile.mkdtemp()), {"license": {"server": "http://127.0.0.1:9", "pubkey": lic["pubkey"], "key": KEY}})
    L.server, L.machine, L.data = SRV, f"pc{i}" + "x" * 29, {}; L.refresh()
    check(L.ok() == want, f"PC #{i} on one Pro license -> {'allowed' if want else 'refused (max 2 PCs)'}")
L = License(Path(tempfile.mkdtemp()), {"license": {"server": "http://127.0.0.1:9", "pubkey": lic["pubkey"], "key": KEY}})
L.data, L.machine = app.LICENSE.data, app.LICENSE.machine; L.refresh()
check(L.ok(), "license server offline -> customers keep trading (72 h grace)")
check(call("/api/admin/overview")[0] == 401, "admin numbers need your admin token")
ls.STRIPE_WHSEC = "whsec_selftest"
uid = ls.q("SELECT id FROM users WHERE email=?", "customer@example.com", one=True)["id"]
ev = json.dumps({"id": "evt_selftest", "type": "invoice.payment_failed", "data": {"object": {"customer": "cus_none"}}}).encode()
t = int(time.time()); sig = hmac.new(b"whsec_selftest", f"{t}.".encode() + ev, hashlib.sha256).hexdigest()
check(call("/api/billing/webhook", {"id": "evt_fake", "type": "invoice.paid", "data": {"object": {}}})[0] == 400, "fake Stripe webhook (no valid signature) refused")
import urllib.request as U  # noqa: E402
req = U.Request(SRV + "/api/billing/webhook", data=ev, headers={"stripe-signature": f"t={t},v1={sig}", "Content-Type": "application/json"})
check(json.loads(U.urlopen(req).read()).get("ok"), "real (signed) Stripe webhook accepted")
r = call("/api/license/rotate", {}, token=TOK)[1]; app.LICENSE.refresh()
check(not app.LICENSE.ok(), "customer generates a new key -> the old key stops working at once")
s, j = call("/api/admin/overview", admin="selftest-admin")
check(s == 200 and j["customers"], f"admin overview works: MRR {j['mrr']} EUR, {len(j['customers'])} customer(s)")
s, own = call("/api/auth/signup", {"email": "owner@example.com", "password": "selftest-pass-123"})
OWN = own["token"]
check(call("/api/admin/overview", token=OWN)[0] == 401, "a normal logged-in account cannot open Admin")
check(call("/api/admin/claim", {}, token=OWN)[0] == 401 and call("/api/admin/claim", {}, token=OWN, admin="wrong")[0] == 401, "making an account admin needs the admin token")
s, j = call("/api/admin/claim", {}, token=OWN, admin="selftest-admin")
check(s == 200 and call("/api/me", token=OWN)[1]["user"]["is_admin"], "owner logged in + token once -> account marked admin")
check(call("/api/admin/overview", token=OWN)[0] == 200, "admin account opens Admin with just its login (no token) -> works on every device")
call("/api/auth/signup", {"email": "delete-me@example.com", "password": "selftest-pass-123"})
check(call("/api/admin/delete_user", {"email": "delete-me@example.com"})[0] == 401, "deleting a user needs your admin token")
s, j = call("/api/admin/delete_user", {"email": "delete-me@example.com"}, admin="selftest-admin")
check(s == 200 and not ls.q("SELECT id FROM users WHERE email=?", "delete-me@example.com", one=True), "admin deletes a user -> account and license gone")
def act(**b):
    return call("/api/admin/action", b, token=OWN)
check(call("/api/admin/action", {"email": "x@example.com", "action": "note"})[0] == 401, "admin actions need admin rights")
s, jc = act(email="manual@example.com", action="create", plan="pro", days=30)
check(s == 200 and jc["password"], "admin adds a customer by hand (Pro, 30 days, temporary password)")
TOKM = call("/api/auth/login", {"email": "manual@example.com", "password": jc["password"]})[1]["token"]
s, _ = act(email="manual@example.com", action="set_plan", plan="elite", interval="year")
check(s == 200 and call("/api/me", token=TOKM)[1]["sub"]["plan"] == "elite", "manual upgrade to Elite yearly")
end1 = call("/api/me", token=TOKM)[1]["sub"]["period_end"]
act(email="manual@example.com", action="extend", days=14)
check(call("/api/me", token=TOKM)[1]["sub"]["period_end"] - end1 >= 13.9 * 86400, "give 14 extra days")
act(email="manual@example.com", action="payment", amount=59, text="Bank transfer")
act(email="manual@example.com", action="note", text="Paid by bank, VIP")
s, ju = call("/api/admin/user?email=manual@example.com", token=OWN)
check(s == 200 and ju["notes_text"] == "Paid by bank, VIP" and any(p["amount"] == 59 for p in ju["payments"]) and len(ju["log"]) >= 4,
      "customer details: note, manual payment and the audit log are kept")
lic = ju["license"]
s, _ = act(email="manual@example.com", action="ban", text="chargeback")
vr = call("/api/license/verify", {"key": lic, "machine": "pc-ban-test-1"})[1]
check(s == 200 and call("/api/auth/login", {"email": "manual@example.com", "password": jc["password"]})[0] == 403 and not vr["valid"]
      and call("/api/me", token=TOKM)[0] == 403, "blocked: no login, session refused, bridge license invalid")
check(act(email="owner@example.com", action="ban")[0] == 400, "you can't block yourself")
act(email="manual@example.com", action="unban")
check(call("/api/auth/login", {"email": "manual@example.com", "password": jc["password"]})[0] == 200, "unblocked: can log in again")
s, jr = act(email="manual@example.com", action="reset_password")
check(s == 200 and call("/api/auth/login", {"email": "manual@example.com", "password": jr["password"]})[0] == 200, "reset password -> new temporary password works")
s, csvd = call("/api/admin/customers.csv", token=OWN, raw=True)
check(s == 200 and b"manual@example.com" in csvd, "customers CSV export")
check(call("/api/admin/activity", token=OWN)[0] == 200, "payments, leads and audit log for the Admin tabs")

section("7. Your own bridge")
try:
    own = json.loads((BACKEND / "config.json").read_text(encoding="utf-8-sig"))
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/health", headers={"X-Gomez-Key": own["api_key"]}), timeout=3) as r:
        h = json.loads(r.read())
    lic_own = h.get("license")
    if lic_own is None:
        print("  • your bridge is running an older app.py: restart start_bridge.bat to load the new version")
    else:
        check(not lic_own["enabled"], "your own bridge runs in owner mode (no license needed, never locked)")
except urllib.error.HTTPError as e:
    print(f"  • something answers on port 8765 but refuses the api_key in your config.json (HTTP {e.code}).")
    print("    Close the bridge window and start start_bridge.bat again so it loads the current config.json and app.py.")
except Exception:
    print("  • your own bridge is not running right now (start_bridge.bat) - skipped, that's fine")
check(not (BACKEND / "license.json").exists(), "no license.json in your own folder (owner mode stays on)")

ok = all(r[0] for r in RESULTS)
print("\n" + ("=" * 60))
print(f"{'ALL GOOD' if ok else 'PROBLEMS FOUND'}: {sum(r[0] for r in RESULTS)}/{len(RESULTS)} checks passed")
if not ok:
    for good, m in RESULTS:
        if not good:
            print("  FAILED:", m)
print("=" * 60, flush=True)
server.should_exit = True
os._exit(0 if ok else 1)
