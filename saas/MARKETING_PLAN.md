# Goldmine Pro: positioning, funnel and 90-day marketing plan

The old page (`terminal_test_v1.html`) sold **features** to "MT5 traders".
The new page (`terminal_test.html`) sells **one outcome** to **one buyer**:

> **The risk manager MT5 never gave you.**
> Lots sized from your risk, ten positions managed like one, and a daily loss limit that locks the buttons before you break a rule.

---

## 1. Who we sell to (in this order)

| # | Buyer | What they feel | Why they pay |
|---|---|---|---|
| 1 | **Prop-firm challenge traders** on MT5 (funded or in a challenge) | One bad day ends the account. A challenge reset costs €300–€600. | Daily loss lock, exact lot sizes, rehearsing on a paper account, up to 5 accounts on Pro. |
| 2 | **Gold (XAUUSD) scalpers who stack positions** | Closing 8 positions one by one while price runs. | Close & keep (runner), basket break-even, partial TP that fires with the page closed. |
| 3 | Small funds / signal-group owners | Managing many accounts. | Elite: 25 accounts, copy trading. |

**Not for:** beginners who want signals, MT4 users, people looking for an EA that trades for them.
Saying no to them makes the yes stronger.

## 2. The offer

- **7 days of Pro, no card.** This is the lead magnet. The email they give is your list.
- **Founding-member price, locked for as long as they stay subscribed.** It is real scarcity only if you end it.
  Pick a date or a number (e.g. "first 300 members") and keep to it.
- **Price anchor = the challenge fee, not a fake "regular price".**
  "One avoided reset pays for 3+ years of Pro" is true and strong.
  - ⚠ The old page shows strike-through prices (€59 → €9.99) that nobody ever paid.
  - Under the EU Omnibus directive a "was" price must be a price you actually charged in the last 30 days.
  - The new page doesn't use them. Remove `regular` from `PLANS` or start charging it to new members first.
- **Recommended (your decision):**
  - A 14-day money-back guarantee on the first payment. It usually lifts trial → paid conversion more than it costs in refunds.
  - Only add it to the page once you have decided to honour it.
- **Lifetime Pro (€249)** stays as the top-of-pricing upsell for the first months. It brings cash for ads and video.
  Close it once you have ~100 lifetime buyers, or the recurring revenue suffers.

## 3. The funnel

```
 TRAFFIC                    LANDING (terminal_test.html)               TRIAL (7 days)                 PAID
 ─────────                  ──────────────────────────                 ──────────────                 ────
 Short videos  ─┐           Hero: "risk manager MT5 never gave you"    Day 0  welcome + 5-min setup    Founding price
 Prop Discords ─┤   ──►     Challenge calculator (interactive)  ──►    Day 1  "do this one thing"  ──► Yearly default
 YouTube collabs┤           Live demo (no sign-up)                      Day 3  case: close & keep       Lifetime upsell
 SEO pages     ─┤           A trading day with Goldmine (story)         Day 5  founding price reminder
 Affiliates    ─┘           Pricing vs challenge fee · FAQ              Day 6  last day + objections
                            Sticky CTA (phone) · exit intent → demo     Day 7+3 win-back (demo + 20%)
```

**Main conversion goal:** the trial sign-up. Second goal: the demo opened. Track both with `data-cta`, see `track()`.

**Targets for the first 90 days:**

| Step | Target |
|---|---|
| Visitor → demo opened | 15–25% |
| Visitor → trial | 4–8% (cold traffic) / 10–20% (Discord or affiliate traffic) |
| Trial → bridge connected within 24 h | ≥ 60% (the biggest lever: an unconnected trial never pays) |
| Trial → paid | 15–25% |
| Monthly churn | < 8% |

## 4. Channels (ranked by cost per customer for this niche)

1. **Short-form video (TikTok, Reels, YouTube Shorts): 4 per week.** The product is visual; the clip *is* the ad.
2. **Prop-firm and gold-trading Discord / Telegram communities.**
   - Don't spam.
   - Offer the challenge calculator as a free tool.
   - Give moderators an Elite account.
3. **YouTube creators (5k–100k subs) who trade gold or prop challenges.**
   - Affiliate link: 30% recurring for 12 months.
   - Plus a free Elite account.
   - Pay a fixed fee only after an affiliate test proves it converts.
4. **SEO pages that answer exact searches.**
   - "close all positions MT5", "MT5 partial close at price", "lot size calculator gold", "MT5 daily loss limit".
   - One page each, with a short clip and a trial CTA.
5. **Paid ads (later, from month 2): only retargeting first.**
   - Meta/YouTube retargeting of demo openers, showing the "close 10 positions in 1 click" clip.
   - Cold paid traffic only once trial → paid is above 15%.
   - Check each ad platform's financial-services rules first.

## 5. Content: the first 5 video scripts (15–30 s each, screen recording + voice)

1. **"10 positions, 1 click"**
   - Hook: *"This is how long it takes to close 10 gold trades in MT5…"* Show the slow way, sped up.
   - Then: *"…and this is Goldmine."* Close & keep: close 9, keep the best as a 0.01 runner, stop at break-even.
   - End card: "7 days free".
2. **"The fat-finger that ends challenges"**
   - Type 0.5 instead of 0.05 in MT5 and show the money at risk.
   - Then Goldmine: the lot comes from your risk %, shown in money before you click.
3. **"I hit my daily limit. Now I can't trade."**
   - Show the lock and the gauge.
   - *"Best feature I never wanted to use."*
4. **"Set it and walk away"**
   - Set a partial TP on the chart, close the laptop lid.
   - Price hits; open again and the ⚡ "partial TP done" message is waiting.
   - Always show "your bridge must be running (PC or VPS)".
5. **"Layer into gold like a pro"**
   - 5 buy limits across a zone, total risk exactly 1%. One click.

Every clip must have:
- A hook in the first second.
- No profit claims.
- The risk warning in the caption: *"Trading software, not advice. CFDs carry a high risk of loss."*

## 6. Trial email sequence

Send it with MailerLite / Brevo (free tiers). Trigger it from sign-up: a webhook or a daily CSV from the Admin tab.

| Day | Subject | Content |
|---|---|---|
| 0 | Your 5-minute setup | Download → START GOLDMINE → connected. One GIF. Reply if stuck. |
| 1 | Do this one thing today | Set your risk % and daily loss limit in Settings. That's the whole product in one minute. |
| 3 | 8 gold entries, 1 click | Close & keep + partial TP walkthrough. Swap in a real member's story once you have one (with permission), never invent it. |
| 5 | Your founding price | What they keep if they subscribe before the trial ends. Yearly = 2 months free. |
| 6 | Last day + "is it allowed by my prop firm?" | The FAQ answers, then the button. |
| 7 + 3 | Still trading by hand? | Demo link + a 20% coupon for the first 3 months (Stripe promo code). |

**In-app (already built):** the trial banner, the founding offer, exit intent → demo, cancel → downsell.

## 7. Affiliate program

- **Tool:** Rewardful or FirstPromoter. Both connect to Stripe in minutes.
- **Payout:** 30% recurring for 12 months.
- **Cookie:** 60 days.
- **What affiliates get:**
  - Swipe files: the 5 scripts above.
  - Banner clips.
  - A personal coupon, e.g. "GOLDMIKE10".
- **Start:** recruit the first 10 by hand from Discord moderators and small YouTubers.

## 8. 90-day calendar

**Weeks 1–2: private beta**
- 20 traders from your network, free Pro.
- Goal: 5 real testimonials with first names and a photo or handle, plus 3 short clips of them using it.
- Fix every setup problem they hit: setup friction kills trials.

**Weeks 3–4: launch**
- Post the 5 clips. Share the calculator in 5 communities.
- Open the affiliate program.
- Founding price live, with a public end date.

**Month 2**
- 4 clips/week. 2 SEO pages/week. 3 creator deals.
- Start retargeting ads (€5–10/day).
- **A/B tests** (one at a time, ≥ 300 visitors each):
  1. The hero headline: "The risk manager MT5 never gave you" vs "Stop losing funded accounts to clicks".
  2. Monthly vs yearly as the default toggle.
  3. With or without the money-back guarantee line.

**Month 3**
- End the founding price for new members. That deadline is your biggest sales day.
- Close lifetime.
- Raise Pro to €19–24/month for new members. The product is worth more than €9.99 to someone protecting a €100k account.

## 9. What to measure every Monday (Admin tab + analytics)

Visitors · demo opens · trials · bridges connected · trial → paid · MRR · churn · refunds.

**The one number that matters most early: % of trials with a connected bridge within 24 h.**

## 10. Legal guardrails

These are already respected on the page; keep them in every ad and post:
- Software only. Never promise profits, never show P/L screenshots as "results", no signals.
- "Works with MetaTrader 5" is fine. Don't put MetaTrader, FTMO or another firm's name in ads, logos or the product name.
- About prop firms, say: "a manual trade-management tool; check your firm's rules". Never "approved by" or "allowed everywhere".
- Testimonials: real people only, and keep their written permission.
- No countdowns or "only 3 seats left" unless they are true.
