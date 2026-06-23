"""Dashboard.

Prints the current top opportunities (and rejections) in a tight CLI table.
Falls back from `rich` to plain ASCII if rich isn't installed.
"""
from __future__ import annotations

from typing import Iterable, List

from .risk_manager import Decision


_COLS = [
    ("Asset",       6),
    ("Horizon",     7),
    ("Side",        6),
    ("Edge(raw)",  10),
    ("Edge(net)",  10),
    ("Synth p",     8),
    ("Cal p",       8),
    ("Ask",         7),
    ("Spread",      7),
    ("Liq($)",      9),
    ("Size($)",     8),
    ("Status",     10),
    ("Slug",       38),
]


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt(v, places=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{places}f}"
    return str(v)


def _print_plain(decisions: List[Decision], show_skipped: bool, limit: int) -> None:
    header = " | ".join(f"{name:<{width}}" for name, width in _COLS)
    print(header)
    print("-" * len(header))

    shown = 0
    for d in decisions:
        if not show_skipped and not d.accepted:
            continue
        s = d.signal
        row = [
            getattr(s, "asset", "-"),
            getattr(s, "horizon", "-"),
            s.side,
            _fmt(s.raw_edge),
            _fmt(s.net_edge),
            _fmt(s.synth_probability),
            _fmt(getattr(s, "calibrated_probability", s.synth_probability)),
            _fmt(s.execution_price),
            _fmt(s.spread),
            f"{s.liquidity:,.0f}" if s.liquidity else "—",
            f"{d.position_size_usd:,.2f}" if d.accepted else "—",
            "ACCEPT" if d.accepted else "skip",
            _truncate(getattr(s, "slug", s.market_question), 38),
        ]
        line = " | ".join(f"{cell:<{width}}" for cell, (_, width) in zip(row, _COLS))
        # Append reason for skipped trades so the user sees *why*.
        if not d.accepted:
            line += f"   ← {d.reason}"
        print(line)
        shown += 1
        if shown >= limit:
            break


def render(decisions: Iterable[Decision], show_skipped: bool = False, limit: int = 25) -> None:
    decisions = sorted(decisions, key=lambda d: (not d.accepted, -d.signal.net_edge))
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        _print_plain(decisions, show_skipped, limit)
        return

    console = Console()
    table = Table(
        title="Polybot — Top Opportunities (paper mode)",
        show_lines=False,
        header_style="bold cyan",
    )
    for name, _ in _COLS:
        table.add_column(name)
    table.add_column("Reason")

    shown = 0
    for d in decisions:
        if not show_skipped and not d.accepted:
            continue
        s = d.signal
        status_style = "green" if d.accepted else "yellow"
        table.add_row(
            getattr(s, "asset", "-"),
            getattr(s, "horizon", "-"),
            f"[bold]{s.side}[/bold]",
            _fmt(s.raw_edge),
            f"[bold]{_fmt(s.net_edge)}[/bold]",
            _fmt(s.synth_probability),
            _fmt(getattr(s, "calibrated_probability", s.synth_probability)),
            _fmt(s.execution_price),
            _fmt(s.spread),
            f"{s.liquidity:,.0f}" if s.liquidity else "—",
            f"${d.position_size_usd:,.2f}" if d.accepted else "—",
            f"[{status_style}]{'ACCEPT' if d.accepted else 'skip'}[/{status_style}]",
            _truncate(getattr(s, "slug", s.market_question), 38),
            _truncate(d.reason, 40),
        )
        shown += 1
        if shown >= limit:
            break

    console.print(table)
