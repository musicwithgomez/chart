
"""Gomez Trading MT5 bridge (DEMO ONLY) - v0.5
Runs on your Windows PC next to MetaTrader 5.
Listens on whatever start_bridge.ps1 binds (0.0.0.0 = reachable from your phone).

v0.5 adds: serves index.html at "/", faster account switching, and COPY
TRADING (master account -> slave account) with risk adaptation:
fixed risk %, master risk x factor, balance ratio, lot multiplier, fixed lot.
Endpoints: /api/copy/status, /config, /start, /stop, /check, /clear-log.

v0.4 adds: /api/live (one-call quotes+candles+positions), pending orders
(place / move / cancel), gold symbol detection, M3 and M12 timeframes.

v0.3 fixes:
- Selects a supported MT5 order filling policy.
- Retries with another supported policy only on error 10030.
- Uses the same filling-mode logic for opening and closing trades.
- Keeps demo-only account protection and existing API routes.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

import MetaTrader5 as mt5
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"

if not CONFIG_PATH.exists():
    raise RuntimeError(
        "Missing config.json. Copy config.example.json to config.json "
        "and configure a demo terminal."
    )

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

if CONFIG.get("mode") not in ("demo", "mixed"):
    raise RuntimeError(
        "config mode must be 'demo' (demo accounts only) or "
        "'mixed' (demo accounts, plus any account with \"allow_live\": true)."
    )

LIVE_ALLOWED = CONFIG.get("mode") == "mixed"

API_KEY = str(CONFIG.get("api_key", ""))

if len(API_KEY) < 32 or API_KEY == "REPLACE_WITH_A_LONG_RANDOM_SECRET":
    raise RuntimeError(
        "Set api_key in config.json to a unique random secret "
        "of at least 32 characters."
    )

TERMINALS = CONFIG.get("terminals", [])

if not TERMINALS:
    raise RuntimeError("Configure at least one demo terminal in config.json.")


def clean_origin(o: str) -> str:
    """Return a CORS origin without any path."""
    p = urlparse(str(o).strip())
    return (
        f"{p.scheme}://{p.netloc}"
        if p.scheme and p.netloc
        else str(o).strip().rstrip("/")
    )


ORIGINS = sorted({
    clean_origin(o)
    for o in CONFIG.get("allowed_origins", [])
    if str(o).strip()
})

print("Allowed browser origins:", ORIGINS)

app = FastAPI(title="Gomez Trading MT5 Demo Bridge", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Gomez-Key"],
)


@app.middleware("http")
async def private_network_access(request: Request, call_next):
    response = await call_next(request)

    if request.headers.get(
        "access-control-request-private-network", ""
    ).lower() == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"

    return response


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(BASE / "terminal.html")


# Site files the tunnel link may serve (login, terminal, shared js/css). config.json is NOT in this list.
SITE_FILES = {
    "index.html", "login.html", "terminal.html", "admin.html",
    "trader.html", "investor.html", "gm.js", "gm.css",
}


@app.get("/{fname}", include_in_schema=False)
def site_file(fname: str):
    if fname in SITE_FILES and (BASE / fname).is_file():
        return FileResponse(BASE / fname)
    raise HTTPException(404, "Not found.")


class OrderIn(BaseModel):
    login: int
    symbol: str = Field(min_length=1, max_length=32)
    side: str
    volume: float = Field(gt=0, le=1.0)
    sl: float = Field(default=0, ge=0)
    tp: float = Field(default=0, ge=0)


class CloseIn(BaseModel):
    login: int
    ticket: int
    percent: float = Field(gt=0, le=100)


class CloseAllIn(BaseModel):
    login: int


class PendingIn(BaseModel):
    login: int
    symbol: str = Field(min_length=1, max_length=32)
    side: str
    kind: str = "auto"  # "limit", "stop" or "auto"
    volume: float = Field(gt=0, le=1.0)
    price: float = Field(gt=0)
    sl: float = Field(default=0, ge=0)
    tp: float = Field(default=0, ge=0)


class PendingModifyIn(BaseModel):
    login: int
    ticket: int
    price: Optional[float] = Field(default=None, gt=0)
    sl: Optional[float] = Field(default=None, ge=0)
    tp: Optional[float] = Field(default=None, ge=0)


class CancelIn(BaseModel):
    login: int
    ticket: int


class ModifyIn(BaseModel):
    login: int
    ticket: int
    sl: float = Field(default=0, ge=0)
    tp: float = Field(default=0, ge=0)


def authorized(k: Optional[str]):
    if not k or not secrets.compare_digest(k, API_KEY):
        raise HTTPException(401, "Missing or invalid bridge key.")


def terminal_cfg(login: int):
    for cfg in TERMINALS:
        if int(cfg.get("login", -1)) == int(login):
            return cfg

    raise HTTPException(
        404, "That account is not configured in the bridge."
    )


LOCK = threading.RLock()
STATE = {"login": None, "path": None}


def _fast_switch(cfg, login):
    """Switch account inside the already running terminal. Much faster than
    a full shutdown + initialize. Returns account info, or None to fall back
    to the normal (slower) path."""
    if (
        STATE.get("login") is None
        or not cfg.get("path")
        or cfg.get("path") != STATE.get("path")
    ):
        return None

    password = cfg.get("password") or os.getenv(
        cfg.get("password_env", "MT5_DEMO_PASSWORD"), ""
    )

    if not password:
        return None

    try:
        if not mt5.login(
            int(cfg["login"]), password=password, server=cfg["server"]
        ):
            return None

        info = mt5.account_info()

        if info is not None and int(info.login) == int(login):
            return info
    except Exception:
        pass

    return None


def ensure(login: int):
    cfg = terminal_cfg(login)

    info = (
        mt5.account_info()
        if STATE["login"] == int(login)
        else None
    )

    if info is None or int(info.login) != int(login):
        info = _fast_switch(cfg, login)

    if info is None or int(info.login) != int(login):
        mt5.shutdown()
        STATE["login"] = None

        # Password lookup order (demo accounts only):
        #   1. "password" written directly in config.json
        #   2. the Windows environment variable named by "password_env"
        #   3. none -> use the password already saved inside the MT5 terminal
        env = cfg.get("password_env", "MT5_DEMO_PASSWORD")
        password = cfg.get("password") or os.getenv(env, "")

        kwargs = {
            "login": int(cfg["login"]),
            "server": cfg["server"],
        }

        if cfg.get("path"):
            kwargs["path"] = cfg["path"]

        if password:
            kwargs["password"] = password

        if not mt5.initialize(**kwargs):
            hint = (
                ""
                if password
                else " (no password given: add \"password\" to this "
                "account in config.json, or log in once in MT5 with "
                "'Save password' ticked)"
            )
            raise HTTPException(
                503, f"MT5 initialize failed: {mt5.last_error()}{hint}"
            )

        info = mt5.account_info()

        if info is None:
            mt5.shutdown()
            raise HTTPException(
                503, "Could not read MT5 account info."
            )

    # Live accounts are only allowed when config mode is "mixed" AND this
    # specific account has "allow_live": true in config.json.
    is_demo_acct = int(info.trade_mode) == int(mt5.ACCOUNT_TRADE_MODE_DEMO)
    if not is_demo_acct and not (LIVE_ALLOWED and cfg.get("allow_live") is True):
        mt5.shutdown()
        STATE["login"] = None
        raise HTTPException(
            403,
            "Live/non-demo account blocked. To allow it, set config mode "
            "to 'mixed' and add \"allow_live\": true to this account."
        )

    if int(info.login) != int(login):
        mt5.shutdown()
        STATE["login"] = None
        raise HTTPException(
            403,
            "The active MT5 login does not match the selected configured login."
        )

    STATE["login"] = int(login)
    STATE["path"] = cfg.get("path")
    return cfg, info


@contextmanager
def session(login: int):
    with LOCK:
        yield ensure(login)


@app.on_event("shutdown")
def _bye():
    with LOCK:
        mt5.shutdown()


def account_dict(cfg, a):
    return {
        "login": int(a.login),
        "name": cfg.get("label", "MT5 Demo"),
        "server": a.server,
        "currency": a.currency,
        "balance": a.balance,
        "equity": a.equity,
        "margin": a.margin,
        "margin_free": a.margin_free,
        "profit": a.profit,
        "is_demo": (
            int(a.trade_mode) == int(mt5.ACCOUNT_TRADE_MODE_DEMO)
        ),
    }


def fmt(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


# ------------------------------------------------------------
# ORDER FILLING MODE FIX
# ------------------------------------------------------------

def supported_filling_modes(symbol_info):
    """
    symbol_info.filling_mode contains SYMBOL_FILLING_* capability
    flags, not an ORDER_FILLING_* request value.

    Build a list of valid order request policies for this symbol.
    ORDER_FILLING_RETURN is not allowed with Market Execution.
    """
    flags = int(symbol_info.filling_mode)
    modes = []

    fok_flag = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
    ioc_flag = getattr(mt5, "SYMBOL_FILLING_IOC", 2)

    if flags & fok_flag:
        modes.append(mt5.ORDER_FILLING_FOK)

    if flags & ioc_flag:
        modes.append(mt5.ORDER_FILLING_IOC)

    market_execution = getattr(
        mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2
    )

    if int(symbol_info.trade_exemode) != int(market_execution):
        modes.append(mt5.ORDER_FILLING_RETURN)

    # Remove duplicates while retaining order.
    return list(dict.fromkeys(modes))


def send_with_supported_filling(request, symbol_info):
    """
    Submit an order using a supported filling policy.

    Retry only when MT5 explicitly returns INVALID_FILL (10030).
    Do not retry other errors, because they may have different causes.
    """
    modes = supported_filling_modes(symbol_info)

    if not modes:
        raise ValueError(
            "No compatible filling mode could be determined for "
            f"{symbol_info.name}. Check the broker's symbol settings."
        )

    last_result = None
    invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)

    for mode in modes:
        request["type_filling"] = mode
        result = mt5.order_send(request)

        if result is None:
            return None

        last_result = result

        if int(result.retcode) == int(invalid_fill):
            # Only this specific error permits trying another policy.
            continue

        return result

    # All candidate policies were rejected as unsupported.
    return last_result


# ------------------------------------------------------------
# ACCOUNT AND MARKET DATA
# ------------------------------------------------------------

@app.get("/api/health")
def health(x_gomez_key: Optional[str] = Header(default=None)):
    authorized(x_gomez_key)
    return {
        "ok": True,
        "mode": CONFIG.get("mode"),
        "bridge": "Gomez Trading MT5 Bridge",
        "version": "0.5.0",
        "live_trading_enabled": LIVE_ALLOWED,
    }


@app.get("/api/accounts")
def accounts(x_gomez_key: Optional[str] = Header(default=None)):
    authorized(x_gomez_key)
    out = []

    # Just list what is in config.json. Do NOT log in to every account here:
    # that was slow and made the whole list fail if one account had no password.
    # The real login happens when you pick an account and the page uses it.
    for c in TERMINALS:
        out.append({
            "login": int(c["login"]),
            "name": c.get("label", "MT5 Demo"),
            "server": c.get("server", ""),
            "is_demo": not (LIVE_ALLOWED and c.get("allow_live") is True),
        })

    return {"accounts": out}


@app.get("/api/account")
def account(
    login: int = Query(...),
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(login) as (c, a):
        return account_dict(c, a)


@app.get("/api/positions")
def get_positions(
    login: int = Query(...),
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(login):
        out = []

        for p in (mt5.positions_get() or []):
            out.append({
                "ticket": int(p.ticket),
                "symbol": p.symbol,
                "type": (
                    "BUY"
                    if p.type == mt5.POSITION_TYPE_BUY
                    else "SELL"
                ),
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit + p.swap,
                "time": fmt(p.time),
                "time_epoch": int(p.time),
            })

        pend = []

        for o in (mt5.orders_get() or []):
            pend.append({
                "ticket": int(o.ticket),
                "symbol": o.symbol,
                "type": o.type,
                "volume": o.volume_current,
                "price": o.price_open,
                "sl": o.sl,
                "tp": o.tp,
            })

        return {"positions": out, "pending": pend}


@app.get("/api/symbols")
def symbols(
    login: int = Query(...),
    all: int = Query(0),
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(login):
        all_symbols = mt5.symbols_get() or []
        if all:
            # every symbol the broker offers (not just Market Watch)
            names = [s.name for s in all_symbols]
        else:
            names = [s.name for s in all_symbols if s.visible]

            if not names:
                names = [s.name for s in all_symbols][:500]

        # Find this broker's gold symbol (XAUUSD, GOLD, XAUUSD.a, ...).
        gold = []

        for s in all_symbols:
            n = s.name.upper()
            d = (getattr(s, "description", "") or "").upper()

            if n.startswith(("XAU", "GOLD")) or "GOLD" in d:
                if "XAUEUR" in n or "XAUAUD" in n or "XAUGBP" in n:
                    continue
                gold.append(s.name)

        # exact XAUUSD first, then visible ones, then the rest
        gold.sort(key=lambda n: (
            0 if n.upper() == "XAUUSD" else
            1 if n.upper().startswith("XAUUSD") else
            2 if n.upper() == "GOLD" else 3,
            len(n),
        ))

        out = {"symbols": sorted(names), "gold": gold}

        if all:
            # broker's own folder for each symbol, e.g. "Forex\\Majors\\EURUSD"
            out["paths"] = {
                s.name: (getattr(s, "path", "") or "") for s in all_symbols
            }

        return out


TF = {
    "M1": (mt5.TIMEFRAME_M1, 60),
    "M3": (mt5.TIMEFRAME_M3, 180),
    "M5": (mt5.TIMEFRAME_M5, 300),
    "M12": (mt5.TIMEFRAME_M12, 720),
    "M15": (mt5.TIMEFRAME_M15, 900),
    "M30": (mt5.TIMEFRAME_M30, 1800),
    "H1": (mt5.TIMEFRAME_H1, 3600),
    "H4": (mt5.TIMEFRAME_H4, 14400),
    "D1": (mt5.TIMEFRAME_D1, 86400),
}


@app.get("/api/candles")
def candles(
    login: int = Query(...),
    symbol: str = Query(..., min_length=1, max_length=32),
    timeframe: str = Query("M15"),
    count: int = Query(500, ge=1, le=3000),
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    if timeframe not in TF:
        raise HTTPException(400, "Unknown timeframe.")

    with session(login):
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(
                400, f"Broker symbol unavailable: {symbol}"
            )

        si = mt5.symbol_info(symbol)
        rates = mt5.copy_rates_from_pos(
            symbol, TF[timeframe][0], 0, count
        )

        if rates is None or len(rates) == 0:
            raise HTTPException(
                503,
                f"No candle data yet for {symbol} "
                "(open it once in MT5, then retry)."
            )

        data = [
            {
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for r in rates
        ]

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "tf_seconds": TF[timeframe][1],
            "digits": int(si.digits),
            "point": si.point,
            "candles": data,
        }


def pos_list():
    out = []

    for p in (mt5.positions_get() or []):
        out.append({
            "ticket": int(p.ticket),
            "symbol": p.symbol,
            "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": p.volume,
            "price_open": p.price_open,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit + p.swap,
            "time_epoch": int(p.time),
        })

    return out


PEND_NAMES = {
    mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT",
    mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT",
    mt5.ORDER_TYPE_BUY_STOP: "BUY STOP",
    mt5.ORDER_TYPE_SELL_STOP: "SELL STOP",
}


def pend_list():
    out = []

    for o in (mt5.orders_get() or []):
        out.append({
            "ticket": int(o.ticket),
            "symbol": o.symbol,
            "type": o.type,
            "name": PEND_NAMES.get(o.type, "PENDING"),
            "volume": o.volume_current,
            "price": o.price_open,
            "sl": o.sl,
            "tp": o.tp,
        })

    return out


@app.get("/api/live")
def live(
    login: int = Query(...),
    symbol: str = Query(..., min_length=1, max_length=32),
    timeframe: str = Query("M5"),
    x_gomez_key: Optional[str] = Header(default=None),
):
    """Everything the chart needs in ONE call: quote, the last two
    candles, open positions, pending orders and account numbers."""
    authorized(x_gomez_key)

    if timeframe not in TF:
        raise HTTPException(400, "Unknown timeframe.")

    with session(login) as (c, a):
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        si = mt5.symbol_info(symbol)

        if tick is None or si is None:
            raise HTTPException(503, f"No quote for {symbol} yet.")

        rates = mt5.copy_rates_from_pos(symbol, TF[timeframe][0], 0, 2)

        bars = [
            {
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for r in (rates if rates is not None else [])
        ]

        acc = mt5.account_info()

        return {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "time": int(tick.time),
            "digits": int(si.digits),
            "point": si.point,
            "contract": si.trade_contract_size,
            "bars": bars,
            "positions": pos_list(),
            "pending": pend_list(),
            "account": {
                "balance": acc.balance,
                "equity": acc.equity,
                "margin": acc.margin,
                "margin_free": acc.margin_free,
                "profit": acc.profit,
                "currency": acc.currency,
            },
        }


@app.get("/api/history")
def history(
    login: int = Query(...),
    limit: int = Query(8, ge=1, le=200),
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(login):
        now = time.time() + 86400
        deals = sorted(
            mt5.history_deals_get(now - 90 * 86400, now) or [],
            key=lambda d: d.time,
            reverse=True,
        )

        out = []

        for d in deals:
            if d.entry not in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY,
                mt5.DEAL_ENTRY_INOUT,
            ):
                continue

            out.append({
                "ticket": int(d.ticket),
                "symbol": d.symbol,
                "type": (
                    "BUY"
                    if d.type == mt5.DEAL_TYPE_BUY
                    else "SELL"
                ),
                "volume": d.volume,
                "price": d.price,
                "profit": d.profit + d.swap + d.commission,
                "time": fmt(d.time)[:16],
                "time_epoch": int(d.time),
            })

            if len(out) >= limit:
                break

        return {"deals": out}


# ------------------------------------------------------------
# OPEN MARKET ORDER
# ------------------------------------------------------------

@app.post("/api/order")
def order(
    body: OrderIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    if body.side.lower() not in ("buy", "sell"):
        raise HTTPException(400, "Side must be buy or sell.")

    with session(body.login):
        symbol = body.symbol.strip()

        if not mt5.symbol_select(symbol, True):
            raise HTTPException(
                400,
                f"Broker symbol unavailable: {symbol}. "
                "Check exact Market Watch symbol."
            )

        si = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        if si is None or tick is None:
            raise HTTPException(400, "Could not read symbol quote.")

        vol = round(body.volume / si.volume_step) * si.volume_step

        if vol < si.volume_min or vol > min(si.volume_max, 1.0):
            raise HTTPException(
                400,
                f"Volume must be between {si.volume_min} and "
                f"{min(si.volume_max, 1.0)} lots for this symbol."
            )

        is_buy = body.side.lower() == "buy"
        price = tick.ask if is_buy else tick.bid

        if body.sl and (
            (is_buy and body.sl >= price)
            or (not is_buy and body.sl <= price)
        ):
            raise HTTPException(
                400,
                "Stop loss must be below entry for buys "
                "and above entry for sells."
            )

        if body.tp and (
            (is_buy and body.tp <= price)
            or (not is_buy and body.tp >= price)
        ):
            raise HTTPException(
                400,
                "Take profit must be above entry for buys "
                "and below entry for sells."
            )

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": vol,
            "type": (
                mt5.ORDER_TYPE_BUY
                if is_buy
                else mt5.ORDER_TYPE_SELL
            ),
            "price": price,
            "sl": body.sl,
            "tp": body.tp,
            "deviation": 20,
            "magic": 20261013,
            "comment": "Gomez dashboard DEMO",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        try:
            r = send_with_supported_filling(req, si)
        except ValueError as e:
            raise HTTPException(400, str(e))

        if r is None:
            raise HTTPException(
                502,
                f"MT5 order_send returned no result: {mt5.last_error()}"
            )

        if r.retcode not in (
            mt5.TRADE_RETCODE_DONE,
            mt5.TRADE_RETCODE_PLACED,
            mt5.TRADE_RETCODE_DONE_PARTIAL,
        ):
            raise HTTPException(
                400,
                f"MT5 rejected order ({r.retcode}): {r.comment}"
            )

        return {
            "ok": True,
            "ticket": int(r.order or r.deal),
            "retcode": r.retcode,
            "message": r.comment,
        }


# ------------------------------------------------------------
# PENDING ORDERS (limit / stop): place, move, cancel
# ------------------------------------------------------------

def send_pending(req, si):
    """Pending orders usually want RETURN filling; fall back to the
    others only on error 10030 (invalid fill)."""
    modes = [mt5.ORDER_FILLING_RETURN] + [
        m for m in supported_filling_modes(si)
        if m != mt5.ORDER_FILLING_RETURN
    ]
    last = None

    for mode in modes:
        req["type_filling"] = mode
        r = mt5.order_send(req)

        if r is None:
            return None

        last = r

        if int(r.retcode) == 10030:
            continue

        return r

    return last


@app.post("/api/pending")
def pending(
    body: PendingIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    side = body.side.lower()
    kind = body.kind.lower()

    if side not in ("buy", "sell"):
        raise HTTPException(400, "Side must be buy or sell.")

    if kind not in ("auto", "limit", "stop"):
        raise HTTPException(400, "Kind must be limit, stop or auto.")

    with session(body.login):
        symbol = body.symbol.strip()

        if not mt5.symbol_select(symbol, True):
            raise HTTPException(400, f"Broker symbol unavailable: {symbol}")

        si = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        if si is None or tick is None:
            raise HTTPException(400, "Could not read symbol quote.")

        vol = round(body.volume / si.volume_step) * si.volume_step

        if vol < si.volume_min or vol > min(si.volume_max, 1.0):
            raise HTTPException(
                400,
                f"Volume must be between {si.volume_min} and "
                f"{min(si.volume_max, 1.0)} lots for this symbol."
            )

        is_buy = side == "buy"
        price = round(body.price, int(si.digits))

        # Work out limit vs stop from where the price sits.
        if is_buy:
            below = price < tick.ask
            otype = (
                mt5.ORDER_TYPE_BUY_LIMIT if below
                else mt5.ORDER_TYPE_BUY_STOP
            )
        else:
            above = price > tick.bid
            otype = (
                mt5.ORDER_TYPE_SELL_LIMIT if above
                else mt5.ORDER_TYPE_SELL_STOP
            )

        is_limit = otype in (
            mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT
        )

        if kind == "limit" and not is_limit:
            raise HTTPException(
                400,
                "A limit order needs the price BELOW market for buys "
                "and ABOVE market for sells."
            )

        if kind == "stop" and is_limit:
            raise HTTPException(
                400,
                "A stop order needs the price ABOVE market for buys "
                "and BELOW market for sells."
            )

        if body.sl and (
            (is_buy and body.sl >= price)
            or (not is_buy and body.sl <= price)
        ):
            raise HTTPException(
                400, "SL must be on the losing side of the entry price."
            )

        if body.tp and (
            (is_buy and body.tp <= price)
            or (not is_buy and body.tp >= price)
        ):
            raise HTTPException(
                400, "TP must be on the winning side of the entry price."
            )

        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": vol,
            "type": otype,
            "price": price,
            "sl": body.sl,
            "tp": body.tp,
            "deviation": 20,
            "magic": 20261013,
            "comment": "Gomez dashboard DEMO pending",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        r = send_pending(req, si)

        if r is None:
            raise HTTPException(
                502, f"MT5 order_send returned no result: {mt5.last_error()}"
            )

        if r.retcode not in (
            mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED
        ):
            raise HTTPException(
                400, f"MT5 rejected order ({r.retcode}): {r.comment}"
            )

        return {
            "ok": True,
            "ticket": int(r.order),
            "type": PEND_NAMES.get(otype, "PENDING"),
            "message": r.comment,
        }


@app.post("/api/pending/modify")
def pending_modify(
    body: PendingModifyIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(body.login):
        found = mt5.orders_get(ticket=body.ticket)

        if not found:
            raise HTTPException(404, "Pending order not found.")

        o = found[0]
        si = mt5.symbol_info(o.symbol)
        digits = int(si.digits) if si else 2

        price = o.price_open if body.price is None else round(body.price, digits)
        sl = o.sl if body.sl is None else body.sl
        tp = o.tp if body.tp is None else body.tp

        buy = o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)

        if sl and ((buy and sl >= price) or (not buy and sl <= price)):
            raise HTTPException(
                400, "SL must be on the losing side of the entry price."
            )

        if tp and ((buy and tp <= price) or (not buy and tp >= price)):
            raise HTTPException(
                400, "TP must be on the winning side of the entry price."
            )

        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": o.ticket,
            "symbol": o.symbol,
            "price": price,
            "sl": sl,
            "tp": tp,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": o.type_filling,
        })

        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(
                400,
                "Pending update rejected: "
                f"{getattr(r, 'comment', mt5.last_error())}"
            )

        return {"ok": True, "message": "Pending order updated"}


@app.post("/api/cancel")
def cancel(
    body: CancelIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(body.login):
        found = mt5.orders_get(ticket=body.ticket)

        if not found:
            raise HTTPException(404, "Pending order not found.")

        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": found[0].ticket,
        })

        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(
                400,
                "Cancel rejected: "
                f"{getattr(r, 'comment', mt5.last_error())}"
            )

        return {"ok": True, "message": "Pending order cancelled"}


# ------------------------------------------------------------
# MODIFY STOP LOSS / TAKE PROFIT
# ------------------------------------------------------------

@app.post("/api/modify")
def modify(
    body: ModifyIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(body.login):
        found = mt5.positions_get(ticket=body.ticket)

        if not found:
            raise HTTPException(404, "Position ticket not found.")

        p = found[0]
        tick = mt5.symbol_info_tick(p.symbol)

        if tick is None:
            raise HTTPException(400, "Could not read symbol info.")

        buy = p.type == mt5.POSITION_TYPE_BUY

        if body.sl and (
            (buy and body.sl >= tick.bid)
            or (not buy and body.sl <= tick.ask)
        ):
            raise HTTPException(
                400, "SL is on the wrong side of the current market price."
            )

        if body.tp and (
            (buy and body.tp <= tick.bid)
            or (not buy and body.tp >= tick.ask)
        ):
            raise HTTPException(
                400, "TP is on the wrong side of the current market price."
            )

        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": p.symbol,
            "sl": body.sl,
            "tp": body.tp,
        })

        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(
                400,
                "SL/TP update rejected: "
                f"{getattr(r, 'comment', mt5.last_error())}"
            )

        return {"ok": True, "message": "SL/TP updated"}


# ------------------------------------------------------------
# CLOSE OR PARTIALLY CLOSE A POSITION
# ------------------------------------------------------------

def close_ticket(ticket, percent):
    ps = mt5.positions_get(ticket=ticket)

    if not ps:
        raise ValueError("Position ticket not found")

    p = ps[0]
    si = mt5.symbol_info(p.symbol)
    tick = mt5.symbol_info_tick(p.symbol)

    if si is None or tick is None:
        raise ValueError("No symbol quote")

    volume = (
        round(p.volume * percent / 100 / si.volume_step)
        * si.volume_step
    )

    if percent >= 99.999:
        volume = p.volume

    if volume < si.volume_min:
        raise ValueError(
            f"Partial close rounds below broker minimum "
            f"{si.volume_min} lots"
        )

    if volume >= p.volume and percent < 99.999:
        raise ValueError(
            "Partial close would close the full position; "
            "choose 100% instead"
        )

    typ = (
        mt5.ORDER_TYPE_SELL
        if p.type == mt5.POSITION_TYPE_BUY
        else mt5.ORDER_TYPE_BUY
    )

    price = tick.bid if typ == mt5.ORDER_TYPE_SELL else tick.ask

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": p.ticket,
        "symbol": p.symbol,
        "volume": volume,
        "type": typ,
        "price": price,
        "deviation": 20,
        "magic": 20261013,
        "comment": "Gomez dashboard DEMO close",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    r = send_with_supported_filling(req, si)

    if r is None:
        raise ValueError(
            f"Close order_send returned no result: {mt5.last_error()}"
        )

    if r.retcode not in (
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
    ):
        raise ValueError(
            f"Close rejected ({r.retcode}): {r.comment}"
        )

    return int(r.deal or r.order)


@app.post("/api/close")
def close(
    body: CloseIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(body.login):
        try:
            ticket = close_ticket(body.ticket, body.percent)
        except ValueError as e:
            raise HTTPException(400, str(e))

        return {
            "ok": True,
            "ticket": ticket,
            "message": f"Demo close request sent ({body.percent:g}%).",
        }


@app.post("/api/close-all")
def close_all(
    body: CloseAllIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)

    with session(body.login):
        closed = 0
        failed = 0
        errors = []

        for p in (mt5.positions_get() or []):
            try:
                close_ticket(int(p.ticket), 100)
                closed += 1
            except Exception as e:
                failed += 1
                errors.append(f"{p.ticket}: {e}")

        return {
            "ok": failed == 0,
            "closed": closed,
            "failed": failed,
            "errors": errors,
        }


# ------------------------------------------------------------
# COPY TRADING: master account -> slave account (DEMO ONLY)
#
# A background thread watches the MASTER account and mirrors every
# change onto the SLAVE account:
#   - new position       -> opened on the slave with an ADAPTED lot size
#   - SL / TP changed    -> SL / TP changed on the slave
#   - partial close      -> same share closed on the slave
#   - full close         -> closed on the slave
#   - pending orders     -> mirrored (limit / stop), moved, cancelled
#
# Every copied trade carries magic COPY_MAGIC and the comment "CP<master
# ticket>", so the links survive a bridge restart (they are rebuilt from
# the slave's open positions) and a trade is never copied twice.
#
# MT5 can only be logged into ONE account at a time per terminal, so the
# copier reads the master every <interval> seconds and only switches to the
# slave when there is something to do.
# ------------------------------------------------------------

COPY_MAGIC = 20261014
COPY_FILE = BASE / "copy_settings.json"

PEND_TYPES = (
    mt5.ORDER_TYPE_BUY_LIMIT,
    mt5.ORDER_TYPE_SELL_LIMIT,
    mt5.ORDER_TYPE_BUY_STOP,
    mt5.ORDER_TYPE_SELL_STOP,
)
BUY_PEND_TYPES = (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)

COPY_DEFAULTS = {
    "master": 0,
    "slave": 0,
    # fixed_risk | risk_factor | balance_ratio | multiplier | fixed_lot
    "mode": "fixed_risk",
    "risk_pct": 1.0,        # fixed_risk: % of MY balance risked per trade
    "risk_factor": 0.1,     # risk_factor: my risk % = master risk % x this
    "ratio_mult": 1.0,      # balance_ratio: extra multiplier
    "lot_mult": 1.0,        # multiplier: master lots x this
    "fixed_lot": 0.01,      # fixed_lot: lots per trade
    "risk_basis": "balance",  # balance | equity
    "max_lot": 1.0,         # cap per copied trade (bridge hard cap is 1.0)
    "below_min": "min",     # min | skip  (lots below the broker minimum)
    "no_sl": "balance",     # balance | skip (risk modes, master has no SL)
    "copy_sltp": True,
    "copy_pending": True,
    "copy_existing": False,  # also copy trades already open at start
    "dry_run": False,        # log only, send no orders
    "symbol_map": "",        # e.g. "XAUUSD=GOLD, US30=DJ30"
    "interval": 2.0,         # seconds between master checks
}


def _load_copy_cfg():
    cfg = dict(COPY_DEFAULTS)

    try:
        saved = json.loads(COPY_FILE.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in saved.items() if k in COPY_DEFAULTS})
    except Exception:
        pass

    logins = [int(t["login"]) for t in TERMINALS]

    if int(cfg["master"]) not in logins:
        cfg["master"] = logins[0]

    if (
        int(cfg["slave"]) not in logins
        or int(cfg["slave"]) == int(cfg["master"])
    ):
        cfg["slave"] = next(
            (x for x in logins if x != int(cfg["master"])), logins[0]
        )

    return cfg


def _save_copy_cfg(cfg):
    try:
        COPY_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print("Could not save copy settings:", e)


CSTATE = threading.RLock()  # protects the copier's bookkeeping only

COPY = {
    "running": False,
    "thread": None,
    "stop": threading.Event(),
    "cfg": _load_copy_cfg(),
    "pos": {},    # master position ticket -> link dict
    "pend": {},   # master pending ticket  -> link dict
    "log": deque(maxlen=300),
    "stats": {
        "opened": 0, "closed": 0, "partial": 0,
        "modified": 0, "pending": 0, "skipped": 0, "errors": 0,
    },
    "last": {
        "poll": 0, "error": "", "m_bal": None, "s_bal": None,
        "m_ccy": "", "s_ccy": "", "m_open": 0,
    },
    "sym": {"login": None, "names": set(), "ts": 0.0},
}


def clog(level, msg):
    with CSTATE:
        COPY["log"].append({"t": int(time.time()), "lv": level, "msg": msg})

        if level == "err":
            COPY["stats"]["errors"] += 1

    print(f"[copy {level}] {msg}")


def stat(name, n=1):
    with CSTATE:
        COPY["stats"][name] += n


# ---------- helpers ----------

def parse_cp(comment):
    m = re.match(r"^CP(\d+)", comment or "")
    return int(m.group(1)) if m else None


def parse_symbol_map(text):
    out = {}

    for part in re.split(r"[,\n;]+", text or ""):
        if "=" in part:
            a, b = (x.strip() for x in part.split("=", 1))

            if a and b:
                out[a.upper()] = b

    return out


def slave_symbol_names(login):
    c = COPY["sym"]

    if c["login"] != login or time.time() - c["ts"] > 600:
        c["names"] = {s.name for s in (mt5.symbols_get() or [])}
        c["login"] = login
        c["ts"] = time.time()

    return c["names"]


def map_symbol(msym, cfg, login):
    """Find the slave broker's name for a master symbol (suffixes like
    .a / m / # are handled automatically, odd names via Symbol mapping)."""
    names = slave_symbol_names(login)
    target = parse_symbol_map(cfg.get("symbol_map")).get(msym.upper())

    if target and target in names:
        return target

    if msym in names:
        return msym

    stem = re.split(r"[._#-]", msym)[0]

    if stem in names:
        return stem

    base = re.sub(r"[^A-Za-z0-9]", "", stem).upper()

    if len(base) < 3:
        return None

    cands = sorted(
        (
            n for n in names
            if re.sub(r"[^A-Za-z0-9]", "", n).upper().startswith(base)
        ),
        key=len,
    )

    return cands[0] if cands else None


def tick_spec(si):
    """(tick size, money lost per tick per 1 lot) for a symbol."""
    ts = float(si.trade_tick_size or si.point or 0)
    tv = float(
        getattr(si, "trade_tick_value_loss", 0)
        or si.trade_tick_value
        or 0
    )
    return ts, tv


def risk_money(vol, entry, sl, ts, tv):
    """Money lost if the stop loss is hit (account currency)."""
    if not sl or not ts or not tv or not entry:
        return None

    return vol * abs(entry - sl) / ts * tv


def base_of(acc, cfg):
    v = acc.equity if cfg.get("risk_basis") == "equity" else acc.balance
    return float(v or 0)


def size_for_slave(cfg, m_base, s_base, m_spec, s_spec, vol, entry, sl):
    """Work out the lot size for the slave. Returns (lots or None, note)."""
    mode = cfg["mode"]

    def ratio():
        r = (s_base / m_base) if m_base > 0 else 1.0
        return vol * r * float(cfg["ratio_mult"]), f"balance ratio x{r:.3f}"

    if mode == "fixed_lot":
        return float(cfg["fixed_lot"]), "fixed lot"

    if mode == "multiplier":
        return vol * float(cfg["lot_mult"]), f"x{float(cfg['lot_mult']):g} lots"

    if mode == "balance_ratio":
        return ratio()

    # ----- risk based modes -----
    m_risk = risk_money(vol, entry, sl, m_spec[0], m_spec[1])

    if m_risk is None or not s_spec[0] or not s_spec[1]:
        if cfg["no_sl"] == "skip":
            return None, "master trade has no stop loss"

        lots, note = ratio()
        return lots, "no SL on master, used " + note

    m_pct = (m_risk / m_base * 100) if m_base > 0 else 0.0

    if mode == "fixed_risk":
        target = float(cfg["risk_pct"])
    else:
        target = m_pct * float(cfg["risk_factor"])

    per_lot = abs(entry - sl) / s_spec[0] * s_spec[1]

    if per_lot <= 0 or s_base <= 0:
        return None, "could not compute risk on the slave"

    lots = (s_base * target / 100.0) / per_lot

    return lots, f"master risk {m_pct:.2f}% -> my risk {target:.2f}%"


def finalize_volume(vol, si, cfg):
    """Round to the broker's step, apply caps. Returns (lots or None, note)."""
    step = float(si.volume_step or 0.01)
    cap = min(float(cfg["max_lot"]), 1.0, float(si.volume_max))
    notes = []

    if vol > cap:
        vol = cap
        notes.append("capped at max lot")

    v = math.floor(vol / step + 1e-9) * step

    if v < float(si.volume_min) - 1e-12:
        if cfg["below_min"] == "min":
            v = float(si.volume_min)
            notes.append("raised to broker minimum")
        else:
            return None, "below broker minimum lot"

    decimals = max(0, int(round(-math.log10(step)))) if step > 0 else 2

    return round(v, decimals), ", ".join(notes)


def sltp_valid(buy, sl, tp, ref):
    """Is SL / TP on the correct side of the reference price?"""
    sl_ok = (not sl) or (sl < ref if buy else sl > ref)
    tp_ok = (not tp) or (tp > ref if buy else tp < ref)
    return sl_ok, tp_ok


def slave_pos_for(t, symbol=None):
    ps = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()

    for sp in ps or []:
        if sp.magic == COPY_MAGIC and parse_cp(sp.comment) == t:
            return sp

    return None


def snap_pos(p):
    return {
        "ticket": int(p.ticket),
        "symbol": p.symbol,
        "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
        "volume": float(p.volume),
        "entry": float(p.price_open),
        "sl": float(p.sl),
        "tp": float(p.tp),
        "magic": int(p.magic),
    }


def snap_ord(o):
    return {
        "ticket": int(o.ticket),
        "symbol": o.symbol,
        "type": int(o.type),
        "side": "buy" if o.type in BUY_PEND_TYPES else "sell",
        "volume": float(o.volume_current),
        "price": float(o.price_open),
        "sl": float(o.sl),
        "tp": float(o.tp),
        "magic": int(o.magic),
    }


def new_link(symbol, side, vol, sl, tp, slave=None, svol=None, ignored=False):
    return {
        "slave": slave, "svol": svol, "vol": vol, "sl": sl, "tp": tp,
        "symbol": symbol, "side": side, "miss": 0, "tries": 0,
        "ctries": 0, "ignored": ignored,
    }


# ---------- executing actions on the slave ----------

def cancel_slave_order(ticket):
    found = mt5.orders_get(ticket=ticket)

    if not found:
        return False

    r = mt5.order_send({
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": found[0].ticket,
    })

    return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE


def c_open(ctx, t):
    cfg = ctx["cfg"]
    p = ctx["mpos"][t]
    st = COPY["pos"][t]
    buy = p["side"] == "buy"
    sym = map_symbol(p["symbol"], cfg, cfg["slave"])

    if not sym:
        st["tries"] = 99
        stat("skipped")
        clog(
            "err",
            f"No matching symbol for {p['symbol']} on the slave account. "
            "Add it under Symbol mapping."
        )
        return

    mt5.symbol_select(sym, True)
    si = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)

    if si is None or tick is None:
        st["tries"] += 1
        clog("warn", f"No quote for {sym} on the slave yet "
                     f"(try {st['tries']}/3).")
        return

    # Already copied (for example a mirrored pending order that filled)?
    ex = slave_pos_for(t, sym)

    if ex:
        st["slave"] = int(ex.ticket)
        st["svol"] = float(ex.volume)
        COPY["pend"].pop(t, None)
        clog("info", f"{sym} master #{t} already exists on the slave "
                     f"(#{ex.ticket}), linked.")
        return

    # The master's pending order filled: drop our mirrored pending, because
    # we now open this position at market instead.
    ost = COPY["pend"].pop(t, None)

    if ost and ost.get("slave") and ost["slave"] > 0 and not cfg["dry_run"]:
        cancel_slave_order(ost["slave"])

    lots, note = size_for_slave(
        cfg,
        ctx["m_base"],
        base_of(mt5.account_info(), cfg),
        ctx["specs"].get(p["symbol"], (0, 0)),
        tick_spec(si),
        p["volume"], p["entry"], p["sl"],
    )

    if lots is not None:
        lots, note2 = finalize_volume(lots, si, cfg)

        if lots is None:
            note = note2
        elif note2:
            note = f"{note}; {note2}"

    if lots is None:
        st["tries"] = 99
        stat("skipped")
        clog("warn", f"Skipped {p['side'].upper()} {p['symbol']}: {note}.")
        return

    sl = p["sl"] if cfg["copy_sltp"] else 0.0
    tp = p["tp"] if cfg["copy_sltp"] else 0.0
    price = tick.ask if buy else tick.bid
    sl_ok, tp_ok = sltp_valid(buy, sl, tp, price)

    if not sl_ok:
        st["tries"] = 99
        stat("skipped")
        clog("warn", f"Skipped {p['side'].upper()} {sym}: the master's stop "
                     "loss is already beyond the slave's price.")
        return

    if not tp_ok:
        tp = 0.0
        clog("warn", f"{sym}: master TP is beyond the slave's price, "
                     "opened without TP.")

    if cfg["dry_run"]:
        st["slave"] = -1
        st["svol"] = lots
        clog("info", f"[TEST] would open {p['side'].upper()} {lots} {sym} "
                     f"(master {p['volume']} lots, {note}).")
        return

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": lots,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 30,
        "magic": COPY_MAGIC,
        "comment": f"CP{t}",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    try:
        r = send_with_supported_filling(req, si)
    except ValueError as e:
        st["tries"] += 1
        clog("err", f"Open {sym} failed: {e}")
        return

    if r is None or r.retcode not in (
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_PLACED,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
    ):
        st["tries"] += 1
        why = (
            f"{r.retcode}: {r.comment}" if r is not None
            else str(mt5.last_error())
        )
        clog("err", f"Open {p['side'].upper()} {sym} rejected ({why}), "
                    f"try {st['tries']}/3.")
        return

    sp = slave_pos_for(t, sym)
    st["slave"] = int(sp.ticket) if sp else int(r.order or r.deal)
    st["svol"] = lots
    stat("opened")
    clog("ok", f"Opened {p['side'].upper()} {lots} {sym} @ {price} "
               f"(master {p['volume']} lots; {note}).")


def c_close(ctx, t):
    cfg = ctx["cfg"]
    st = COPY["pos"][t]
    s = st["slave"]

    if s and s > 0 and not cfg["dry_run"]:
        try:
            close_ticket(s, 100)
            stat("closed")
            clog("ok", f"Closed {st['symbol']} (master closed #{t}).")
        except ValueError as e:
            if "not found" in str(e).lower():
                clog("info", f"{st['symbol']}: already closed on the slave.")
            else:
                st["ctries"] += 1
                clog("err", f"Close {st['symbol']} failed: {e} "
                            f"(try {st['ctries']}/5).")

                if st["ctries"] < 5:
                    return
    elif s == -1:
        clog("info", f"[TEST] would close {st['symbol']} "
                     f"(master closed #{t}).")

    COPY["pos"].pop(t, None)


def c_partial(ctx, t, old, new):
    cfg = ctx["cfg"]
    st = COPY["pos"][t]
    s = st["slave"]
    pct = (1 - new / old) * 100
    st["vol"] = new

    if not s or s < 0:
        clog("info", f"[TEST] would close {pct:.0f}% of {st['symbol']}.")
        return

    try:
        close_ticket(s, pct)
        stat("partial")
        clog("ok", f"Closed {pct:.0f}% of {st['symbol']} "
                   f"(master partially closed #{t}).")
    except ValueError as e:
        if "not found" in str(e).lower():
            clog("info", f"{st['symbol']}: slave position already closed.")
        else:
            clog("err", f"Partial close {st['symbol']} failed: {e}")


def c_sltp(ctx, t):
    cfg = ctx["cfg"]
    st = COPY["pos"][t]
    p = ctx["mpos"][t]
    s = st["slave"]
    st["sl"], st["tp"] = p["sl"], p["tp"]

    if not s or s < 0:
        clog("info", f"[TEST] would set SL/TP on {st['symbol']} to "
                     f"{p['sl']}/{p['tp']}.")
        return

    ps = mt5.positions_get(ticket=s)

    if not ps:
        clog("info", f"{st['symbol']}: slave position is already closed.")
        st["slave"] = None
        st["tries"] = 99
        return

    sp = ps[0]
    buy = sp.type == mt5.POSITION_TYPE_BUY
    tick = mt5.symbol_info_tick(sp.symbol)

    if tick is None:
        return

    sl_ok, tp_ok = sltp_valid(
        buy, p["sl"], p["tp"], tick.bid if buy else tick.ask
    )

    if not sl_ok or not tp_ok:
        clog("warn", f"{sp.symbol}: SL/TP update skipped, a level is on the "
                     "wrong side of the slave's price.")

    r = mt5.order_send({
        "action": mt5.TRADE_ACTION_SLTP,
        "position": sp.ticket,
        "symbol": sp.symbol,
        "sl": p["sl"] if sl_ok else sp.sl,
        "tp": p["tp"] if tp_ok else sp.tp,
    })

    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        clog("err", f"SL/TP update on {sp.symbol} rejected: "
                    f"{getattr(r, 'comment', mt5.last_error())}")
        return

    stat("modified")
    clog("ok", f"Updated SL/TP on {sp.symbol} (SL {p['sl'] or '-'}, "
               f"TP {p['tp'] or '-'}).")


def c_pnew(ctx, t):
    cfg = ctx["cfg"]
    o = ctx["mord"][t]
    st = COPY["pend"][t]
    sym = map_symbol(o["symbol"], cfg, cfg["slave"])

    if not sym:
        st["tries"] = 99
        stat("skipped")
        clog("err", f"No matching symbol for {o['symbol']} on the slave "
                    "account (pending order not copied).")
        return

    mt5.symbol_select(sym, True)
    si = mt5.symbol_info(sym)

    if si is None:
        st["tries"] += 1
        return

    lots, note = size_for_slave(
        cfg,
        ctx["m_base"],
        base_of(mt5.account_info(), cfg),
        ctx["specs"].get(o["symbol"], (0, 0)),
        tick_spec(si),
        o["volume"], o["price"], o["sl"],
    )

    if lots is not None:
        lots, note2 = finalize_volume(lots, si, cfg)

        if lots is None:
            note = note2

    if lots is None:
        st["tries"] = 99
        stat("skipped")
        clog("warn", f"Skipped pending {o['symbol']}: {note}.")
        return

    price = round(o["price"], int(si.digits))
    sl = o["sl"] if cfg["copy_sltp"] else 0.0
    tp = o["tp"] if cfg["copy_sltp"] else 0.0
    name = PEND_NAMES.get(o["type"], "PENDING")

    if cfg["dry_run"]:
        st["slave"] = -1
        clog("info", f"[TEST] would place {name} {lots} {sym} @ {price}.")
        return

    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": sym,
        "volume": lots,
        "type": o["type"],
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 30,
        "magic": COPY_MAGIC,
        "comment": f"CP{t}",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    r = send_pending(req, si)

    if r is None or r.retcode not in (
        mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED
    ):
        st["tries"] += 1
        why = (
            f"{r.retcode}: {r.comment}" if r is not None
            else str(mt5.last_error())
        )
        clog("err", f"Pending {name} {sym} rejected ({why}), "
                    f"try {st['tries']}/3.")
        return

    st["slave"] = int(r.order)
    stat("pending")
    clog("ok", f"Placed {name} {lots} {sym} @ {price} ({note}).")


def c_pmod(ctx, t):
    cfg = ctx["cfg"]
    o = ctx["mord"][t]
    st = COPY["pend"][t]
    s = st["slave"]
    st["price"], st["sl"], st["tp"] = o["price"], o["sl"], o["tp"]

    if not s or s < 0:
        return

    found = mt5.orders_get(ticket=s)

    if not found:
        st["slave"] = None
        st["tries"] = 99
        return

    so = found[0]
    si = mt5.symbol_info(so.symbol)
    digits = int(si.digits) if si else 2

    r = mt5.order_send({
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": so.ticket,
        "symbol": so.symbol,
        "price": round(o["price"], digits),
        "sl": o["sl"] if cfg["copy_sltp"] else so.sl,
        "tp": o["tp"] if cfg["copy_sltp"] else so.tp,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": so.type_filling,
    })

    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        clog("err", f"Pending update on {so.symbol} rejected: "
                    f"{getattr(r, 'comment', mt5.last_error())}")
        return

    stat("modified")
    clog("ok", f"Moved pending order on {so.symbol} to {o['price']}.")


def c_pdel(ctx, t):
    cfg = ctx["cfg"]
    st = COPY["pend"].get(t)

    if not st:
        return

    s = st["slave"]

    if s and s > 0 and not cfg["dry_run"]:
        if cancel_slave_order(s):
            clog("ok", f"Cancelled pending order on {st['symbol']} "
                       "(master cancelled it).")

    COPY["pend"].pop(t, None)


# ---------- one pass of the copier ----------

def STOP_NOW():
    return threading.current_thread() is not COPY["thread"] or not COPY["running"]


def copy_cycle():
    cfg = dict(COPY["cfg"])
    m_login, s_login = int(cfg["master"]), int(cfg["slave"])

    # ----- 1. read the master -----
    with LOCK:
        was = STATE["login"]
        _, m_acc = ensure(m_login)

        if was != m_login:
            time.sleep(0.4)  # let the terminal finish syncing this account
            m_acc = mt5.account_info() or m_acc

        mpos = {
            int(p.ticket): snap_pos(p)
            for p in (mt5.positions_get() or [])
            if int(p.magic) != COPY_MAGIC
        }
        mord = {
            int(o.ticket): snap_ord(o)
            for o in (mt5.orders_get() or [])
            if o.type in PEND_TYPES and int(o.magic) != COPY_MAGIC
        }
        specs = {}

        for sym in {p["symbol"] for p in mpos.values()} | {
            o["symbol"] for o in mord.values()
        }:
            si = mt5.symbol_info(sym)

            if si is not None:
                specs[sym] = tick_spec(si)

        m_base = base_of(m_acc, cfg)

    with CSTATE:
        COPY["last"].update({
            "poll": int(time.time()), "m_bal": float(m_acc.balance),
            "m_ccy": m_acc.currency, "m_open": len(mpos),
        })

    if STOP_NOW():
        return

    # ----- 2. decide what changed -----
    actions = []

    with CSTATE:
        P, O = COPY["pos"], COPY["pend"]

        for t, p in mpos.items():
            st = P.get(t)

            if st is None:
                P[t] = new_link(p["symbol"], p["side"], p["volume"],
                                p["sl"], p["tp"])
                actions.append(("open", t))
                continue

            st["miss"] = 0

            if st["ignored"]:
                continue

            if st["slave"] is None:
                if st["tries"] < 3:
                    actions.append(("open", t))

                continue

            if p["volume"] < st["vol"] - 1e-9:
                actions.append(("partial", t, st["vol"], p["volume"]))
            elif p["volume"] > st["vol"] + 1e-9:
                st["vol"] = p["volume"]

            if cfg["copy_sltp"] and (
                abs(p["sl"] - st["sl"]) > 1e-9
                or abs(p["tp"] - st["tp"]) > 1e-9
            ):
                actions.append(("sltp", t))

        for t, st in list(P.items()):
            if t in mpos:
                continue

            st["miss"] += 1

            # A position must be missing twice in a row before we close the
            # copy, so one bad read can never close your slave trades.
            if st["miss"] >= 2:
                if st["slave"] is None or st["ignored"]:
                    P.pop(t, None)
                else:
                    actions.append(("close", t))

        for t, o in mord.items():
            st = O.get(t)

            if st is None:
                O[t] = new_link(o["symbol"], o["side"], o["volume"],
                                o["sl"], o["tp"])
                O[t]["price"] = o["price"]

                if cfg["copy_pending"]:
                    actions.append(("pnew", t))

                continue

            st["miss"] = 0

            if st["ignored"] or not cfg["copy_pending"]:
                continue

            if st["slave"] is None:
                if st["tries"] < 3:
                    actions.append(("pnew", t))

                continue

            if (
                abs(o["price"] - st.get("price", o["price"])) > 1e-9
                or abs(o["sl"] - st["sl"]) > 1e-9
                or abs(o["tp"] - st["tp"]) > 1e-9
            ):
                actions.append(("pmod", t))

        for t, st in list(O.items()):
            if t in mord:
                continue

            st["miss"] += 1

            if st["miss"] >= 2:
                if st["slave"] is None or st["ignored"]:
                    O.pop(t, None)
                else:
                    actions.append(("pdel", t))

    if not actions:
        return

    # open first, then partial/sltp, then close, then pending housekeeping
    order = {"open": 0, "partial": 1, "sltp": 2, "close": 3,
             "pnew": 4, "pmod": 5, "pdel": 6}
    actions.sort(key=lambda a: order[a[0]])

    if STOP_NOW():
        return

    # ----- 3. apply on the slave -----
    with LOCK:
        was = STATE["login"]
        _, s_acc = ensure(s_login)

        if was != s_login:
            time.sleep(0.4)

        s_acc = mt5.account_info() or s_acc
        ctx = {
            "cfg": cfg, "mpos": mpos, "mord": mord,
            "specs": specs, "m_base": m_base,
        }

        with CSTATE:
            COPY["last"].update({
                "s_bal": float(s_acc.balance), "s_ccy": s_acc.currency,
            })

        for a in actions:
            try:
                kind, t = a[0], a[1]

                if kind == "open":
                    c_open(ctx, t)
                elif kind == "partial":
                    c_partial(ctx, t, a[2], a[3])
                elif kind == "sltp":
                    c_sltp(ctx, t)
                elif kind == "close":
                    c_close(ctx, t)
                elif kind == "pnew":
                    c_pnew(ctx, t)
                elif kind == "pmod":
                    c_pmod(ctx, t)
                elif kind == "pdel":
                    c_pdel(ctx, t)
            except Exception as e:
                clog("err", f"{a[0]} #{a[1]} failed: {e}")


def copy_prepare(cfg):
    """Called once when copying starts: rebuild the links from the slave's
    open copies and baseline the master's current trades."""
    with LOCK:
        _, s_acc = ensure(int(cfg["slave"]))
        time.sleep(0.2)
        spos = {}

        for p in (mt5.positions_get() or []):
            t = parse_cp(p.comment)

            if p.magic == COPY_MAGIC and t:
                spos[t] = p

        sord = {}

        for o in (mt5.orders_get() or []):
            t = parse_cp(o.comment)

            if o.magic == COPY_MAGIC and t:
                sord[t] = o

        _, m_acc = ensure(int(cfg["master"]))
        time.sleep(0.4)
        mpos = [
            snap_pos(p) for p in (mt5.positions_get() or [])
            if int(p.magic) != COPY_MAGIC
        ]
        mord = [
            snap_ord(o) for o in (mt5.orders_get() or [])
            if o.type in PEND_TYPES and int(o.magic) != COPY_MAGIC
        ]

    with CSTATE:
        P, O = COPY["pos"], COPY["pend"]
        P.clear()
        O.clear()

        for p in mpos:
            t = p["ticket"]
            sp = spos.get(t)

            if sp is not None:
                P[t] = new_link(p["symbol"], p["side"], p["volume"],
                                p["sl"], p["tp"], int(sp.ticket),
                                float(sp.volume))
            elif not cfg["copy_existing"]:
                P[t] = new_link(p["symbol"], p["side"], p["volume"],
                                p["sl"], p["tp"], ignored=True)

        for o in mord:
            t = o["ticket"]
            so = sord.get(t)

            if so is not None:
                O[t] = new_link(o["symbol"], o["side"], o["volume"],
                                o["sl"], o["tp"], int(so.ticket))
                O[t]["price"] = o["price"]
            elif not (cfg["copy_existing"] and cfg["copy_pending"]):
                O[t] = new_link(o["symbol"], o["side"], o["volume"],
                                o["sl"], o["tp"], ignored=True)
                O[t]["price"] = o["price"]

        COPY["last"].update({
            "poll": int(time.time()), "error": "",
            "m_bal": float(m_acc.balance), "m_ccy": m_acc.currency,
            "s_bal": float(s_acc.balance), "s_ccy": s_acc.currency,
            "m_open": len(mpos),
        })

    linked = len(spos) + len(sord)
    skipped = sum(1 for v in COPY["pos"].values() if v["ignored"])
    clog("info", f"Copy started. {linked} existing copy(ies) re-linked, "
                 f"{skipped} trade(s) already open on the master ignored.")


def copy_loop(stop):
    last_err = ""

    while not stop.is_set():
        t0 = time.time()

        try:
            copy_cycle()
            last_err = ""

            with CSTATE:
                COPY["last"]["error"] = ""
        except HTTPException as e:
            err = str(e.detail)
        except Exception as e:  # keep the copier alive no matter what
            err = f"{type(e).__name__}: {e}"
        else:
            err = ""

        if err:
            with CSTATE:
                COPY["last"]["error"] = err

            if err != last_err:  # do not spam the same message
                clog("err", err)
                last_err = err

        wait = max(0.3, float(COPY["cfg"].get("interval", 2)) -
                   (time.time() - t0))
        stop.wait(wait)

    with CSTATE:
        if COPY["thread"] is threading.current_thread():
            COPY["running"] = False

    clog("info", "Copy stopped.")


# ---------- API ----------

class CopyCfgIn(BaseModel):
    master: int
    slave: int
    mode: Literal[
        "fixed_risk", "risk_factor", "balance_ratio", "multiplier",
        "fixed_lot",
    ] = "fixed_risk"
    risk_pct: float = Field(1.0, gt=0, le=50)
    risk_factor: float = Field(0.1, gt=0, le=20)
    ratio_mult: float = Field(1.0, gt=0, le=100)
    lot_mult: float = Field(1.0, gt=0, le=100)
    fixed_lot: float = Field(0.01, gt=0, le=1.0)
    risk_basis: Literal["balance", "equity"] = "balance"
    max_lot: float = Field(1.0, gt=0, le=1.0)
    below_min: Literal["min", "skip"] = "min"
    no_sl: Literal["balance", "skip"] = "balance"
    copy_sltp: bool = True
    copy_pending: bool = True
    copy_existing: bool = False
    dry_run: bool = False
    symbol_map: str = Field("", max_length=500)
    interval: float = Field(2.0, ge=1, le=30)


class CopyCheckIn(BaseModel):
    master: int
    slave: int


def copy_validate(body: CopyCfgIn):
    terminal_cfg(body.master)
    terminal_cfg(body.slave)

    if int(body.master) == int(body.slave):
        raise HTTPException(
            400, "Master and slave must be two different accounts."
        )

    return body.dict() if hasattr(body, "dict") else body.model_dump()


def copy_status_dict():
    with CSTATE:
        links = []

        for t, st in (COPY["pos"].items() if COPY["running"] else []):
            if st["ignored"] or st["slave"] is None:
                continue

            links.append({
                "master": t, "symbol": st["symbol"], "side": st["side"],
                "m_vol": st["vol"], "slave": st["slave"],
                "s_vol": st.get("svol"),
            })

        return {
            "running": COPY["running"],
            "cfg": dict(COPY["cfg"]),
            "stats": dict(COPY["stats"]),
            "last": dict(COPY["last"]),
            "links": links,
            "ignored": sum(1 for v in COPY["pos"].values() if v["ignored"]),
            "pending_links": sum(
                1 for v in COPY["pend"].values()
                if v["slave"] and not v["ignored"]
            ),
            "log": list(COPY["log"])[-80:][::-1],
            "now": int(time.time()),
        }


@app.get("/api/copy/status")
def copy_status(x_gomez_key: Optional[str] = Header(default=None)):
    authorized(x_gomez_key)
    return copy_status_dict()


@app.post("/api/copy/config")
def copy_config(
    body: CopyCfgIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)
    cfg = copy_validate(body)

    with CSTATE:
        if COPY["running"] and (
            int(cfg["master"]) != int(COPY["cfg"]["master"])
            or int(cfg["slave"]) != int(COPY["cfg"]["slave"])
        ):
            raise HTTPException(
                400, "Stop copying before changing the master or slave."
            )

        COPY["cfg"] = cfg

    _save_copy_cfg(cfg)
    clog("info", "Copy settings saved.")
    return {"ok": True}


@app.post("/api/copy/start")
def copy_start(
    body: CopyCfgIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    authorized(x_gomez_key)
    cfg = copy_validate(body)

    with CSTATE:
        if COPY["running"]:
            raise HTTPException(400, "Copying is already running.")

    old = COPY["thread"]

    if old is not None and old.is_alive():
        old.join(timeout=20)

    COPY["cfg"] = cfg
    _save_copy_cfg(cfg)
    copy_prepare(cfg)  # raises if an account cannot be reached

    COPY["stop"] = threading.Event()
    th = threading.Thread(
        target=copy_loop, args=(COPY["stop"],), name="copier", daemon=True
    )

    with CSTATE:
        COPY["running"] = True
        COPY["thread"] = th

    th.start()
    return {"ok": True}


@app.post("/api/copy/stop")
def copy_stop(x_gomez_key: Optional[str] = Header(default=None)):
    authorized(x_gomez_key)
    COPY["stop"].set()

    with CSTATE:
        COPY["running"] = False

    clog("info", "Copy stop requested.")
    return {"ok": True}


@app.post("/api/copy/clear-log")
def copy_clear_log(x_gomez_key: Optional[str] = Header(default=None)):
    authorized(x_gomez_key)

    with CSTATE:
        COPY["log"].clear()
        for k in COPY["stats"]:
            COPY["stats"][k] = 0

    return {"ok": True}


@app.post("/api/copy/check")
def copy_check(
    body: CopyCheckIn,
    x_gomez_key: Optional[str] = Header(default=None),
):
    """Log in to both accounts once and report balances, so you can see
    that both are reachable before you start copying."""
    authorized(x_gomez_key)
    out = []

    for role, login in (("master", body.master), ("slave", body.slave)):
        try:
            with session(login) as (c, a):
                d = account_dict(c, a)
                d.update({"role": role, "ok": True})
                out.append(d)
        except HTTPException as e:
            out.append({
                "role": role, "login": int(login), "ok": False,
                "error": str(e.detail),
            })

    return {"accounts": out}


@app.on_event("shutdown")
def _stop_copier():
    COPY["stop"].set()
