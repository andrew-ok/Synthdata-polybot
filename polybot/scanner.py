"""Scanner entrypoint — Strategy B paper-trading scanner.

Fetches live Synth forecasts + real two-sided CLOB data, writes snapshots,
evaluates exits on open positions, scores and ranks entry candidates, and
(with --execute) writes paper fills linked to positions by position_id.

Usage:
    python -m polybot.scanner                    # dry run: ranked table only
    python -m polybot.scanner --show-skipped     # include rejection reasons
    python -m polybot.scanner --execute          # evaluate exits + write fills
    python -m polybot.scanner --snapshot-backtest  # replay snapshots.sqlite3
    python -m polybot.scanner --backtest 2026-06-10T00:00:00Z 2026-06-14T00:00:00Z
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional

from .config import CONFIG, apply_profile, _PROFILES
from .calibration import write_calibration_report
from .order_lifecycle import evaluate_pending_orders
from .clob_enrichment import enrich_real_clob
from .dashboard import render
from .exit_rules import evaluate_and_apply_exits, evaluate_synth_updates
from .reports import (
    current_report_date,
    daily_report_rows,
    format_daily_report,
    write_daily_report,
    write_strategy_b_reports,
    strategy_health_report,
)
from .execution import execute_decisions
from .risk_manager import RiskManager
from .signal_engine import evaluate
from .trade_log import log_signal
from .snapshot_store import write_snapshots
from .synth_client import SynthInsightsClient, configured_horizons


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _cancel_pending_on_restart() -> int:
    """Cancel all PENDING orders on bot restart (prevents stale orders)."""
    from .order_manager import get_pending_orders, cancel_order
    from .order_log import log_order_event
    pending = get_pending_orders()
    for order in pending:
        cancelled = cancel_order(order, "BOT_RESTART")
        log_order_event("ORDER_CANCELLED", cancelled, reason="BOT_RESTART")
    if pending:
        logging.getLogger("scanner").info("Cancelled %d stale PENDING orders on restart", len(pending))
    return len(pending)


def run_scan(execute: bool, show_skipped: bool, limit: int, kelly: bool) -> int:
    CONFIG.assert_paper_only()
    log = logging.getLogger("scanner")

    client = SynthInsightsClient()
    horizons = configured_horizons()
    opps = client.fetch_all(assets=CONFIG.synth_assets, horizons=horizons)
    opps = enrich_real_clob(opps)
    snapshot_rows = write_snapshots(opps)
    log.info("Opportunities: %d  (assets=%s)", len(opps), CONFIG.synth_assets)
    log.info("Snapshots written: %d", len(snapshot_rows))

    all_side_signals = evaluate(opps, threshold=-1.0)
    if CONFIG.maker_fill_model == "order_lifecycle":
        signal_map = {(s.event_key, s.side): s for s in all_side_signals}
        filled_orders, cancelled_orders = evaluate_pending_orders(signal_map)
        if filled_orders:
            log.info("Order lifecycle: %d orders FILLED", len(filled_orders))
        if cancelled_orders:
            log.info("Order lifecycle: %d orders CANCELLED", len(cancelled_orders))
    if execute:
        exits = evaluate_and_apply_exits(all_side_signals)
        if exits:
            log.info("Exit signals written: %d (see %s/exit_signals.jsonl)", len(exits), CONFIG.log_dir)
        synth_exits = evaluate_synth_updates(all_side_signals)
        if synth_exits:
            log.info("Synth-update exits: %d position(s) closed (SYNTH_EV_COLLAPSE)", len(synth_exits))

    signals = evaluate(opps)
    log.info("Signals at fair edge threshold %.3f: %d", CONFIG.min_entry_edge, len(signals))

    risk = RiskManager(use_kelly=kelly)
    decisions = risk.evaluate(signals)
    for d in decisions:
        if d.accepted:
            log_signal(event_type="ENTRY", signal=d.signal, reason=d.reason,
                       position_size=d.position_size_usd)
        else:
            log_signal(event_type="SKIP", signal=d.signal, reason=d.reason)
    accepted = [d for d in decisions if d.accepted]
    log.info("Decisions: %d accepted / %d total", len(accepted), len(decisions))

    render(decisions, show_skipped=show_skipped, limit=limit)

    if execute:
        fills = execute_decisions(decisions)
        write_strategy_b_reports()
        log.info("Paper-trade fills written: %d (see %s)", len(fills), CONFIG.log_dir)

    return 0


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
        if results:
            rs = results[0].get("resolution_stats", {})
            if rs:
                rows.append("")
                rows.append("Label resolution breakdown:")
                rows.append(f"  synth resolved:    {rs.get('labels_synth', 0)}")
                rows.append(f"  gamma_prices:      {rs.get('labels_gamma_prices', 0)}")
                rows.append(f"  gamma_winner:      {rs.get('labels_gamma_winner', 0)}")
                rows.append(f"  gamma_unresolved:  {rs.get('gamma_unresolved', 0)}")
                rows.append(f"  gamma_errors:      {rs.get('gamma_errors', 0)}")
                rows.append(f"  cache_hits:        {rs.get('gamma_cache_hits', 0)}")
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
    p.add_argument("--no-kelly", action="store_true", help="Disable fractional-Kelly sizing (Kelly is on by default)")
    p.add_argument("--backtest", nargs=2, metavar=("START_ISO", "END_ISO"),
                   help="Run a historical backtest between two ISO timestamps")
    p.add_argument("--profile", choices=list(_PROFILES), default=None,
                   help="Apply a named config profile (paper or live_safe)")
    p.add_argument("--snapshot-backtest", action="store_true",
                   help="Replay stored SQLite snapshots chronologically with Strategy B "
                        "(no Synth API calls — safe to run anytime)")
    p.add_argument("--daily-report", nargs="?", const="today", metavar="YYYY-MM-DD",
                   help="Print and save a daily paper-trade report")
    p.add_argument("--calibration-report", action="store_true",
                   help="Generate calibration metrics and reliability curves")
    p.add_argument("--strategy-b-report", action="store_true",
                   help="Generate Strategy B positions, exposure, and edge-decay reports")
    p.add_argument("--strategy-health", action="store_true",
                   help="Print a dry-run strategy health report: config, exposure, recent signals, exits")
    p.add_argument("--loop", action="store_true",
                   help="Run continuously until Ctrl-C")
    p.add_argument("--interval-seconds", type=int, default=600,
                   help="Seconds between scans when --loop is used (default 600 / 10 min). "
                        "Synth API costs 1 token per (asset × horizon) call. "
                        "BTC+ETH × 2 horizons = 4 calls/scan → ~17k tokens/month at 10 min. "
                        "Adding SOL/HYPE doubles to 8 calls/scan. Minimum enforced: 60s.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.loop:
        interval = max(60, args.interval_seconds)
        if args.interval_seconds < 60:
            logging.getLogger("scanner").warning(
                "interval_seconds=%d < 60 minimum; using 60s. "
                "At 20k Synth tokens/month the safe interval is ~260s.",
                args.interval_seconds,
            )
        _setup_logging(args.verbose)
        log = logging.getLogger("scanner")
        log.info("Starting paper trading loop: interval=%ds profile=%s", interval, args.profile or "none")
        if args.profile:
            apply_profile(CONFIG, args.profile)
        n_cancelled = _cancel_pending_on_restart()
        if n_cancelled:
            log.info("Restart: cancelled %d stale pending orders", n_cancelled)
        scan_count = 0
        while True:
            try:
                scan_count += 1
                log.info("─── Scan #%d ───", scan_count)
                run_scan(execute=True, show_skipped=args.show_skipped,
                         limit=args.limit, kelly=not args.no_kelly)
            except KeyboardInterrupt:
                log.info("Paper trader stopped (KeyboardInterrupt).")
                break
            except Exception as exc:
                log.error("Scan error: %s", exc, exc_info=True)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("Paper trader stopped during sleep.")
                break
        return 0

    _setup_logging(args.verbose)

    if args.profile:
        apply_profile(CONFIG, args.profile)
        logging.getLogger("scanner").info("Applied config profile: %s", args.profile)

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

    if args.strategy_b_report:
        paths = write_strategy_b_reports()
        for label, path in paths.items():
            print(f"Saved {label}: {path}")
        return 0

    if args.strategy_health:
        print(strategy_health_report())
        return 0

    if args.backtest:
        return run_backtest_cli(*args.backtest)
    if args.snapshot_backtest:
        from .backtester import run_snapshot_backtest
        result = run_snapshot_backtest()
        print(f"\n=== SNAPSHOT REPLAY BACKTEST (Strategy B) ===")
        print(f"Source: {result.get('source', 'snapshot_replay')}")
        print(f"Snapshots replayed: {result.get('snapshots', 0)}")
        print(f"Trades: {result['trades']}  Wins: {result['wins']}  Losses: {result['losses']}  Open: {result['open_trades']}")
        print(f"Win rate: {result['win_rate']:.2%}  Realized PnL: ${result['realized_pnl']:.2f}  ROI: {result['roi']:.2%}")
        print(f"Max drawdown: ${result['max_drawdown']:.2f}  Sharpe proxy: {result['sharpe_proxy']:.3f}")
        if result['exit_reasons']:
            print(f"Exit reasons: {result['exit_reasons']}")
        if result.get('note'):
            print(f"Note: {result['note']}")
        print()
        return 0
    return run_scan(args.execute, args.show_skipped, args.limit, not args.no_kelly)


if __name__ == "__main__":
    sys.exit(main())
