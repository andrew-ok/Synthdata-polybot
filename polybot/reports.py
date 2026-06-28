"""Local paper-trading reports backed by the fills ledger."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import CONFIG
from .position_manager import load_positions, open_positions, exposure_summary

log = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(CONFIG.report_timezone)
    except ZoneInfoNotFoundError:
        log.warning("Unknown REPORT_TIMEZONE=%r; using UTC", CONFIG.report_timezone)
        return ZoneInfo("UTC")


def load_fills() -> List[Dict[str, Any]]:
    path = os.path.join(CONFIG.log_dir, "fills.jsonl")
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


def _parse_ts(row: Dict[str, Any]) -> Optional[datetime]:
    raw = row.get("timestamp")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _is_win(row: Dict[str, Any]) -> Optional[bool]:
    outcome = str(row.get("resolved_outcome") or "").upper()
    side = str(row.get("side") or "").upper()
    if outcome in ("UP", "DOWN"):
        return side == outcome
    if "realized_pnl" in row:
        try:
            return float(row["realized_pnl"]) > 0
        except (TypeError, ValueError):
            return None
    return None


def daily_report_rows(report_date: date) -> Dict[str, Any]:
    tz = _tz()
    start = datetime.combine(report_date, time.min, tz)
    end = datetime.combine(report_date, time.max, tz)

    by_asset: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "open": 0,
        "notional": 0.0,
    })
    total = {"trades": 0, "wins": 0, "losses": 0, "open": 0, "notional": 0.0}

    for row in load_fills():
        ts = _parse_ts(row)
        if ts is None:
            continue
        local_ts = ts.astimezone(tz)
        if local_ts < start or local_ts > end:
            continue

        asset = str(row.get("asset") or "-").upper()
        bucket = by_asset[asset]
        win = _is_win(row)
        notional = float(row.get("notional_usd") or 0.0)

        for target in (bucket, total):
            target["trades"] += 1
            target["notional"] += notional
            if win is True:
                target["wins"] += 1
            elif win is False:
                target["losses"] += 1
            else:
                target["open"] += 1

    return {
        "date": report_date.isoformat(),
        "timezone": str(tz),
        "total": total,
        "by_asset": dict(sorted(by_asset.items())),
    }


def current_report_date() -> date:
    return datetime.now(timezone.utc).astimezone(_tz()).date()


def format_daily_report(report: Dict[str, Any]) -> str:
    total = report["total"]
    lines = [
        f"**POLYBOT DAILY PAPER REPORT - {report['date']} ({report['timezone']})**",
        (
            f"Total: `{total['trades']}` trades | "
            f"W `{total['wins']}` / L `{total['losses']}` / Open `{total['open']}` | "
            f"Notional `${total['notional']:.2f}`"
        ),
    ]
    if not report["by_asset"]:
        lines.append("No paper trades recorded for this date.")
        return "\n".join(lines)

    lines.append("")
    lines.append("By asset:")
    for asset, row in report["by_asset"].items():
        lines.append(
            f"- `{asset}`: `{row['trades']}` trades | "
            f"W `{row['wins']}` / L `{row['losses']}` / Open `{row['open']}` | "
            f"`${row['notional']:.2f}`"
        )
    return "\n".join(lines)


def write_daily_report(report_date: Optional[date] = None) -> Dict[str, str]:
    """Persist the daily report as Markdown plus machine-readable JSON."""
    when = report_date or current_report_date()
    report = daily_report_rows(when)
    os.makedirs(CONFIG.report_dir, exist_ok=True)

    base = os.path.join(CONFIG.report_dir, when.isoformat())
    md_path = f"{base}.md"
    json_path = f"{base}.json"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(format_daily_report(report) + "\n")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    return {"markdown": md_path, "json": json_path}


def write_strategy_b_reports() -> Dict[str, str]:
    os.makedirs(CONFIG.report_dir, exist_ok=True)
    positions = load_positions()
    open_pos = [p for p in positions if p.status == "open"]
    closed = [p for p in positions if p.status == "closed"]
    pnl = sum(float(p.realized_pnl or 0.0) for p in closed)
    exits: Dict[str, int] = {}
    for p in closed:
        exits[p.exit_reason or "-"] = exits.get(p.exit_reason or "-", 0) + 1

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "open_positions": len(open_pos),
        "closed_positions": len(closed),
        "realized_pnl": round(pnl, 4),
        "open_exposure": exposure_summary(),
        "exit_reasons": exits,
    }
    summary_path = os.path.join(CONFIG.report_dir, "strategy_b_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")

    positions_path = os.path.join(CONFIG.report_dir, "positions_report.md")
    lines = [
        "# Positions Report",
        "",
        f"Open positions: {len(open_pos)}",
        f"Closed positions: {len(closed)}",
        f"Realized PnL: ${pnl:.2f}",
        "",
        "| Status | Asset | Horizon | Side | Event key | Entry | Latest score | Exit reason | PnL |",
        "|---|---|---|---|---|---:|---:|---|---:|",
    ]
    for p in positions:
        lines.append(
            f"| {p.status} | {p.asset} | {p.horizon} | {p.side} | `{p.event_key}` | "
            f"{p.entry_price:.4f} | {p.latest_score:.4f} | {p.exit_reason or ''} | {float(p.realized_pnl or 0.0):.2f} |"
        )
    with open(positions_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    edge_decay_path = os.path.join(CONFIG.report_dir, "edge_decay_report.md")
    with open(edge_decay_path, "w", encoding="utf-8") as f:
        f.write(_edge_decay_report())

    return {
        "summary": summary_path,
        "positions": positions_path,
        "edge_decay": edge_decay_path,
    }


def _edge_decay_report() -> str:
    if not os.path.exists(CONFIG.snapshot_db_path):
        return "# Edge Decay Report\n\nNo snapshots recorded yet.\n"
    rows = []
    try:
        with sqlite3.connect(CONFIG.snapshot_db_path) as conn:
            rows = conn.execute(
                """
                SELECT event_key, side, COUNT(*) AS n,
                       MIN(raw_synth_probability), MAX(raw_synth_probability),
                       AVG(COALESCE(synth_probability_delta, 0))
                FROM snapshots
                GROUP BY event_key, side
                ORDER BY n DESC
                LIMIT 50
                """
            ).fetchall()
    except sqlite3.Error:
        rows = []
    lines = [
        "# Edge Decay Report",
        "",
        "| Event key | Side | Snapshots | Min p | Max p | Avg delta |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for event_key, side, n, min_p, max_p, avg_delta in rows:
        lines.append(f"| `{event_key}` | {side} | {n} | {min_p:.4f} | {max_p:.4f} | {avg_delta:.6f} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Calibration bucket report (probability range → realized win rate)
# ---------------------------------------------------------------------------

_PROB_BUCKETS: List[Tuple[float, float, str]] = [
    (0.50, 0.55, "50–55%"),
    (0.55, 0.60, "55–60%"),
    (0.60, 0.65, "60–65%"),
    (0.65, 0.70, "65–70%"),
    (0.70, 0.75, "70–75%"),
    (0.75, 1.01, "75%+"),
]


def calibration_bucket_report() -> str:
    """Bucket Synth probabilities by predicted range and report realized win rates."""
    from .calibration import load_observations, _is_win as cal_is_win
    rows = load_observations()
    buckets: Dict[str, Dict[str, Any]] = {label: {"n": 0, "wins": 0, "prob_sum": 0.0}
                                           for _, _, label in _PROB_BUCKETS}
    for row in rows:
        if row.realized_outcome not in ("UP", "DOWN"):
            continue
        p = row.predicted_probability
        for lo, hi, label in _PROB_BUCKETS:
            if lo <= p < hi:
                buckets[label]["n"] += 1
                buckets[label]["prob_sum"] += p
                if cal_is_win(row.side, row.realized_outcome):
                    buckets[label]["wins"] += 1
                break

    lines = [
        "# Calibration Bucket Report",
        "",
        "| Range | N | Avg predicted | Win rate | Cal error |",
        "|---|---:|---:|---:|---:|",
    ]
    for _, _, label in _PROB_BUCKETS:
        b = buckets[label]
        n = b["n"]
        if n == 0:
            lines.append(f"| {label} | 0 | — | — | — |")
            continue
        avg_pred = b["prob_sum"] / n
        win_rate = b["wins"] / n
        cal_error = abs(avg_pred - win_rate)
        lines.append(
            f"| {label} | {n} | {avg_pred:.3f} | {win_rate:.3f} | {cal_error:.3f} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Edge decay summary (fills-based: entry edge vs. exit price movement)
# ---------------------------------------------------------------------------

def edge_decay_summary() -> str:
    """Analyze how quickly entry edge converges after a fill.

    Uses positions.jsonl: compares entry_edge to the final exit price to
    estimate whether the edge captured at entry was actually realised.
    Snapshot-level intra-holding analysis is not available without
    time-series per-position snapshots; this is an approximation.
    """
    positions = load_positions()
    closed = [p for p in positions if p.status == "closed" and p.exit_price is not None]

    n = len(closed)
    if n == 0:
        return "# Edge Decay Summary\n\nNo closed positions to analyse.\n"

    entry_edges: List[float] = []
    final_fills: List[float] = []
    converged = 0
    widened_against = 0
    total_pnl = 0.0

    for p in closed:
        entry_edge = p.entry_edge  # net edge at entry
        exit_px = float(p.exit_price)
        entry_px = float(p.entry_price)
        remaining_at_exit = float(p.entry_raw_synth_probability) - exit_px
        entry_edges.append(entry_edge)
        final_fills.append(remaining_at_exit)
        # Edge converged: exit price moved toward fair value
        if remaining_at_exit <= CONFIG.exit_edge_threshold:
            converged += 1
        # Edge widened against us: exit bid fell below entry price (unrealised loss)
        if exit_px < entry_px:
            widened_against += 1
        total_pnl += float(p.realized_pnl or 0.0)

    avg_entry_edge = sum(entry_edges) / n
    avg_remaining = sum(final_fills) / n

    lines = [
        "# Edge Decay Summary",
        "",
        f"Closed positions analysed: {n}",
        f"Average entry net edge:    {avg_entry_edge:.4f}",
        f"Average remaining edge at exit: {avg_remaining:.4f}",
        f"Edge converged (≤ {CONFIG.exit_edge_threshold:.3f}): {converged}/{n} ({converged/n:.1%})",
        f"Edge widened against us:   {widened_against}/{n} ({widened_against/n:.1%})",
        f"Total realised PnL:        ${total_pnl:.2f}",
        "",
        "Note: intra-holding edge decay at 30/60/120s requires per-position",
        "snapshot series. Run the scanner with --execute to accumulate data.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Strategy health report (dry-run dashboard)
# ---------------------------------------------------------------------------

def strategy_health_report() -> str:
    """Print a dry-run health snapshot: config, exposure, recent signals, exits."""
    from .calibration import load_observations
    lines: List[str] = []

    lines += [
        "=" * 70,
        "STRATEGY HEALTH REPORT",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "=" * 70,
        "",
        "── CONFIG ──────────────────────────────────────────────────────────",
        f"  execution_mode:           {CONFIG.execution_mode}",
        f"  maker_fill_model:         {CONFIG.maker_fill_model}",
        f"  min_entry_edge:           {CONFIG.min_entry_edge}",
        f"  thin_market_min_edge:     {CONFIG.thin_market_min_edge} ({', '.join(CONFIG.thin_market_assets)})",
        f"  min_exit_edge:            {CONFIG.min_exit_edge}",
        f"  exit_edge_threshold:      {CONFIG.exit_edge_threshold}",
        f"  time_stop_seconds:        {CONFIG.time_stop_seconds}",
        f"  cancel_quotes_before_close_seconds: {CONFIG.cancel_quotes_before_close_seconds}",
        f"  min_seconds_to_enter:     {CONFIG.min_seconds_to_enter}",
        f"  kelly_fraction:           {CONFIG.kelly_fraction}",
        f"  max_position_size:        {CONFIG.max_position_size} ({CONFIG.max_position_size*100:.0f}% of bankroll)",
        f"  bankroll_usd:             ${CONFIG.bankroll_usd:.2f}",
        f"  min_liquidity_usd:        ${CONFIG.min_liquidity:.0f}",
        f"  liquidity_size_multiple:  {CONFIG.liquidity_size_multiple}×",
        f"  paper_position_size_usd:  ${CONFIG.paper_position_size_usd:.2f}",
        f"  one_position_per_market:  {CONFIG.one_position_per_market}",
        f"  allow_reentry_after_exit: {CONFIG.allow_reentry_after_exit}",
        f"  paper_trade_mode:         {CONFIG.paper_trade_mode}",
        "",
    ]

    # Exposure
    exp = exposure_summary()
    lines += [
        "── EXPOSURE ────────────────────────────────────────────────────────",
        f"  Open positions: {exp['count']}   Total notional: ${exp['total']:.2f}",
    ]
    if exp["by_asset"]:
        for asset, notional in sorted(exp["by_asset"].items()):
            lines.append(f"    {asset}: ${notional:.2f}")
    if exp["by_horizon"]:
        for hz, notional in sorted(exp["by_horizon"].items()):
            lines.append(f"    {hz}: ${notional:.2f}")
    lines.append("")

    # Positions
    all_positions = load_positions()
    open_pos = [p for p in all_positions if p.status == "open"]
    closed_pos = [p for p in all_positions if p.status == "closed"]
    realized_pnl = sum(float(p.realized_pnl or 0.0) for p in closed_pos)
    exit_counts: Dict[str, int] = {}
    for p in closed_pos:
        k = p.exit_reason or "-"
        exit_counts[k] = exit_counts.get(k, 0) + 1

    lines += [
        "── POSITIONS ───────────────────────────────────────────────────────",
        f"  Open: {len(open_pos)}   Closed: {len(closed_pos)}   Realised PnL: ${realized_pnl:.2f}",
    ]
    if exit_counts:
        lines.append("  Exit reasons: " + "  ".join(f"{k}={v}" for k, v in sorted(exit_counts.items())))
    if open_pos:
        lines.append("  Open positions:")
        for p in open_pos[:10]:
            lines.append(
                f"    [{p.asset}/{p.horizon}/{p.side}] edge={p.entry_edge:.3f} "
                f"score={p.latest_score:.4f} notional=${p.notional_usd:.2f}"
            )
    lines.append("")

    # Calibration observations
    obs = load_observations()
    lines += [
        "── CALIBRATION ─────────────────────────────────────────────────────",
        f"  Total observations: {len(obs)}",
    ]
    if len(obs) < CONFIG.min_calibration_samples_global:
        lines.append(
            f"  WARNING: fewer than {CONFIG.min_calibration_samples_global} observations; "
            "calibration is raw-pass-through (shrinkage toward raw Synth probability)."
        )
    lines.append("")

    # Recent fills
    fills = load_fills()
    entry_fills = [f for f in fills if f.get("entry_or_exit") == "entry"]
    exit_fills = [f for f in fills if f.get("entry_or_exit") == "exit"]
    lines += [
        "── RECENT FILLS (last 10 entries) ──────────────────────────────────",
    ]
    for row in entry_fills[-10:]:
        ts = (row.get("timestamp") or "")[:19]
        lines.append(
            f"  {ts} {row.get('asset','?')}/{row.get('horizon','?')}/{row.get('side','?')} "
            f"fill={row.get('fill_price',0):.4f} edge={row.get('net_edge',0):.4f} "
            f"mode={row.get('execution_mode','?')}"
        )
    if not entry_fills:
        lines.append("  (none)")
    lines.append("")

    lines += [
        "── RECENT EXITS (last 10) ───────────────────────────────────────────",
    ]
    for row in exit_fills[-10:]:
        ts = (row.get("timestamp") or "")[:19]
        pnl = row.get("realized_pnl", row.get("notional_usd", 0))
        lines.append(
            f"  {ts} {row.get('asset','?')}/{row.get('side','?')} "
            f"reason={row.get('exit_reason','?')} pnl=${float(pnl or 0):.2f}"
        )
    if not exit_fills:
        lines.append("  (none)")
    lines.append("")

    # Calibration bucket summary inline
    lines += [
        "── CALIBRATION BUCKETS ─────────────────────────────────────────────",
        calibration_bucket_report(),
    ]

    # Edge decay inline
    lines += [
        "── EDGE DECAY ──────────────────────────────────────────────────────",
        edge_decay_summary(),
    ]

    # Order lifecycle summary
    try:
        from .order_manager import load_orders
        all_orders = load_orders()
        pending = [o for o in all_orders if o.status == "PENDING"]
        cancelled = [o for o in all_orders if o.status == "CANCELLED"]
        filled = [o for o in all_orders if o.status == "FILLED"]
        cancel_reasons: Dict[str, int] = {}
        for o in cancelled:
            k = o.cancel_reason or "-"
            cancel_reasons[k] = cancel_reasons.get(k, 0) + 1
        lines += [
            "── ORDER LIFECYCLE ─────────────────────────────────────────────────",
            f"  Pending: {len(pending)}   Filled: {len(filled)}   Cancelled: {len(cancelled)}",
        ]
        if cancel_reasons:
            lines.append("  Cancel reasons: " + "  ".join(f"{k}={v}" for k, v in sorted(cancel_reasons.items())))
        lines.append("")
    except Exception as exc:
        lines += [
            "── ORDER LIFECYCLE ─────────────────────────────────────────────────",
            f"  (unavailable: {exc})",
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Adverse selection report
# ---------------------------------------------------------------------------

def adverse_selection_report() -> str:
    """Compute post-fill price movement as an adverse selection proxy.

    Compares fill_price to best_bid at T+30s and T+60s using snapshot data.
    Returns a text report. Requires accumulated snapshots.
    """
    fills = [f for f in load_fills() if f.get("entry_or_exit") == "entry"]
    if not fills:
        return "# Adverse Selection Report\n\nNo entry fills recorded yet.\n"

    if not os.path.exists(CONFIG.snapshot_db_path):
        return (
            "# Adverse Selection Report\n\n"
            f"Entry fills analysed: {len(fills)}\n\n"
            "No snapshot database found. Run the scanner with --execute to accumulate data.\n"
        )

    lines = [
        "# Adverse Selection Report",
        "",
        "Compares fill price to subsequent best bid in snapshot DB.",
        "",
        f"Entry fills analysed: {len(fills)}",
        "",
        "| Asset/Horizon/Side | N | Avg fill px | Avg bid +30s | Avg bid +60s | Avg Δ30s | Avg Δ60s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    try:
        with sqlite3.connect(CONFIG.snapshot_db_path) as conn:
            conn.row_factory = sqlite3.Row
            by_segment: Dict[str, Any] = {}
            for fill in fills:
                ts_str = fill.get("timestamp", "")
                asset = fill.get("asset", "?")
                horizon = fill.get("horizon", "?")
                side = fill.get("side", "?")
                fill_px = float(fill.get("fill_price") or 0)
                event_key = fill.get("event_key", "")
                seg = f"{asset}/{horizon}/{side}"

                if not ts_str or not event_key:
                    continue
                try:
                    fill_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if seg not in by_segment:
                    by_segment[seg] = {
                        "n": 0,
                        "fill_sum": 0.0,
                        "bid_30_sum": 0.0,
                        "bid_60_sum": 0.0,
                        "n_30": 0,
                        "n_60": 0,
                    }
                by_segment[seg]["n"] += 1
                by_segment[seg]["fill_sum"] += fill_px

                for delta_sec, key in [(30, "bid_30"), (60, "bid_60")]:
                    target_ts = fill_time.timestamp() + delta_sec
                    row = conn.execute(
                        """
                        SELECT best_bid FROM snapshots
                        WHERE event_key = ? AND side = ?
                          AND ABS(strftime('%s', timestamp_utc) - ?) < 15
                        ORDER BY ABS(strftime('%s', timestamp_utc) - ?)
                        LIMIT 1
                        """,
                        (event_key, side, target_ts, target_ts),
                    ).fetchone()
                    bid = float(row["best_bid"]) if row and row["best_bid"] is not None else None

                    if key == "bid_30" and bid is not None:
                        by_segment[seg]["bid_30_sum"] += bid
                        by_segment[seg]["n_30"] += 1
                    if key == "bid_60" and bid is not None:
                        by_segment[seg]["bid_60_sum"] += bid
                        by_segment[seg]["n_60"] += 1

        if not by_segment:
            lines.append("| — | No matches found | — | — | — | — | — |")
        else:
            for seg, d in sorted(by_segment.items()):
                n = d["n"]
                avg_fill = d["fill_sum"] / n if n else 0.0
                avg_30 = d["bid_30_sum"] / d["n_30"] if d["n_30"] else None
                avg_60 = d["bid_60_sum"] / d["n_60"] if d["n_60"] else None
                d30 = f"{avg_30 - avg_fill:+.4f}" if avg_30 is not None else "—"
                d60 = f"{avg_60 - avg_fill:+.4f}" if avg_60 is not None else "—"
                avg_30_str = f"{avg_30:.4f}" if avg_30 is not None else "—"
                avg_60_str = f"{avg_60:.4f}" if avg_60 is not None else "—"
                lines.append(
                    f"| {seg} | {n} | {avg_fill:.4f} | {avg_30_str} | {avg_60_str} | {d30} | {d60} |"
                )
    except sqlite3.Error as e:
        lines.append(f"Error reading snapshots: {e}")

    return "\n".join(lines) + "\n"
