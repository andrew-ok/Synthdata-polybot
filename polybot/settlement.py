"""Position settlement — the missing close-out leg.

Entry writes an open row to fills.jsonl. This module is what *closes* it, in
one of two ways, and writes the outcome back onto the same row so reports,
calibration, and the dashboard can score the trade:

  1. exit-at-fair  — while the market is still live, if the model edge has
     collapsed (market price reached ~Synth fair), sell into the book at the
     current exit price. This is Strategy E's take-profit leg.
  2. resolution    — once the event window closes, fetch the historical Synth
     snapshot for that window (same mechanism the backtester uses) to get the
     realized UP/DOWN outcome and settle win/loss.

A binary contract pays $1 per contract on a win. With notional = fill_price *
contracts:
    win  -> realized_pnl = contracts * (1 - fill_price)
    loss -> realized_pnl = -contracts * fill_price
Exit-at-fair instead realizes contracts * (exit_price - fill_price).

fills.jsonl is rewritten atomically (temp file + os.replace) so a crash mid
write cannot corrupt the ledger.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .calibration import CalibrationObservation, append_observation
from .exit_rules import evaluate_exit_for_fill, _market_exit_price
from .synth_client import Opportunity, SynthInsightsClient

log = logging.getLogger(__name__)


def _fills_path(fills_name: str = "fills.jsonl") -> str:
    return os.path.join(CONFIG.log_dir, fills_name)


def _load_rows(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _rewrite_rows(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _is_open(row: Dict[str, Any]) -> bool:
    if row.get("voided"):
        return False
    return not row.get("resolved_outcome") and not row.get("exit_timestamp")


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _win_pnl(contracts: float, fill_price: float, won: bool) -> float:
    return contracts * (1.0 - fill_price) if won else -contracts * fill_price


def settle_positions(
    opportunities: List[Opportunity],
    client: Optional[SynthInsightsClient] = None,
    fills_name: str = "fills.jsonl",
) -> Dict[str, int]:
    """Close out open paper positions in the given ledger. Returns counts by
    close kind. fills_name lets a parallel strategy (e.g. C) settle its own
    ledger with the same logic."""
    path = _fills_path(fills_name)
    rows = _load_rows(path)
    if not rows:
        return {"exit_at_fair": 0, "resolution": 0, "pending": 0}

    client = client or SynthInsightsClient()
    by_slug = {opp.slug: opp for opp in opportunities}
    now = datetime.now(timezone.utc)
    counts = {"exit_at_fair": 0, "resolution": 0, "pending": 0}
    changed = False

    for row in rows:
        if not _is_open(row):
            continue
        side = str(row.get("side") or "").upper()
        contracts = float(row.get("contracts") or 0.0)
        fill_price = float(row.get("fill_price") or 0.0)
        condition_id = str(row.get("condition_id") or "")
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")

        # --- 1. exit-at-fair while the market is still live ---
        live = by_slug.get(condition_id)
        if live is not None:
            exit_decision = evaluate_exit_for_fill(row, live)
            if exit_decision is not None and exit_decision.reason == "TAKE_PROFIT_EDGE_COLLAPSE":
                exit_price = _market_exit_price(live, side)
                if exit_price is not None and 0.0 < exit_price < 1.0:
                    pnl = contracts * (exit_price - fill_price)
                    row["exit_timestamp"] = now.isoformat()
                    row["exit_price"] = round(exit_price, 6)
                    row["exit_reason"] = exit_decision.reason
                    row["realized_pnl"] = round(pnl, 6)
                    row["close_kind"] = "exit_at_fair"
                    counts["exit_at_fair"] += 1
                    changed = True
                    continue

        # --- 2. resolution once the window has closed ---
        end_dt = _parse_dt(row.get("event_end_time"))
        if end_dt is None or now < end_dt:
            counts["pending"] += 1
            continue

        outcome = _resolved_outcome(client, asset, horizon, row.get("event_start_time"))
        if outcome is None:
            counts["pending"] += 1  # ended but label not available yet — retry next cycle
            continue

        won = (side == outcome)
        pnl = _win_pnl(contracts, fill_price, won)
        row["resolved_outcome"] = outcome
        row["realized_pnl"] = round(pnl, 6)
        row["resolution_timestamp"] = now.isoformat()
        row["close_kind"] = "resolution"
        counts["resolution"] += 1
        changed = True

        # Feed the learning loop: record the realized outcome for calibration.
        try:
            append_observation(CalibrationObservation(
                timestamp=str(row.get("timestamp") or now.isoformat()),
                asset=asset.upper(),
                horizon=horizon,
                predicted_probability=float(row.get("calibrated_probability") or 0.0)
                if side == "UP" else 1.0 - float(row.get("calibrated_probability") or 0.0),
                realized_outcome=outcome,
                ask_price=fill_price,
                side=side,
                source="live_paper",
            ))
        except (TypeError, ValueError):
            pass

    if changed:
        _rewrite_rows(path, rows)
        log.info("Settlement[%s]: %d exit-at-fair, %d resolved, %d still pending",
                 fills_name, counts["exit_at_fair"], counts["resolution"], counts["pending"])
    return counts


def _window_start_price(
    client: SynthInsightsClient, asset: str, horizon: str, window_start: datetime
) -> Optional[float]:
    """start_price of the window beginning at window_start, verified against the
    snapshot's own event_start_time (requests near a boundary can return the
    prior window)."""
    for offset_sec in (150, 400):
        req = (window_start + __import__("datetime").timedelta(seconds=offset_sec))
        iso = req.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            opp = client.fetch(asset, horizon, start_time=iso)
        except Exception as exc:
            log.debug("Settlement fetch failed %s/%s @ %s: %s", asset, horizon, iso, exc)
            continue
        if opp is None or not opp.start_price:
            continue
        if abs((opp.event_start_time - window_start).total_seconds()) < 1:
            return opp.start_price
    return None


def _resolved_outcome(
    client: SynthInsightsClient, asset: str, horizon: str, event_start_iso: Any
) -> Optional[str]:
    """Realized UP/DOWN for a finished window, derived from the price chain:
    outcome = sign(next window's start_price - this window's start_price).
    (The insights endpoint never populates resolved_outcome — the field-based
    approach silently left every live fill open forever.)"""
    start_dt = _parse_dt(event_start_iso)
    if start_dt is None:
        return None
    step = {"15M": 900, "1H": 3600}.get(horizon.upper())
    if step is None:
        return None
    from datetime import timedelta as _td
    sp_this = _window_start_price(client, asset, horizon, start_dt)
    sp_next = _window_start_price(client, asset, horizon, start_dt + _td(seconds=step))
    if not sp_this or not sp_next or sp_next == sp_this:
        return None
    return "UP" if sp_next > sp_this else "DOWN"
