/* =====================================================================
   GOLDMINE — shared core for terminal_pro.html and journal.html
   utilities · settings · bridge client · demo broker · time zones · dialogs
   ===================================================================== */
'use strict';

/* ---------- 1. utilities ---------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const sleep = ms => new Promise(r => setTimeout(r, ms));
// Pages under /saas/ are the customer website running inside YOUR bridge for testing. They act like the hosted site:
// own saved settings (never your terminal's key) and they look for the customer's bridge instead of using this one.
const SAAS_PAGE = location.pathname.startsWith('/saas/');
const SP = SAAS_PAGE ? 'gps_' : 'gp_';
const store = {
  get(k, d) { try { const v = localStorage.getItem(SP + k); return v == null ? d : JSON.parse(v) } catch { return d } },
  set(k, v) { try { localStorage.setItem(SP + k, JSON.stringify(v)) } catch {} },
};
const legacy = k => { if (SAAS_PAGE) return ''; try { return localStorage.getItem(k) || '' } catch { return '' } }; // saved by the old terminal.html
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const num = (v, d = 2) => Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const sum = (a, f = x => x) => a.reduce((s, x) => s + f(x), 0);
const enc = encodeURIComponent;
const pad2 = n => String(n).padStart(2, '0');
const TF_SEC = { M1: 60, M2: 120, M3: 180, M4: 240, M5: 300, M6: 360, M10: 600, M12: 720, M15: 900, M20: 1200, M30: 1800, H1: 3600, H2: 7200, H3: 10800, H4: 14400, H6: 21600, H8: 28800, H12: 43200, D1: 86400, W1: 604800, MN1: 2592000 };
let CURRENCY = 'USD';
const CUR = () => ({ USD: '$', EUR: '€', GBP: '£', JPY: '¥', AUD: 'A$', CAD: 'C$', CHF: 'CHF ' })[CURRENCY] ?? (CURRENCY + ' ');
const money = (v, sign = true) => (v < 0 ? '−' : sign && v > 0 ? '+' : '') + CUR() + num(Math.abs(v));
const cls = v => v > 0 ? 'up' : v < 0 ? 'dn' : '';

/* ---------- 2. settings (shared by both pages) ---------- */
const BRIDGE_ORIGIN = !SAAS_PAGE && (location.port === '8765' || /trycloudflare\.com$/.test(location.hostname)) ? location.origin : '';
const CFG = Object.assign({
  base: legacy('gm_base') || 'http://127.0.0.1:8765', key: legacy('gm_key'), demo: false,
  theme: legacy('gm_theme') === 'dark' ? 'dark' : 'light', tz: 'Europe/Brussels',
  oneClick: false, sound: true, riskPct: 1, maxDailyLoss: 5, maxPositions: 10, welcomed: false,
  chartColors: {}, // per theme overrides: { light: { up, down, wick, border, bg, grid } }
}, store.get('cfg', {}));
if (CFG.v !== 2) { CFG.v = 2; if (!['light', 'dark', 'midnight'].includes(CFG.theme)) CFG.theme = 'light' }
if (BRIDGE_ORIGIN) CFG.base = BRIDGE_ORIGIN;
{ // share links: ?demo=1 opens straight into the live demo, ?theme=dark|light|midnight picks a theme
  const q = new URLSearchParams(location.search);
  if (q.has('demo')) { CFG.demo = q.get('demo') !== '0'; CFG.welcomed = true }
  if (['light', 'dark', 'midnight'].includes(q.get('theme'))) CFG.theme = q.get('theme');
}
const saveCfg = () => store.set('cfg', CFG);
const applyTheme = () => { document.documentElement.dataset.theme = CFG.theme };
applyTheme();

/* ---------- 3. time zones ----------
   MT5 timestamps are broker SERVER time written as if it were UTC.
   GP.off = server time − real UTC (seconds), learned from live ticks.            */
const GP = { off: store.get('off', 0) };
const TZ_LIST = [['broker', 'Broker server time'], ['Europe/Brussels', 'CET / CEST'], ['Europe/London', 'London'], ['America/New_York', 'New York'], ['UTC', 'UTC'], ['local', 'My computer']];
const _fmts = {};
function tzParts(t) {
  const tz = CFG.tz, broker = tz === 'broker', d = new Date((broker ? t : t - GP.off) * 1000);
  const zone = broker ? 'UTC' : tz === 'local' ? undefined : tz, k = zone || 'local';
  const f = _fmts[k] ||= new Intl.DateTimeFormat('en-GB', { timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', weekday: 'short' });
  const o = {}; f.formatToParts(d).forEach(x => o[x.type] = x.value);
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute, wd: o.weekday };
}
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fmtDT = t => { if (!t) return ''; const q = tzParts(t); return `${pad2(q.d)} ${MON[q.mo - 1]} ${pad2(q.h)}:${pad2(q.mi)}` };
const fmtDate = t => { const q = tzParts(t); return `${q.wd} ${pad2(q.d)} ${MON[q.mo - 1]} ${q.y}` };
const dayKey = t => { const q = tzParts(t); return `${q.y}-${pad2(q.mo)}-${pad2(q.d)}` };
const fmtDur = s => { s = Math.max(0, Math.round(s)); if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm'; if (s < 86400) return Math.floor(s / 3600) + 'h ' + pad2(Math.floor(s % 3600 / 60)) + 'm'; return (s / 86400).toFixed(1) + 'd' };

/* ---------- 4. bridge client (one code path for real + demo) ---------- */
async function api(path, body) {
  if (CFG.demo) return Demo.call(path, body);
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 30000);
  const opt = { headers: { 'X-Gomez-Key': CFG.key }, signal: ctl.signal };
  if (body) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body) }
  let r;
  try { r = await fetch(CFG.base.replace(/\/+$/, '') + path, opt) }
  catch (e) { throw new Error(e.name === 'AbortError' ? 'The bridge took too long to answer.' : `Cannot reach the bridge at ${CFG.base}. Is start_bridge.bat running?`) }
  finally { clearTimeout(timer) }
  let j = null; try { j = await r.json() } catch {}
  if (!r.ok) {
    const d = j?.detail;
    if (r.status === 404 && (d === 'Not Found' || !d) && /^\/api\/(journal|paper|accounts\/(add|remove))/.test(path)) throw new Error('Your bridge is older than this page. Close the start_bridge.bat window and start it again so it loads the new app.py.');
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join(', ') : `Bridge error ${r.status}`);
  }
  return j;
}

/* ---------- 5. demo broker: random-walk prices, orders, SL/TP, MAE/MFE, full journal ---------- */
const Demo = (() => {
  const SPEC = {
    XAUUSD: { p: 2648.5, d: 2, c: 100, v: .00010, spr: 22, com: 7, path: 'Metals\\Gold' },
    XAGUSD: { p: 31.24, d: 3, c: 5000, v: .00016, spr: 25, com: 7, path: 'Metals\\Silver' },
    EURUSD: { p: 1.08425, d: 5, c: 100000, v: .00004, spr: 8, com: 7, path: 'Forex\\Majors' },
    GBPUSD: { p: 1.27155, d: 5, c: 100000, v: .00005, spr: 10, com: 7, path: 'Forex\\Majors' },
    USDJPY: { p: 151.420, d: 3, c: 100000, v: .00005, spr: 12, com: 7, path: 'Forex\\Majors' },
    BTCUSD: { p: 68250.0, d: 2, c: 1, v: .00024, spr: 2500, com: 0, path: 'Crypto' },
    US30: { p: 42150.0, d: 1, c: 1, v: .00008, spr: 20, com: 0, path: 'Indices\\US' },
    NAS100: { p: 20480.0, d: 1, c: 1, v: .00010, spr: 15, com: 0, path: 'Indices\\US' },
    GER40: { p: 19210.0, d: 1, c: 1, v: .00009, spr: 12, com: 0, path: 'Indices\\EU' },
  };
  const now = () => Math.floor(Date.now() / 1000);
  const gauss = r => { let u = 0, v = 0; while (!u) u = r(); while (!v) v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v) };
  const rng = seed => () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296 };
  const hash = s => [...s].reduce((h, c) => Math.imul(h ^ c.charCodeAt(0), 16777619), 2166136261);
  const R = Math.random, mids = {}, drift = {}, bars = {};
  for (const s in SPEC) { mids[s] = SPEC[s].p; drift[s] = 0 }
  const pt = s => Math.pow(10, -SPEC[s].d);
  const quote = s => { const h = SPEC[s].spr * pt(s) / 2, d = SPEC[s].d; return { bid: +(mids[s] - h).toFixed(d), ask: +(mids[s] + h).toFixed(d) } };
  const mpu = (s, px = mids[s]) => s.startsWith('USD') ? SPEC[s].c / px : SPEC[s].c; // money per 1.0 price move per lot

  function history(s, tf, n) {
    const r = rng(hash(s + tf)), tfs = TF_SEC[tf], sig = SPEC[s].v * Math.sqrt(tfs);
    const out = []; let x = 1, mu = 0;
    for (let i = 0; i < n; i++) {
      mu = mu * .9 + gauss(r) * sig * .05 - (x - 1) * .004;
      const o = x; let h = o, l = o;
      for (let k = 0; k < 4; k++) { x *= 1 + mu / 4 + gauss(r) * sig / 2; h = Math.max(h, x); l = Math.min(l, x) }
      out.push({ open: o, high: h, low: l, close: x });
    }
    const f = mids[s] / x, last = Math.floor(now() / tfs) * tfs, d = SPEC[s].d;
    return out.map((b, i) => ({ time: last - (n - 1 - i) * tfs, open: +(b.open * f).toFixed(d), high: +(b.high * f).toFixed(d), low: +(b.low * f).toFixed(d), close: +(b.close * f).toFixed(d) }));
  }
  const series = (s, tf) => bars[s + '|' + tf] ||= history(s, tf, 5000);

  // ~6 months of realistic closed trades with built-in patterns, so every journal statistic has something to say
  function genAccount(label, bal0, seed, n, days) {
    const r = rng(seed), trades = [], cash = [];
    const W = [['XAUUSD', .44], ['EURUSD', .14], ['NAS100', .12], ['GBPUSD', .1], ['BTCUSD', .08], ['US30', .06], ['USDJPY', .06]];
    const pick = () => { let x = r(), a = 0; for (const [s, w] of W) { a += w; if (x <= a) return s } return 'XAUUSD' };
    let t = now() - days * 86400, bal = bal0, pos = 8800000;
    cash.push({ time: t - 3600, type: 'balance', amount: bal0, comment: 'Deposit' });
    for (let i = 0; i < n; i++) {
      t += days * 86400 / n * (.25 + r() * 1.5);
      if (t > now() - 3600) break;
      let dw = new Date(t * 1000).getUTCDay(); if (dw === 6) t += 2 * 86400; if (dw === 0) t += 86400;
      if (i === Math.floor(n * .45)) { cash.push({ time: t - 7200, type: 'balance', amount: 2000, comment: 'Deposit' }); bal += 2000 }
      if (i === Math.floor(n * .8)) { cash.push({ time: t - 7200, type: 'balance', amount: -1500, comment: 'Withdrawal' }); bal -= 1500 }
      const d = new Date(t * 1000), h = d.getUTCHours(), dow = d.getUTCDay();
      const s = pick(), sp = SPEC[s], side = r() < .56 ? 'BUY' : 'SELL', dir = side === 'BUY' ? 1 : -1;
      const hold = Math.exp(Math.log(300) + r() * Math.log(900));
      let wp = h >= 7 && h < 11 ? .63 : h >= 12 && h < 16 ? .55 : h < 7 ? .39 : .45;
      if (dow === 5) wp -= .12; if (side === 'SELL') wp -= .05; if (s === 'BTCUSD') wp -= .1; if (hold > 86400) wp -= .08;
      const win = r() < wp, hasSl = r() < .87;
      const price = sp.p * (1 + gauss(r) * .025), stop = sp.p * sp.v * Math.sqrt(3600) * (.8 + r() * 1.6), m = mpu(s, price);
      const vol = +clamp(Math.round(bal * (.004 + r() * .009) / (stop * m) * 100) / 100, .01, 3).toFixed(2);
      const Rm = win ? (r() < .3 ? 2 : .25 + r() * 2.6) : (hasSl && r() < .72 ? -1 : -(.15 + r() * .8) * (hasSl ? 1 : 1.9));
      const open = +price.toFixed(sp.d), close = +(open + dir * Rm * stop).toFixed(sp.d);
      const profit = +((close - open) * dir * m * vol).toFixed(2), commission = -+(sp.com * vol).toFixed(2), swap = hold > 86400 ? -+(Math.floor(hold / 86400) * vol * 4.2).toFixed(2) : 0;
      const tp0 = hasSl && r() < .7 ? +(open + dir * 2 * stop).toFixed(sp.d) : 0;
      const mfe = Math.max(0, Rm * stop) + r() * stop * (win ? .9 : .55), mae = win ? r() * stop * .85 : Math.abs(Rm * stop) + r() * stop * .08;
      const net = +(profit + commission + swap).toFixed(2); bal += net;
      trades.push({ position: ++pos, symbol: s, side, volume: vol, closed_volume: vol, open_time: Math.floor(t), close_time: Math.floor(t + hold), open_price: open, close_price: close,
        profit, swap, commission, fee: 0, net, exits: r() < .15 ? 2 : 1, magic: r() < .1 ? 20261014 : 20261013, comment: 'Gomez dashboard DEMO', close_comment: '',
        sl_initial: hasSl ? +(open - dir * stop).toFixed(sp.d) : 0, sl: hasSl ? +(open - dir * stop * (win && r() < .4 ? 0 : 1)).toFixed(sp.d) : 0, tp_initial: tp0, tp: tp0,
        reason: win && Rm === 2 && tp0 ? 5 : !win && Rm === -1 ? 4 : 0, money_per_unit: m, digits: sp.d, point: pt(s), mfe, mae });
    }
    return { label, balance: +bal.toFixed(2), positions: [], pending: [], trades, cash };
  }
  const ACC = { 50010001: genAccount('Gold Demo 10K', 10000, 7, 330, 190), 50010002: genAccount('Prop Challenge 100K', 100000, 11, 140, 75) };
  let tk = 7000000;

  const profitOf = (p) => { const q = quote(p.symbol); return ((p.type === 'BUY' ? q.bid - p.price_open : p.price_open - q.ask) * mpu(p.symbol) * p.volume) };
  function closePos(a, p, price, vol = p.volume, reason = 0) {
    const s = p.symbol, dir = p.type === 'BUY' ? 1 : -1, m = mpu(s);
    const pl = +((price - p.price_open) * dir * m * vol).toFixed(2), com = -+(SPEC[s].com * vol).toFixed(2);
    a.balance = +(a.balance + pl + com).toFixed(2);
    a.trades.push({ position: p.ticket, symbol: s, side: p.type, volume: vol, closed_volume: vol, open_time: p.time_epoch, close_time: now(), open_price: p.price_open, close_price: price,
      profit: pl, swap: 0, commission: com, fee: 0, net: +(pl + com).toFixed(2), exits: 1, magic: 20261013, comment: 'Gomez dashboard DEMO', close_comment: '',
      sl_initial: p.sl0 || 0, sl: p.sl, tp_initial: p.tp0 || 0, tp: p.tp, reason, money_per_unit: m, digits: SPEC[s].d, point: pt(s), mfe: p.mfe || 0, mae: p.mae || 0 });
    p.volume = +(p.volume - vol).toFixed(2);
    if (p.volume <= 1e-9) a.positions.splice(a.positions.indexOf(p), 1);
  }
  function step() {
    const t = now();
    for (const s in SPEC) {
      drift[s] = drift[s] * .995 + gauss(R) * SPEC[s].v * .02;
      mids[s] *= 1 + drift[s] + gauss(R) * SPEC[s].v * .5;
      const px = +mids[s].toFixed(SPEC[s].d);
      for (const tf in TF_SEC) {
        const B = bars[s + '|' + tf]; if (!B) continue;
        const tfs = TF_SEC[tf], bt = Math.floor(t / tfs) * tfs, last = B[B.length - 1];
        if (bt > last.time) { B.push({ time: bt, open: last.close, high: Math.max(last.close, px), low: Math.min(last.close, px), close: px }); if (B.length > 8000) B.splice(0, 2000) }
        else { last.close = px; last.high = Math.max(last.high, px); last.low = Math.min(last.low, px) }
      }
    }
    for (const a of Object.values(ACC)) {
      for (const o of [...a.pending]) {
        const q = quote(o.symbol), hit = { 2: q.ask <= o.price, 3: q.bid >= o.price, 4: q.ask >= o.price, 5: q.bid <= o.price }[o.type];
        if (hit) { a.pending.splice(a.pending.indexOf(o), 1); a.positions.push({ ticket: o.ticket, symbol: o.symbol, type: o.type % 2 === 0 ? 'BUY' : 'SELL', volume: o.volume, price_open: o.type % 2 === 0 ? q.ask : q.bid, sl: o.sl, tp: o.tp, sl0: o.sl, tp0: o.tp, time_epoch: t }) }
      }
      for (const p of [...a.positions]) {
        const q = quote(p.symbol), buy = p.type === 'BUY', px = buy ? q.bid : q.ask, mv = (px - p.price_open) * (buy ? 1 : -1);
        p.mfe = Math.max(p.mfe || 0, mv); p.mae = Math.max(p.mae || 0, -mv);
        if (p.sl && (buy ? px <= p.sl : px >= p.sl)) closePos(a, p, p.sl, p.volume, 4);
        else if (p.tp && (buy ? px >= p.tp : px <= p.tp)) closePos(a, p, p.tp, p.volume, 5);
      }
    }
  }
  setInterval(step, 250);

  const err = m => { throw new Error(m) };
  const acc = l => ACC[+l] || err('That account is not configured in the bridge.');
  const sym = s => SPEC[s] ? s : err(`Broker symbol unavailable: ${s}`);
  const vol = v => { v = Math.round(v * 100) / 100; if (v < .01 || v > 1) err('Volume must be between 0.01 and 1.0 lots for this symbol.'); return v };
  const sideCheck = (buy, ref, sl, tp) => {
    if (sl && (buy ? sl >= ref : sl <= ref)) err('Stop loss must be below entry for buys and above entry for sells.');
    if (tp && (buy ? tp <= ref : tp >= ref)) err('Take profit must be above entry for buys and below entry for sells.');
  };
  const NAMES = { 2: 'BUY LIMIT', 3: 'SELL LIMIT', 4: 'BUY STOP', 5: 'SELL STOP' };
  const posOut = a => a.positions.map(({ sl0, tp0, mfe, mae, ...p }) => ({ ...p, profit: +profitOf(p).toFixed(2) }));
  const acctOut = A => {
    const fl = sum(A.positions, profitOf), margin = sum(A.positions, x => x.volume * mpu(x.symbol) * mids[x.symbol] / 100);
    return { balance: A.balance, equity: +(A.balance + fl).toFixed(2), margin: +margin.toFixed(2), margin_free: +(A.balance + fl - margin).toFixed(2), profit: +fl.toFixed(2), currency: 'USD' };
  };

  return {
    SPEC,
    async call(path, body) {
      await sleep(20 + R() * 40);
      const u = new URL(path, 'http://demo'), q = Object.fromEntries(u.searchParams), p = u.pathname;
      const a = () => acc(body ? body.login : q.login);
      switch (p) {
        case '/api/health': return { ok: true, mode: 'demo', version: 'demo' };
        case '/api/accounts': return { accounts: Object.entries(ACC).map(([l, x]) => ({ login: +l, name: x.label, server: x.paper ? 'Goldmine Paper' : 'Goldmine-Demo', is_demo: true, paper: !!x.paper })) };
        case '/api/paper/create': {
          const l = 90000001 + Object.values(ACC).filter(x => x.paper).length, bal = +body.balance || 10000;
          ACC[l] = Object.assign(genAccount(body.name || `Paper ${num(bal, 0)}`, bal, l, 0, 1), { paper: true });
          return { ok: true, account: { login: l, name: ACC[l].label, balance: bal, currency: 'USD', paper: true } };
        }
        case '/api/paper/reset': { const A = a(); const bal = body.balance || A.cash[0]?.amount || 10000; Object.assign(A, { balance: bal, positions: [], pending: [], trades: [], cash: [{ time: now(), type: 'balance', amount: bal, comment: 'Reset' }] }); return { ok: true } }
        case '/api/paper/deposit': { const A = a(); A.balance = +(A.balance + body.amount).toFixed(2); A.cash.push({ time: now(), type: 'balance', amount: body.amount, comment: body.amount >= 0 ? 'Deposit' : 'Withdrawal' }); return { ok: true } }
        case '/api/paper/delete': { a(); delete ACC[+body.login]; return { ok: true } }
        case '/api/accounts/add': err('In demo mode nothing connects to MT5. Connect your bridge to add a real MT5 demo account.');
        case '/api/symbols': return { symbols: Object.keys(SPEC).sort(), gold: ['XAUUSD'], paths: Object.fromEntries(Object.entries(SPEC).map(([k, v]) => [k, v.path + '\\' + k])) };
        case '/api/candles': { a(); const s = sym(q.symbol), B = series(s, q.timeframe); return { symbol: s, timeframe: q.timeframe, tf_seconds: TF_SEC[q.timeframe], digits: SPEC[s].d, point: pt(s), candles: B.slice(-(+q.count || 500)).map(b => ({ ...b })) } }
        case '/api/live': {
          const A = a(), s = sym(q.symbol), qt = quote(s), B = series(s, q.timeframe);
          return { symbol: s, ...qt, time: now(), digits: SPEC[s].d, point: pt(s), contract: SPEC[s].c, tick_size: pt(s), tick_value: mpu(s) * pt(s), volume_min: .01, volume_step: .01, volume_max: 1,
            bars: B.slice(-2).map(b => ({ ...b })), positions: posOut(A), pending: A.pending.map(o => ({ ...o, name: NAMES[o.type] })), account: acctOut(A) };
        }
        case '/api/history': { const A = a(); return { deals: A.trades.slice().sort((x, y) => y.close_time - x.close_time).slice(0, +q.limit || 8).map(t => ({ ticket: t.position, symbol: t.symbol, type: t.side === 'BUY' ? 'SELL' : 'BUY', volume: t.closed_volume, price: t.close_price, profit: t.net, time_epoch: t.close_time, time: new Date(t.close_time * 1000).toISOString().slice(0, 16).replace('T', ' ') })) } }
        case '/api/journal': {
          const A = a(), from = now() - (+q.days || 365) * 86400;
          return { account: { login: +(q.login), name: A.label, owner: 'Demo Trader', server: 'Goldmine-Demo', company: 'Goldmine Demo Broker', currency: 'USD', leverage: 100, ...acctOut(A), is_demo: true },
            trades: A.trades.filter(t => t.close_time >= from).map(t => ({ ...t })), cash: A.cash.filter(c => c.time >= from), open: posOut(A), now: now() };
        }
        case '/api/order': {
          const A = a(), s = sym(body.symbol), v = vol(body.volume), buy = body.side === 'buy', qt = quote(s), px = buy ? qt.ask : qt.bid;
          sideCheck(buy, px, body.sl, body.tp);
          A.positions.push({ ticket: ++tk, symbol: s, type: buy ? 'BUY' : 'SELL', volume: v, price_open: px, sl: body.sl || 0, tp: body.tp || 0, sl0: body.sl || 0, tp0: body.tp || 0, time_epoch: now() });
          return { ok: true, ticket: tk, message: 'Request executed' };
        }
        case '/api/pending': {
          const A = a(), s = sym(body.symbol), v = vol(body.volume), buy = body.side === 'buy', qt = quote(s), price = +(+body.price).toFixed(SPEC[s].d);
          const type = buy ? (price < qt.ask ? 2 : 4) : (price > qt.bid ? 3 : 5), isLimit = type <= 3;
          if (body.kind === 'limit' && !isLimit) err('A limit order needs the price BELOW market for buys and ABOVE market for sells.');
          if (body.kind === 'stop' && isLimit) err('A stop order needs the price ABOVE market for buys and BELOW market for sells.');
          sideCheck(buy, price, body.sl, body.tp);
          A.pending.push({ ticket: ++tk, symbol: s, type, volume: v, price, sl: body.sl || 0, tp: body.tp || 0 });
          return { ok: true, ticket: tk, type: NAMES[type], message: 'Order placed' };
        }
        case '/api/pending/modify': {
          const A = a(), o = A.pending.find(x => x.ticket === body.ticket) || err('Pending order not found.');
          const price = body.price ?? o.price, sl = body.sl ?? o.sl, tp = body.tp ?? o.tp, qt = quote(o.symbol);
          if (body.price != null && { 2: price >= qt.ask, 3: price <= qt.bid, 4: price <= qt.ask, 5: price >= qt.bid }[o.type]) err(`Invalid price: a ${NAMES[o.type]} must stay ${o.type === 2 || o.type === 5 ? 'below' : 'above'} the current price.`);
          sideCheck(o.type % 2 === 0, price, sl, tp); Object.assign(o, { price, sl, tp });
          return { ok: true, message: 'Pending order updated' };
        }
        case '/api/cancel': { const A = a(), i = A.pending.findIndex(x => x.ticket === body.ticket); if (i < 0) err('Pending order not found.'); A.pending.splice(i, 1); return { ok: true, message: 'Pending order cancelled' } }
        case '/api/modify': {
          const A = a(), x = A.positions.find(y => y.ticket === body.ticket) || err('Position ticket not found.'), qt = quote(x.symbol), buy = x.type === 'BUY';
          if (body.sl && (buy ? body.sl >= qt.bid : body.sl <= qt.ask)) err('SL is on the wrong side of the current market price.');
          if (body.tp && (buy ? body.tp <= qt.bid : body.tp >= qt.ask)) err('TP is on the wrong side of the current market price.');
          if (!x.sl0 && body.sl) x.sl0 = body.sl; if (!x.tp0 && body.tp) x.tp0 = body.tp;
          x.sl = body.sl || 0; x.tp = body.tp || 0; return { ok: true, message: 'SL/TP updated' };
        }
        case '/api/close': {
          const A = a(), x = A.positions.find(y => y.ticket === body.ticket) || err('Position ticket not found'), qt = quote(x.symbol);
          const v = body.percent >= 99.999 ? x.volume : Math.round(x.volume * body.percent) / 100;
          if (v < .01) err('Partial close rounds below broker minimum 0.01 lots');
          closePos(A, x, x.type === 'BUY' ? qt.bid : qt.ask, +v.toFixed(2)); return { ok: true, message: `Close request sent (${body.percent}%).` };
        }
        case '/api/close-all': { const A = a(); let n = 0; for (const x of [...A.positions]) { const qt = quote(x.symbol); closePos(A, x, x.type === 'BUY' ? qt.bid : qt.ask); n++ } return { ok: true, closed: n, failed: 0 } }
        default: err('Copy trading runs inside your MT5 bridge. Connect to your bridge to use it.');
      }
    },
  };
})();

/* ---------- 6. toasts, sound, dialogs, popovers ---------- */
function toast(msg, type = 'info', ms) {
  let box = $('#toasts'); if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box) }
  const el = document.createElement('div'); el.className = 'toast ' + type; el.textContent = msg;
  box.appendChild(el); setTimeout(() => el.remove(), ms || (type === 'err' ? 6000 : 3000));
  while (box.children.length > 4) box.firstChild.remove();
}
let _ac;
function beep(f = 880, d = .09) {
  if (!CFG.sound) return;
  try { _ac ||= new AudioContext(); const o = _ac.createOscillator(), g = _ac.createGain(); o.frequency.value = f; g.gain.setValueAtTime(.08, _ac.currentTime); g.gain.exponentialRampToValueAtTime(.0001, _ac.currentTime + d); o.connect(g).connect(_ac.destination); o.start(); o.stop(_ac.currentTime + d) } catch {}
}
let modalResolve = null;
function modalEl() { let m = $('#modal'); if (!m) { m = document.createElement('div'); m.id = 'modal'; m.className = 'modal'; document.body.appendChild(m); m.addEventListener('pointerdown', e => { if (e.target === m) resolveModal(null) }) } return m }
function openModal(html, wide) { const m = modalEl(); m.className = 'modal on' + (wide ? ' wide' : ''); m.innerHTML = `<div class="mbox">${html}</div>`; return m }
function resolveModal(v) { const r = modalResolve; modalResolve = null; r && r(v); const m = modalEl(); m.className = 'modal'; m.innerHTML = '' } // read the form BEFORE emptying it
const modalOpen = () => modalEl().classList.contains('on');
function confirmBox(title, body, ok = 'Confirm', tone = 'acc') {
  return new Promise(res => {
    openModal(`<h3>${esc(title)}</h3><div class="mbody">${body}</div><div class="mact"><button class="btn ghost" data-m="no">Cancel</button><button class="btn ${tone}" data-m="yes">${esc(ok)}</button></div>`);
    modalResolve = v => res(!!v); setTimeout(() => $('#modal [data-m=yes]')?.focus(), 30);
  });
}
function formBox(title, fields, ok = 'Save', intro = '') {
  return new Promise(res => {
    const f = x => x.type === 'textarea' ? `<textarea data-f="${x.id}" placeholder="${esc(x.ph || '')}">${esc(x.value ?? '')}</textarea>`
      : x.type === 'select' ? `<select data-f="${x.id}">${x.options.map(o => `<option value="${esc(o[0])}" ${String(o[0]) === String(x.value) ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`
      : `<input data-f="${x.id}" value="${esc(x.value ?? '')}" inputmode="${x.text ? 'text' : 'decimal'}" placeholder="${esc(x.ph || '')}">`;
    openModal(`<h3>${esc(title)}</h3>${intro ? `<p>${intro}</p>` : ''}<div class="mbody">${fields.map(x => `<label>${esc(x.label)}${f(x)}</label>`).join('')}</div><div class="mact"><button class="btn ghost" data-m="no">Cancel</button><button class="btn acc" data-m="yes">${esc(ok)}</button></div>`);
    modalResolve = v => res(v ? Object.fromEntries($$('#modal [data-f]').map(i => [i.dataset.f, i.value.trim()])) : null);
    setTimeout(() => $('#modal [data-f]')?.focus(), 30);
  });
}
let popEl = null;
function popover(anchor, html, cls = '') {
  closePopover(); const r = anchor.getBoundingClientRect();
  popEl = document.createElement('div'); popEl.className = 'pop ' + cls; popEl.innerHTML = html; document.body.appendChild(popEl);
  popEl.style.left = clamp(r.left, 8, innerWidth - popEl.offsetWidth - 8) + 'px';
  const top = r.bottom + 6; popEl.style.top = (top + popEl.offsetHeight > innerHeight - 8 ? Math.max(8, r.top - popEl.offsetHeight - 6) : top) + 'px';
  popEl._a = anchor; return popEl;
}
function closePopover() { popEl?.remove(); popEl = null }
document.addEventListener('pointerdown', e => { if (popEl && !popEl.contains(e.target) && !popEl._a.contains(e.target)) closePopover() }, true);
document.addEventListener('click', e => { const b = e.target.closest('[data-m]'); if (b && b.closest('#modal')) resolveModal(b.dataset.m === 'yes') });
