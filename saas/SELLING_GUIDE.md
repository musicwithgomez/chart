# Goldmine Pro: from your PC to a paid subscription business

Each customer runs **their own bridge** next to their own MT5. **You** host one small license server.
That server sells the subscription, gives every customer a license key, and every bridge checks that key.

```
 Customer browser ──► app.yourdomain.com  (YOUR license server: website, sign-up, Stripe, dashboard, terminal files)
        │                     ▲
        │ trades              │ "is key GP-XXXX active?"  (signed answer, every 30 min, 72 h offline grace)
        ▼                     │
 Customer's bridge (app.py on THEIR PC / VPS) ──► Customer's MetaTrader 5 ──► their broker
```

Nothing changes for **your own** bridge: without a `license.json` it runs exactly like before (owner mode).

---

## 1. Test everything (one double-click, no Stripe needed)

Double-click **`saas\TEST_EVERYTHING.bat`**. It does two things:

1. **Self-test (about 30 s):** a full customer journey with a fake MT5. It uses its own temporary database, so it never touches your data.
   - sign up, then download the zip, unzip it and start *that* bridge
   - auto-detect the account and activate the license
   - trial → expired → paid → payment failed → cancelled → ended
   - forged licenses, extra PCs, Stripe webhooks
   - a check that your own bridge stays in owner mode

   It ends with `ALL GOOD: 36/36`.
2. **Website in test mode** at http://127.0.0.1:8800, opened for you.
   - Use the **🧪 Test tools** panel (bottom left) to create a test customer in one click.
   - The same panel jumps your account to *trial last day / expired / paying / payment failed / ended*, so you see exactly what customers see.
   - Admin tab token: `admin-local-test`.

### The customer install (what you are selling)
1. Sign up: email + password → 7-day Pro trial, no card.
2. **Download** the zip. It already contains a ready `config.json` with a random private key, plus the license.
3. Open MetaTrader 5, unzip, double-click **START GOLDMINE**. The first time it installs Python by itself.
   It picks up the MT5 account that is logged in (no login or password typed) and opens the website, which **connects by itself**.

Optional: phone or another PC through Cloudflare, in Setup → "Trade from anywhere".
Real-money accounts: in `config.json` set `"live_trading": true` (demo-only by default, like your own bridge).

### Hosting the website on GitHub Pages
`https://musicwithgomez.github.io/chart/terminal_test.html` works once these files are in the repo's `chart` folder:
`terminal_test.html` (underscore!), `terminal_pro.html`, `journal.html`, `gp-shared.js`, `gp-theme.css`.
- GitHub Pages only serves files, so the license server still has to run somewhere.
- While testing, keep `TEST_EVERYTHING.bat` running on your PC. The GitHub page talks to `http://127.0.0.1:8800` on that PC by itself (Chrome / Edge).
- In production, set `SAAS_SERVER = 'https://api.yourdomain.com'` at the top of the script in `terminal_test.html`.
Don't use your own backend folder for this.

## 2. What you need before you sell

| Item | Why | Where |
|---|---|---|
| A company / sole trader registration + VAT number | Required to sell; Stripe needs it | Your country's business register |
| Stripe account | Card payments, subscriptions, invoices, the customer portal | stripe.com |
| Stripe Tax (or a merchant of record such as Paddle / Lemon Squeezy) | EU VAT on digital services is charged at the **customer's** country rate | Stripe dashboard → Tax |
| A domain, e.g. `goldminepro.com` | Brand + `https://app.` address | Cloudflare, Namecheap… |
| Hosting for the license server | It must be online 24/7 | Render, Railway, Fly.io or a €5 VPS |
| Terms, Privacy policy, Risk disclosure | Legally required; placeholders are in the footer | A lawyer, or Termly / iubenda |
| Support email | Customers need a contact | e.g. support@yourdomain.com |

**Important (legal):**
- Sell it as **software**. Never promise profits and never give signals or advice. Those are regulated (MiFID) and banned by Stripe.
- "MetaTrader" is a trademark of MetaQuotes. You may say "works with MetaTrader 5", but don't put it in your product name or logo.
- Check Stripe's restricted-businesses list once. Trading **tools** are normally fine; signal services and copy-trading *of other people's money* are not.

## 3. Stripe setup (about 20 minutes)

1. Stripe → **Products** → create **Goldmine Pro Starter**, **Pro** and **Elite**.
   Give each one two recurring prices: monthly (€4.99 / €9.99 / €19.99, founding-member launch prices) and yearly (€49 / €99 / €199).
   To change the prices, edit `PLANS` at the top of `license_server.py` so the website shows the same amounts.
2. Copy each Price ID (`price_...`) into the matching variable in `.env.example`.
3. Stripe → **Developers → Webhooks** → add the endpoint `https://app.yourdomain.com/api/billing/webhook` with these events:
   - `checkout.session.completed`
   - `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`
   - `invoice.paid`, `invoice.payment_failed`

   Copy the signing secret (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
4. Stripe → **Settings → Billing → Customer portal**: turn on cancel, switch plan and update card.
5. Start with the `sk_test_...` key and test card `4242 4242 4242 4242`. Switch to `sk_live_...` when everything works.

## 4. Put the license server online (Render example)

1. Put the `backend` folder in a **private** GitHub repo.
   Do NOT include config.json, accounts_extra.json, paper_accounts.json, copy_settings.json, any private key file, or `saas/data/`.
2. Render → New → **Web Service** → choose the repo.
   - Build command: `pip install -r saas/requirements.txt`
   - Start command: `uvicorn saas.license_server:app --host 0.0.0.0 --port $PORT`
   - Add a **Disk** mounted at `/data` and set `GP_DATA_DIR=/data`.
3. Add all the variables from `.env.example` (`GP_PUBLIC_URL`, `GP_ADMIN_TOKEN`, the Stripe keys and price ids).
4. Point `app.yourdomain.com` at the service (Render → Custom domain). HTTPS is automatic.
5. **Back up `data/signing_seed` and `data/licenses.db`.** If you lose the seed, every bridge you shipped stops accepting answers.

## 5. Before you ship the bridge to customers

1. Open the server log and copy the **public key** it prints.
   Paste it into `licensing.py` → `PINNED_PUBKEY = "..."`. A customer then cannot point their bridge at a fake license server.
2. Optional but recommended: compile the bridge to an `.exe` with Nuitka or PyInstaller. The Python source is then not easy to edit.
   Plain `.py` files are readable, so a technical customer could remove the check.
   Your real protection is that the website, updates and support only work with an active subscription.
3. The download in the dashboard builds the zip automatically. It contains the bridge and the customer's `license.json` and **never** your secrets (this is tested).

## 6. What is enforced where

| Rule | Enforced by |
|---|---|
| No active plan or trial → terminal stops working | Bridge (402 on every API call) + website paywall |
| Starter = 1 MT5 account, Pro = 5, Elite = 25 | Bridge (`plan_terminals`) |
| Paper accounts and copy trading only on Pro / Elite | Bridge (`_PLAN_FEATURE`) |
| Key works on 1 / 2 / 3 PCs | License server (customers can remove old PCs in the dashboard) |
| Cancel → access until the end of the paid period | License server |
| Payment failed → 'past_due', still works until Stripe gives up | Stripe retries, then the webhook sets 'canceled' |
| Server offline → bridges keep working 72 h | Signed cache in the bridge |

## 7. Launch plan (suggested)

1. **Week 1: private beta.** 10–20 traders from your own network on free Pro. Collect feedback and testimonials. Only publish **real** ones.
2. **Week 2:** record a 60-second screen video (drag SL/TP, layers, basket BE, journal). That clip is your best ad.
3. **Launch:** trading Discord/Telegram groups, X/Twitter, YouTube shorts, prop-firm communities. Offer a launch coupon (Stripe promotion codes are already enabled at checkout).
4. **Affiliates:** Stripe doesn't do affiliates; add Rewardful or FirstPromoter (30% recurring is standard in this niche).
5. **Numbers to watch** (Admin tab): trial → paid conversion (aim for 15–25%), monthly churn (keep it under 8%), MRR.

## 8. Files

| File | What it is | Who runs it |
|---|---|---|
| `saas/license_server.py` | Accounts, Stripe, license keys, admin, website files, bridge download | You (hosted) |
| `terminal_test.html` | Customer website: landing, pricing, sign-up, dashboard, setup wizard, billing, terminal, admin | Served by the license server |
| `licensing.py` | License check inside the bridge (off without license.json) | Customer bridge |
| `gp_sign.py` | Ed25519 signatures, pure Python (no extra packages) | Both |
| `app.py` | Your bridge + license gate, `/api/license/status`, `/api/license/activate` | Customer + you |
| `saas/.env.example` | All server settings | You |
| `saas/start_license_server.bat` | Local test-mode server | You |
