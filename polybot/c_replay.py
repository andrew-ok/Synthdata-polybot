"""Replay the UPDATED production Strategy C over the cached 8 weeks.

Unlike c_faithful (which tested the pure post rule), this replays the exact
live gate cascade from strategy_c.evaluate_c, using each snapshot's own
current_time as the wall clock:

    signal (identity calibrator, edge >= C_MIN_EDGE)
    -> horizon 1H
    -> late window: 120s <= (event_end - snapshot_time) <= 1200s
    -> favorite band 0.70-0.95
    -> liquidity >= MIN_LIQUIDITY (payload top-of-book USD)
    -> forecast age <= MAX_FORECAST_AGE_SEC
    -> dedupe per window

Reports per-week results AND gate attrition (what each gate costs vs the pure
rule). All snapshots cached -> zero new API calls.

Run: python -m polybot.c_replay
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import CONFIG
from .calibration import Calibrator
from .signal_engine import evaluate
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _ENTRY_OFFSET_SEC
from .strategy_lab import _stats
from .strategy_c import C_MIN_EDGE, C_MIN_SEC_TO_END, C_MAX_SEC_TO_END, C_FAVORITE_MIN_PRICE, C_FAVORITE_MAX_PRICE

START, END = "2026-05-08T21:00:00Z", "2026-07-03T21:00:00Z"
LATE_OFFSET = 2400.0
SLIP = 0.005
STEP = 3600
RAW_CAL = Calibrator(observations=[])


def _collect(client, asset, events, offset, max_lag):
    bw: Dict[datetime, Dict] = {}
    for ws in events:
        opp = client.fetch(asset, "1H", start_time=_iso_zulu(ws + timedelta(seconds=offset)))
        if opp is None:
            continue
        w = opp.event_start_time
        rec = bw.setdefault(w, {"sp": None, "opp": None, "lag": None})
        if opp.start_price:
            rec["sp"] = opp.start_price
        lag = (opp.current_time - w).total_seconds()
        if 0 <= lag <= max_lag and (rec["opp"] is None or lag < rec["lag"]):
            rec["opp"], rec["lag"] = opp, lag
    return bw


def main() -> int:
    client = SynthInsightsClient()
    start = datetime.fromisoformat(START.replace("Z", "+00:00"))
    end = datetime.fromisoformat(END.replace("Z", "+00:00"))
    events = _walk(start, end, STEP)

    attrition = {"signal+favorite (pure rule)": 0, "late-window clock": 0,
                 "liquidity": 0, "forecast age": 0, "TRADE": 0}
    trades = []   # (side, entry, outcome, t)

    for asset in CONFIG.synth_assets:
        outcomes = _collect(client, asset, events, _ENTRY_OFFSET_SEC, _ENTRY_OFFSET_SEC + 90)
        late = _collect(client, asset, events, LATE_OFFSET, 3600)
        for w in sorted(late):
            opp = late[w]["opp"]
            if opp is None:
                continue
            a = outcomes.get(w, {}).get("sp")
            nx = outcomes.get(w + timedelta(seconds=STEP))
            b = nx.get("sp") if nx else None
            if not a or not b or a == b:
                continue
            outcome = "UP" if b > a else "DOWN"

            # production cascade, snapshot clock as "now"
            sigs = evaluate([opp], threshold=C_MIN_EDGE, calibrator=RAW_CAL)
            fav = [s for s in sigs
                   if C_FAVORITE_MIN_PRICE <= s.execution_price <= C_FAVORITE_MAX_PRICE]
            if not fav:
                continue
            sig = max(fav, key=lambda s: s.raw_edge)
            attrition["signal+favorite (pure rule)"] += 1

            sec_to_end = (opp.event_end_time - opp.current_time).total_seconds()
            if not (C_MIN_SEC_TO_END <= sec_to_end <= C_MAX_SEC_TO_END):
                attrition["late-window clock"] += 1
                continue
            if sig.liquidity < CONFIG.min_liquidity:
                attrition["liquidity"] += 1
                continue
            age = sig.forecast_age_sec
            if age is not None and age > CONFIG.max_forecast_age_sec:
                attrition["forecast age"] += 1
                continue
            attrition["TRADE"] += 1
            trades.append((sig.side, min(0.999, sig.execution_price + SLIP), outcome, w))

    print(f"Replay of PRODUCTION Strategy C, {START[:10]} .. {END[:10]}\n")
    print("Gate attrition (candidates passing the pure rule, then each gate):")
    for k, v in attrition.items():
        print(f"  {k:36s} {v}")

    # per-week scoring
    if trades:
        o = min(t[3] for t in trades)
        wk: Dict[int, List] = {}
        for s, e, out, t in trades:
            wk.setdefault(int((t - o).total_seconds() // (7 * 86400)), []).append((s, e, out))
        print(f"\n{'week':>6}{'n':>5}{'win%':>7}{'avg/$':>8}{'pnl($37.5)':>12}")
        for w in sorted(wk):
            st = _stats(wk[w])
            print(f"{'W'+str(w+1):>6}{st['n']:>5}{st['win']*100:>6.0f}%{st['avg']:>+8.2f}{st['pnl']:>+12.0f}")
        agg = _stats([(s, e, o2) for s, e, o2, _ in trades])
        print(f"{'AGG':>6}{agg['n']:>5}{agg['win']*100:>6.0f}%{agg['avg']:>+8.2f}{agg['pnl']:>+12.0f}")
    else:
        print("\nNo trades survived the production gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
