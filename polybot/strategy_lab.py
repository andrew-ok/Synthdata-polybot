"""Large strategy battery over the cached 7-day window, 15m / 1h / both.

Families tested:
  - Synth-signal: raw edge, fade, extreme-confidence, calibrated (train-fit)
  - Pure price: early-window momentum continuation / reversal
  - Market microstructure: buy favorite / buy underdog (market-implied)
  - Hybrids: Synth agrees-with-momentum, fade-with-filters

Every strategy is scored IN-SAMPLE (7d) and OUT-OF-SAMPLE (fit/select on the
first 70% by time, score the last 30%). With this many strategies on one week,
several will look good in-sample by chance — only OOS survivors with adequate
sample size (n>=20) are candidates, and even those need multi-week confirmation.

Dataset rebuilt from cached snapshots (no new API calls).
Return per $1 risked: win -> (1-entry)/entry, loss -> -1; entry = ask + slippage.
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
MIN_N = 20   # OOS sample-size floor to take a result seriously


def _no_ask(opp) -> Optional[float]:
    if opp.no_ask_price is not None:
        return opp.no_ask_price
    if opp.best_bid_price is not None:
        return max(0.0, min(1.0, 1.0 - opp.best_bid_price))
    return None


def build_dataset(start_iso: str = START, end_iso: str = END) -> List[Dict]:
    client = SynthInsightsClient()
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
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
                ya, na = opp.best_ask_price, _no_ask(opp)
                if ya is None or na is None or not opp.current_price:
                    continue
                recs.append({
                    "asset": asset, "horizon": horizon, "t": w,
                    "p_up": opp.synth_probability_up, "yes_ask": ya, "no_ask": na,
                    "mom": (opp.current_price - sp) / sp,      # early-window move at ~150s
                    "outcome": "UP" if nxt["sp"] > sp else "DOWN",
                })
    recs.sort(key=lambda r: r["t"])
    return recs


def _ret(side: str, entry: float, outcome: str) -> float:
    entry = min(0.999, entry + SLIP)
    return (1 - entry) / entry if side == outcome else -1.0


def _stats(trades: List[Tuple[str, float, str]]) -> Dict:
    if not trades:
        return {"n": 0, "win": 0.0, "avg": 0.0, "sharpe": 0.0, "pnl": 0.0}
    rets = [_ret(s, e, o) for s, e, o in trades]
    n = len(rets)
    wins = sum(1 for s, e, o in trades if s == o)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n if n > 1 else 0.0
    sharpe = mean / math.sqrt(var) if var > 1e-12 else 0.0
    return {"n": n, "win": wins / n, "avg": mean, "sharpe": sharpe, "pnl": sum(rets) * 37.5}


# ---------- strategy factories: rec -> (side, entry) or None ----------
def raw_edge(thr):
    def f(r):
        if r["p_up"] - r["yes_ask"] >= thr: return ("UP", r["yes_ask"])
        if (1 - r["p_up"]) - r["no_ask"] >= thr: return ("DOWN", r["no_ask"])
        return None
    return f

def fade_edge(thr):
    def f(r):
        if r["p_up"] - r["yes_ask"] >= thr: return ("DOWN", r["no_ask"])
        if (1 - r["p_up"]) - r["no_ask"] >= thr: return ("UP", r["yes_ask"])
        return None
    return f

def extreme_dir(hi, lo):
    def f(r):
        if r["p_up"] >= hi: return ("UP", r["yes_ask"])
        if r["p_up"] <= lo: return ("DOWN", r["no_ask"])
        return None
    return f

def mom_trend(thr):
    def f(r):
        if r["mom"] >= thr: return ("UP", r["yes_ask"])
        if r["mom"] <= -thr: return ("DOWN", r["no_ask"])
        return None
    return f

def mom_revert(thr):
    def f(r):
        if r["mom"] >= thr: return ("DOWN", r["no_ask"])
        if r["mom"] <= -thr: return ("UP", r["yes_ask"])
        return None
    return f

def mkt_favorite(r):
    if r["yes_ask"] >= 0.55: return ("UP", r["yes_ask"])
    if r["no_ask"] >= 0.55: return ("DOWN", r["no_ask"])
    return None

def mkt_underdog(r):
    if r["yes_ask"] <= 0.45 and r["yes_ask"] >= 0.15: return ("UP", r["yes_ask"])
    if r["no_ask"] <= 0.45 and r["no_ask"] >= 0.15: return ("DOWN", r["no_ask"])
    return None

def synth_mom_agree(thr):
    def f(r):
        up_edge = r["p_up"] - r["yes_ask"] >= thr
        dn_edge = (1 - r["p_up"]) - r["no_ask"] >= thr
        if up_edge and r["mom"] > 0: return ("UP", r["yes_ask"])
        if dn_edge and r["mom"] < 0: return ("DOWN", r["no_ask"])
        return None
    return f

def fade_synth_mom(thr):
    def f(r):
        # fade Synth, but only when early momentum agrees with the fade
        up_edge = r["p_up"] - r["yes_ask"] >= thr      # Synth bullish -> fade DOWN
        dn_edge = (1 - r["p_up"]) - r["no_ask"] >= thr  # Synth bearish -> fade UP
        if dn_edge and r["mom"] > 0: return ("UP", r["yes_ask"])
        if up_edge and r["mom"] < 0: return ("DOWN", r["no_ask"])
        return None
    return f

def calibrated_edge(cal, thr):
    def f(r):
        cp = cal.get(int(r["p_up"] * 10), r["p_up"])
        if cp - r["yes_ask"] >= thr: return ("UP", r["yes_ask"])
        if (1 - cp) - r["no_ask"] >= thr: return ("DOWN", r["no_ask"])
        return None
    return f

def fit_cal(recs):
    from collections import defaultdict
    b = defaultdict(lambda: [0, 0])
    for r in recs:
        k = int(r["p_up"] * 10)
        b[k][0] += 1 if r["outcome"] == "UP" else 0
        b[k][1] += 1
    return {k: v[0] / v[1] for k, v in b.items() if v[1] >= 8}


def run(recs, fn):
    return _stats([(p[0], p[1], r["outcome"]) for r in recs for p in [fn(r)] if p])


def main() -> int:
    recs = build_dataset()
    n = len(recs)
    split = int(n * 0.7)
    train, test = recs[:split], recs[split:]
    cal_tr = fit_cal(train)
    up = sum(1 for r in recs if r["outcome"] == "UP") / n
    print(f"Dataset: {n} windows, {recs[0]['t'].date()}..{recs[-1]['t'].date()}, base {up:.0%} UP, "
          f"train={len(train)} test={len(test)}\n")

    strategies: List[Tuple[str, Callable, Optional[Callable]]] = [
        ("raw edge >=.20 (Strat A)", raw_edge(.20), None),
        ("raw edge >=.30", raw_edge(.30), None),
        ("FADE edge >=.20", fade_edge(.20), None),
        ("FADE edge >=.30", fade_edge(.30), None),
        ("extreme dir p>=.60/<=.08", extreme_dir(.60, .08), None),
        ("mom continuation .05%", mom_trend(.0005), None),
        ("mom continuation .15%", mom_trend(.0015), None),
        ("mom reversion .05%", mom_revert(.0005), None),
        ("mom reversion .15%", mom_revert(.0015), None),
        ("mkt favorite (ask>=.55)", mkt_favorite, None),
        ("mkt underdog (.15-.45)", mkt_underdog, None),
        ("synth+mom agree >=.20", synth_mom_agree(.20), None),
        ("fade synth+mom >=.20", fade_synth_mom(.20), None),
        ("calibrated edge >=.10", calibrated_edge(cal_tr, .10), "cal10"),
        ("calibrated edge >=.15", calibrated_edge(cal_tr, .15), "cal15"),
    ]

    for uni, flt in [("BOTH 15m+1h", lambda r: True),
                     ("15m only", lambda r: r["horizon"] == "15M"),
                     ("1h only", lambda r: r["horizon"] == "1H")]:
        R = [r for r in recs if flt(r)]
        Rtr = [r for r in train if flt(r)]
        Rte = [r for r in test if flt(r)]
        print(f"===== {uni}  (n={len(R)}) =====")
        print(f"{'strategy':28s}{'IN-SAMPLE':>26s}{'OUT-OF-SAMPLE':>26s}")
        print(f"{'':28s}{'n  win  avg   shrp  pnl':>26s}{'n  win  avg   shrp  pnl':>26s}")
        for name, fn, tag in strategies:
            s_is = run(R, fn)
            fn_oos = fn
            if tag in ("cal10", "cal15"):
                fn_oos = calibrated_edge(fit_cal(Rtr), .10 if tag == "cal10" else .15)
            s_oos = run(Rte, fn_oos)
            mark = " *" if (s_oos["avg"] > 0 and s_oos["n"] >= MIN_N and s_is["avg"] > 0) else "  "
            def fmt(s): return f"{s['n']:>3} {s['win']*100:>3.0f} {s['avg']:>+5.2f} {s['sharpe']:>+5.2f}{s['pnl']:>+6.0f}"
            print(f"{name:28s}{fmt(s_is):>26s}{fmt(s_oos):>24s}{mark}")
        print()
    print("* = positive in-sample AND out-of-sample with OOS n>=20 (survivor)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
