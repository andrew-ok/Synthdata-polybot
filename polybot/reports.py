"""Local paper-trading reports backed by the fills ledger."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional
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
