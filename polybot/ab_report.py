"""A/B strategy overlay report.

Scores the SAME fills ledger under two rulebooks, so both strategies are
live-tested from one stream of real trades with zero extra API calls:

  A. "Spec 20c"  — the strategy actually running: every non-voided fill,
     P&L exactly as settled (fill_price already includes slippage).
  B. "E 30c taker" — virtual: only fills whose raw_edge >= E_THRESHOLD at
     entry, with entry cost re-priced to include the taker fee. Settlement
     outcomes (exit price / resolved outcome) are reused as recorded.

Per-trade P&L is computed per contract and normalized per $1 risked, so the
comparison is sizing-independent. A fixed virtual stake also produces a
cumulative-$ view for each book.

Run:  python -m polybot.ab_report
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional

from .config import CONFIG

E_THRESHOLD = 0.30          # strategy B takes only >=30c edges
E_TAKER_FEE_BPS = 150.0     # extra cost strategy B pays crossing the spread
VIRTUAL_STAKE_USD = 37.50   # fixed per-trade stake for the cumulative-$ view

# Strategy C (own live ledger) — mirror strategy_c.py's env-tunable knobs.
C_LEDGER = "fills_C.jsonl"
C_MIN_EDGE = float(os.environ.get("C_MIN_EDGE", "0.10"))
C_MAX_SEC = float(os.environ.get("C_MAX_SEC_TO_END", "1200"))


def _load_fills(name: str = "fills.jsonl") -> List[Dict[str, Any]]:
    path = os.path.join(CONFIG.log_dir, name)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("voided"):
                rows.append(row)
    return rows


def _entry_price(row: Dict[str, Any], taker: bool) -> Optional[float]:
    """Entry cost per contract. A uses the recorded fill price; B re-prices
    from the intended ask with slippage + taker fee (both proportional)."""
    if not taker:
        px = row.get("fill_price")
    else:
        intended = row.get("intended_price")
        if intended is None:
            return None
        px = float(intended) * (1.0 + (CONFIG.assumed_slippage_bps + E_TAKER_FEE_BPS) / 10_000.0)
    try:
        px = float(px)
    except (TypeError, ValueError):
        return None
    return px if 0.0 < px < 1.0 else None


def _trade_return(row: Dict[str, Any], entry: float) -> Optional[Dict[str, float]]:
    """Return per $1 risked for a settled fill, else None if still open."""
    if row.get("close_kind") == "exit_at_fair" and row.get("exit_price") is not None:
        exit_px = float(row["exit_price"])
        r = (exit_px - entry) / entry
        return {"ret": r, "won": 1.0 if r > 0 else 0.0}
    outcome = str(row.get("resolved_outcome") or "").upper()
    if outcome in ("UP", "DOWN"):
        won = str(row.get("side") or "").upper() == outcome
        r = (1.0 - entry) / entry if won else -1.0
        return {"ret": r, "won": 1.0 if won else 0.0}
    return None  # still open


def _book(rows: List[Dict[str, Any]], taker: bool, min_edge: float) -> Dict[str, Any]:
    rets: List[float] = []
    wins = 0
    open_count = 0
    eligible = 0
    for row in rows:
        try:
            edge = float(row.get("raw_edge") or 0.0)
        except (TypeError, ValueError):
            continue
        if edge < min_edge:
            continue
        eligible += 1
        entry = _entry_price(row, taker)
        if entry is None:
            continue
        result = _trade_return(row, entry)
        if result is None:
            open_count += 1
            continue
        rets.append(result["ret"])
        wins += int(result["won"])

    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rets) / n
        sharpe = mean / math.sqrt(var) if var > 1e-12 else 0.0
    else:
        sharpe = 0.0
    return {
        "eligible_fills": eligible,
        "settled": n,
        "open": open_count,
        "wins": wins,
        "losses": n - wins,
        "win_rate": wins / n if n else 0.0,
        "avg_return_per_$": mean,
        "per_trade_sharpe": sharpe,
        "cum_pnl_usd_fixed_stake": sum(r * VIRTUAL_STAKE_USD for r in rets),
    }


def format_report() -> str:
    # Live test = the three C variants (A/B execution retired 2026-07-05).
    cols = [
        ("C-RAMP", _book(_load_fills("fills_C_ramp.jsonl"), taker=False, min_edge=0.0)),
        ("C 70-75c", _book(_load_fills("fills_C_b7075.jsonl"), taker=False, min_edge=0.0)),
        ("C 80-85c", _book(_load_fills("fills_C_b8085.jsonl"), taker=False, min_edge=0.0)),
        ("C-VETO", _book(_load_fills("fills_C_veto.jsonl"), taker=False, min_edge=0.0)),
        ("C-RAMP+P", _book(_load_fills("fills_C_rampp.jsonl"), taker=False, min_edge=0.0)),
    ]
    W = 18
    lines = [
        "**LIVE TEST — three Strategy C variants (A/B retired 2026-07-05)**",
        "(hourly favorites, last 20min | RAMP: edge 1.5pp@70c->5.9pp@90c | "
        "70-75c @ >=1.5pp | 80-85c @ >=3pp)",
        "",
        f"{'':22s}" + "".join(f"{name:>{W}s}" for name, _ in cols),
    ]

    def line(label, render):
        lines.append(f"{label:22s}" + "".join(f"{render(bk):>{W}s}" for _, bk in cols))

    line("fills seen",        lambda bk: str(bk["eligible_fills"]))
    line("settled / open",    lambda bk: f"{bk['settled']} / {bk['open']}")
    line("wins / losses",     lambda bk: f"{bk['wins']} / {bk['losses']}")
    line("win rate",          lambda bk: f"{bk['win_rate']:.1%}")
    line("avg return / $1",   lambda bk: f"{bk['avg_return_per_$']:+.3f}")
    line("per-trade Sharpe",  lambda bk: f"{bk['per_trade_sharpe']:.3f}")
    line("cum P&L (fixed)",   lambda bk: f"${bk['cum_pnl_usd_fixed_stake']:+.2f}")
    return "\n".join(lines)


def main() -> int:
    print(format_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
