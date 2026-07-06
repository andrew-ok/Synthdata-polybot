"""Round 2 of mechanism-driven strategy generation — refinements on the C-VETO
platform, tested per-week over the 8 cached weeks.

All mechanisms declared a priori (see module users' notes). Scoring fixes the
double-slippage quirk: entry = ask + SLIP applied exactly once here.

Run: python -m polybot.strategy_gen2
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .strategy_gen import build_joined

SLIP = 0.005
STAKE = 37.5


def _fav(r):
    if 0.70 <= r["yes_ask"] <= 0.92:
        return ("UP", r["yes_ask"], r["p_up"])
    if 0.70 <= r["no_ask"] <= 0.92:
        return ("DOWN", r["no_ask"], 1 - r["p_up"])
    return None


def _stats(trades):
    """trades: (side, ask_raw, outcome, t, stake_mult). Single slippage here."""
    if not trades:
        return {"n": 0, "win": 0.0, "avg": 0.0, "pnl": 0.0, "maxdd": 0.0}
    rets, pnl_curve, cum = [], [], 0.0
    wins = 0
    for s, ask, o, t, m in trades:
        e = min(0.999, ask + SLIP)
        r = (1 - e) / e if s == o else -1.0
        rets.append(r)
        wins += int(s == o)
        cum += r * STAKE * m
        pnl_curve.append(cum)
    peak, mdd = -1e9, 0.0
    for x in pnl_curve:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)
    return {"n": len(rets), "win": wins / len(rets), "avg": sum(rets) / len(rets),
            "pnl": cum, "maxdd": mdd}


def main() -> int:
    recs = build_joined()
    # index cross-asset favorite direction per window time
    fav_dir: Dict = {}
    for r in recs:
        f = _fav(r)
        if f:
            fav_dir[(r["t"], r["asset"])] = f[0]
    moms = sorted(abs(r["early_mom"]) for r in recs if r["early_mom"] is not None)
    med_mom = moms[len(moms) // 2] if moms else 0.0

    def candidates():
        for r in recs:
            f = _fav(r)
            if f is None:
                continue
            side, ask, prob = f
            edge = prob - ask
            if edge < -0.02:            # the VETO — common to all H variants
                continue
            yield r, side, ask, prob, edge

    def base_trades(filt, stake_fn=None):
        out = []
        for r, side, ask, prob, edge in candidates():
            if not filt(r, side, ask, prob, edge):
                continue
            out.append([side, ask, r["outcome"], r["t"], 1.0])
        out.sort(key=lambda x: x[3])
        if stake_fn:
            stake_fn(out)
        return [tuple(x) for x in out]

    def pause_filter(trades, max_losses=3, hours=24.0):
        kept, losses, pause_until = [], [], None
        for tr in sorted(trades, key=lambda x: x[3]):
            s, ask, o, t, m = tr
            if pause_until and t < pause_until:
                continue
            kept.append(tr)
            if s != o:
                losses = [x for x in losses if (t - x).total_seconds() <= hours * 3600] + [t]
                if len(losses) >= max_losses:
                    pause_until = t + timedelta(hours=hours)
                    losses = []
        return kept

    def defensive_sizing(trades, hours=12.0):
        last_loss = None
        for tr in trades:
            s, ask, o, t, m = tr
            if last_loss and (t - last_loss).total_seconds() <= hours * 3600:
                tr[4] = 0.5
            if s != o:
                last_loss = t
        return trades

    veto = base_trades(lambda r, s, a, p, e: True)
    strategies = [
        ("VETO baseline", veto),
        ("H1 GOLDILOCKS (skip e>+.10)", base_trades(lambda r, s, a, p, e: e <= 0.10)),
        ("H2 HIGH-VOL (|early|>median)", base_trades(
            lambda r, s, a, p, e: r["early_mom"] is not None and abs(r["early_mom"]) > med_mom)),
        ("H3 CROSS-ASSET agree", base_trades(
            lambda r, s, a, p, e: fav_dir.get((r["t"], "BTC" if r["asset"] == "ETH" else "ETH")) == s)),
        ("H4a UP favorites only", base_trades(lambda r, s, a, p, e: s == "UP")),
        ("H4b DOWN favorites only", base_trades(lambda r, s, a, p, e: s == "DOWN")),
        ("H5 VETO+PAUSE", pause_filter(veto)),
        ("H6 VETO half-stake 12h post-loss", base_trades(lambda r, s, a, p, e: True,
                                                         stake_fn=defensive_sizing)),
    ]

    origin = min(t[3] for t in veto)
    n_weeks = 8
    hdr = "".join(f"{'W'+str(w+1):>8s}" for w in range(n_weeks))
    print(f"{len(recs)} windows | median |early move| = {med_mom*100:.3f}%\n")
    print(f"{'strategy':34s}{hdr}{'  wks+':>7s}{'    n':>6s}{' win%':>6s}{' avg/$':>7s}{'   cum$':>8s}{'  maxDD':>8s}")
    print("-" * (34 + 8 * n_weeks + 42))
    for name, trades in strategies:
        wk: Dict[int, List] = {}
        for tr in trades:
            wk.setdefault(int((tr[3] - origin).total_seconds() // (7 * 86400)), []).append(tr)
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
        agg = _stats(trades)
        print(f"{name:34s}{row}{pos:>4d}/{tot}{agg['n']:>6d}{agg['win']*100:>5.0f}%"
              f"{agg['avg']:>+7.2f}{agg['pnl']:>+8.0f}{agg['maxdd']:>+8.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
