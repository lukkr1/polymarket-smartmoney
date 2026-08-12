# Polymarket Smart Money

Two pieces that do different jobs.

## 1. The live site — `index.html`

An interactive tracker you run yourself. Build lists of wallets to follow, then
see what they hold and what they're buying right now.

**To open it:** double-click `index.html`. That's it — it runs entirely in your
browser and talks to Polymarket directly.

If your browser blocks the data (some do on `file://` pages), run the tiny
included server instead:

```
powershell -ExecutionPolicy Bypass -File serve.ps1
```

then open <http://localhost:8765/>. Press Ctrl+C in that window to stop it.

### The five tabs

| Tab | What it's for |
|---|---|
| **Discover** | Top wallets by profit or volume, over 1d / 7d / 30d / all time. Add them to a list one at a time, or add the top 10/20/50 in one click. |
| **Holdings** | What your list currently has money in — share of the group's capital per market, how many wallets agree, what they paid vs today's price. |
| **Flow** | What they've been buying and selling over the last 6h to 7 days, netted per outcome. |
| **Signals** | The shortlist: positions several wallets independently hold, ranked with the ones still near their entry price first. |
| **Members** | Rename lists, remove wallets, copy the addresses out. |

### Two things worth knowing

**Weighting.** The *Weight by* dropdown changes who counts. "Dollars" lets the
biggest wallets dominate the percentages. "Per wallet" averages each wallet's own
allocation instead, so a sharp trader with $50k counts as much as a whale with
$5M. They can tell very different stories — check both.

**Your lists live in this browser.** They're saved locally, so they survive
closing the tab but won't follow you to another device or browser. Use *Copy
addresses* in Members to move a list somewhere else.

## 2. The daily report — runs without you

A scheduled agent runs every morning at 07:00 (Ljubljana time), reads the top 40
wallets by 30-day profit, works out which positions three or more of them
independently hold, and rebuilds a report page.

- **The report:** <https://claude.ai/code/artifact/8602c582-6ab7-4401-bda6-0bae16ce262d>
  — same link every day, always the latest version.
- **Manage the schedule:** <https://claude.ai/code/routines>

`daily_report.py` is the same logic as a standalone script, if you ever want to
run it yourself: `python daily_report.py --wallets 40 --out report.html`.

## How the numbers are worked out

Everything comes from Polymarket's public API — no key, no login, no account
needed.

| What | Where from |
|---|---|
| Wallet rankings | `lb-api.polymarket.com/profit` and `/volume` |
| Open positions | `data-api.polymarket.com/positions` |
| Recent trades | `data-api.polymarket.com/activity` |

Three decisions shape every number, and they're deliberate:

**Positions are grouped by outcome, not by market.** Two wallets can sit on
opposite sides of the same market. Merging those would invent agreement that
isn't there.

**Decided markets are thrown out.** Polymarket keeps finished bets in a wallet's
position list until the trader cashes them out, and many sit at 0¢ fully lost
without being flagged as settled. One wallet tested here had a $3.2k live book
carrying $661k of "unrealised" loss — 292 dead bets it had never redeemed.
Counted naively, that single graveyard set the headline number for the whole
group. So anything pinned to 0¢ or 100¢ is excluded: those markets are over.

**Agreement is ranked ahead of price.** It's tempting to treat "the price is now
below what they paid" as a discount and sort by it. That inverts the list — it
floats the group's *worst* positions to the top, because a price falling away
from smart money usually means the market learned something they hadn't. So
agreement leads, and then closeness to their entry price in *either* direction.

## What this can't tell you

The leaderboard ranks by raw dollars, which tracks bankroll size at least as much
as skill, and no 30-day window separates skill from luck. Copying a position
means entering at a worse price than they got. You can't see their hedges, their
bankroll, or why they took the bet — a position that looks like conviction may be
one leg of something larger.

Treat all of it as a research shortlist, not advice.
