"""Search the cached 7-day window for strategies that would have been profitable.

CRITICAL HONESTY: finding a strategy that wins IN-SAMPLE on one week is trivial
and usually overfitting. So every candidate is scored two ways:
  - IN-SAMPLE (all 7 days): what "would have worked" — the user's question.
  - OUT-OF-SAMPLE: fit/select on the first ~5 days (TRAIN), score on the last
    ~2 days (TEST). Only strategies that survive TEST are candidates for real.

Dataset is rebuilt from cached snapshots (no new API calls). Each record:
asset, horizon, window_start, p_up (raw Synth), yes_ask, no_ask, outcome.

Return per $1 risked: win -> (1-entry)/entry, loss -> -1. Entry = ask + slippage.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Callable, Tuple

from .config import CONFIG
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _HORIZON_STEP_SEC, _ENTRY_OFFSET_SEC

SLIP = CONFIG.assumed_slippage_bps / 10_000.0
START, END = "2026-06-26T21:00:00Z", "2026-07-03T21:00:00Z"


def _no_ask(opp) -> Optional[float]:
    if opp.no_ask_price is not None:
        return opp.no_ask_price
    if opp.best_bid_price is not None:
        return max(0.0, min(1.0, 1.0 - opp.best_bid_price))
    return None


def build_dataset() -> List[Dict]:
    client = SynthInsightsClient()
    start = datetime.fromisoformat(START.replace("Z", "+00:00"))
    end = datetime.fromisoformat(END.replace("Z", "+00:00"))
    recs: List[Dict] = []
    for horizon in ("15M", "1H"):
        step = _HORIZON_STEP_SEC[horizon]
        events = _walk(start, end, step)
        for asset in CONFIG.synth_assets:
            bw: Dict[datetime, Dict] = {}
            for ws in events:
                opp = client.fetch(asset, horizon, start_time=_iso_zulu(ws + timedelta(seconds=_ENTRY_OFFSET_SEC)))
                if opp is None:
                    continue
                w = opp.event_start_time
                rec = bw.setdefault(w, {"sp": None, "opp": None, "lag": None})
                if opp.start_price:
                    rec["sp"] = opp.start_price
                lag = (opp.current_time - w).total_seconds()
                if 0 <= lag <= _ENTRY_OFFSET_SEC + 90 and (rec["opp"] is None or lag < rec["lag"]):
                    rec["opp"] = opp
                    rec["lag"] = lag
            for w in sorted(bw):
                opp = bw[w]["opp"]
                sp, nxt = bw[w]["sp"], bw.get(w + timedelta(seconds=step))
                if opp is None or not sp or not nxt or not nxt["sp"] or nxt["sp"] == sp:
                    continue
                yes_ask = opp.best_ask_price
                no_ask = _no_ask(opp)
                if yes_ask is None or no_ask is None:
                    continue
                recs.append({
                    "asset": asset, "horizon": horizon, "t": w,
                    "p_up": opp.synth_probability_up,
                    "yes_ask": yes_ask, "no_ask": no_ask,
                    "outcome": "UP" if nxt["sp"] > sp else "DOWN",
                })
    recs.sort(key=lambda r: r["t"])
    return recs


def _ret(side: str, entry: float, outcome: str) -> float:
    entry = min(0.999, entry + SLIP)
    won = (side == outcome)
    return (1 - entry) / entry if won else -1.0


def _stats(trades: List[Tuple[str, float, str]]) -> Dict:
    rets = [_ret(s, e, o) for s, e, o in trades]
    n = len(rets)
    if n == 0:
        return {"n": 0, "win": 0.0, "avg": 0.0, "sharpe": 0.0, "pnl": 0.0}
    wins = sum(1 for s, e, o in trades if s == o)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n if n > 1 else 0.0
    sharpe = mean / math.sqrt(var) if var > 1e-12 else 0.0
    return {"n": n, "win": wins / n, "avg": mean, "sharpe": sharpe, "pnl": sum(rets) * 37.5}


# ---- Strategy definitions: each maps a record -> (side, entry) or None ----
def raw_edge(thr):
    def f(r):
        if r["p_up"] - r["yes_ask"] >= thr:
            return ("UP", r["yes_ask"])
        if (1 - r["p_up"]) - r["no_ask"] >= thr:
            return ("DOWN", r["no_ask"])
        return None
    return f

def fade_edge(thr):
    def f(r):
        if r["p_up"] - r["yes_ask"] >= thr:
            return ("DOWN", r["no_ask"])       # Synth says UP -> we buy DOWN
        if (1 - r["p_up"]) - r["no_ask"] >= thr:
            return ("UP", r["yes_ask"])         # Synth says DOWN -> we buy UP
        return None
    return f

def extreme_dir(hi, lo):
    def f(r):
        if r["p_up"] >= hi:
            return ("UP", r["yes_ask"])
        if r["p_up"] <= lo:
            return ("DOWN", r["no_ask"])
        return None
    return f

def extreme_fade(hi, lo):
    def f(r):
        if r["p_up"] >= hi:
            return ("DOWN", r["no_ask"])
        if r["p_up"] <= lo:
            return ("UP", r["yes_ask"])
        return None
    return f

def calibrated_edge(cal: Dict[int, float], thr):
    def f(r):
        cp = cal.get(int(r["p_up"] * 10), r["p_up"])   # de-biased p_up
        if cp - r["yes_ask"] >= thr:
            return ("UP", r["yes_ask"])
        if (1 - cp) - r["no_ask"] >= thr:
            return ("DOWN", r["no_ask"])
        return None
    return f


def fit_calibration(recs: List[Dict]) -> Dict[int, float]:
    from collections import defaultdict
    b = defaultdict(lambda: [0, 0])
    for r in recs:
        k = int(r["p_up"] * 10)
        b[k][0] += 1 if r["outcome"] == "UP" else 0
        b[k][1] += 1
    return {k: v[0] / v[1] for k, v in b.items() if v[1] >= 8}   # need >=8 samples/bin


def run_strategy(recs, fn) -> Dict:
    trades = []
    for r in recs:
        pick = fn(r)
        if pick is not None:
            trades.append((pick[0], pick[1], r["outcome"]))
    return _stats(trades)


def main() -> int:
    recs = build_dataset()
    n = len(recs)
    split = int(n * 0.7)
    train, test = recs[:split], recs[split:]
    cal_all = fit_calibration(recs)
    cal_train = fit_calibration(train)
    print(f"Dataset: {n} resolved windows ({recs[0]['t'].date()} .. {recs[-1]['t'].date()}), "
          f"train={len(train)} test={len(test)}")
    up_rate = sum(1 for r in recs if r['outcome'] == 'UP') / n
    print(f"Base rate: {up_rate:.0%} UP\n")

    cands = [
        ("raw edge >=0.20 (=Strat A)", raw_edge(0.20), None),
        ("raw edge >=0.30", raw_edge(0.30), None),
        ("FADE edge >=0.20", fade_edge(0.20), None),
        ("FADE edge >=0.30", fade_edge(0.30), None),
        ("extreme dir (p>=.55 / <=.10)", extreme_dir(0.55, 0.10), None),
        ("extreme dir (p>=.60 / <=.08)", extreme_dir(0.60, 0.08), None),
        ("extreme FADE (p>=.55 / <=.10)", extreme_fade(0.55, 0.10), None),
        ("calibrated edge >=0.10 (IS cal)", calibrated_edge(cal_all, 0.10), "oos"),
        ("calibrated edge >=0.15 (IS cal)", calibrated_edge(cal_all, 0.15), "oos"),
    ]

    print(f"{'strategy':34s}{'IN-SAMPLE (all 7d)':>30s}{'OUT-OF-SAMPLE (last ~2d)':>34s}")
    print(f"{'':34s}{'n  win%  avg/$  shrp   pnl':>30s}{'n  win%  avg/$  shrp   pnl':>34s}")
    print("-" * 98)
    for name, fn, oos in cands:
        s_all = run_strategy(recs, fn)
        # out-of-sample: for calibrated, refit on train and score test
        fn_test = calibrated_edge(cal_train, 0.10 if "0.10" in name else 0.15) if oos == "oos" else fn
        s_test = run_strategy(test, fn_test)
        def fmt(s):
            return f"{s['n']:>3} {s['win']*100:>4.0f}% {s['avg']:>+5.2f} {s['sharpe']:>+5.2f} {s['pnl']:>+7.0f}"
        print(f"{name:34s}{fmt(s_all):>30s}{fmt(s_test):>34s}")

    # per-asset / per-horizon for the best raw and fade
    print("\nSegment view (raw edge >=0.20):")
    for seg in [("BTC", None), ("ETH", None), (None, "15M"), (None, "1H")]:
        sub = [r for r in recs if (seg[0] is None or r["asset"] == seg[0]) and (seg[1] is None or r["horizon"] == seg[1])]
        s = run_strategy(sub, raw_edge(0.20))
        label = seg[0] or seg[1]
        print(f"  {label:>5}: n={s['n']:>3} win={s['win']*100:>3.0f}% avg={s['avg']:+.2f} pnl={s['pnl']:+.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
