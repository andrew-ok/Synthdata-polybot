"""Local paper-trading reports backed by the fills ledger."""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import CONFIG

log = logging.getLogger(__name__)


def _tz() -> "ZoneInfo | timezone":
    """Resolve the report timezone. On Windows (no system IANA db) this needs
    the `tzdata` package; if it's missing entirely, fall back to fixed UTC so a
    report never crashes."""
    try:
        return ZoneInfo(CONFIG.report_timezone)
    except (ZoneInfoNotFoundError, ModuleNotFoundError, KeyError):
        log.warning("Timezone %r unavailable (install tzdata); using UTC", CONFIG.report_timezone)
        try:
            return ZoneInfo("UTC")
        except (ZoneInfoNotFoundError, ModuleNotFoundError, KeyError):
            return timezone.utc


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
        "realized_pnl": 0.0,
    })
    total = {"trades": 0, "wins": 0, "losses": 0, "open": 0, "notional": 0.0, "realized_pnl": 0.0}

    for row in load_fills():
        if row.get("voided"):
            continue
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
        try:
            realized = float(row.get("realized_pnl")) if row.get("realized_pnl") is not None else 0.0
        except (TypeError, ValueError):
            realized = 0.0

        for target in (bucket, total):
            target["trades"] += 1
            target["notional"] += notional
            target["realized_pnl"] += realized
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
            f"Notional `${total['notional']:.2f}` | "
            f"Realized PnL `${total['realized_pnl']:+.2f}`"
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
            f"`${row['notional']:.2f}` | PnL `${row['realized_pnl']:+.2f}`"
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
