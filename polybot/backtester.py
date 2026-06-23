"""Backtester.

Replays past windows by stepping `start_time` across the Synth insights
endpoints. Each historical call returns Synth's prob, the Polymarket book at
that time, and (because the call is for a finished event) the realized
outcome via `current_outcome` (it's the actual Up/Down realized between
event_start_time and event_end_time).

Sweep thresholds ∈ {0.10, 0.15, 0.20, 0.25, 0.30}. Per threshold:
    trades, win_rate, avg_EV, realized_pnl, max_drawdown,
    avg_spread, avg_slippage_cost, sharpe_proxy
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .config import CONFIG
from .calibration import CalibrationObservation, Calibrator, append_observation
from .signal_engine import evaluate
from .synth_client import INSIGHTS_PATHS, SynthInsightsClient, configured_horizons

log = logging.getLogger(__name__)

# How often events fire per horizon.
_HORIZON_STEP_SEC = {"15M": 15 * 60, "1H": 60 * 60}


@dataclass
class _Stats:
    threshold: float
    trades: int = 0
    wins: int = 0
    losses: int = 0
    open_trades: int = 0
    total_ev: float = 0.0
    total_notional: float = 0.0
    realized_pnl: float = 0.0
    entry_edges: List[float] = field(default_factory=list)
    realized_edges: List[float] = field(default_factory=list)
    spreads: List[float] = field(default_factory=list)
    slippage_cost: float = 0.0
    holding_period_sec: List[float] = field(default_factory=list)
    pnl_curve: List[float] = field(default_factory=list)
    pnl_by_asset: Dict[str, float] = field(default_factory=dict)
    pnl_by_horizon: Dict[str, float] = field(default_factory=dict)
    pnl_by_side: Dict[str, float] = field(default_factory=dict)
    pnl_by_calibration_method: Dict[str, float] = field(default_factory=dict)
    exit_reasons: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> Dict[str, object]:
        wr = self.wins / self.trades if self.trades else 0.0
        avg_ev = self.total_ev / self.trades if self.trades else 0.0
        return {
            "threshold": self.threshold,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "open_trades": self.open_trades,
            "win_rate": round(wr, 4),
            "avg_EV": round(avg_ev, 4),
            "realized_pnl": round(self.realized_pnl, 2),
            "roi": round(self.realized_pnl / self.total_notional, 4) if self.total_notional else 0.0,
            "max_drawdown": round(_max_drawdown(self.pnl_curve), 2),
            "avg_entry_edge": round(sum(self.entry_edges) / len(self.entry_edges), 4) if self.entry_edges else 0.0,
            "avg_realized_edge": round(sum(self.realized_edges) / len(self.realized_edges), 4) if self.realized_edges else 0.0,
            "avg_spread": round(sum(self.spreads) / len(self.spreads), 4) if self.spreads else 0.0,
            "avg_slippage_cost": round(self.slippage_cost / self.trades, 4) if self.trades else 0.0,
            "avg_holding_period_sec": round(sum(self.holding_period_sec) / len(self.holding_period_sec), 2) if self.holding_period_sec else 0.0,
            "sharpe_proxy": round(_sharpe_proxy(self.pnl_curve), 3),
            "pnl_by_asset": {k: round(v, 4) for k, v in sorted(self.pnl_by_asset.items())},
            "pnl_by_horizon": {k: round(v, 4) for k, v in sorted(self.pnl_by_horizon.items())},
            "pnl_by_side": {k: round(v, 4) for k, v in sorted(self.pnl_by_side.items())},
            "pnl_by_calibration_method": {k: round(v, 4) for k, v in sorted(self.pnl_by_calibration_method.items())},
            "exit_reasons": dict(sorted(self.exit_reasons.items())),
        }


def _max_drawdown(curve: List[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    mdd = 0.0
    for x in curve:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)
    return mdd


def _sharpe_proxy(curve: List[float]) -> float:
    if len(curve) < 2:
        return 0.0
    rets = [curve[i] - curve[i - 1] for i in range(1, len(curve))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    return (mean / std) * math.sqrt(len(rets)) if std > 1e-9 else 0.0


def _iso_zulu(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _walk(start: datetime, end: datetime, step_sec: int) -> List[datetime]:
    out = []
    t = start
    while t < end:
        out.append(t)
        t += timedelta(seconds=step_sec)
    return out


def estimate_call_count(
    start_iso: str,
    end_iso: str,
    assets: Optional[List[str]] = None,
    horizons: Optional[List[str]] = None,
) -> int:
    assets = assets or CONFIG.synth_assets
    horizons = horizons or configured_horizons()
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    total = 0
    for horizon in horizons:
        step = _HORIZON_STEP_SEC.get(horizon)
        if step is None:
            log.warning("Skipping unsupported backtest horizon %s", horizon)
            continue
        total += len(_walk(start, end, step)) * len(assets)
    return total


def _process_signal(stats: _Stats, sig, resolved_up: bool, holding_period_sec: float) -> None:
    """Apply a signal as if traded; charge fees+slippage; record outcome."""
    fee = CONFIG.taker_fee_bps / 10_000.0
    slip = CONFIG.assumed_slippage_bps / 10_000.0
    fill_px = min(0.999, sig.execution_price + slip)
    if fill_px <= 0 or fill_px >= 1:
        return

    fill_ratio = max(0.0, min(1.0, CONFIG.backtest_partial_fill_ratio))
    if fill_ratio <= 0:
        return
    notional = 1.0 * fill_ratio
    contracts = notional / fill_px
    won = (sig.side == "UP" and resolved_up) or (sig.side == "DOWN" and not resolved_up)
    payoff = contracts * 1.0 if won else 0.0
    pnl = payoff - notional - (notional * fee)

    prob = sig.calibrated_probability
    ev = prob * (1.0 - fill_px) - (1.0 - prob) * fill_px - fee

    stats.trades += 1
    stats.wins += int(won)
    stats.losses += int(not won)
    stats.total_ev += ev
    stats.total_notional += notional
    stats.realized_pnl += pnl
    stats.entry_edges.append(sig.net_edge)
    stats.realized_edges.append((1.0 - fill_px) if won else -fill_px)
    stats.slippage_cost += slip
    stats.holding_period_sec.append(max(0.0, holding_period_sec))
    if sig.spread is not None:
        stats.spreads.append(float(sig.spread))
    running = (stats.pnl_curve[-1] if stats.pnl_curve else 0.0) + pnl
    stats.pnl_curve.append(running)
    stats.pnl_by_asset[sig.asset] = stats.pnl_by_asset.get(sig.asset, 0.0) + pnl
    stats.pnl_by_horizon[sig.horizon] = stats.pnl_by_horizon.get(sig.horizon, 0.0) + pnl
    stats.pnl_by_side[sig.side] = stats.pnl_by_side.get(sig.side, 0.0) + pnl
    stats.pnl_by_calibration_method[sig.calibration_method] = stats.pnl_by_calibration_method.get(sig.calibration_method, 0.0) + pnl
    stats.exit_reasons["RESOLVED"] = stats.exit_reasons.get("RESOLVED", 0) + 1


def run_backtest(
    start_iso: str,
    end_iso: str,
    assets: Optional[List[str]] = None,
    horizons: Optional[List[str]] = None,
    thresholds: Optional[Iterable[float]] = None,
    request_delay_sec: float = 0.0,
) -> List[Dict[str, object]]:
    assets = assets or CONFIG.synth_assets
    horizons = horizons or configured_horizons()
    thresholds = list(thresholds or CONFIG.backtest_thresholds)
    stats = {t: _Stats(threshold=t) for t in thresholds}

    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(timezone.utc)

    client = SynthInsightsClient()
    calibrator = Calibrator()
    total_pulled = 0
    skipped_late = 0
    skipped_unresolved = 0

    for horizon in horizons:
        step = _HORIZON_STEP_SEC.get(horizon)
        if step is None:
            log.warning("Skipping unsupported backtest horizon %s", horizon)
            continue
        events = _walk(start, end, step)
        log.info("Backtest %s: %d windows × %d assets = %d calls",
                 horizon, len(events), len(assets), len(events) * len(assets))
        for event_start in events:
            iso = _iso_zulu(event_start)
            for asset in assets:
                opp = client.fetch(asset, horizon, start_time=iso)
                if request_delay_sec:
                    time.sleep(request_delay_sec)
                if opp is None:
                    continue
                total_pulled += 1
                lag_sec = (opp.current_time - opp.event_start_time).total_seconds()
                if lag_sec > CONFIG.max_backtest_snapshot_lag_sec:
                    skipped_late += 1
                    continue
                label = opp.resolved_outcome
                if label is None and CONFIG.allow_current_outcome_backtest_label:
                    label = opp.current_outcome
                outcome = str(label or "").strip().lower()
                if outcome not in ("up", "down"):
                    skipped_unresolved += 1
                    continue
                resolved_up = outcome == "up"
                append_observation(CalibrationObservation(
                    timestamp=iso,
                    asset=asset,
                    horizon=horizon,
                    predicted_probability=opp.synth_probability_up,
                    realized_outcome="UP" if resolved_up else "DOWN",
                    source="backtest",
                ))
                holding_period_sec = (opp.event_end_time - opp.current_time).total_seconds()

                recorded_signal_observation = False
                for t in thresholds:
                    for sig in evaluate([opp], threshold=t, calibrator=calibrator):
                        if not recorded_signal_observation:
                            append_observation(CalibrationObservation(
                                timestamp=iso,
                                asset=asset,
                                horizon=horizon,
                                predicted_probability=sig.raw_synth_probability,
                                realized_outcome="UP" if resolved_up else "DOWN",
                                ask_price=sig.execution_price,
                                side=sig.side,
                                source="backtest_signal",
                            ))
                            recorded_signal_observation = True
                        _process_signal(stats[t], sig, resolved_up, holding_period_sec)

    log.info(
        "Backtest pulled %d opportunities; skipped %d late snapshots (> %.0fs after event start); skipped %d unresolved labels",
        total_pulled,
        skipped_late,
        CONFIG.max_backtest_snapshot_lag_sec,
        skipped_unresolved,
    )

    results = [stats[t].summary() for t in thresholds]
    results.sort(key=lambda r: (r["sharpe_proxy"], r["realized_pnl"]), reverse=True)
    return results


def best_threshold(results: List[Dict[str, object]]) -> Optional[float]:
    if not results:
        return None
    return results[0]["threshold"]
