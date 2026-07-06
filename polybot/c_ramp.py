"""8-week per-week replay: price-scaled edge ramp vs per-5c-bucket hurdles vs
current production C.

Strategies:
  PROD C (baseline)   : flat edge >= 0.05, band 0.70-0.90
  RAMP                : required_edge(P) = 0.015 + 0.22*(P-0.70), band 0.70-0.92
                        (~1.5pp @70c, ~3.7pp @80c, ~5.9pp @90c — from the
                        empirical required-edge curve)
  RAMP+CAP            : ramp, but skip reported edges > 12pp (the recurring
                        "huge disagreement = Synth overconfident" pattern)
  5c buckets          : each price bucket alone, at its empirical hurdle:
                        70-75 @ >=1.5pp, 75-80 @ >=1.5pp, 80-85 @ >=3pp,
                        85-90 @ >=5pp, 90-95 @ >=5pp (expected to fail)

All from cached snapshots (zero new API calls); outcomes from the settlement
price chain; entries at ask + 0.5% slippage; $37.50 fixed stake for cum $.
NOTE: ramp parameters are fitted on this same 8 weeks — per-week consistency is
the only honesty check available until forward data accumulates.

Run: python -m polybot.c_ramp
"""
from __future__ import annotations

from typing import Dict, List

from .c_sweep import build_all
from .strategy_lab import _stats

SLIP = 0.005
STAKE = 37.5


def _candidates(recs):
    out = []
    for r in recs:
        for side, ask, p in (("UP", r["yes_ask"], r["p_up"]), ("DOWN", r["no_ask"], 1 - r["p_up"])):
            if ask >= 0.60 and p - ask >= 0.0:
                out.append({"t": r["t"], "side": side, "price": ask,
                            "edge": p - ask, "outcome": r["outcome"]})
                break
    return out


def ramp_rule(lo=0.70, hi=0.92, cap=None):
    def f(c):
        if not (lo <= c["price"] <= hi):
            return False
        req = 0.015 + 0.22 * (c["price"] - 0.70)
        if c["edge"] < req:
            return False
        if cap is not None and c["edge"] > cap:
            return False
        return True
    return f


def flat_rule(edge, lo, hi):
    return lambda c: lo <= c["price"] <= hi and c["edge"] >= edge


def score(cands, rule, origin):
    trades = [(c["side"], min(0.999, c["price"] + SLIP), c["outcome"], c["t"])
              for c in cands if rule(c)]
    wk: Dict[int, List] = {}
    for s, e, o, t in trades:
        wk.setdefault(int((t - origin).total_seconds() // (7 * 86400)), []).append((s, e, o))
    cells = {}
    pos = tot = 0
    for w, sub in wk.items():
        st = _stats(sub)
        cells[w] = st
        if st["n"] >= 3:
            tot += 1
            pos += 1 if st["avg"] > 0 else 0
    agg = _stats([(s, e, o) for s, e, o, _ in trades])
    return cells, agg, pos, tot


def main() -> int:
    recs = build_all()
    cands = _candidates(recs)
    origin = min(c["t"] for c in cands)
    n_weeks = 8

    strategies = [
        ("PROD C: flat >=5pp, 70-90c", flat_rule(0.05, 0.70, 0.90)),
        ("RAMP: 1.5pp@70c -> 5.9pp@90c", ramp_rule()),
        ("RAMP + cap 12pp", ramp_rule(cap=0.12)),
        ("bucket 70-75c @ >=1.5pp", flat_rule(0.015, 0.70, 0.75)),
        ("bucket 75-80c @ >=1.5pp", flat_rule(0.015, 0.75, 0.80)),
        ("bucket 80-85c @ >=3pp", flat_rule(0.03, 0.80, 0.85)),
        ("bucket 85-90c @ >=5pp", flat_rule(0.05, 0.85, 0.90)),
        ("bucket 90-95c @ >=5pp", flat_rule(0.05, 0.90, 0.95)),
    ]

    hdr = "".join(f"{'W'+str(w+1):>9s}" for w in range(n_weeks))
    print(f"{len(cands)} favorite candidates over 8 weeks "
          f"({min(c['t'] for c in cands).date()}..{max(c['t'] for c in cands).date()})\n")
    print(f"{'strategy (weekly avg/$1)':30s}{hdr}{'  wks+':>7s}{'    n':>6s}{'  win%':>7s}{'  avg/$':>8s}{'  cum$':>8s}")
    print("-" * (30 + 9 * n_weeks + 36))
    for name, rule in strategies:
        cells, agg, pos, tot = score(cands, rule, origin)
        row = ""
        for w in range(n_weeks):
            st = cells.get(w)
            row += f"{st['avg']:>+9.2f}" if st and st["n"] >= 3 else f"{'--':>9s}"
        print(f"{name:30s}{row}{pos:>4d}/{tot}{agg['n']:>6d}{agg['win']*100:>6.0f}%{agg['avg']:>+8.2f}{agg['pnl']:>+8.0f}")
    print("\n'--' = fewer than 3 trades that week. cum$ at fixed $37.50/trade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
