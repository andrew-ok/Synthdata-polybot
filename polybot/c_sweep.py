"""Sensitivity map for Strategy C: entry-price band x edge threshold, over the
cached 8 weeks (both 4-week periods), pure rule (raw Synth, favorite side).

Purpose is NOT to pick the single best cell (that's overfitting) — it's to see
whether the edge lives on a PLATEAU (robust: neighboring cells agree) or in a
spike (fragile: one lucky cell), and to answer:
  - is 5pp enough edge, or is 7pp the floor?
  - do cheaper favorites (75-85c) or expensive ones (85-95c) carry the edge?

Each cell: n trades / win% / avg return per $1 / weeks-positive-of-8.
Zero new API calls. Run: python -m polybot.c_sweep
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

from .c_faithful import build as _build_period
import polybot.c_faithful as CF
from .strategy_lab import _stats

SLIP = 0.005
PERIODS = [("2026-05-08T21:00:00Z", "2026-06-05T21:00:00Z"),
           ("2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z")]


def build_all() -> List[Dict]:
    recs: List[Dict] = []
    for s, e in PERIODS:
        CF.START, CF.END = s, e
        recs.extend(_build_period())
    recs.sort(key=lambda r: r["t"])
    return recs


def cell(recs, lo, hi, edge):
    trades = []
    for r in recs:
        for side, ask, p in (("UP", r["yes_ask"], r["p_up"]), ("DOWN", r["no_ask"], 1 - r["p_up"])):
            if lo <= ask <= hi and p - ask >= edge:
                trades.append((side, min(0.999, ask + SLIP), r["outcome"], r["t"]))
                break
    return trades


def weeks_positive(trades, origin):
    wk: Dict[int, List] = {}
    for s, e, o, t in trades:
        wk.setdefault(int((t - origin).total_seconds() // (7 * 86400)), []).append((s, e, o))
    pos = tot = 0
    for w, sub in wk.items():
        if len(sub) >= 3:
            tot += 1
            if _stats(sub)["avg"] > 0:
                pos += 1
    return pos, tot


def main() -> int:
    recs = build_all()
    origin = recs[0]["t"]
    print(f"{len(recs)} late-window hourly observations, "
          f"{recs[0]['t'].date()}..{recs[-1]['t'].date()}\n")

    bands = [(0.70, 0.80), (0.80, 0.90), (0.85, 0.95), (0.70, 0.95), (0.75, 0.90), (0.80, 0.95)]
    edges = [0.03, 0.05, 0.07, 0.10]

    print(f"{'band':>12} | " + " | ".join(f"{'edge>='+str(e):^24s}" for e in edges))
    print(f"{'':>12} | " + " | ".join(f"{'n win% avg/$ wks+':^24s}" for _ in edges))
    print("-" * (15 + 27 * len(edges)))
    for lo, hi in bands:
        row = []
        for e in edges:
            tr = cell(recs, lo, hi, e)
            st = _stats([(s, en, o) for s, en, o, _ in tr])
            wp, wt = weeks_positive(tr, origin)
            row.append(f"{st['n']:>4} {st['win']*100:>3.0f}% {st['avg']:>+5.2f} {wp}/{wt}")
        print(f"{f'{lo:.2f}-{hi:.2f}':>12} | " + " | ".join(f"{c:^24s}" for c in row))

    # per-period split for the headline cells
    print("\nStability check (May period vs June period), band 0.70-0.95:")
    cut = datetime.fromisoformat("2026-06-05T21:00:00+00:00")
    for e in edges:
        a = [t for t in cell(recs, .70, .95, e) if t[3] < cut]
        b = [t for t in cell(recs, .70, .95, e) if t[3] >= cut]
        sa = _stats([(s, en, o) for s, en, o, _ in a])
        sb = _stats([(s, en, o) for s, en, o, _ in b])
        print(f"  edge>={e:.2f}:  May n={sa['n']:>3} avg={sa['avg']:>+5.2f}  |  Jun n={sb['n']:>3} avg={sb['avg']:>+5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
