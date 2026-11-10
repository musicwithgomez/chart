# What every file is

Names stay as they are on purpose: the website address (terminal_test.html), the customer bridges and the start
scripts all look for these exact names. Renaming one would break a live link or a customer's install.

## Your own bridge (runs on your PC)
| File | What it is |
|---|---|
| `start_bridge.bat` | **Double-click this** to start your own bridge. |
| `start_bridge.ps1` | What start_bridge.bat runs (your PC only, never sent to customers). |
| `app.py` | The bridge itself: talks to MetaTrader 5, places orders, serves the terminal. |
| `paper.py` | Paper (practice) accounts and price targets. |
| `licensing.py`, `gp_sign.py` | License check for customer bridges (off on your own PC). |
| `requirements.txt` | Python packages the bridge needs. |
| `config.json` | **Private.** Your accounts and private key. Never upload. |
| `config.example.json`, `SETUP_GUIDE.txt` | How to set up config.json by hand (reference). |
| `accounts_extra.json`, `paper_accounts.json`, `copy_settings.json`, `targets.json` | **Private.** Your saved accounts, paper accounts, copy settings and price targets. Never upload. |
| `gomez-private-key-BACKUP.json` | **Private.** Backup of your key. Never upload. |
| `.venv/` | Python installed for the bridge. Leave it. |

## What customers get (built into their zip automatically)
| File | What it is |
|---|---|
| `start_customer.bat` | Becomes **START GOLDMINE.bat** in the customer's zip. |
| `run_bridge.py` | Starts the customer's bridge and opens the setup page. |
| (plus app.py, paper.py, licensing.py, gp_sign.py, requirements.txt and the pages below) | |

## The website and the terminal (upload these to GitHub)
| File | What it is |
|---|---|
| `terminal_test.html` | **The website**: landing page, sign-up, dashboard, setup, billing, admin. |
| `terminal_pro.html` | **The trading terminal.** |
| `journal.html` | The trading journal. |
| `rulebook.html` | The free Risk Rulebook (lead magnet). |
| `gp-shared.js`, `gp-theme.css` | Shared code and colours for the pages above. |
| `lp-*.jpg` | Screenshots used on the landing page. |

## The business side (`saas/`)
| File | What it is |
|---|---|
| `saas/license_server.py` | Sign-ups, payments (Stripe), license keys, admin, leads. |
| `saas/.env.example` | All server settings (prices, Stripe keys, founding seats…). |
| `saas/data/` | **Private.** Customer database and owners.txt. Never upload. |
| `saas/selftest/` | The automatic test (`.venv\Scripts\python.exe saas\selftest\selftest.py`). |
| `saas/SELLING_GUIDE.md`, `saas/MARKETING_PLAN.md`, `saas/EMAILS.md` | How to sell, the marketing plan, the follow-up emails. |

## Promo video (`promo_video/`)
| File | What it is |
|---|---|
| `out/Goldmine promo - phone (9x16).mp4`, `out/Goldmine promo - YouTube (16x9).mp4` | **The finished videos.** |
| `src/`, `public/`, `package.json` | The video project. To edit and re-render: `npm install`, then `npm run render`. |
| `shots.cjs` | Re-takes the landing-page screenshots. |
