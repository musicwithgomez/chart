"""Goldmine paper accounts: unlimited demo accounts created from the app.

They use LIVE prices from your MT5 terminal, but every order is simulated
inside the bridge: nothing is ever sent to the broker. Accounts are saved in
paper_accounts.json next to app.py, so they survive a restart.

Market orders fill at the live bid / ask. Pending orders, stop loss and take
profit are checked every second by a background thread (and on every page
update), using the live price at that moment.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

PAPER_BASE = 90_000_001          # paper logins start here (never a real MT5 login)
NAMES = {2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP", 5: "SELL STOP"}
MAX_LOT = 1.0                    # same per-order cap as the real bridge
HIST_MAX = 200_000               # ticks replayed per check when catching up after a gap
LEVERAGES = (30, 50, 100, 200, 500, 1000, 2000, 5000, 10000)  # paper account leverage choices (1:x)
STOP_OUT = 20.0     # margin level (equity / margin, %) at which the biggest losing position is closed, like a broker



try:
    import numpy as np  # comes with the MetaTrader5 package
except Exception:  # pragma: no cover
    np = None


def tick_history(mt5, sym, frm, upto):
    """Ticks after `frm` up to `upto` (MT5 tick time in ms), so moves that happened while the bridge was not
    looking (PC locked / asleep, MT5 busy on another account) are not missed.
    Returns (times, bids, asks, complete) or None when MT5 gives no tick history."""
    if np is None or upto <= frm or not hasattr(mt5, "copy_ticks_from"):
        return None
    try:
        from datetime import datetime, timezone
        r = mt5.copy_ticks_from(sym, datetime.fromtimestamp(frm // 1000, tz=timezone.utc), HIST_MAX,
                                getattr(mt5, "COPY_TICKS_ALL", -1))
        if r is None or len(r) == 0:
            return None
        tm = np.asarray(r["time_msc"], dtype="int64")
        bid, ask = np.asarray(r["bid"], dtype=float), np.asarray(r["ask"], dtype=float)
        keep = (tm > frm) & (tm <= upto) & (bid > 0) & (ask >= bid)
        return tm[keep], bid[keep], ask[keep], len(r) < HIST_MAX
    except Exception as e:
        print(f"[paper] tick history {sym}: {e}", flush=True)
        return None


def first_true(mask):
    """Index of the first True, or -1."""
    if not len(mask):
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def fmt_px(px, si):
    return f"{px:.{int(getattr(si, 'digits', 2) or 2)}f}"


def new_target(kind, symbol, side, price, ref, percent, ticket=0, be=False, chk=0):
    """A price target: 'partial' closes `percent` % of position `ticket` when the price reaches `price`;
    'keepbest' closes every `side` position on `symbol` except the best entry and closes `percent` % of that one.
    `ref` = the current close price for that side: the target fires when the price crosses from there to `price`."""
    if price == ref:
        raise PaperError(400, "That price is the current price: pick a level above or below it.")
    return {"id": int(time.time() * 1000) % 10**12 + int(price * 100) % 1000, "kind": kind, "symbol": symbol,
            "side": side, "price": float(price), "dir": 1 if price > ref else -1, "percent": float(percent),
            "ticket": int(ticket or 0), "be": bool(be), "created": int(time.time()), "chk": int(chk)}


def public_target(g):
    return {k: g[k] for k in ("id", "kind", "symbol", "side", "price", "dir", "percent", "ticket", "be", "created")}


class PaperError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


class Paper:
    def __init__(self, path, mt5, lock, feed):
        """mt5: the MetaTrader5 module. lock: the bridge's MT5 lock.
        feed(login): makes sure MT5 is connected to the broker of `login`
        (called with the lock held) and returns mt5.account_info()."""
        self.path = Path(path)
        self.mt5 = mt5
        self.lock = lock
        self.feed = feed
        self.dlock = threading.RLock()
        self.data = self._load()
        self.quotes = {}  # (server, symbol) -> newest trusted tick (see _quote)
        self._dirty = False
        self._last_save = 0.0
        threading.Thread(target=self._loop, name="paper", daemon=True).start()

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            d.setdefault("accounts", {})
            d.setdefault("next", PAPER_BASE)
            return d
        except Exception:
            return {"accounts": {}, "next": PAPER_BASE}

    def save(self):
        with self.dlock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save = time.time()

    # ------------------------------------------------------------ accounts
    def is_paper(self, login) -> bool:
        try:
            return str(int(login)) in self.data["accounts"]
        except (TypeError, ValueError):
            return False

    def acc(self, login):
        a = self.data["accounts"].get(str(int(login)))
        if not a:
            raise PaperError(404, "Paper account not found.")
        return a

    def feed_of(self, login) -> int:
        return int(self.acc(login)["feed"])

    def list(self):
        return [
            {"login": a["login"], "name": a["name"], "server": "Goldmine Paper",
             "is_demo": True, "paper": True, "leverage": int(a.get("leverage") or 100),
             "challenge": bool(a.get("challenge")), "challenge_status": (a.get("challenge") or {}).get("status")}
            for a in self.data["accounts"].values()
        ]

    def create(self, name: str, balance: float, feed_login: int, leverage: int = 500):
        if not (100 <= balance <= 10_000_000):
            raise PaperError(400, "Starting balance must be between 100 and 10,000,000.")
        if leverage not in LEVERAGES:
            raise PaperError(400, f"Leverage must be one of: {', '.join('1:' + str(x) for x in LEVERAGES)}.")
        info = self.feed(feed_login)
        with self.dlock:
            login = int(self.data["next"])
            self.data["next"] = login + 1
            now = int(time.time())
            a = {
                "login": login, "name": (name or "").strip()[:40] or f"Paper {balance:,.0f}",
                "feed": int(feed_login), "currency": getattr(info, "currency", "USD") or "USD",
                # margin from MT5 is at the broker's leverage (feed_leverage); paper rescales it to this account's leverage
                "leverage": int(leverage), "feed_leverage": int(getattr(info, "leverage", 100) or 100), "created": now,
                "balance": float(balance), "positions": [], "pending": [], "trades": [],
                "cash": [{"time": now, "type": "balance", "amount": float(balance), "comment": "Initial deposit"}],
                "ticket": login * 1000,
            }
            self.data["accounts"][str(login)] = a
            self.save()
            return self.public(a)

    def set_leverage(self, login, leverage: int):
        if leverage not in LEVERAGES:
            raise PaperError(400, f"Leverage must be one of: {', '.join('1:' + str(x) for x in LEVERAGES)}.")
        with self.dlock:
            a = self.data["accounts"].get(str(int(login)))
            if not a:
                raise PaperError(404, "Paper account not found.")
            self._ch_lock(a, "changing the leverage")
            a.setdefault("feed_leverage", a.get("leverage") or 100)  # older accounts: margin was at the broker's leverage
            a["leverage"] = int(leverage)
            self.save()
            return self.public(a)

    def reset(self, login, balance=None):
        with self.dlock:
            a = self.acc(login)
            self._ch_lock(a, "a reset")
            bal = float(balance or a["cash"][0]["amount"] if a["cash"] else balance or 10_000)
            now = int(time.time())
            a.update(balance=bal, positions=[], pending=[], trades=[], targets=[], events=[],
                     cash=[{"time": now, "type": "balance", "amount": bal, "comment": "Reset"}])
            self.save()
            return self.public(a)

    def delete(self, login):
        with self.dlock:
            a = self.acc(login)
            if (a.get("challenge") or {}).get("status") == "active" and not (a["challenge"]["end"] and time.time() > a["challenge"]["end"]):
                raise PaperError(400, "This challenge is still running: you can delete the account after it ends.")
            del self.data["accounts"][str(int(login))]
            self.save()

    def deposit(self, login, amount: float):
        with self.dlock:
            a = self.acc(login)
            self._ch_lock(a, "adding or removing money")
            if a["balance"] + amount < 0:
                raise PaperError(400, "Withdrawal is larger than the balance.")
            a["balance"] = round(a["balance"] + amount, 2)
            a["cash"].append({"time": int(time.time()), "type": "balance", "amount": float(amount),
                              "comment": "Deposit" if amount >= 0 else "Withdrawal"})
            self.save()
            return self.public(a)

    def public(self, a):
        return {"login": a["login"], "name": a["name"], "balance": a["balance"], "leverage": int(a.get("leverage") or 100),
                "currency": a["currency"], "server": "Goldmine Paper", "paper": True}


    # ------------------------------------------------------------ monthly challenge accounts
    # A challenge account is a paper account with fixed rules: fixed balance and leverage, no deposits / resets /
    # leverage changes, a daily loss limit and a max drawdown (break one = challenge failed, positions closed).
    def challenge_create(self, e: dict, feed_login: int):
        """e: the server's entry {id, challenge_id, name, balance, leverage, daily_loss, max_dd, min_trades, start, end, nickname}."""
        with self.dlock:
            for a in self.data["accounts"].values():
                if (a.get("challenge") or {}).get("entry") == e["id"]:
                    return self.public(a)  # already made on this PC
        lev = int(e.get("leverage") or 100)
        acc = self.create(f"🏆 {e.get('name') or 'Challenge'}"[:40], float(e["balance"]), feed_login, lev if lev in LEVERAGES else 100)
        with self.dlock:
            a = self.acc(acc["login"])
            a["challenge"] = {"entry": e["id"], "challenge_id": e.get("challenge_id"), "name": e.get("name", ""),
                              "start_balance": float(e["balance"]), "daily_loss": float(e.get("daily_loss") or 5),
                              "max_dd": float(e.get("max_dd") or 10), "min_trades": int(e.get("min_trades") or 0),
                              "start": int(e.get("start") or time.time()), "end": int(e.get("end") or 0),
                              "status": "active", "fail": "", "peak": float(e["balance"]), "max_dd_pct": 0.0,
                              "day": time.strftime("%Y-%m-%d", time.gmtime()), "day_start": float(e["balance"])}
            self.save()
            return {**self.public(a), "challenge": True}

    @staticmethod
    def _ch_lock(a, what):
        if a.get("challenge"):
            raise PaperError(400, f"This is a challenge account: {what} is not allowed, so everyone plays with the same rules.")

    def _ch_can_trade(self, a):
        ch = a.get("challenge")
        if not ch:
            return
        if ch["end"] and time.time() > ch["end"]:
            raise PaperError(400, "This challenge has ended. Watch the leaderboard for the results.")
        if ch["status"] != "active":
            raise PaperError(400, f"Challenge over for this account: {ch.get('fail') or ch['status']}. You can still watch the leaderboard.")

    def _ch_end(self, a, status, reason):
        """Fail or finish a challenge account: close everything at the current price."""
        ch = a["challenge"]
        for p in list(a["positions"]):
            si, t = self._sym(p["symbol"])
            self._close(a, p, t.bid if p["type"] == "BUY" else t.ask, p["volume"], 6, si, note=f"Challenge {status}")
        a["pending"] = []
        a["targets"] = []
        ch.update(status=status, fail=reason, ended=int(time.time()))
        self._event(a, f"🏆 Challenge {status}: {reason}" if reason else f"🏆 Challenge {status}.", "err" if status == "failed" else "ok")
        self._dirty = True

    def _ch_track(self, a):
        """Watch the challenge rules on every price update."""
        ch = a.get("challenge")
        if not ch or ch["status"] != "active":
            return
        if ch["end"] and time.time() > ch["end"]:
            return self._ch_end(a, "finished", "")
        eq = self.account(a)["equity"]
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if ch.get("day") != day:  # a new trading day (UTC): the daily limit starts again from the equity now
            ch.update(day=day, day_start=eq)
        ch["peak"] = max(ch.get("peak", eq), eq)
        ch["max_dd_pct"] = round(max(ch.get("max_dd_pct", 0.0), (ch["peak"] - eq) / ch["start_balance"] * 100), 2)
        if eq <= ch["day_start"] - ch["start_balance"] * ch["daily_loss"] / 100:
            self._ch_end(a, "failed", f"daily loss limit of {ch['daily_loss']:g}% reached (equity {eq:,.2f})")
        elif eq <= ch["start_balance"] * (1 - ch["max_dd"] / 100):
            self._ch_end(a, "failed", f"max drawdown of {ch['max_dd']:g}% reached (equity {eq:,.2f})")

    def challenge_stats(self, entry=None, login=None):
        """What the leaderboard needs (takes the MT5 lock itself and connects each account's price feed)."""
        with self.lock:
            with self.dlock:
                for a in self.data["accounts"].values():
                    if a.get("challenge") and (entry is None or a["challenge"]["entry"] == entry):
                        try:
                            self.feed(a["feed"])
                        except Exception:
                            pass
                return self._challenge_stats(entry, login)

    def _challenge_stats(self, entry=None, login=None):
        with self.dlock:
            accs = [a for a in self.data["accounts"].values() if a.get("challenge") and
                    (entry is None or a["challenge"]["entry"] == entry) and (login is None or a["login"] == int(login))]
            out = []
            for a in accs:
                try:
                    self.process(a)
                    self._ch_track(a)
                except Exception:
                    pass
                ch, acc = a["challenge"], self.account(a)
                tr = [t for t in a["trades"] if t["close_time"] >= ch["start"]]
                wins = [t for t in tr if t["net"] > 0]
                gp, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in tr if t["net"] < 0)
                status = "finished" if ch["status"] == "active" and ch["end"] and time.time() > ch["end"] else ch["status"]
                out.append({"entry": ch["entry"], "challenge_id": ch.get("challenge_id"), "login": a["login"], "status": status,
                            "fail": ch.get("fail", ""), "start_balance": ch["start_balance"], "balance": acc["balance"],
                            "equity": acc["equity"], "return_pct": round((acc["equity"] - ch["start_balance"]) / ch["start_balance"] * 100, 2),
                            "trades": len(tr), "wins": len(wins), "win_rate": round(len(wins) / len(tr) * 100, 1) if tr else 0.0,
                            "profit_factor": round(gp / gl, 2) if gl else (round(gp, 2) and 99.0),
                            "best_trade": round(max((t["net"] for t in tr), default=0.0), 2), "max_dd_pct": ch.get("max_dd_pct", 0.0),
                            "open_positions": len(a["positions"]), "updated": int(time.time())})
            if self._dirty:
                self.save()
            return out

    # ------------------------------------------------------------ market helpers (MT5 lock held)
    def _sym(self, sym):
        m = self.mt5
        m.symbol_select(sym, True)
        si, t = m.symbol_info(sym), m.symbol_info_tick(sym)
        if si is None or t is None or not t.bid:
            raise PaperError(400, f"Broker symbol unavailable: {sym}")
        return si, t

    def _quote(self, sym):
        """Live quote + whether it can be trusted to trigger fills.

        After MT5 switches between accounts, symbol_info_tick() can return an OLD price from the
        terminal's memory for a moment (seen: a TP 'hit' at a price from two hours earlier).
        A quote is trusted only when
          - its tick time is not older than the newest tick already seen for this symbol, and
          - it is not a sudden jump of more than 0.5% from the last trusted price, unless the next fresh quote confirms it.
        Returns (symbol_info, tick, trusted, why) with why in ok / first / stale / jump / bad."""
        si, t = self._sym(sym)
        bid, ask = float(t.bid), float(t.ask)
        if ask and ask < bid:
            return si, t, False, "bad"
        try:
            srv = getattr(self.mt5.account_info(), "server", "") or ""
        except Exception:
            srv = ""
        tm = int(getattr(t, "time_msc", 0) or 0) or int(t.time) * 1000
        k = (srv, si.name)
        L = self.quotes.get(k)
        if L is None:  # first look since the bridge started: remember it, act on the next fresh one
            self.quotes[k] = {"tm": tm, "bid": bid, "ok": False}
            return si, t, False, "first"
        if tm < L["tm"]:  # an older tick than one we already saw = stale memory after an account switch
            return si, t, False, "stale"
        if tm == L["tm"]:  # no new tick yet: same quote as last time, trusted only if that one was
            same = abs(bid - L["bid"]) < 1e-9
            return si, t, L["ok"] and same, "ok" if L["ok"] and same else ("first" if same else "stale")
        if L["ok"] and L["bid"] and abs(bid - L["bid"]) / L["bid"] > 0.005:
            c = L.get("cand")
            if not (c and abs(bid - c) / c < 0.001):  # a big jump must be confirmed by the next fresh quote
                L["cand"] = bid
                L["tm"] = tm
                return si, t, False, "jump"
        self.quotes[k] = {"tm": tm, "bid": bid, "ok": True}
        return si, t, True, "ok"

    def _live(self, sym):
        """Quote for an order or a manual close: refuse to trade on a price that is not trusted (yet)."""
        for _ in range(6):  # wait up to ~0.6 s for a fresh, trusted quote
            si, t, ok, why = self._quote(sym)
            if ok or why == "first":  # "first": nothing to compare with yet (just started), use it like MT5 would
                return si, t
            time.sleep(0.1)
        raise PaperError(503, "The price feed is catching up (MT5 just switched account). Try again in a second.")

    def _mpu(self, si):
        ts = float(si.trade_tick_size or si.point or 0.01)
        return float(si.trade_tick_value or 0) / ts if ts else 0.0

    def _profit(self, sym, side, vol, open_, close, si):
        m = self.mt5
        try:
            r = m.order_calc_profit(m.ORDER_TYPE_BUY if side == "BUY" else m.ORDER_TYPE_SELL,
                                    sym, vol, open_, close)
            if r is not None:
                return float(r)
        except Exception:
            pass
        return (close - open_) * (1 if side == "BUY" else -1) * self._mpu(si) * vol

    def _vol(self, v, si):
        step = float(si.volume_step or 0.01)
        v = round(round(v / step) * step, 8)
        hi = min(float(si.volume_max or MAX_LOT), MAX_LOT)
        if v < float(si.volume_min or 0.01) or v > hi:
            raise PaperError(400, f"Volume must be between {si.volume_min} and {hi} lots for this symbol.")
        return v

    def _next(self, a):
        a["ticket"] += 1
        return a["ticket"]

    # ------------------------------------------------------------ simulation
    def _close(self, a, p, price, vol, reason, si, when=None, note=""):
        vol = min(vol, p["volume"])
        pl = round(self._profit(p["symbol"], p["type"], vol, p["price_open"], price, si), 2)
        a["balance"] = round(a["balance"] + pl, 2)
        a["trades"].append({
            "position": p["ticket"], "symbol": p["symbol"], "side": p["type"], "volume": vol,
            "closed_volume": vol, "open_time": p["time"], "close_time": int(when or time.time()),
            "open_price": p["price_open"], "close_price": price, "profit": pl, "swap": 0.0,
            "commission": 0.0, "fee": 0.0, "net": pl, "exits": 1, "magic": 0,
            "comment": "Goldmine paper", "close_comment": note, "sl": p["sl"], "tp": p["tp"],
            "sl_initial": p.get("sl0", 0.0), "tp_initial": p.get("tp0", 0.0), "reason": reason,
            "money_per_unit": self._mpu(si), "digits": int(si.digits), "point": float(si.point),
            "mfe": p.get("mfe", 0.0), "mae": p.get("mae", 0.0),
        })
        p["volume"] = round(p["volume"] - vol, 8)
        if p["volume"] <= 1e-9:
            a["positions"].remove(p)
        self._dirty = True
        return pl

    @staticmethod
    def _tm(t):
        return int(getattr(t, "time_msc", 0) or 0) or int(t.time) * 1000

    def _history(self, sym, frm, upto):
        h = tick_history(self.mt5, sym, frm, upto)
        return h

    def _fill(self, a, o, fill, when, chk):
        buy = o["type"] in (2, 4)
        a["pending"].remove(o)
        try:  # like MT5: a pending order that fills without enough free margin is cancelled
            self._need_margin(a, o["symbol"], "BUY" if buy else "SELL", o["volume"], float(fill), f"{NAMES[o['type']]} #{o['ticket']}")
        except PaperError as e:
            self._event(a, f"{NAMES[o['type']]} #{o['ticket']} on {o['symbol']} was cancelled at {fill}: {e.msg}", "err")
            return
        a["positions"].append({
            "ticket": o["ticket"], "symbol": o["symbol"], "type": "BUY" if buy else "SELL",
            "volume": o["volume"], "price_open": float(fill), "sl": o["sl"], "tp": o["tp"],
            "sl0": o["sl"], "tp0": o["tp"], "time": int(when), "mfe": 0.0, "mae": 0.0, "chk": int(chk),
        })

    def _part_vol(self, p, pct, si):
        """Volume for closing `pct` % of a position, rounded to the broker step. None = too small to split."""
        if pct >= 99.999:
            return p["volume"]
        step = float(si.volume_step or 0.01)
        v = round(round(p["volume"] * pct / 100 / step) * step, 8)
        if v < float(si.volume_min or 0.01):
            return None
        return min(v, p["volume"])

    def _event(self, a, text, kind="info"):
        ev = a.setdefault("events", [])
        ev.append({"id": int(time.time() * 1000) + len(ev), "time": int(time.time()), "text": text, "kind": kind})
        del ev[:-30]
        self._dirty = True

    def _keep_best(self, a, sym, side, pct, be, px, si, when, why):
        """Close every `side` position on `sym` except the best entry; optionally close `pct` % of that one
        (leaving a runner) and move its stop to break-even. `px` = the close price for that side."""
        ps = [p for p in a["positions"] if p["symbol"] == sym and p["type"] == side]
        if not ps:
            return "no open positions"
        buy = side == "BUY"
        best = min(ps, key=lambda p: p["price_open"]) if buy else max(ps, key=lambda p: p["price_open"])
        rest = [p for p in ps if p is not best]
        pl = 0.0
        for p in rest:
            pl += self._close(a, p, px, p["volume"], 0, si, when=when, note=why)
        msg = f"closed {len(rest)} ({pl:+.2f})" if rest else "nothing else to close"
        if pct:
            v = self._part_vol(best, pct, si)
            if v is None:
                msg += f"; best entry {best['volume']} lot is too small to close {pct:g}%"
            elif v >= best["volume"] - 1e-9:
                r = self._close(a, best, px, v, 0, si, when=when, note=why)
                msg += f"; best entry closed too ({r:+.2f})"
                best = None
            else:
                r = self._close(a, best, px, v, 0, si, when=when, note=why)
                msg += f"; {v:g} of the best entry closed ({r:+.2f}), runner {best['volume']:g} lot left"
        if best is not None and be and (px - best["price_open"]) * (1 if buy else -1) > 0:
            best["sl"] = best["price_open"]
            msg += ", runner stop at break-even"
        return msg

    def process(self, a):
        """Fill pending orders, hit SL / TP and fire price targets. MT5 lock held.

        Every price since the last check is replayed from MT5's tick history (not only the price right
        now), so anything touched while the PC was locked or asleep still happens, in the right order,
        at the price and time it was touched, like on a real MT5 server."""
        now_s = time.time()
        tg = a.setdefault("targets", [])
        # targets whose position is gone (closed by hand, SL, TP) are dropped
        open_t = {p["ticket"] for p in a["positions"]}
        sides = {(p["symbol"], p["type"]) for p in a["positions"]}
        for g in list(tg):
            if (g["kind"] == "partial" and g["ticket"] not in open_t) or \
               (g["kind"] == "keepbest" and (g["symbol"], g["side"]) not in sides):
                tg.remove(g)
                self._dirty = True
        changed = False
        syms = {x["symbol"] for x in a["pending"] + a["positions"] + tg}
        for sym in syms:
            si, t, ok, _ = self._quote(sym)
            if not ok:  # never act on a stale / unconfirmed price
                continue
            now = self._tm(t)
            items = [x for x in a["pending"] + a["positions"] + tg if x["symbol"] == sym]
            for x in items:
                if not x.get("chk"):
                    x["chk"] = now - 1  # older items without a check time: start from the price now
            h = self._history(sym, min(int(x["chk"]) for x in items), now)
            full = True
            if h is not None and len(h[0]):
                tm, bid, ask, full = h
                if full and now > tm[-1]:  # the live quote is newer than the history: replay it too
                    tm, bid, ask = np.append(tm, now), np.append(bid, float(t.bid)), np.append(ask, float(t.ask))
            else:
                tm, bid, ask = np.array([now], dtype="int64"), np.array([float(t.bid)]), np.array([float(t.ask)])
            if self._replay(a, sym, si, tm, bid, ask, now, now_s):
                changed = True
            end = now if full else int(tm[-1])  # more history than one read: continue from there next second
            for x in a["pending"] + a["positions"] + tg:
                if x["symbol"] == sym and x["chk"] < end:
                    x["chk"] = end
                    self._dirty = True
        if self._stop_out(a):
            changed = True
        self._ch_track(a)
        if changed:
            self.save()

    def _replay(self, a, sym, si, tm, bid, ask, now, now_s):
        """Walk through the ticks and run the earliest event first, again and again, until nothing hits."""
        changed = False
        tg = a["targets"]
        for p in a["positions"]:
            if p["symbol"] == sym:
                buy = p["type"] == "BUY"
                sel = tm > p["chk"]
                if sel.any():
                    mv = ((bid if buy else ask)[sel] - p["price_open"]) * (1 if buy else -1)
                    p["mfe"] = max(p.get("mfe", 0.0), float(mv.max()))
                    p["mae"] = max(p.get("mae", 0.0), float(-mv.min()))
        for _ in range(500):
            ev = None

            def cand(mask, prio, kind, obj):
                nonlocal ev
                i = first_true(mask)
                if i >= 0 and (ev is None or (i, prio) < (ev[0], ev[1])):
                    ev = (i, prio, kind, obj)

            for o in a["pending"]:
                if o["symbol"] == sym:
                    pr = o["price"]
                    hit = {2: ask <= pr, 3: bid >= pr, 4: ask >= pr, 5: bid <= pr}[o["type"]]
                    cand((tm > o["chk"]) & hit, 0, "fill", o)
            for p in a["positions"]:
                if p["symbol"] == sym:
                    buy = p["type"] == "BUY"
                    px, after = (bid if buy else ask), tm > p["chk"]
                    if p["sl"]:
                        cand(after & ((px <= p["sl"]) if buy else (px >= p["sl"])), 1, "sl", p)
                    if p["tp"]:
                        cand(after & ((px >= p["tp"]) if buy else (px <= p["tp"])), 2, "tp", p)
            for g in tg:
                if g["symbol"] == sym:
                    px = bid if g["side"] == "BUY" else ask
                    cand((tm > g["chk"]) & ((px >= g["price"]) if g["dir"] > 0 else (px <= g["price"])), 3, "target", g)
            if ev is None:
                break
            i, _, kind, obj = ev
            when = now_s - (now - int(tm[i])) / 1000
            changed = True
            if kind == "fill":
                buy = obj["type"] in (2, 4)
                self._fill(a, obj, float((ask if buy else bid)[i]), when, int(tm[i]))
            elif kind in ("sl", "tp"):
                buy = obj["type"] == "BUY"
                self._close(a, obj, float((bid if buy else ask)[i]), obj["volume"], 4 if kind == "sl" else 5, si, when=when)
                for g in [g for g in tg if g["kind"] == "partial" and g["ticket"] == obj["ticket"]]:
                    tg.remove(g)
            else:
                g = obj
                tg.remove(g)
                px = float((bid if g["side"] == "BUY" else ask)[i])
                if g["kind"] == "partial":
                    p = next((x for x in a["positions"] if x["ticket"] == g["ticket"]), None)
                    if p is None:
                        continue
                    v = self._part_vol(p, g["percent"], si)
                    if v is None:
                        self._event(a, f"Partial TP {fmt_px(px, si)} on #{p['ticket']}: {g['percent']:g}% of {p['volume']:g} lot is below the broker minimum, skipped.", "warn")
                        continue
                    r = self._close(a, p, px, v, 5, si, when=when, note="Partial TP")
                    if g.get("be") and p in a["positions"] and (px - p["price_open"]) * (1 if p["type"] == "BUY" else -1) > 0:
                        p["sl"] = p["price_open"]
                    self._event(a, f"Partial TP hit at {fmt_px(px, si)}: closed {v:g} lot of #{p['ticket']} ({r:+.2f})"
                                   + (f", {p['volume']:g} lot still open" if p in a["positions"] else "") + ".", "ok")
                else:
                    msg = self._keep_best(a, sym, g["side"], g["percent"], g.get("be"), px, si, when, "Keep best")
                    self._event(a, f"Keep-best {g['side']} {sym} at {fmt_px(px, si)}: {msg}.", "ok")
        return changed

    def _loop(self):
        while True:
            time.sleep(1.0)
            try:
                with self.dlock:
                    busy = [a for a in self.data["accounts"].values() if a["positions"] or a["pending"]]
                if busy:
                    with self.lock:
                        for a in busy:
                            try:
                                try:
                                    info = self.feed(a["feed"], background=True)
                                except TypeError:  # a feed function without the background option
                                    info = self.feed(a["feed"])
                                if info is None:  # MT5 is busy on another account: check again soon, don't flip it now
                                    continue
                                with self.dlock:
                                    self.process(a)
                            except Exception as e:  # keep the simulator alive
                                print(f"[paper] {a['login']}: {e}", flush=True)
                if self._dirty and time.time() - self._last_save > 30:
                    self.save()
            except Exception as e:
                print(f"[paper] loop: {e}", flush=True)

    # ------------------------------------------------------------ views (MT5 lock held, feed already selected)
    def positions(self, a):
        out = []
        for p in a["positions"]:
            si, t = self._sym(p["symbol"])
            close = t.bid if p["type"] == "BUY" else t.ask
            out.append({
                "ticket": p["ticket"], "symbol": p["symbol"], "type": p["type"], "volume": p["volume"],
                "price_open": p["price_open"], "sl": p["sl"], "tp": p["tp"], "price_current": close,
                "profit": round(self._profit(p["symbol"], p["type"], p["volume"], p["price_open"], close, si), 2),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["time"])), "time_epoch": p["time"],
            })
        return out

    def pending_list(self, a):
        return [{"ticket": o["ticket"], "symbol": o["symbol"], "type": o["type"], "name": NAMES[o["type"]],
                 "volume": o["volume"], "price": o["price"], "sl": o["sl"], "tp": o["tp"]} for a_ in [a] for o in a_["pending"]]

    def _margin_for(self, a, sym, side, vol, price):
        """Margin one position needs at this paper account's leverage (the broker's margin is at the broker's leverage)."""
        try:
            m = float(self.mt5.order_calc_margin(self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL, sym, vol, price) or 0)
        except Exception:
            m = 0.0
        lev, feed_lev = int(a.get("leverage") or 100), int(a.get("feed_leverage") or a.get("leverage") or 100)
        return m * feed_lev / lev if lev else m

    def _need_margin(self, a, sym, side, vol, price, what="This trade"):
        """Refuse a new position when the free margin can't carry it (like MT5's 'not enough money')."""
        acc = self.account(a)
        need = self._margin_for(a, sym, side, vol, price)
        if need > acc["margin_free"] + 1e-6:
            raise PaperError(400, f"Not enough free margin: {what.lower()} needs {need:,.2f} {acc['currency']} margin, "
                                  f"you have {max(0.0, acc['margin_free']):,.2f} free (1:{int(a.get('leverage') or 100)}). Use a smaller lot.")

    def _stop_out(self, a):
        """Margin level at or below STOP_OUT %: close the biggest loser, again and again, like a broker's stop-out.
        The balance never goes below zero (negative balance protection)."""
        closed = []
        for _ in range(200):
            if not a["positions"]:
                break
            pos = self.positions(a)
            acc = self.account(a, pos)
            if acc["margin"] <= 0 or acc["equity"] / acc["margin"] * 100 > STOP_OUT:
                break
            worst = min(pos, key=lambda x: x["profit"])
            p = next(x for x in a["positions"] if x["ticket"] == worst["ticket"])
            si, _t = self._sym(p["symbol"])
            lvl = acc["equity"] / acc["margin"] * 100
            pl = self._close(a, p, worst["price_current"], p["volume"], 6, si, note=f"Stop out (margin level {lvl:.0f}%)")
            for g in [g for g in a.get("targets", []) if g["kind"] == "partial" and g["ticket"] == p["ticket"]]:
                a["targets"].remove(g)
            closed.append(f"#{p['ticket']} {p['symbol']} ({pl:+.2f})")
        if closed:
            self._event(a, f"Stop out: margin level fell to {STOP_OUT:.0f}% or below. Closed " + ", ".join(closed) + ".", "err")
        if a["balance"] < 0 and not a["positions"]:
            a["cash"].append({"time": int(time.time()), "type": "balance", "amount": round(-a["balance"], 2), "comment": "Negative balance reset to 0"})
            self._event(a, f"The balance went {a['balance']:.2f}: reset to 0 (negative balance protection).", "err")
            a["balance"] = 0.0
            closed.append("reset")
        if closed:
            self._dirty = True
        return bool(closed)

    def account(self, a, pos=None):
        pos = self.positions(a) if pos is None else pos
        fl = sum(p["profit"] for p in pos)
        margin = sum(self._margin_for(a, p["symbol"], p["type"], p["volume"], p["price_open"]) for p in a["positions"])
        lev = int(a.get("leverage") or 100)
        eq = a["balance"] + fl
        return {"login": a["login"], "name": a["name"], "server": "Goldmine Paper", "currency": a["currency"], "leverage": lev,
                "balance": round(a["balance"], 2), "equity": round(eq, 2), "margin": round(margin, 2),
                "margin_free": round(eq - margin, 2), "profit": round(fl, 2), "is_demo": True, "paper": True}

    def state(self, login):
        """Everything /api/live needs besides the quote."""
        with self.dlock:
            a = self.acc(login)
            self.process(a)
            pos = self.positions(a)
            acc = self.account(a, pos)
            return {"positions": pos, "pending": self.pending_list(a),
                    "targets": [public_target(g) for g in a.get("targets", [])], "events": a.get("events", [])[-10:],
                    "account": {k: acc[k] for k in ("balance", "equity", "margin", "margin_free", "profit", "currency")}}

    # ------------------------------------------------------------ price targets (MT5 lock held)
    def target_add(self, b):
        with self.dlock:
            a = self.acc(b.login)
            self.process(a)  # settle everything up to now first: a new target only looks at prices from now on
            if b.kind == "partial":
                p = next((x for x in a["positions"] if x["ticket"] == b.ticket), None)
                if not p:
                    raise PaperError(404, "Position not found.")
                sym, side = p["symbol"], p["type"]
            else:
                sym, side = b.symbol.strip(), b.side.upper()
                if not any(x["symbol"] == sym and x["type"] == side for x in a["positions"]):
                    raise PaperError(404, f"No open {side} positions on {sym}.")
            si, t = self._live(sym)
            ref = t.bid if side == "BUY" else t.ask
            g = new_target(b.kind, sym, side, round(b.price, int(si.digits)), ref, b.percent, b.ticket, b.be, self._tm(t))
            a.setdefault("targets", []).append(g)
            self.save()
            return {"ok": True, "target": public_target(g)}

    def target_remove(self, login, tid):
        with self.dlock:
            a = self.acc(login)
            n = len(a.get("targets", []))
            a["targets"] = [g for g in a.get("targets", []) if g["id"] != tid]
            if len(a["targets"]) == n:
                raise PaperError(404, "Target not found (it may just have fired).")
            self.save()
            return {"ok": True}

    def keep_best_now(self, b):
        with self.dlock:
            a = self.acc(b.login)
            sym, side = b.symbol.strip(), b.side.upper()
            si, t = self._live(sym)
            msg = self._keep_best(a, sym, side, b.percent, b.be, float(t.bid if side == "BUY" else t.ask), si, None, "Keep best")
            self.process(a)
            self.save()
            return {"ok": True, "message": msg[:1].upper() + msg[1:] + "."}

    def history(self, login, limit):
        a = self.acc(login)
        tr = sorted(a["trades"], key=lambda t: t["close_time"], reverse=True)[:limit]
        return [{"ticket": t["position"], "symbol": t["symbol"], "type": "SELL" if t["side"] == "BUY" else "BUY",
                 "volume": t["closed_volume"], "price": t["close_price"], "profit": t["net"],
                 "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t["close_time"])), "time_epoch": t["close_time"]}
                for t in tr]

    def journal(self, login, days):
        with self.dlock:
            a = self.acc(login)
            frm = time.time() - days * 86400
            acc = self.account(a)
            acc.update(owner=a["name"], company="Goldmine paper trading (live MT5 prices)", leverage=a["leverage"])
            return {"account": acc, "trades": [dict(t) for t in a["trades"] if t["close_time"] >= frm],
                    "cash": [c for c in a["cash"] if c["time"] >= frm], "open": self.positions(a), "now": int(time.time())}

    # ------------------------------------------------------------ trading (MT5 lock held)
    @staticmethod
    def _check(buy, ref, sl, tp, where="entry"):
        if sl and (sl >= ref if buy else sl <= ref):
            raise PaperError(400, f"Stop loss must be below the {where} for buys and above it for sells.")
        if tp and (tp <= ref if buy else tp >= ref):
            raise PaperError(400, f"Take profit must be above the {where} for buys and below it for sells.")

    def order(self, b):
        with self.dlock:
            a = self.acc(b.login)
            si, t = self._live(b.symbol.strip())
            buy = b.side.lower() == "buy"
            px = t.ask if buy else t.bid
            self._check(buy, px, b.sl, b.tp)
            vol = self._vol(b.volume, si)
            self.process(a)
            self._ch_can_trade(a)
            self._need_margin(a, si.name, "BUY" if buy else "SELL", vol, px)
            tk = self._next(a)
            a["positions"].append({"ticket": tk, "symbol": si.name, "type": "BUY" if buy else "SELL",
                                   "volume": vol, "price_open": px, "sl": b.sl, "tp": b.tp,
                                   "sl0": b.sl, "tp0": b.tp, "time": int(time.time()), "mfe": 0.0, "mae": 0.0,
                                   "chk": self._tm(t)})
            self.save()
            return {"ok": True, "ticket": tk, "retcode": 10009, "message": "Paper order filled"}

    def pending(self, b):
        with self.dlock:
            a = self.acc(b.login)
            self._ch_can_trade(a)
            si, t = self._live(b.symbol.strip())
            buy = b.side.lower() == "buy"
            price = round(b.price, int(si.digits))
            otype = (2 if price < t.ask else 4) if buy else (3 if price > t.bid else 5)
            limit = otype in (2, 3)
            if b.kind == "limit" and not limit:
                raise PaperError(400, "A limit order needs the price BELOW market for buys and ABOVE market for sells.")
            if b.kind == "stop" and limit:
                raise PaperError(400, "A stop order needs the price ABOVE market for buys and BELOW market for sells.")
            self._check(buy, price, b.sl, b.tp)
            tk = self._next(a)
            a["pending"].append({"ticket": tk, "symbol": si.name, "type": otype, "volume": self._vol(b.volume, si),
                                 "price": price, "sl": b.sl, "tp": b.tp, "chk": self._tm(t)})
            self.save()
            return {"ok": True, "ticket": tk, "type": NAMES[otype], "message": "Paper order placed"}

    def pending_modify(self, b):
        with self.dlock:
            a = self.acc(b.login)
            self.process(a)  # settle everything up to now with the OLD levels first
            o = next((x for x in a["pending"] if x["ticket"] == b.ticket), None)
            if not o:
                raise PaperError(404, "Pending order not found (it may just have been filled).")
            si, t = self._live(o["symbol"])
            o["chk"] = max(int(o.get("chk") or 0), self._tm(t))  # new levels apply from now on, never to the past
            price = o["price"] if b.price is None else b.price
            sl = o["sl"] if b.sl is None else b.sl
            tp = o["tp"] if b.tp is None else b.tp
            if b.price is not None:  # like MT5: a pending order may not be moved to the wrong side of the market
                bad = {2: price >= t.ask, 3: price <= t.bid, 4: price <= t.ask, 5: price >= t.bid}[o["type"]]
                if bad:
                    side = "below" if o["type"] in (2, 5) else "above"
                    raise PaperError(400, f"Invalid price: a {NAMES[o['type']]} must stay {side} the current price "
                                          f"({t.ask if o['type'] in (2, 4) else t.bid}).")
                price = round(price, int(si.digits))
            self._check(o["type"] in (2, 4), price, sl, tp)
            o.update(price=price, sl=sl, tp=tp)
            self.save()
            return {"ok": True, "message": "Pending order updated"}

    def cancel(self, b):
        with self.dlock:
            a = self.acc(b.login)
            o = next((x for x in a["pending"] if x["ticket"] == b.ticket), None)
            if not o:
                raise PaperError(404, "Pending order not found.")
            a["pending"].remove(o)
            self.save()
            return {"ok": True, "message": "Pending order cancelled"}

    def modify(self, b):
        with self.dlock:
            a = self.acc(b.login)
            self.process(a)  # settle everything up to now with the OLD SL / TP first
            p = next((x for x in a["positions"] if x["ticket"] == b.ticket), None)
            if not p:
                raise PaperError(404, "Position not found: it was already closed (its SL or TP was hit).")
            si, t = self._live(p["symbol"])
            p["chk"] = max(int(p.get("chk") or 0), self._tm(t))  # the new SL / TP apply from now on, never to the past
            buy = p["type"] == "BUY"
            if b.sl and (b.sl >= t.bid if buy else b.sl <= t.ask):
                raise PaperError(400, "SL is on the wrong side of the current market price.")
            if b.tp and (b.tp <= t.bid if buy else b.tp >= t.ask):
                raise PaperError(400, "TP is on the wrong side of the current market price.")
            if b.sl and not p.get("sl0"):
                p["sl0"] = b.sl
            if b.tp and not p.get("tp0"):
                p["tp0"] = b.tp
            p["sl"], p["tp"] = b.sl, b.tp
            self.save()
            return {"ok": True, "message": "SL/TP updated"}

    def close(self, b):
        with self.dlock:
            a = self.acc(b.login)
            p = next((x for x in a["positions"] if x["ticket"] == b.ticket), None)
            if not p:
                raise PaperError(400, "Position ticket not found")
            si, t = self._live(p["symbol"])
            step = float(si.volume_step or 0.01)
            vol = p["volume"] if b.percent >= 99.999 else round(round(p["volume"] * b.percent / 100 / step) * step, 8)
            if vol < float(si.volume_min or 0.01):
                raise PaperError(400, f"Partial close rounds below broker minimum {si.volume_min} lots")
            if vol >= p["volume"] and b.percent < 99.999:
                raise PaperError(400, "Partial close would close the full position; choose 100% instead")
            self._close(a, p, t.bid if p["type"] == "BUY" else t.ask, vol, 0, si)
            self.save()
            return {"ok": True, "ticket": p["ticket"], "message": f"Paper close ({b.percent:g}%)."}

    def close_all(self, login):
        with self.dlock:
            a = self.acc(login)
            n = 0
            for p in list(a["positions"]):
                si, t = self._live(p["symbol"])
                self._close(a, p, t.bid if p["type"] == "BUY" else t.ask, p["volume"], 0, si)
                n += 1
            self.save()
            return {"ok": True, "closed": n, "failed": 0, "errors": []}
