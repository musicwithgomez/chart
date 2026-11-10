"""Fake MetaTrader5 module: lets the self-test run the real bridge code without a real MT5 terminal or broker."""
import os
import time
from types import SimpleNamespace as NS

ACCOUNT_TRADE_MODE_DEMO = 0
ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = range(6)
POSITION_TYPE_BUY = 0
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1


def __getattr__(name):  # any other constant
    return abs(hash(name)) % 100000 + 1000


# logged_in=False simulates "MT5 is open but nobody is logged in"
S = {"login": None, "price": 2650.0, "mode": int(os.getenv("FAKE_MT5_REAL", "0")) and 2, "inits": 0, "logged_in": True, "account": 51234567}
SYMS = {"XAUUSD": dict(digits=2, point=.01, trade_tick_size=.01, trade_tick_value=1.0, trade_contract_size=100,
                       volume_min=.01, volume_step=.01, volume_max=100, filling_mode=1, trade_exemode=2)}


def initialize(**kw):
    S["inits"] += 1
    S["login"] = kw.get("login", S["account"])
    return True


def login(l, password=None, server=None):
    S["login"] = l
    return True


def shutdown():
    pass


def last_error():
    return (0, "ok")


def terminal_info():
    return NS(path=r"C:\Program Files\MetaTrader 5", connected=True, name="MetaTrader 5", trade_allowed=os.getenv("FAKE_MT5_ALGO", "1") == "1")


def account_info():
    if not S["logged_in"]:
        return None
    return NS(login=S["login"], trade_mode=S["mode"], balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
              profit=0.0, currency="USD", leverage=100, server="FakeBroker-Demo", name="Test Trader", company="FakeBroker")


def symbol_select(s, on=True):
    return s in SYMS


def symbol_info(s):
    return NS(name=s, visible=True, path="Metals\\" + s, description="Gold", **SYMS[s]) if s in SYMS else None


def symbol_info_tick(s):
    p = S["price"]
    return NS(bid=p, ask=round(p + .2, 2), time=int(time.time())) if s in SYMS else None


def symbols_get():
    return [symbol_info(s) for s in SYMS]


def copy_rates_from_pos(s, tf, start, n):
    t = int(time.time()) // 60 * 60
    return [{"time": t - 60 * (n - 1 - i), "open": S["price"], "high": S["price"] + 1, "low": S["price"] - 1, "close": S["price"]} for i in range(n)]


def order_calc_profit(t, s, v, o, c):
    return (c - o) * (1 if t == ORDER_TYPE_BUY else -1) * 100 * v


def order_calc_margin(t, s, v, p):
    return v * 100 * p / 100


POS = []  # open positions: dicts with ticket, symbol, type (0 buy / 1 sell), volume, price_open, sl, tp
SENT = []  # every order_send request


def positions_get(**k):
    ps = [p for p in POS if p["volume"] > 1e-9]
    if "ticket" in k:
        ps = [p for p in ps if p["ticket"] == k["ticket"]]
    if "symbol" in k:
        ps = [p for p in ps if p["symbol"] == k["symbol"]]
    return [NS(swap=0.0, profit=0.0, price_current=S["price"], time=int(time.time()), magic=0, comment="", **p) for p in ps]


def order_send(req):
    import MetaTrader5 as me
    SENT.append(dict(req))
    p = next((x for x in POS if x["ticket"] == req.get("position")), None)
    if p and req.get("action") == me.TRADE_ACTION_SLTP:
        p.update(sl=req["sl"], tp=req["tp"])
    elif p and req.get("action") == me.TRADE_ACTION_DEAL:
        p["volume"] = round(p["volume"] - req["volume"], 8)
    return NS(retcode=me.TRADE_RETCODE_DONE, deal=len(SENT), order=len(SENT), comment="done")


def orders_get(**k):
    return []


def history_deals_get(a, b):
    return []


def history_orders_get(a, b):
    return []
