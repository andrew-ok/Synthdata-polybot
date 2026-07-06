"""Required-edge curve: at each entry price, how much Synth-reported edge is
needed for positive EV?

Theory: buying at all-in entry E (price + slippage), EV per $1 risked is
(p_true - E)/E — positive iff p_true > E. So the TRUE-probability hurdle at
85c is just ~85.4%. The real question is empirical: how much REPORTED edge
(synth_prob - price) does it take before the REALIZED win rate clears that
hurdle? Synth's reported probabilities are not truth; this measures the
exchange rate between reported edge and real edge, per price bucket.

Data: cached 8-week late-window hourly favorites (both assets, both sides).
Run: python -m polybot.edge_curve
"""
from __future__ import annotations

from typing import Dict, List

from .c_sweep import build_all

SLIP = 0.005


def main() -> int:
    recs = build_all()
    # candidate favorite-side entries with reported edge >= 0 (all, no threshold)
    rows = []
    for r in recs:
        for side, ask, p in (("UP", r["yes_ask"], r["p_up"]), ("DOWN", r["no_ask"], 1 - r["p_up"])):
            if 0.70 <= ask <= 0.92 and p - ask >= 0.0:
                won = (side == r["outcome"])
                rows.append({"price": ask, "edge": p - ask, "won": won})
                break
    print(f"{len(rows)} favorite-side candidates (price 0.70-0.92, any reported edge >= 0)\n")

    price_buckets = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.92)]
    edge_bins = [(0.00, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 0.15)]

    print(f"{'price':>10} {'breakeven':>10} | " +
          " | ".join(f"{'edge %.2f-%.2f' % (a,b):^20s}" for a, b in edge_bins))
    print(f"{'':>10} {'win% need':>10} | " +
          " | ".join(f"{'n  win%  EV/$1':^20s}" for _ in edge_bins))
    print("-" * (24 + 23 * len(edge_bins)))
    for plo, phi in price_buckets:
        sub = [r for r in rows if plo <= r["price"] < phi]
        mid = (plo + phi) / 2
        be = mid * (1 + SLIP / mid)          # all-in entry = break-even win rate
        cells = []
        for elo, ehi in edge_bins:
            ss = [r for r in sub if elo <= r["edge"] < ehi]
            if len(ss) < 8:
                cells.append(f"{'(n<8)':^20s}")
                continue
            n = len(ss)
            win = sum(1 for r in ss if r["won"]) / n
            avg_entry = sum(r["price"] for r in ss) / n + SLIP
            ev = (win - avg_entry) / avg_entry
            cells.append(f"{n:>4} {win*100:>4.0f}% {ev:>+7.3f}")
        print(f"{f'{plo:.2f}-{phi:.2f}':>10} {be*100:>9.1f}% | " + " | ".join(cells))

    print("\nbreakeven win% = all-in entry cost (price + slippage). EV/$1 = (win% - entry)/entry.")
    print("Positive EV requires realized win% > breakeven column, NOT reported edge > 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
