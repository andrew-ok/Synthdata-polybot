"""Per-week consistency test over the full multi-week backfill.

The point is NOT "what won aggregate" (re-mineable). It's: does a strategy stay
positive across INDEPENDENT weeks, or does the winner keep changing? A real edge
persists week to week; a regime artifact flips sign. This is a poor-man's PBO /
cross-validation on a PRE-REGISTERED, FIXED strategy set (no new strategies
invented here — adding more trials just raises the noise ceiling per the
Deflated-Sharpe / False-Strategy-Theorem literature).

No new API calls (cached snapshots). Run:
  python -m polybot.strategy_multiweek 2026-06-05T21:00:00Z 2026-07-03T21:00:00Z
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from .strategy_lab import (build_dataset, _stats, run,
                           raw_edge, fade_edge, extreme_dir, mom_revert,
                           mom_trend, mkt_favorite)

# PRE-REGISTERED strategy set — locked, so we are not fishing across more trials.
STRATS = [
    ("raw edge >=.20 (A)", raw_edge(.20)),
    ("fade edge >=.20", fade_edge(.20)),
    ("fade edge >=.30", fade_edge(.30)),
    ("extreme dir .60/.08", extreme_dir(.60, .08)),
    ("mom reversion .05%", mom_revert(.0005)),
    ("mom reversion .15%", mom_revert(.0015)),
    ("mom continuation .15%", mom_trend(.0015)),
    ("mkt favorite >=.55", mkt_favorite),
]


def _week_index(t: datetime, origin: datetime) -> int:
    return int((t - origin).total_seconds() // (7 * 86400))


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 2 else "2026-06-05T21:00:00Z"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-07-03T21:00:00Z"
    recs = build_dataset(start, end)
    if not recs:
        print("No data — is the backfill finished?")
        return 1
    origin = recs[0]["t"]
    weeks: Dict[int, List[Dict]] = {}
    for r in recs:
        weeks.setdefault(_week_index(r["t"], origin), []).append(r)
    wk_ids = sorted(weeks)
    print(f"Dataset: {len(recs)} windows, {recs[0]['t'].date()}..{recs[-1]['t'].date()}, {len(wk_ids)} weeks")
    for w in wk_ids:
        sub = weeks[w]
        up = sum(1 for r in sub if r["outcome"] == "UP") / len(sub)
        print(f"  week {w+1}: {sub[0]['t'].date()} .. {sub[-1]['t'].date()}  n={len(sub)}  base {up:.0%} UP")
    print()

    # header
    wcols = "".join(f"{'W'+str(w+1):>9s}" for w in wk_ids)
    print(f"{'strategy (avg return / $1 per week)':34s}{wcols}{'  +wks':>7s}{'  aggAvg':>9s}")
    print("-" * (34 + 9 * len(wk_ids) + 16))
    for name, fn in STRATS:
        cells = []
        pos = 0
        for w in wk_ids:
            s = run(weeks[w], fn)
            cells.append(f"{s['avg']:>+9.2f}" if s["n"] >= 10 else f"{'(n<10)':>9s}")
            if s["n"] >= 10 and s["avg"] > 0:
                pos += 1
        agg = run(recs, fn)
        star = " <<" if pos >= len(wk_ids) - 0 else ("  <" if pos >= len(wk_ids) - 1 else "")
        print(f"{name:34s}{''.join(cells)}{pos:>5d}/{len(wk_ids)}{agg['avg']:>+9.2f}{star}")

    print("\n+wks = weeks with positive avg return (n>=10). A real edge -> positive")
    print("in most/all weeks. Sign-flipping across weeks = regime artifact, not edge.")
    print("<< = positive every week   < = positive all but one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
