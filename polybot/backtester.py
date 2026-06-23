"""Backtester — two distinct backtest modes.

Historical API backtest (run_backtest):
    Calls the Synth API for past windows, sweeps entry-edge thresholds, and
    records calibration observations. Useful for threshold selection and
    collecting ground-truth data, but does not replay live Strategy B rules.
    Sweep thresholds ∈ {0.10, 0.15, 0.20, 0.25, 0.30}. Per threshold:
        trades, win_rate, avg_EV, realized_pnl, max_drawdown,
        avg_spread, avg_slippage_cost, sharpe_proxy

Snapshot replay (run_snapshot_backtest):
    Replays the local snapshots.sqlite3 database that accumulates during live
    scanner runs. Each stored timestamp is treated as an atomic scan with full
    Strategy B rules: exits evaluated first (STALE_DATA, EDGE_COLLAPSE,
    MODEL_REVERSAL, TIME_STOP, RESOLVED), then candidates scored and ranked,
    then max_trades_per_scan + full exposure limits enforced before opening.
    Requires accumulated live snapshots. This is the authoritative Strategy B
    backtest.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
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


@dataclass
class _ReplayPosition:
    event_key: str
    asset: str
    horizon: str
    side: str
    entry_time: datetime
    entry_price: float
    contracts: float
    notional: float
    entry_edge: float
    calibration_method: str


def run_snapshot_backtest(snapshot_db_path: Optional[str] = None) -> Dict[str, object]:
    """Replay stored snapshots chronologically with Strategy B rules.

    Each stored timestamp is treated as one atomic scan:
      A. Update open positions / evaluate exits (including MODEL_REVERSAL)
      B. Build all candidate entries for this scan
      C. Score and rank candidates
      D. Apply max_trades_per_scan, max_open_positions, and exposure limits
      E. Open the best candidates in rank order
    """
    path = snapshot_db_path or CONFIG.snapshot_db_path
    stats = _Stats(threshold=CONFIG.min_entry_edge)
    if not os.path.exists(path):
        out = stats.summary()
        out["source"] = "snapshot_replay"
        out["note"] = "snapshot database does not exist"
        return out

    calibrator = Calibrator()
    open_by_event: Dict[str, _ReplayPosition] = {}
    entry_cost = (CONFIG.taker_fee_bps + CONFIG.assumed_slippage_bps) / 10_000.0
    position_size = CONFIG.bankroll_usd * CONFIG.max_position_size

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY timestamp_utc ASC, event_key ASC, side ASC"
        ).fetchall()

    # Group rows into per-timestamp scans.
    scan_groups: Dict[str, list] = {}
    for row in rows:
        ts = row["timestamp_utc"]
        if ts not in scan_groups:
            scan_groups[ts] = []
        scan_groups[ts].append(row)

    for ts_str in sorted(scan_groups.keys()):
        scan_rows = scan_groups[ts_str]
        timestamp = datetime.fromisoformat(ts_str)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        # Fast lookup for this scan: (event_key, side) -> row.
        scan_lookup = {(r["event_key"], r["side"]): r for r in scan_rows}

        # --- A+B: Evaluate exits for every open position before any new entry ---
        to_close = []  # list of (event_key, exit_reason, bid_float)
        for event_key, pos in list(open_by_event.items()):
            opp_side = "DOWN" if pos.side == "UP" else "UP"
            same_row = scan_lookup.get((event_key, pos.side))
            opp_row = scan_lookup.get((event_key, opp_side))

            if same_row is None or same_row["best_bid"] is None:
                to_close.append((event_key, "STALE_DATA", pos.entry_price))
                continue

            bid = float(same_row["best_bid"])
            is_stale = bool(same_row["is_stale"])
            cal = calibrator.calibrate_side(
                same_row["asset"], same_row["horizon"], pos.side, same_row["raw_synth_probability"]
            )
            hold_edge = cal.fair_probability - bid - entry_cost

            exit_reason = None
            if is_stale:
                exit_reason = "STALE_DATA"
            elif hold_edge < CONFIG.min_exit_edge:
                exit_reason = "EDGE_COLLAPSE"
            elif (
                same_row["seconds_to_event_end"] is not None
                and same_row["seconds_to_event_end"] < CONFIG.time_stop_seconds
            ):
                exit_reason = "TIME_STOP"
            elif same_row["resolved_outcome"] in ("UP", "DOWN"):
                exit_reason = "RESOLVED"
            elif (
                opp_row is not None
                and not bool(opp_row["is_stale"])
                and opp_row["best_ask"] is not None
            ):
                # MODEL_REVERSAL: opposite side now has stronger positive net edge.
                opp_cal = calibrator.calibrate_side(
                    opp_row["asset"], opp_row["horizon"], opp_side, opp_row["raw_synth_probability"]
                )
                opp_net_edge = opp_cal.fair_probability - float(opp_row["best_ask"]) - entry_cost
                same_ask = same_row["best_ask"]
                same_net_edge = (
                    cal.fair_probability - float(same_ask) - entry_cost
                    if same_ask is not None else -1.0
                )
                if opp_net_edge > same_net_edge:
                    exit_reason = "MODEL_REVERSAL"

            if exit_reason:
                to_close.append((event_key, exit_reason, bid))

        for event_key, exit_reason, bid in to_close:
            pos = open_by_event.pop(event_key)
            exit_px = max(0.001, bid - CONFIG.assumed_slippage_bps / 10_000.0)
            pnl = (exit_px * pos.contracts) - pos.notional
            won = pnl > 0
            stats.trades += 1
            stats.wins += int(won)
            stats.losses += int(not won)
            stats.total_notional += pos.notional
            stats.realized_pnl += pnl
            stats.entry_edges.append(pos.entry_edge)
            stats.realized_edges.append(pnl / pos.notional if pos.notional else 0.0)
            stats.holding_period_sec.append(max(0.0, (timestamp - pos.entry_time).total_seconds()))
            stats.pnl_curve.append((stats.pnl_curve[-1] if stats.pnl_curve else 0.0) + pnl)
            stats.pnl_by_asset[pos.asset] = stats.pnl_by_asset.get(pos.asset, 0.0) + pnl
            stats.pnl_by_horizon[pos.horizon] = stats.pnl_by_horizon.get(pos.horizon, 0.0) + pnl
            stats.pnl_by_side[pos.side] = stats.pnl_by_side.get(pos.side, 0.0) + pnl
            stats.pnl_by_calibration_method[pos.calibration_method] = (
                stats.pnl_by_calibration_method.get(pos.calibration_method, 0.0) + pnl
            )
            stats.exit_reasons[exit_reason] = stats.exit_reasons.get(exit_reason, 0) + 1

        # --- C: Build candidate entries ---
        candidates = []
        for row in scan_rows:
            event_key = row["event_key"]
            side = row["side"]
            if event_key in open_by_event:
                continue
            ask = row["best_ask"]
            bid = row["best_bid"]
            spread = row["spread"]
            liquidity = row["near_top_liquidity_usd"] or 0.0
            if bool(row["is_stale"]) or ask is None or bid is None:
                continue
            if not (0 < float(ask) < 1) or spread is None or float(spread) > CONFIG.max_spread:
                continue
            if liquidity < CONFIG.min_liquidity:
                continue
            cal = calibrator.calibrate_side(row["asset"], row["horizon"], side, row["raw_synth_probability"])
            net_ev = cal.fair_probability - float(ask) - entry_cost
            if net_ev < CONFIG.min_entry_edge or cal.confidence_score < CONFIG.min_confidence_score:
                continue
            score = net_ev * cal.confidence_score
            candidates.append((score, net_ev, cal, row))

        # --- D: Rank by score, cap at max_trades_per_scan ---
        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[: CONFIG.max_trades_per_scan]

        # Current exposure snapshot (after exits, before new entries).
        max_total = CONFIG.bankroll_usd * CONFIG.max_total_exposure
        max_asset = CONFIG.bankroll_usd * CONFIG.max_asset_exposure
        max_horizon = CONFIG.bankroll_usd * CONFIG.max_horizon_exposure
        total_exp = sum(p.notional for p in open_by_event.values())
        asset_exp: Dict[str, float] = {}
        horizon_exp: Dict[str, float] = {}
        for p in open_by_event.values():
            asset_exp[p.asset] = asset_exp.get(p.asset, 0.0) + p.notional
            horizon_exp[p.horizon] = horizon_exp.get(p.horizon, 0.0) + p.notional

        # --- E: Open best candidates in rank order, enforcing all limits ---
        for score, net_ev, cal, row in candidates:
            event_key = row["event_key"]
            side = row["side"]
            asset = row["asset"]
            horizon = row["horizon"]

            if event_key in open_by_event:
                continue
            if len(open_by_event) >= CONFIG.max_open_positions:
                break
            if total_exp + position_size > max_total:
                break
            if asset_exp.get(asset, 0.0) + position_size > max_asset:
                continue
            if horizon_exp.get(horizon, 0.0) + position_size > max_horizon:
                continue

            fill_px = min(0.999, float(row["best_ask"]) + CONFIG.assumed_slippage_bps / 10_000.0)
            contracts = position_size / fill_px
            open_by_event[event_key] = _ReplayPosition(
                event_key=event_key,
                asset=asset,
                horizon=horizon,
                side=side,
                entry_time=timestamp,
                entry_price=fill_px,
                contracts=contracts,
                notional=position_size,
                entry_edge=net_ev,
                calibration_method=cal.calibration_method,
            )
            total_exp += position_size
            asset_exp[asset] = asset_exp.get(asset, 0.0) + position_size
            horizon_exp[horizon] = horizon_exp.get(horizon, 0.0) + position_size
            if row["spread"] is not None:
                stats.spreads.append(float(row["spread"]))
            stats.slippage_cost += CONFIG.assumed_slippage_bps / 10_000.0

    stats.open_trades = len(open_by_event)
    out = stats.summary()
    out["source"] = "snapshot_replay"
    out["snapshots"] = len(rows)
    return out
