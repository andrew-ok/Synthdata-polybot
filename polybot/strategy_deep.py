"""Deeper, theory-motivated search — the angles the earlier tests missed.

Motivated (not fished): each variant has a prior reason it might work.
  - MAKER execution (fill at mid, not ask): Synth's bot likely POSTS limit
    orders and captures spread; a thin edge dies as a taker but survives as a
    maker. Biggest untested lever.
  - 1h ONLY: Synth's public bot trades BTC hourly; 15m dominated prior tests.
  - EXTREME conviction only: Synth is accurate in the tails (p>0.70 -> ~80% UP),
    wrong in the middle. Trade only the tails.
  - LARGE edge only: the biggest Synth-vs-market disagreements.

Every variant scored PER WEEK across 4 weeks (consistency = the honesty filter).
Maker mid = (yes_ask + (1-no_ask))/2 for UP; ((no_ask)+(1-yes_ask))/2 for DOWN.
No new API calls. Run: python -m polybot.strategy_deep
"""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, List

from .strategy_lab import build_dataset, _stats

START, END = "2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z"
SLIP = 0.005


def _entry(r, side, maker):
    if side == "UP":
        ask, bid = r["yes_ask"], max(0.0, 1.0 - r["no_ask"])
    else:
        ask, bid = r["no_ask"], max(0.0, 1.0 - r["yes_ask"])
    if maker:                      # optimistic maker fill at mid, no slippage
        return min(0.999, (ask + bid) / 2.0)
    return min(0.999, ask + SLIP)  # taker: cross + slippage


def _run(recs, min_edge, maker=False, horizon=None, p_hi=None, p_lo=None, min_price=0.0):
    trades = []
    for r in recs:
        if horizon and r["horizon"] != horizon:
            continue
        side = None
        # extreme-conviction override if p_hi/p_lo set
        if p_hi is not None:
            if r["p_up"] >= p_hi:
                side = "UP"
            elif r["p_up"] <= p_lo:
                side = "DOWN"
            else:
                continue
        else:
            if r["p_up"] - r["yes_ask"] >= min_edge:
                side = "UP"
            elif (1 - r["p_up"]) - r["no_ask"] >= min_edge:
                side = "DOWN"
            else:
                continue
        entry = _entry(r, side, maker)
        if entry < min_price or entry <= 0 or entry >= 1:
            continue
        trades.append((side, entry, r["outcome"]))
    return _stats(trades)


def _week(recs):
    o = recs[0]["t"]
    out: Dict[int, List] = {}
    for r in recs:
        out.setdefault(int((r["t"] - o).total_seconds() // (7 * 86400)), []).append(r)
    return out


def main() -> int:
    recs = build_dataset(START, END)
    wk = _week(recs)
    ids = sorted(wk)
    variants = [
        ("raw .20  TAKER  (baseline)", dict(min_edge=.20, maker=False)),
        ("raw .20  MAKER (mid fill)", dict(min_edge=.20, maker=True)),
        ("raw .20  1h TAKER", dict(min_edge=.20, maker=False, horizon="1H")),
        ("raw .20  1h MAKER", dict(min_edge=.20, maker=True, horizon="1H")),
        ("extreme >.70/<.05 TAKER", dict(min_edge=0, maker=False, p_hi=.70, p_lo=.05)),
        ("extreme >.70/<.05 MAKER", dict(min_edge=0, maker=True, p_hi=.70, p_lo=.05)),
        ("large edge >.40 TAKER", dict(min_edge=.40, maker=False)),
        ("large edge >.40 MAKER", dict(min_edge=.40, maker=True)),
        ("large >.40 1h MAKER", dict(min_edge=.40, maker=True, horizon="1H")),
    ]
    wcols = "".join(f"{'W'+str(w+1):>8s}" for w in ids)
    print(f"4 weeks, {len(recs)} windows\n")
    print(f"{'variant':30s}{wcols}{'  +wks':>7s}{'  aggAvg':>9s}{'  aggN':>7s}")
    print("-" * (30 + 8 * len(ids) + 23))
    for name, kw in variants:
        cells, pos = [], 0
        for w in ids:
            s = _run(wk[w], **kw)
            cells.append(f"{s['avg']:>+8.2f}" if s["n"] >= 8 else f"{'(n<8)':>8s}")
            if s["n"] >= 8 and s["avg"] > 0:
                pos += 1
        agg = _run(recs, **kw)
        mark = " <<" if pos == len(ids) else ("  <" if pos == len(ids) - 1 else "")
        print(f"{name:30s}{''.join(cells)}{pos:>5d}/{len(ids)}{agg['avg']:>+9.2f}{agg['n']:>7d}{mark}")
    print("\n+wks = weeks with positive avg (n>=8). << all weeks, < all but one.")
    print("MAKER assumes an optimistic mid-price fill (upper bound on spread capture).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
