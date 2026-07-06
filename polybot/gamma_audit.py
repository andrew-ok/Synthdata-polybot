"""Label audit: validate our price-chain outcome labels against Polymarket's
Gamma API official market outcomes (the other branch's settlement source).

Every backtest number we've produced rests on outcomes derived from Synth
start-price chains. If those labels are wrong even a few % of the time, the
thin edges we measured could be artifacts. Gamma is authoritative (it's the
actual market resolution). Gamma calls are Polymarket's free API — zero Synth
quota. Samples hourly windows across both 4-week periods.

Run: python -m polybot.gamma_audit
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import CONFIG
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _ENTRY_OFFSET_SEC

STEP = 3600
SAMPLE_EVERY = 11   # every 11th hourly window -> ~30 samples/period/asset


def gamma_outcome(slug: str):
    url = f"{CONFIG.polymarket_gamma_url}/markets?slug={slug}&closed=true"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polybot/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not data:
        return None
    m = data[0]
    # Gamma resolved binary: outcomePrices like ["1","0"] with outcomes ["Up","Down"]
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
    except (TypeError, ValueError):
        return None
    if len(prices) != len(outcomes) or not prices:
        return None
    if not m.get("closed", False):
        return None
    winner = max(range(len(prices)), key=lambda i: float(prices[i]))
    label = str(outcomes[winner]).strip().upper()
    return label if label in ("UP", "DOWN") else None


def main() -> int:
    client = SynthInsightsClient()
    periods = [("2026-05-08T21:00:00Z", "2026-06-05T21:00:00Z"),
               ("2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z")]
    agree = disagree = unresolved = 0
    mismatches = []
    for s_iso, e_iso in periods:
        start = datetime.fromisoformat(s_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(e_iso.replace("Z", "+00:00"))
        events = _walk(start, end, STEP)
        for asset in CONFIG.synth_assets:
            # build price-chain map from cached early snapshots
            bw = {}
            for ws in events:
                opp = client.fetch(asset, "1H", start_time=_iso_zulu(ws + timedelta(seconds=_ENTRY_OFFSET_SEC)))
                if opp is None:
                    continue
                if opp.start_price:
                    bw[opp.event_start_time] = {"sp": opp.start_price, "slug": opp.slug}
            sampled = sorted(bw)[::SAMPLE_EVERY]
            for w in sampled:
                nxt = bw.get(w + timedelta(seconds=STEP))
                if not nxt or nxt["sp"] == bw[w]["sp"]:
                    continue
                ours = "UP" if nxt["sp"] > bw[w]["sp"] else "DOWN"
                slug = bw[w]["slug"]
                if not slug:
                    continue
                official = gamma_outcome(slug)
                if official is None:
                    unresolved += 1
                    continue
                if official == ours:
                    agree += 1
                else:
                    disagree += 1
                    mismatches.append((asset, str(w), slug, ours, official))
    total = agree + disagree
    print(f"Label audit vs Polymarket Gamma official outcomes")
    print(f"  compared: {total}  agree: {agree}  DISAGREE: {disagree}  (gamma-unresolved/skipped: {unresolved})")
    if total:
        print(f"  label accuracy: {agree/total*100:.1f}%")
    for m in mismatches[:10]:
        print(f"  MISMATCH: {m[0]} {m[1]} {m[2]} ours={m[3]} gamma={m[4]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
