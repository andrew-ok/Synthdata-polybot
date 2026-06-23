# polybot — Polymarket × Synth paper-trading scanner

Paper-trading and backtesting first. **No live order placement.**

Compares Synth's probabilistic Up/Down forecast for a Polymarket contract
against the *actual CLOB best ask* on the matching YES/NO side (never the
midpoint). Flags trades when Synth's probability exceeds the executable ask by
at least the edge threshold.

## Install

```bash
cd /Users/andrewok/Desktop/Synthbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r polybot/requirements.txt
cp polybot/.env.example .env
# edit .env and set SYNTH_API_KEY
set -a; source .env; set +a
```

## Run the scanner

```bash
python -m polybot.scanner                 # ranked opportunities
python -m polybot.scanner --show-skipped  # see rejection reasons too
python -m polybot.scanner --execute       # write paper fills to polybot/logs/
python -m polybot.scanner --kelly         # fractional-Kelly sizing
```

## Daily reports

Paper fills are written to `polybot/logs/fills.jsonl`. Daily reports are saved
under `polybot/logs/reports/` as both Markdown and JSON so they can be read
back into chat or processed by another tool.

```bash
python -m polybot.scanner --execute
python -m polybot.scanner --daily-report
python -m polybot.scanner --daily-report 2026-06-22
python -m polybot.scanner --calibration-report
```

Daily reports use `REPORT_TIMEZONE` and group trades by asset with
win/loss/open counts. Win/loss requires a fill row to have `resolved_outcome`
or `realized_pnl`; unresolved paper trades are reported as open.

## Backtest

There are two distinct backtest modes. They are not interchangeable.

### Historical API backtest (`--backtest`)

Calls the Synth API for past windows. Spends Synth API quota:
`windows × assets × horizons`. For BTC/ETH/SOL/HYPE across 15M+1H, 24h is
about 480 calls and 7d is about 3,360 calls. Runs over
`MAX_BACKTEST_CALLS_WITHOUT_CONFIRM` refuse to start unless explicitly
confirmed, and every completed run is saved to
`polybot/logs/last_backtest.txt`.

```bash
python -m polybot.scanner --backtest 2026-06-14T00:00:00Z 2026-06-15T00:00:00Z
CONFIRM_BACKTEST_SPEND=YES python -m polybot.scanner --backtest 2026-06-08T00:00:00Z 2026-06-15T00:00:00Z
```

Sweeps thresholds `{0.10, 0.15, 0.20, 0.25, 0.30}` and ranks by Sharpe proxy.
Synth responses are cached under `polybot/data/synth_cache/`. Useful for
collecting calibration observations and rough threshold sweeps, but does not
replay live Strategy B execution rules.

Backtests only use final labels from `resolved_outcome` / `final_outcome` /
`event_outcome` / `actual_outcome` by default. Set
`ALLOW_CURRENT_OUTCOME_BACKTEST_LABEL=true` only when you have verified that
Synth's `current_outcome` field is a final historical label for the endpoint
being tested.

### Snapshot replay (`--snapshot-backtest`)

Replays the local `polybot/data/snapshots.sqlite3` database that accumulates
during live scanner runs. This is the Strategy B backtest: each stored
timestamp is treated as one atomic scan with the full execution pipeline:
exits evaluated first (including MODEL_REVERSAL), then candidates scored,
ranked, and capped by `MAX_TRADES_PER_SCAN`, then exposure limits enforced
before opening positions. Requires accumulated snapshots from prior live runs.

```bash
python -m polybot.scanner --snapshot-backtest
```

## Calibration

Historical backtests append observations to
`polybot/data/calibration/observations.jsonl`. The signal engine uses those
records to calibrate Synth probabilities per asset and horizon before
calculating edge:

```text
calibrated_edge = calibrated_probability - ask
```

The calibration report saves reliability curves, Brier score, Sharpe, win
rate, and PnL by segment under `polybot/logs/calibration/`.

## Modules

| File | Responsibility |
|---|---|
| `config.py` | env-driven config, defaults, paper-mode lock |
| `synth_client.py` | Synth API → normalized `Opportunity` |
| `polymarket_client.py` | Gamma metadata + real two-sided CLOB best bid/ask + liquidity |
| `clob_enrichment.py` | fetches live CLOB books and enriches opportunities |
| `event_identity.py` | derives stable `event_key` from market metadata |
| `matcher.py` | legacy strict matcher for standalone Polymarket markets |
| `signal_engine.py` | calibrated fair probability, net edge, and EV score per side |
| `risk_manager.py` | spread/liquidity/exposure gates, position sizing |
| `calibration.py` | calibration observations, per-side reliability curves, segment metrics |
| `snapshot_store.py` | writes every scanned opportunity side to `snapshots.sqlite3` |
| `position_manager.py` | durable paper position ledger (event-log JSONL); tracks open/closed positions by `position_id` and `event_key` |
| `exit_rules.py` | Strategy B exit checks: STALE_DATA, EDGE_COLLAPSE, MODEL_REVERSAL, RANK_DECAY, TIME_STOP |
| `execution.py` | paper fills with slippage; links fills to positions via `position_id`; live trading stubbed |
| `reports.py` | local Markdown/JSON daily reports, Strategy B positions/exposure/edge-decay reports |
| `backtester.py` | historical API backtest (threshold sweep) and snapshot replay (Strategy B rules) |
| `dashboard.py` | CLI ranked table of opportunities + rejection reasons |
| `scanner.py` | entrypoint |

## Safety rails

- `PAPER_TRADE_MODE=true` is enforced at every fill; `assert_paper_only()` raises if the flag is unset.
- `place_live_limit_order` raises `NotImplementedError` — live order placement is not implemented.
- Marketable orders are blocked unless `ALLOW_MARKETABLE_ORDERS=true`.
- `REQUIRE_REAL_TWO_SIDED_CLOB=true` (default) rejects opportunities without a real NO-side CLOB.
- `ALLOW_COMPLEMENTARY_BOOK_FALLBACK=false` (default) prevents using `1 - YES_bid` as a synthetic NO ask.
- The position manager blocks duplicate open positions on the same `event_key` before writing any fill.
- Exposure gates enforce `MAX_TOTAL_EXPOSURE`, `MAX_ASSET_EXPOSURE`, `MAX_HORIZON_EXPOSURE`, and `MAX_OPEN_POSITIONS` per scan.
- `MAX_TRADES_PER_SCAN` caps new entries per scan cycle.
- Position cooldown (`POSITION_COOLDOWN_SECONDS`) prevents rapid re-entry after a close.
- All fills and positions are linked by `position_id` for clean traceability across `fills.jsonl` and `positions.jsonl`.

## Notes on Synth endpoints

The Synth endpoint paths in `synth_client.py` are best-guess defaults
(`/v1/polymarket/forecasts`). Adjust the two `SYNTH_*_PATH` constants at the
top of that file if your Synthdata Pro plan exposes them under a different
route. The normalizer accepts several common payload shapes
(`probability` / `p_yes` / `synth_yes_probability`).
