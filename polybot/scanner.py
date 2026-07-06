"""Scanner entrypoint.

Pulls pre-matched Synth+Polymarket opportunities for {BTC, ETH, SOL, HYPE}
across the 15M and 1H horizons, runs the signal engine + risk gates, and
prints the ranked table. Paper trading only.

Usage:
    python -m polybot.scanner
    python -m polybot.scanner --show-skipped
    python -m polybot.scanner --execute
    python -m polybot.scanner --backtest 2026-06-10T00:00:00Z 2026-06-14T00:00:00Z
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional

from .config import CONFIG
from .calibration import write_calibration_report
from .clob_enrichment import enrich_real_clob
from .dashboard import render
from .settlement import settle_positions
from .reports import (
    current_report_date,
    daily_report_rows,
    format_daily_report,
    write_daily_report,
)
from .execution import execute_decisions
from .risk_manager import RiskManager
from .signal_engine import evaluate
from .synth_client import SynthInsightsClient, configured_horizons


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def run_scan(execute: bool, show_skipped: bool, limit: int, kelly: bool) -> int:
    CONFIG.assert_paper_only()
    log = logging.getLogger("scanner")

    client = SynthInsightsClient()
    horizons = configured_horizons()
    opps = client.fetch_all(assets=CONFIG.synth_assets, horizons=horizons)
    opps = enrich_real_clob(opps)
    log.info("Opportunities: %d  (assets=%s)", len(opps), CONFIG.synth_assets)

    if execute:
        settled = settle_positions(opps, client)
        if settled["exit_at_fair"] or settled["resolution"]:
            log.info("Settled: %d exit-at-fair, %d at resolution, %d pending",
                     settled["exit_at_fair"], settled["resolution"], settled["pending"])

    signals = evaluate(opps)
    log.info("Signals at raw threshold %.2f: %d", CONFIG.min_edge_threshold, len(signals))

    risk = RiskManager(use_kelly=kelly)
    decisions = risk.evaluate(signals)
    accepted = [d for d in decisions if d.accepted]
    log.info("Decisions: %d accepted / %d total", len(accepted), len(decisions))

    render(decisions, show_skipped=show_skipped, limit=limit)

    if execute:
        # A/B live testing RETIRED (user directive 2026-07-05): the 8-week
        # backtests showed no edge for A/B, so execution now runs only the
        # Strategy C variants. A's signal/gate pipeline above still runs for
        # logging and for settling any legacy open A fills.
        settled_a = settle_positions(opps, client)
        if settled_a["resolution"] or settled_a["exit_at_fair"]:
            log.info("Legacy A ledger settled: %d resolved", settled_a["resolution"])

        try:
            from .strategy_c import run_c
            c = run_c(opps, client)
            if c["fills"] or c["resolution"]:
                log.info("C variants: %d fills, %d settled this cycle", c["fills"], c["resolution"])
        except Exception as exc:  # noqa: BLE001 — C isolation preserved
            log.warning("Strategy C pass failed: %s", exc)

    return 0 if accepted else 1


def run_backtest_cli(start: str, end: str) -> int:
    from .backtester import estimate_call_count, run_backtest, best_threshold

    estimated = estimate_call_count(start, end)
    if estimated > CONFIG.max_backtest_calls_without_confirm and os.environ.get("CONFIRM_BACKTEST_SPEND") != "YES":
        print(
            f"Refusing to run: estimated {estimated} Synth calls. "
            f"Set CONFIRM_BACKTEST_SPEND=YES to allow this run."
        )
        print("No API calls were made.")
        return 2

    os.makedirs(CONFIG.log_dir, exist_ok=True)
    out_path = os.path.join(CONFIG.log_dir, "last_backtest.txt")
    with _single_instance(os.path.join(CONFIG.log_dir, "backtest.lock")):
        horizons = configured_horizons()
        rows = [
            f"Backtest started: {datetime.now(timezone.utc).isoformat()}",
            f"Window: {start} -> {end}",
            f"Assets: {','.join(CONFIG.synth_assets)}",
            f"Horizons: {','.join(horizons)}",
            f"Estimated Synth calls: {estimated}",
            "",
        ]
        results = run_backtest(start, end, horizons=horizons)
        if not results:
            rows.append("No backtest results — window may have no data, or API failed.")
            _write_and_print(out_path, rows)
            return 1

        cols = ["threshold", "trades", "win_rate", "avg_EV", "realized_pnl",
                "max_drawdown", "avg_spread", "avg_slippage_cost",
                "avg_holding_period_sec", "sharpe_proxy"]
        rows.append(" | ".join(f"{c:>14}" for c in cols))
        rows.append("-" * (17 * len(cols)))
        for r in results:
            rows.append(" | ".join(f"{r[c]:>14}" for c in cols))
        rows.append("")
        rows.append(f"Best threshold by Sharpe proxy: {best_threshold(results)}")
        rows.append(f"Backtest finished: {datetime.now(timezone.utc).isoformat()}")
        _write_and_print(out_path, rows)
        print(f"\nSaved to {out_path}")
    return 0


@contextmanager
def _single_instance(lock_path: str):
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise SystemExit(f"Another backtest appears to be running ({lock_path}). Refusing duplicate run.")
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def _write_and_print(path: str, rows: List[str]) -> None:
    text = "\n".join(rows) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Polymarket × Synth paper-trading scanner")
    p.add_argument("--execute", action="store_true", help="Write paper fills for accepted signals")
    p.add_argument("--show-skipped", action="store_true", help="Show why signals were rejected")
    p.add_argument("--limit", type=int, default=25, help="Max rows to print")
    p.add_argument("--kelly", action="store_true", help="Use fractional-Kelly sizing")
    p.add_argument("--backtest", nargs=2, metavar=("START_ISO", "END_ISO"),
                   help="Run a historical backtest between two ISO timestamps")
    p.add_argument("--daily-report", nargs="?", const="today", metavar="YYYY-MM-DD",
                   help="Print and save a daily paper-trade report")
    p.add_argument("--calibration-report", action="store_true",
                   help="Generate calibration metrics and reliability curves")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    if args.daily_report:
        if args.daily_report == "today":
            report_date = current_report_date()
        else:
            report_date = datetime.strptime(args.daily_report, "%Y-%m-%d").date()
        report = daily_report_rows(report_date)
        print(format_daily_report(report))
        paths = write_daily_report(report_date)
        print(f"\nSaved report: {paths['markdown']}")
        print(f"Saved stats:  {paths['json']}")
        return 0

    if args.calibration_report:
        paths = write_calibration_report()
        print(f"Saved calibration report: {paths['markdown']}")
        print(f"Saved calibration stats:  {paths['json']}")
        return 0

    if args.backtest:
        return run_backtest_cli(*args.backtest)
    return run_scan(args.execute, args.show_skipped, args.limit, args.kelly)


if __name__ == "__main__":
    sys.exit(main())
