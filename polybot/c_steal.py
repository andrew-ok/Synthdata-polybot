"""Test the other branch's (strategy-b-calibrated-ranking) parameter ideas
against our 8 weeks of cached late-window data.

Their LATE_WINDOW_PROFILE: 1H, <20min left, market price >= 0.75, Synth
conviction >= 0.82, net edge >= 0.08, taker fill.

Variants tested (per-week consistency, same scoring as all prior tests):
  A. THEIR exact profile
  B. their profile, edge floor relaxed to our curve (>=0.03)
  C. their profile, price floor relaxed to 0.70
  D. our RAMP baseline (current live)
  E. our RAMP + their conviction floor (>=0.82)   <- the hybrid to evaluate
  F. our RAMP + conviction >= 0.90 (stricter)

Conviction = Synth's probability for the side being bought.
Zero new Synth API calls. Run: python -m polybot.c_steal
"""
from __future__ import annotations

from typing import Dict, List

from .c_sweep import build_all
from .strategy_lab import _stats

SLIP = 0.005
STAKE = 37.5


def rule_factory(min_price, max_price, min_edge, min_conviction, ramp=False):
    def f(c):
        p, e, conv = c["price"], c["edge"], c["conviction"]
        if not (min_price <= p <= max_price):
            return False
        req = (0.015 + 0.22 * (p - 0.70)) if ramp else min_edge
        if e < req:
            return False
        if conv < min_conviction:
            return False
        return True
    return f


def main() -> int:
    recs = build_all()
    cands = []
    for r in recs:
        for side, ask, prob in (("UP", r["yes_ask"], r["p_up"]), ("DOWN", r["no_ask"], 1 - r["p_up"])):
            if ask >= 0.60 and prob - ask >= 0.0:
                cands.append({"t": r["t"], "side": side, "price": ask,
                              "edge": prob - ask, "conviction": prob,
                              "outcome": r["outcome"]})
                break
    origin = min(c["t"] for c in cands)

    variants = [
        ("A THEIRS: p>=.75 conv>=.82 e>=.08", rule_factory(0.75, 0.95, 0.08, 0.82)),
        ("B theirs, edge relaxed >=.03", rule_factory(0.75, 0.95, 0.03, 0.82)),
        ("C theirs, price floor .70", rule_factory(0.70, 0.95, 0.08, 0.82)),
        ("D our RAMP (live baseline)", rule_factory(0.70, 0.92, None, 0.0, ramp=True)),
        ("E RAMP + conv>=.82 (hybrid)", rule_factory(0.70, 0.92, None, 0.82, ramp=True)),
        ("F RAMP + conv>=.90", rule_factory(0.70, 0.92, None, 0.90, ramp=True)),
    ]

    n_weeks = 8
    hdr = "".join(f"{'W'+str(w+1):>8s}" for w in range(n_weeks))
    print(f"{len(cands)} candidates, 8 weeks\n")
    print(f"{'variant':34s}{hdr}{'  wks+':>7s}{'    n':>6s}{' win%':>6s}{' avg/$':>7s}{'  cum$':>7s}")
    print("-" * (34 + 8 * n_weeks + 33))
    for name, rule in variants:
        trades = [(c["side"], min(0.999, c["price"] + SLIP), c["outcome"], c["t"])
                  for c in cands if rule(c)]
        wk: Dict[int, List] = {}
        for s, e, o, t in trades:
            wk.setdefault(int((t - origin).total_seconds() // (7 * 86400)), []).append((s, e, o))
        row, pos, tot = "", 0, 0
        for w in range(n_weeks):
            sub = wk.get(w, [])
            if len(sub) >= 3:
                st = _stats(sub)
                row += f"{st['avg']:>+8.2f}"
                tot += 1
                pos += 1 if st["avg"] > 0 else 0
            else:
                row += f"{'--':>8s}"
        agg = _stats([(s, e, o) for s, e, o, _ in trades])
        print(f"{name:34s}{row}{pos:>4d}/{tot}{agg['n']:>6d}{agg['win']*100:>5.0f}%{agg['avg']:>+7.2f}{agg['pnl']:>+7.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
