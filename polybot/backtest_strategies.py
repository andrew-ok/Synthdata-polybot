"""Backtest A / B / C on the cached 7-day window, one consistent methodology.

  A: early-entry (150s in), edge >= 0.20, maker fill (ask + slippage), 15m + 1h
  B: early-entry, edge >= 0.30, taker fill (ask + slippage + taker fee), 15m + 1h
  C: LATE-window (last ~20 min), edge >= 0.10, hourly only, maker fill

Outcomes come from each window's successor start_price (the real settlement
print). A/B reuse the cached early snapshots (free); C fetches late-window
hourly snapshots (~336 calls) since those were never cached.

Return per $1 risked: win -> (1-entry)/entry, loss -> -1 (consistent with the
live ab_report). Fixed $37.50 stake gives a comparable cumulative-$ view.

Run:  python -m polybot.backtest_strategies 2026-06-26T21:00:00Z 2026-07-03T21:00:00Z
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import CONFIG
from .signal_engine import evaluate
from .calibration import Calibrator
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _HORIZON_STEP_SEC, _ENTRY_OFFSET_SEC

# Test the RAW strategies (identity calibrator). A live Calibrator() would load
# the backfill's own observations and de-bias Synth — which erases the edge and
# yields ~0 signals (a real finding, but not what "test strategy A/B/C" means).
_RAW_CAL = Calibrator(observations=[])

SLIP = CONFIG.assumed_slippage_bps / 10_000.0
TAKER = 150.0 / 10_000.0
STAKE = 37.50
C_LATE_OFFSET_SEC = 2400.0   # ~40 min into a 60-min window => ~20 min left
C_MIN_EDGE = 0.10


def _fetch(client, asset, horizon, window_start, offset) -> Optional[object]:
    return client.fetch(asset, horizon, start_time=_iso_zulu(window_start + timedelta(seconds=offset)))


def _collect(client, asset, horizon, events, offset, max_lag) -> Dict[datetime, Dict]:
    """Robust binning (same approach as the fixed backtester): one request per
    window, but bin every result by its TRUE event_start and keep the
    smallest-lag snapshot per window. Returns {event_start: {sp, opp, lag}}.
    A request can return the prior window, so start_prices accumulate across
    windows and the entry snapshot is whichever landed closest to window open."""
    bw: Dict[datetime, Dict] = {}
    for ws in events:
        opp = _fetch(client, asset, horizon, ws, offset)
        if opp is None:
            continue
        w = opp.event_start_time
        rec = bw.setdefault(w, {"sp": None, "opp": None, "lag": None})
        if opp.start_price:
            rec["sp"] = opp.start_price
        lag = (opp.current_time - w).total_seconds()
        if 0 <= lag <= max_lag and (rec["opp"] is None or lag < rec["lag"]):
            rec["opp"] = opp
            rec["lag"] = lag
    return bw


def _outcome(bw, w, step) -> Optional[str]:
    a = bw.get(w, {}).get("sp")
    nxt = bw.get(w + timedelta(seconds=step))
    b = nxt.get("sp") if nxt else None
    if not a or not b or a == b:
        return None
    return "UP" if b > a else "DOWN"


def _best_signal(opp, min_edge):
    sigs = evaluate([opp], threshold=min_edge, calibrator=_RAW_CAL)
    return max(sigs, key=lambda s: s.raw_edge) if sigs else None


def _summary(rets: List[float], wins: int) -> Dict:
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rets) / n
        sharpe = mean / math.sqrt(var) if var > 1e-12 else 0.0
    else:
        sharpe = 0.0
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": wins / n if n else 0.0,
        "avg_return_per_$": mean,
        "per_trade_sharpe": sharpe,
        "cum_pnl": sum(r * STAKE for r in rets),
    }


def run(start_iso: str, end_iso: str) -> Dict[str, Dict]:
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    client = SynthInsightsClient()
    assets = CONFIG.synth_assets

    a_rets, a_wins = [], 0
    b_rets, b_wins = [], 0
    c_rets, c_wins = [], 0

    # ---- A / B: early-entry snapshots on 15m + 1h ----
    for horizon in ("15M", "1H"):
        step = _HORIZON_STEP_SEC[horizon]
        events = _walk(start, end, step)
        for asset in assets:
            bw = _collect(client, asset, horizon, events, _ENTRY_OFFSET_SEC, _ENTRY_OFFSET_SEC + 90)
            for w in sorted(bw):
                opp = bw[w]["opp"]
                if opp is None:
                    continue
                res = _outcome(bw, w, step)
                if res is None:
                    continue
                sig = _best_signal(opp, 0.20)
                if sig is None:
                    continue
                won = (sig.side == res)
                entry = min(0.999, sig.execution_price + SLIP)          # A: maker
                a_rets.append((1 - entry) / entry if won else -1.0)
                a_wins += int(won)
                if sig.raw_edge >= 0.30:                                 # B: taker
                    entry_b = min(0.999, sig.execution_price * (1 + TAKER) + SLIP)
                    b_rets.append((1 - entry_b) / entry_b if won else -1.0)
                    b_wins += int(won)

    # ---- C: late-window snapshots on 1h only ----
    step = _HORIZON_STEP_SEC["1H"]
    events = _walk(start, end, step)
    for asset in assets:
        bw_out = _collect(client, asset, "1H", events, _ENTRY_OFFSET_SEC, _ENTRY_OFFSET_SEC + 90)  # cached: outcomes
        bw_late = _collect(client, asset, "1H", events, C_LATE_OFFSET_SEC, 3600)                   # NEW: late entries
        for w in sorted(bw_late):
            opp = bw_late[w]["opp"]
            if opp is None:
                continue
            res = _outcome(bw_out, w, step)
            if res is None:
                continue
            sig = _best_signal(opp, C_MIN_EDGE)
            if sig is None:
                continue
            won = (sig.side == res)
            entry = min(0.999, sig.execution_price + SLIP)
            c_rets.append((1 - entry) / entry if won else -1.0)
            c_wins += int(won)

    return {
        "A: 20c early (15m+1h)": _summary(a_rets, a_wins),
        "B: 30c taker (15m+1h)": _summary(b_rets, b_wins),
        "C: 10c late-window (1h)": _summary(c_rets, c_wins),
    }


def format_report(results: Dict[str, Dict]) -> str:
    lines = ["**7-DAY BACKTEST — Strategies A / B / C on the same week**", ""]
    hdr = f"{'metric':22s}" + "".join(f"{name.split(':')[0]:>14s}" for name in results)
    lines.append(hdr)
    lines.append("-" * len(hdr))
    keys = [("trades", "{:d}"), ("win_rate", "{:.1%}"),
            ("avg_return_per_$", "{:+.3f}"), ("per_trade_sharpe", "{:.3f}"),
            ("cum_pnl", "${:+.2f}")]
    labels = {"trades": "trades", "win_rate": "win rate",
              "avg_return_per_$": "avg return / $1", "per_trade_sharpe": "per-trade Sharpe",
              "cum_pnl": "cum P&L (fixed stake)"}
    for key, fmt in keys:
        lines.append(f"{labels[key]:22s}" + "".join(f"{fmt.format(r[key]):>14s}" for r in results.values()))
    lines.append("")
    for name in results:
        lines.append(f"  {name}")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        start, end = "2026-06-26T21:00:00Z", "2026-07-03T21:00:00Z"
    print(format_report(run(start, end)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
