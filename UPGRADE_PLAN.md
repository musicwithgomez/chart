# Goldmine: analysis and upgrade plan

## 1. What you have today

```
MT5 terminal (Windows)  ⇄  app.py (FastAPI bridge, :8765)  ⇄  terminal.html (browser)
                                      ⇅ Cloudflare tunnel        ⇅ Firebase (login + settings)
```

**What already works well**
- `app.py` is solid. It picks the filling mode properly, guards against live accounts, checks which side SL/TP must be on, and does copy trading with five risk-adapted sizing modes that survive a restart (`CP<ticket>` comments).
- `/api/live` already bundles the quote, bars, positions and account into one call. That was a good design choice.
- The terminal has many features: draggable lines, layered entries, basket breakeven, Fibonacci, drawings and copy trading.

**What holds it back**

| # | Problem | Where | Impact |
|---|---------|-------|--------|
| 1 | `terminal.html` is 286 KB and 2,745 lines, with about 12 `<style>` blocks that override each other ("FIXES", "final overrides", "layoutFixes", "hdrfit"…) | terminal.html | Every change risks breaking something else. It is very hard to maintain. |
| 2 | Dark mode works by **re-mapping every colour at runtime in JavaScript** (the HSL interpolation script at the top, plus about 300 lines of `#darkcss`) | terminal.html:6-34, 730-1027 | Slow, fragile, and impossible to theme for clients |
| 3 | A **hand-written canvas chart engine** (about 1,000 lines for axes, zoom, pan and labels) | terminal.html:1206-1430 | You maintain a chart library instead of a product |
| 4 | The terminal offers **21 timeframes, but the bridge only supported 9**. M2, H2, W1 and others returned "Unknown timeframe". | terminal.html:1067 vs app.py `TF` | Bug. ✅ **Fixed in app.py.** |
| 5 | Six copies of the terminal (`terminal (1).html`, `(2)`, `Oldest`, `DrawingTools`, plus `files.zip`) | backend/ | No version control. Use git. |
| 6 | **One MT5 terminal serves 4 accounts.** Switching account means `mt5.login()` behind a global lock. When copy trading runs, it flips the terminal between master and slave every 2 seconds while the UI polls a third account. | app.py `ensure()`, `copy_cycle()` | Slow account switching, missed ticks, race-prone |
| 7 | Polling every 700 ms over HTTP | terminal.html `startPoll` | Latency, wasted CPU, poor fit for a tunnel |
| 8 | CORS has `allow_origin_regex=r"https://[A-Za-z0-9.-]+"`, which **lets any HTTPS website call your bridge**. Only the single shared API key protects it, and that key sits in localStorage. The bridge also binds to `0.0.0.0` and is published through a tunnel. | app.py:98 | Security. One leaked key gives full trading control. |
| 9 | `gomez-private-key-BACKUP.json` (an RSA private key) is in the served folder. It is not in `SITE_FILES`, so it isn't downloadable today, but it should not live there. | backend/ | Move it out of the project. |
| 10 | `config.json` has `"mode": "mixed"`, so live trading can be switched on per account. | config.json | Make sure that is intended. |
| 11 | The risk/lot maths uses `contract` only, which is wrong for JPY pairs and non-USD accounts. | terminal.html | Wrong position size. ✅ The bridge now sends `tick_value`/`tick_size`. |
| 12 | The hard 1.0 lot cap is fixed in code (`le=1.0`) in four places. | app.py | Make it a per-account config value. |

## 2. What I built: `terminal_pro.html` (your old file is untouched)

**About 1,400 readable lines instead of 2,745 dense ones**, in clear sections: utilities → state → bridge → indicators → chart → ticket → risk guard → polling → actions → rendering → overlays → events → boot.

**Simpler**
- Charting is done by TradingView's open-source **lightweight-charts** (Apache-2.0), so about 1,000 lines of canvas code are gone.
- **One set of colour tokens.** Light and dark only swap CSS variables, with no runtime colour engine.
- **One delegated click handler** with `data-act` actions instead of dozens of separate listeners.
- **Demo engine built in.** The same `api()` function talks either to your bridge or to a simulated broker, so the UI has a single code path.
- Reads your old `gm_key` / `gm_base`, so it connects with no extra setup.

**New features**
- **Risk-% position sizing:** "risk 1% of balance", and the lot size is computed from the SL distance using the broker's exact tick value.
- **ATR stop** (1.5 × ATR(14), click again to flip side) and **TP 1R / 2R / 3R** buttons.
- **Risk/reward zones** drawn on the chart, plus a live risk card (money, %, R:R).
- **Drag a position's entry line** to create an SL or TP. Drag SL/TP lines, pending orders, preview lines and alerts.
- **Prop-firm guard:** a daily-loss limit and a max-positions limit that lock new orders, with a gauge in the account card.
- **Price alerts:** Alt+click on the chart gives a sound, a toast and a desktop notification.
- **Watchlist** with daily % change and a today's-range bar.
- **Performance analytics:** net, win rate, profit factor, expectancy, payoff, max drawdown, losing streak, equity curve, P/L by symbol and **P/L by hour of day**.
- **Trading journal:** notes on every closed trade, plus CSV export.
- **Command palette** (Ctrl+K, or just start typing), keyboard trading (⇧B / ⇧S), number keys switch timeframes.
- Market-session indicator, bar-close countdown, trade markers on the chart, and a **LIVE account banner** with forced confirmation.
- Copy trading tab (thin UI over your existing `/api/copy/*`).
- Phone layout with a slide-up order sheet.

**Small, additive bridge changes in `app.py`**
- `terminal_pro.html` is added to `SITE_FILES`, so it is served at `http://127.0.0.1:8765/terminal_pro.html`.
- All 21 MT5 timeframes are supported.
- `/api/live` also returns `tick_value`, `tick_size`, `volume_min/step/max`.

**Round 2 (restored and expanded, after your feedback):**
- The original Goldmine Sync light and dark palettes are back (cream chart, blue/black candles, slate dark mode), plus "Midnight". Candle, wick, border, background and grid colours can be changed per theme (🎨 Chart).
- **On-chart trade labels** like the old terminal: every entry, pending order and limit/stop preview has a label with **＋SL / ＋TP handles you drag out**, **✓** to send and **×** to close or remove. Clicking the chart in Limit/Stop mode sets the price.
- **Drawing tools** (left rail and keyboard): trend line, ray, extended line, horizontal line, horizontal ray, vertical line, rectangle, Fibonacci retracement, parallel channel, measure, long/short position tool (sends straight to the order ticket), and text. They include magnet snap, colour, width and style, extend options, lock, clone, undo, and are saved per symbol.
- **Old-terminal features ported:**
  - Times in CET (time zone is configurable), PDH/PDL, Asia range, bid/ask lines, the GOLDMINE SYNC watermark.
  - Older history loads automatically as you scroll back. On connect, the chart jumps to the symbol of your oldest open trade.
  - Layered entries (up to 10 orders between two prices), basket breakeven.
  - Automation per symbol: trailing stop, breakeven at R, partial close at R.
- **`journal.html`: a full trading journal** backed by the new `/api/journal` bridge endpoint (entries and exits paired per position, initial/final SL and TP, costs, MAE/MFE). It has 7 views:
  - Overview: 16 KPIs, equity curve and drawdown, account and cash flow, monthly results.
  - Calendar heatmap with daily notes.
  - Sortable trade list, with a per-trade drawer showing a chart of the trade and journal fields (setup, tags, mistakes, emotion, rating, plan).
  - Breakdowns by 10 dimensions.
  - Risk & R: R distribution, MAE/MFE scatter, risk consistency.
  - Psychology: emotions, mistakes, tilt and revenge trading.
  - Auto-generated Insights.

  Plus CSV export and a printable PDF report.
- Share links for marketing: `terminal_pro.html?demo=1&seed=1` opens a ready-made demo scene. `journal.html?demo=1#insights` jumps to a specific view.

**Round 3 (your original panels, rebuilt in the new style):**
- The right panel has tabs **Order · Manage · Layers · Basket**. There is a chart action bar with **🛡 BE ALL · 🎯 Basket BE · ⚙ Manage · ▤ Layers**.
- **Manage:**
  - TP/SL for all positions, with "if hit" totals and draggable preview lines.
  - Break-even on the profitable ones.
  - Close 25/50/75/100% or a custom % with a slider.
  - **Close the 1–5 worst**, close all losing / all profitable.
  - **Keep 1 runner**: closes everything except the best position, optionally moving its stop to break-even.
- **Layers with risk:** zone from/to, 1–20 orders, risk in % or money, equal lots or more lots deeper, SL, TP (or RR 1/2/3 from the average entry). It previews each layer on the chart and in a table before placing.
- **Basket exit:** one price where all positions together reach break-even or +$X. It has the strict mode (TP in profit · SL in loss) or close the whole basket, and the exit line can be dragged on the chart.
- **Rectangles** have handles on the corners and the middle of each edge: left/right resize the width only, top/bottom the height only.
- **Settings for every drawing** (⚙ or double-click):
  - colour, width, style, label, background opacity
  - Fibonacci: editable levels (add, remove, change value and colour), extend, reverse, prices/levels, one colour
  - "save as default" per tool
- **Indicators are fully configurable:** add EMA, SMA, WMA (any length, any source), Bollinger, Donchian, RSI, MACD, Stochastic, ATR as many times as you want, and edit every number and colour.

**Round 4: demo accounts from the app** (Account menu → "＋ New demo account"):
- **Instant paper account:** pick a name and balance (5K to 200K or custom) and it's ready in one second.
  - It trades on live MT5 prices; orders are simulated in the bridge (`paper.py`). Nothing goes to the broker.
  - Pending orders and SL/TP are checked every second, even with the app closed (as long as the bridge runs).
  - You can deposit/withdraw, reset or delete it. It has full history and works in the journal.
  - It's saved in `paper_accounts.json`.
- **Add an MT5 demo account:** after opening one in MT5 (File → Open an Account), paste the login, password and server. The bridge tests the login and saves it to `accounts_extra.json`; there's no config.json edit and no restart. Live accounts are refused here.
- MT5's Python API cannot open new broker accounts itself. That only works in the MT5 terminal's own dialog.
- Never upload `accounts_extra.json` or `paper_accounts.json`. They are not served by the bridge.

Still not ported: the Firebase login gate and cloud settings sync (settings and journal notes are kept in the browser for now).

## 3. Next technical steps (in order)

1. **Git and one file per role.** `git init`, delete the copies, keep `terminal.html` (legacy) and `terminal_pro.html`.
2. **WebSocket push instead of polling.** Add `/ws` to FastAPI. A background thread reads ticks every 100 ms and pushes only the changes. The result is sub-100 ms updates with 10× less load. The UI only needs to replace `poll()`.
3. **One MT5 process per account.** MT5 supports *portable mode* (`terminal64.exe /portable` in separate folders). Run one Python worker per terminal, behind one API. This removes account switching, the global lock, and the copy-trading flip-flop. It is the single biggest reliability gain.
4. **Security:** remove the any-origin CORS regex, use per-user tokens (Firebase ID token verified in FastAPI), rate-limit order endpoints, and move the private key out.
5. **Port the drawing tools** onto lightweight-charts *primitives* (its plugin API): trend line, horizontal line, Fibonacci, rectangle.
6. **Server-side alerts and guard:** move alerts and the daily-loss lock into the bridge so they work when the browser is closed, with push to Telegram or phone.

## 4. Turning it into a product

Right now this is "a nice MT5 web terminal". On its own that sells poorly, because MT5, TradingView and cTrader already exist. What sells is a **specific outcome for a specific buyer**. Three directions that grow from what you already built:

### A. "Prop-Firm Guardian" (strongest fit, fastest to money)
Prop-firm traders (FTMO, The5ers, FundedNext…) **lose accounts by breaking rules**, not by bad analysis. You already have the core:
- Daily loss and max-drawdown limits that **hard-lock** trading, risk-% sizing, news-time lockout, a consistency-rule tracker, and "you can lose X more today".
- Pricing: **$19–39/month per trader.** One saved $100K challenge (about $500 fee) pays for years.
- Moat: rule presets per prop firm, plus an "evaluation passed" share card for social marketing.

### B. Copy-trading / signal-provider platform
`app.py` already has a solid master→follower engine with risk adaptation.
- Sell to **signal providers and trading coaches**: their followers connect MT5, and the provider's trades copy with each follower's own risk settings.
- Revenue: per-follower subscription with a revenue share (for example the provider charges $49, you keep 20–30%).
- Needs: hosted VPS bridges (see the architecture below), a follower dashboard, performance verification pages, and a public track record (a myfxbook-style page that is already half built in the Performance tab).

### C. Trading journal and analytics (lowest risk, no order execution)
The Performance tab plus notes is the seed of a **TraderSync / Edgewonk competitor that is native to MT5**.
- Read-only access, so it carries far less regulatory and liability risk.
- Add AI trade reviews: "you lose 70% of trades opened after 15:00 UTC", "your winners are cut at 0.8R". **Claude can generate these weekly reports from the deal history.**
- Freemium: free journal, Pro at $15/month for AI insights and multi-account views.

**Recommendation:** lead with **A**. Keep **C** as the free hook (a free journal gets users in, the Guardian converts them), and add **B** once you have users.

### Architecture for a hosted SaaS
```
User's browser ── HTTPS/WSS ──► Cloud API (FastAPI, auth, billing, DB: Postgres)
                                      │  job queue
                       ┌──────────────┼──────────────┐
                    Windows VPS    Windows VPS    Windows VPS      ← each runs N portable MT5
                    (MT5 workers)  (MT5 workers)  (MT5 workers)      terminals + a worker agent
```
- Users enter MT5 login, server and investor/trading password once. Credentials are encrypted in a vault, never stored in localStorage.
- **No local install for the user.** That is the main UX leap compared with today's start_bridge.bat plus tunnel.
- Alternative to self-hosting VPSs: **MetaApi.cloud**, which offers MT4/MT5 API as a service. It is faster to launch and costs a bit per account. Prototype on it, then self-host when margins matter.

### Go-to-market checklist
- **Branding:** "Goldmine" is fine. Get the domain and a landing page whose hero is the live demo you now have (demo mode needs no MT5).
- **Demo-first funnel:** landing page → "Try live demo" (`terminal_pro.html` in demo mode) → connect MT5 → 7-day trial.
- **Content:** short videos ("how I stopped blowing prop challenges", "drag-to-set-SL in 3 seconds") on TikTok, YouTube Shorts and X. Prop traders live there.
- **Partnerships:** prop firms and trading educators (affiliate 20–30%).
- **Legal (important):** add a disclaimer, terms of use, and "not investment advice". Copy trading and signals can be regulated activities depending on the country (EU: MiFID II, or as an "investment advice" service). Get a short consult with a fintech lawyer before you sell B. A (risk tool) and C (journal) are much lighter.
- **Billing:** Stripe subscriptions, with license checks in the cloud API.

## 5. 30 / 60 / 90 days
- **30:** git, `terminal_pro` as the default, WebSocket push, security fixes, port drawings, landing page with live demo, waitlist.
- **60:** server-side guard and alerts, prop-firm rule presets, Telegram notifications, AI weekly journal report, Stripe, 10 beta users.
- **90:** hosted MT5 workers (or MetaApi), multi-account dashboard, public performance pages, copy-trading beta with 2–3 signal providers.
