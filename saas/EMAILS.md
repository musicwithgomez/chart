# Follow-up emails for Risk Rulebook leads

**Where the emails come from:** Admin → download the leads CSV (`/api/admin/leads?token=YOUR_ADMIN_TOKEN`). Import it into MailerLite or Brevo, both free up to 1,000 contacts.

**How to send them:** set up an automation that sends one email per day, starting the day after sign-up. Stop it as soon as the person becomes a customer (the CSV has an `is_customer` column).

**Placeholders:** replace `{{site}}` with your website link, and `{{first_name}}` if your email tool has it.

---

## Day 0: Your Risk Rulebook
**Subject:** Your Risk Rulebook (keep this next to your screen)

Hi,

Your Risk Rulebook is here: {{site}}/rulebook.html

It's built from the numbers you entered: your lot for every stop size, your daily stop, your layer plan, and a 10-second checklist for every trade.

Print it. Seriously. A rule you can see is a rule you follow.

Tomorrow: the one mistake that costs MT5 traders more than bad setups.

## Day 1: The click that costs more than the setup
**Subject:** It wasn't the setup

Most blown days don't start with a bad idea. They start with a click:
- 0.50 typed instead of 0.05
- eight positions closed one by one while price runs
- "one more trade" at −4%

None of those is a strategy problem. They're execution problems. Execution problems can be fixed with tools, not willpower.

Look at rule 4 in your Rulebook: your fat-finger ceiling. Know that number by heart.

## Day 2: See it working (2 minutes, no sign-up)
**Subject:** Close 9 positions in one click (try it)

Open the live demo. It has simulated prices, so nothing is real and nothing is at risk:
{{site}}/terminal_pro.html?demo=1

Try three things:
1. **Layers:** five limit orders, lots sized so the total risk is exactly 1%.
2. **Close & keep:** close everything except your best entry, and keep a small runner.
3. **Partial take profit:** click the chart to place it. It fills even with the page closed.

## Day 3: The day it saves you
**Subject:** The worst day of your month

Everyone has one. The morning goes wrong, and by lunch you're trading to "get it back".

Goldmine's daily loss limit locks new trades at your number. It stops new trades only: you can still close what's open. Tomorrow you start fresh, with an account that still exists.

That one feature usually pays for a year of the software in a single bad day.

## Day 4: The offer (founding seats)
**Subject:** {{seats_left}} founding seats left

Here's the 5-Minute Risk Lock:
- Goldmine Pro: the risk terminal next to your MT5
- a 1-on-1 setup call (20 min) where we set your rules with you
- your Risk Rulebook, filled in together
- a private Discord with a weekly trade review
- every update, forever

**Founding price:** €19/month instead of €29, locked for as long as you stay. Only for the first 100 members.

**The guarantee:** use it on 20 trades in your first 30 days. If it didn't pay for itself, you get a full refund.

→ {{site}}#offer

## Day 6: Last call
**Subject:** Founding price closes soon

Short one. The founding price (€19/month for life, plus the setup call) closes on {{founding_end}}, or earlier if the seats run out.

After that it's €29/month and no call.

→ {{site}}#offer

If it's not for you, no problem. Keep the Rulebook, it's yours.

---

**Legal reminder for every email**
- No profit promises.
- No screenshots of P/L presented as results.
- An unsubscribe link (your email tool adds it).
- Your company name and address in the footer.
