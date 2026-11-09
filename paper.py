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
             "is_demo": True, "paper": True}
            for a in self.data["accounts"].values()
        ]

    def create(self, name: str, balance: float, feed_login: int):
        if not (100 <= balance <= 10_000_000):
            raise PaperError(400, "Starting balance must be between 100 and 10,000,000.")
        info = self.feed(feed_login)
        with self.dlock:
            login = int(self.data["next"])
            self.data["next"] = login + 1
            now = int(time.time())
            a = {
                "login": login, "name": (name or "").strip()[:40] or f"Paper {balance:,.0f}",
                "feed": int(feed_login), "currency": getattr(info, "currency", "USD") or "USD",
                "leverage": int(getattr(info, "leverage", 100) or 100), "created": now,
                "balance": float(balance), "positions": [], "pending": [], "trades": [],
                "cash": [{"time": now, "type": "balance", "amount": float(balance), "comment": "Initial deposit"}],
                "ticket": login * 1000,
            }
            self.data["accounts"][str(login)] = a
            self.save()
            return self.public(a)

    def reset(self, login, balance=None):
        with self.dlock:
            a = self.acc(login)
            bal = float(balance or a["cash"][0]["amount"] if a["cash"] else balance or 10_000)
            now = int(time.time())
            a.update(balance=bal, positions=[], pending=[], trades=[],
                     cash=[{"time": now, "type": "balance", "amount": bal, "comment": "Reset"}])
            self.save()
            return self.public(a)

    def delete(self, login):
        with self.dlock:
            self.acc(login)
            del self.data["accounts"][str(int(login))]
            self.save()

    def deposit(self, login, amount: float):
        with self.dlock:
            a = self.acc(login)
            if a["balance"] + amount < 0:
                raise PaperError(400, "Withdrawal is larger than the balance.")
            a["balance"] = round(a["balance"] + amount, 2)
            a["cash"].append({"time": int(time.time()), "type": "balance", "amount": float(amount),
                              "comment": "Deposit" if amount >= 0 else "Withdrawal"})
            self.save()
            return self.public(a)

    def public(self, a):
        return {"login": a["login"], "name": a["name"], "balance": a["balance"],
                "currency": a["currency"], "server": "Goldmine Paper", "paper": True}

    # ------------------------------------------------------------ market helpers (MT5 lock held)
    def _sym(self, sym):
        m = self.mt5
        m.symbol_select(sym, True)
        si, t = m.symbol_info(sym), m.symbol_info_tick(sym)
        if si is None or t is None or not t.bid:
            raise PaperError(400, f"Broker symbol unavailable: {sym}")
        return si, t

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
    def _close(self, a, p, price, vol, reason, si):
        vol = min(vol, p["volume"])
        pl = round(self._profit(p["symbol"], p["type"], vol, p["price_open"], price, si), 2)
        a["balance"] = round(a["balance"] + pl, 2)
        a["trades"].append({
            "position": p["ticket"], "symbol": p["symbol"], "side": p["type"], "volume": vol,
            "closed_volume": vol, "open_time": p["time"], "close_time": int(time.time()),
            "open_price": p["price_open"], "close_price": price, "profit": pl, "swap": 0.0,
            "commission": 0.0, "fee": 0.0, "net": pl, "exits": 1, "magic": 0,
            "comment": "Goldmine paper", "close_comment": "", "sl": p["sl"], "tp": p["tp"],
            "sl_initial": p.get("sl0", 0.0), "tp_initial": p.get("tp0", 0.0), "reason": reason,
            "money_per_unit": self._mpu(si), "digits": int(si.digits), "point": float(si.point),
            "mfe": p.get("mfe", 0.0), "mae": p.get("mae", 0.0),
        })
        p["volume"] = round(p["volume"] - vol, 8)
        if p["volume"] <= 1e-9:
            a["positions"].remove(p)
        self._dirty = True
        return pl

    def process(self, a):
        """Fill pending orders and hit SL / TP at the live price. MT5 lock held."""
        changed = False
        for o in list(a["pending"]):
            si, t = self._sym(o["symbol"])
            hit = {2: t.ask <= o["price"], 3: t.bid >= o["price"],
                   4: t.ask >= o["price"], 5: t.bid <= o["price"]}[o["type"]]
            if hit:
                # fill at the REAL market price at that moment (ask for buys, bid for sells), never at a
                # price the market did not trade: a limit fills at its price or better, a stop with slippage
                buy = o["type"] in (2, 4)
                fill = t.ask if buy else t.bid
                a["pending"].remove(o)
                a["positions"].append({
                    "ticket": o["ticket"], "symbol": o["symbol"], "type": "BUY" if buy else "SELL",
                    "volume": o["volume"], "price_open": fill, "sl": o["sl"], "tp": o["tp"],
                    "sl0": o["sl"], "tp0": o["tp"], "time": int(time.time()), "mfe": 0.0, "mae": 0.0,
                })
                changed = True
        for p in list(a["positions"]):
            si, t = self._sym(p["symbol"])
            buy = p["type"] == "BUY"
            px = t.bid if buy else t.ask
            mv = (px - p["price_open"]) * (1 if buy else -1)
            p["mfe"] = max(p.get("mfe", 0.0), mv)
            p["mae"] = max(p.get("mae", 0.0), -mv)
            # stop loss / take profit close at the live market price (a fast move gives slippage, like MT5)
            if p["sl"] and (px <= p["sl"] if buy else px >= p["sl"]):
                self._close(a, p, px, p["volume"], 4, si)
                changed = True
            elif p["tp"] and (px >= p["tp"] if buy else px <= p["tp"]):
                self._close(a, p, px, p["volume"], 5, si)
                changed = True
        if changed:
            self.save()

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
                                self.feed(a["feed"])
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
                "price_open": p["price_open"], "sl": p["sl"], "tp": p["tp"],
                "profit": round(self._profit(p["symbol"], p["type"], p["volume"], p["price_open"], close, si), 2),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p["time"])), "time_epoch": p["time"],
            })
        return out

    def pending_list(self, a):
        return [{"ticket": o["ticket"], "symbol": o["symbol"], "type": o["type"], "name": NAMES[o["type"]],
                 "volume": o["volume"], "price": o["price"], "sl": o["sl"], "tp": o["tp"]} for a_ in [a] for o in a_["pending"]]

    def account(self, a, pos=None):
        pos = self.positions(a) if pos is None else pos
        fl = sum(p["profit"] for p in pos)
        margin = 0.0
        for p in a["positions"]:
            try:
                m = self.mt5.order_calc_margin(self.mt5.ORDER_TYPE_BUY if p["type"] == "BUY" else self.mt5.ORDER_TYPE_SELL,
                                               p["symbol"], p["volume"], p["price_open"])
                margin += float(m or 0)
            except Exception:
                pass
        eq = a["balance"] + fl
        return {"login": a["login"], "name": a["name"], "server": "Goldmine Paper", "currency": a["currency"],
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
                    "account": {k: acc[k] for k in ("balance", "equity", "margin", "margin_free", "profit", "currency")}}

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
            si, t = self._sym(b.symbol.strip())
            buy = b.side.lower() == "buy"
            px = t.ask if buy else t.bid
            self._check(buy, px, b.sl, b.tp)
            tk = self._next(a)
            a["positions"].append({"ticket": tk, "symbol": si.name, "type": "BUY" if buy else "SELL",
                                   "volume": self._vol(b.volume, si), "price_open": px, "sl": b.sl, "tp": b.tp,
                                   "sl0": b.sl, "tp0": b.tp, "time": int(time.time()), "mfe": 0.0, "mae": 0.0})
            self.save()
            return {"ok": True, "ticket": tk, "retcode": 10009, "message": "Paper order filled"}

    def pending(self, b):
        with self.dlock:
            a = self.acc(b.login)
            si, t = self._sym(b.symbol.strip())
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
                                 "price": price, "sl": b.sl, "tp": b.tp})
            self.save()
            return {"ok": True, "ticket": tk, "type": NAMES[otype], "message": "Paper order placed"}

    def pending_modify(self, b):
        with self.dlock:
            a = self.acc(b.login)
            o = next((x for x in a["pending"] if x["ticket"] == b.ticket), None)
            if not o:
                raise PaperError(404, "Pending order not found.")
            price = o["price"] if b.price is None else b.price
            sl = o["sl"] if b.sl is None else b.sl
            tp = o["tp"] if b.tp is None else b.tp
            if b.price is not None:  # like MT5: a pending order may not be moved to the wrong side of the market
                si, t = self._sym(o["symbol"])
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
            p = next((x for x in a["positions"] if x["ticket"] == b.ticket), None)
            if not p:
                raise PaperError(404, "Position ticket not found.")
            si, t = self._sym(p["symbol"])
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
            si, t = self._sym(p["symbol"])
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
                si, t = self._sym(p["symbol"])
                self._close(a, p, t.bid if p["type"] == "BUY" else t.ask, p["volume"], 0, si)
                n += 1
            self.save()
            return {"ok": True, "closed": n, "failed": 0, "errors": []}
