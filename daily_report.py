#!/usr/bin/env python3
"""
Build a standalone Polymarket smart-money report.

Reads Polymarket's public APIs (no key, no auth), works out what the most
profitable wallets currently agree on, and writes a self-contained report.html
with every number baked into the file. Nothing is fetched when the page is
opened, so it renders anywhere -- including under a strict CSP.

Usage:  python daily_report.py [--wallets 40] [--out report.html]
"""

import argparse
import json
import math
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# A position pinned to either extreme is over in practice: no edge left to copy,
# and its long-settled loss would otherwise swamp the P&L.
DECIDED_LOW, DECIDED_HIGH = 0.01, 0.99
DUST = 500.0            # ignore positions too small to signal conviction
MIN_CONSENSUS = 3       # how many wallets must independently agree


def get(url, tries=3):
    """GET JSON, retrying briefly -- a single blip shouldn't kill the report."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "smart-money-report/1.0",
            })
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                      # noqa: BLE001
            last = e
    raise RuntimeError(f"{url} failed after {tries} tries: {last}")


def clean_name(name, addr):
    """Most 'names' are just the address again, sometimes with a timestamp."""
    if not name:
        return addr[:6] + "…" + addr[-4:]
    bare = name.split("-")[0]
    if bare.lower().startswith("0x") and len(bare) >= 40:
        return addr[:6] + "…" + addr[-4:]
    return name if len(name) <= 26 else name[:25] + "…"


def leaderboard(window, limit):
    rows = get(f"{LB}/profit?window={window}&limit={limit}")
    out = []
    for r in rows:
        addr = (r.get("proxyWallet") or "").lower()
        if not addr:
            continue
        out.append({
            "address": addr,
            "name": clean_name(r.get("name") or r.get("pseudonym"), addr),
            "pnl": float(r.get("amount") or 0),
        })
    return out


def positions(address):
    """Live positions only. redeemable=false is essential, not cosmetic: heavy
    traders carry hundreds of unredeemed settled bets that would otherwise fill
    the 500-row cap and hide everything that is still tradeable."""
    url = (f"{DATA}/positions?user={address}&limit=500&sizeThreshold=1"
           f"&redeemable=false&sortBy=CURRENT&sortDirection=DESC")
    try:
        rows = get(url)
        return rows if isinstance(rows, list) else []
    except Exception:                               # noqa: BLE001
        return None                                 # distinguish failure from empty


def is_live(p):
    if p.get("redeemable") is True:
        return False
    if float(p.get("currentValue") or 0) <= 0:
        return False
    price = float(p.get("curPrice") or 0)
    return DECIDED_LOW <= price <= DECIDED_HIGH


def aggregate(fetched):
    """Group by outcome token, not by market: two wallets can sit on opposite
    sides of the same market, and merging those would invent agreement."""
    wallet_total, decided = {}, 0
    for w, rows in fetched:
        if rows is None:
            continue
        wallet_total[w["address"]] = sum(
            float(p.get("currentValue") or 0) for p in rows if is_live(p))
        decided += sum(1 for p in rows if not is_live(p))

    grand = sum(wallet_total.values())
    active = [a for a, v in wallet_total.items() if v > 0]

    by_asset = {}
    for w, rows in fetched:
        if rows is None:
            continue
        wtot = wallet_total.get(w["address"], 0.0)
        for p in rows:
            if not is_live(p):
                continue
            key = p.get("asset")
            value = float(p.get("currentValue") or 0)
            size = float(p.get("size") or 0)
            e = by_asset.setdefault(key, {
                "title": p.get("title") or "Untitled market",
                "outcome": p.get("outcome") or "?",
                "eventSlug": p.get("eventSlug") or "",
                "endDate": (p.get("endDate") or "")[:10],
                "curPrice": float(p.get("curPrice") or 0),
                "value": 0.0, "size": 0.0, "entry_notional": 0.0,
                "pnl": 0.0, "holders": [],
            })
            e["value"] += value
            e["size"] += size
            e["entry_notional"] += size * float(p.get("avgPrice") or 0)
            e["pnl"] += float(p.get("cashPnl") or 0)
            e["curPrice"] = float(p.get("curPrice") or e["curPrice"])
            e["holders"].append({
                "name": w["name"],
                "value": value,
                "share_of_own": (value / wtot) if wtot > 0 else 0.0,
            })

    rows_out = []
    for e in by_asset.values():
        avg_entry = e["entry_notional"] / e["size"] if e["size"] > 0 else 0.0
        e["avg_entry"] = avg_entry
        e["consensus"] = len(e["holders"])
        e["share"] = (e["value"] / grand) if grand > 0 else 0.0
        rows_out.append(e)

    return {
        "rows": rows_out,
        "book": grand,
        "active": len(active),
        "decided": decided,
        "open_bets": len(rows_out),
        "pnl": sum(r["pnl"] for r in rows_out),
    }


def rank(rows):
    """Agreement leads. Entry quality is about being CLOSE to what they paid --
    far above means the move already happened, far below usually means the
    market learned something they hadn't, which is a warning not a discount."""
    out = []
    for r in rows:
        if r["consensus"] < MIN_CONSENSUS:
            continue
        if not (0.02 < r["curPrice"] < 0.98):
            continue
        if r["value"] < DUST:
            continue
        entry_room = ((r["avg_entry"] - r["curPrice"]) / r["avg_entry"]
                      if r["avg_entry"] > 0 else 0.0)
        conviction = math.log2(1 + r["consensus"]) * 2 + min(1.5, r["share"] * 20)
        proximity = 1 - min(1, abs(entry_room) / 0.25)
        r = dict(r)
        r["entry_room"] = entry_room
        r["score"] = conviction + proximity * 0.8
        out.append(r)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ----------------------------------------------------------------- rendering

def usd(n):
    a, s = abs(n), "-" if n < 0 else ""
    if a >= 1e9:
        return f"{s}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a/1e6:.2f}M"
    if a >= 1e3:
        return f"{s}${a/1e3:.1f}k"
    return f"{s}${a:.0f}"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def read_pill(room):
    if room > 0.25:
        return ("warn", "down hard since they bought")
    if room > 0.05:
        return ("accent", "below their entry")
    if room > -0.05:
        return ("yes", "near their entry")
    if room > -0.25:
        return ("other", "moved up")
    return ("warn", "move already happened")


CSS = """
:root{--ground:#F7F8F7;--surface:#FFF;--surface-2:#F0F3F1;--line:#DCE3DF;
--ink:#12201C;--ink-2:#4A5C55;--ink-3:#7A8A83;--accent:#0E7C66;--accent-soft:#E2F0EB;
--pos:#1A7F4B;--neg:#B4453A;--warn:#B0761E;--pos-soft:#E4F1E8;--neg-soft:#F8E7E4;
--mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#0E1513;--surface:#16201D;--surface-2:#1C2723;--line:#2A3733;
--ink:#E8EFEB;--ink-2:#A3B3AC;--ink-3:#74857E;--accent:#4FC0A3;--accent-soft:#17332C;
--pos:#52C07E;--neg:#E2796C;--warn:#D9A44A;--pos-soft:#142E20;--neg-soft:#331C19}}
:root[data-theme="dark"]{--ground:#0E1513;--surface:#16201D;--surface-2:#1C2723;
--line:#2A3733;--ink:#E8EFEB;--ink-2:#A3B3AC;--ink-3:#74857E;--accent:#4FC0A3;
--accent-soft:#17332C;--pos:#52C07E;--neg:#E2796C;--warn:#D9A44A;
--pos-soft:#142E20;--neg-soft:#331C19}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:34px 20px 60px;
display:flex;flex-direction:column;gap:22px}
header h1{margin:0 0 4px;font-size:23px;font-weight:650;letter-spacing:-.02em}
header .when{font-family:var(--mono);font-size:11px;color:var(--ink-3);
text-transform:uppercase;letter-spacing:.09em}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:5px;
padding:11px 13px}
.stat dt{margin:0;font-family:var(--mono);font-size:9.5px;font-weight:600;
text-transform:uppercase;letter-spacing:.1em;color:var(--ink-3)}
.stat dd{margin:2px 0 0;font-family:var(--mono);font-size:19px;font-weight:600;
font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.stat .sub{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;
overflow:hidden}
.panel-head{padding:12px 15px;border-bottom:1px solid var(--line);
display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.panel-head h2{margin:0;font-size:13.5px;font-weight:620}
.panel-head p{margin:0;font-size:12px;color:var(--ink-3)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--surface-2);font-family:var(--mono);font-size:9.5px;
font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);
text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;
white-space:nowrap}
th.num{text-align:right}
.pos{color:var(--pos)}.neg{color:var(--neg)}.dim{color:var(--ink-3)}
.mkt{min-width:250px;max-width:430px}
.mkt a{color:var(--ink);text-decoration:none;font-weight:500}
.mkt a:hover{color:var(--accent);text-decoration:underline}
.mkt .sub{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}
.pill{display:inline-block;padding:1px 7px;border-radius:3px;font-family:var(--mono);
font-size:10.5px;font-weight:600;white-space:nowrap}
.pill-yes{background:var(--pos-soft);color:var(--pos)}
.pill-no{background:var(--neg-soft);color:var(--neg)}
.pill-other{background:var(--surface-2);color:var(--ink-2)}
.pill-accent{background:var(--accent-soft);color:var(--accent)}
.pill-warn{background:var(--surface-2);color:var(--warn)}
.dots{display:inline-flex;gap:2px;align-items:center;justify-content:flex-end}
.dots i{width:5px;height:5px;border-radius:50%;background:var(--accent)}
.dots .more{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);margin-left:3px}
.note{background:var(--surface-2);border:1px solid var(--line);
border-left:2px solid var(--warn);border-radius:4px;padding:11px 14px;
font-size:12.5px;color:var(--ink-2)}
.note b{color:var(--ink)}
footer{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
border-top:1px solid var(--line);padding-top:14px}
"""


def render(agg, ranked, meta):
    stamp = meta["generated"].strftime("%d %b %Y, %H:%M UTC")
    pnl = agg["pnl"]

    rows_html = []
    for i, r in enumerate(ranked[:25], 1):
        kind, label = read_pill(r["entry_room"])
        side = r["outcome"].lower()
        side_cls = ("pill-yes" if side == "yes"
                    else "pill-no" if side == "no" else "pill-other")
        href = (f"https://polymarket.com/event/{r['eventSlug']}"
                if r["eventSlug"] else "https://polymarket.com")
        n = r["consensus"]
        dots = "<i></i>" * min(n, 6)
        room = r["entry_room"] * 100
        rows_html.append(f"""<tr>
<td class="num dim">{i}</td>
<td class="mkt"><a href="{esc(href)}" target="_blank" rel="noopener noreferrer">{esc(r['title'])}</a>
<div class="sub">{'ends ' + esc(r['endDate']) if r['endDate'] else ''}</div></td>
<td><span class="pill {side_cls}">{esc(r['outcome'])}</span></td>
<td class="num"><span class="dots">{dots}<span class="more">{n}</span></span></td>
<td class="num">{r['share']*100:.2f}%</td>
<td class="num">{usd(r['value'])}</td>
<td class="num dim">{r['avg_entry']*100:.1f}¢</td>
<td class="num">{r['curPrice']*100:.1f}¢</td>
<td class="num {'pos' if room >= 0 else 'neg'}">{room:+.0f}%</td>
<td><span class="pill pill-{kind}">{esc(label)}</span></td></tr>""")

    empty = ('<div class="note">No position is held by at least '
             f'{MIN_CONSENSUS} of these wallets right now. That is a real '
             'answer, not a failure — the smart money simply is not clustered '
             'today.</div>')

    table = f"""<div class="panel">
<div class="panel-head"><h2>Where the money agrees</h2>
<p>{len(ranked)} position{'' if len(ranked)==1 else 's'} held by {MIN_CONSENSUS}+ of the top wallets</p></div>
<div class="scroll"><table><thead><tr>
<th class="num">#</th><th>Market</th><th>Side</th><th class="num">Wallets</th>
<th class="num">Share</th><th class="num">Value</th><th class="num">They paid</th>
<th class="num">Now</th><th class="num">vs entry</th><th>Read</th>
</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div></div>"""

    return f"""<title>Smart Money Daily</title>
<style>{CSS}</style>
<div class="wrap">
<header>
<h1>Where Polymarket's sharpest money sits today</h1>
<div class="when">Generated {stamp} · top {meta['requested']} wallets by 30-day profit</div>
</header>

<dl class="stats">
<div class="stat"><dt>Wallets read</dt><dd>{agg['active']}</dd>
<span class="sub">of {meta['requested']} ranked{f", {meta['failed']} unreachable" if meta['failed'] else ""}</span></div>
<div class="stat"><dt>Live book</dt><dd>{usd(agg['book'])}</dd>
<span class="sub">open bets only</span></div>
<div class="stat"><dt>Open bets</dt><dd>{agg['open_bets']:,}</dd>
<span class="sub">{agg['decided']:,} decided, excluded</span></div>
<div class="stat"><dt>Agreed positions</dt><dd>{len(ranked)}</dd>
<span class="sub">{MIN_CONSENSUS}+ wallets on one side</span></div>
<div class="stat"><dt>Unrealised P&amp;L</dt>
<dd class="{'pos' if pnl >= 0 else 'neg'}">{usd(pnl)}</dd>
<span class="sub">on those open bets</span></div>
</dl>

{table if rows_html else empty}

<div class="note"><b>How to read this.</b> Agreement comes first: several of the
most profitable wallets independently holding the same side, with real money
behind it. <b>vs entry</b> compares today's price to what they paid — near zero
is the sweet spot. A large positive number means the price fell away from them,
which usually means the market learned something they hadn't, so treat it as a
warning rather than a discount. A large negative number means the move already
happened and you would be buying in late.</div>

<div class="note"><b>What this cannot tell you.</b> These wallets are ranked by
raw profit, which tracks bankroll size as much as skill, and a 30-day window
cannot separate skill from luck. Copying a position always means entering at a
worse price than they got, and you cannot see their hedges, their bankroll, or
why they took the bet. This is a research shortlist, not advice.</div>

<footer><a href="index.html" style="color:var(--accent)">Open the live tracker</a>
· Source: Polymarket public API (lb-api leaderboard, data-api positions).
Settled and decided markets excluded. Rebuilt automatically each morning.</footer>
</div>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", type=int, default=40)
    ap.add_argument("--window", default="30d", choices=["1d", "7d", "30d", "all"])
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()

    print(f"Fetching top {args.wallets} wallets by {args.window} profit…")
    wallets = leaderboard(args.window, args.wallets)
    print(f"  got {len(wallets)}")

    print("Fetching live positions…")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda w: (w, positions(w["address"])), wallets))
    failed = sum(1 for _, r in results if r is None)
    print(f"  {len(results) - failed} ok, {failed} failed")

    agg = aggregate(results)
    ranked = rank(agg["rows"])
    print(f"  {agg['open_bets']} open bets, {agg['decided']} decided excluded, "
          f"{len(ranked)} with {MIN_CONSENSUS}+ agreeing")

    html = render(agg, ranked, {
        "generated": datetime.now(timezone.utc),
        "requested": len(wallets),
        "failed": failed,
    })
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
