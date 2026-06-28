# Shared Knowledge Base — Polymarket Trading Desk

All four agents share this file as their ground truth. Read it at the start of every session.

---

## Strategy

We run the **late-window convergence strategy** proven by the Synth post:

- **Market:** Polymarket hourly (1H) crypto up/down contracts — BTC and ETH only.
- **Entry window:** Enter only when **fewer than 20 minutes remain** on an hourly contract.
- **Price filter:** Only buy contracts the market already prices at **75¢ or higher**.
- **Edge required:** Synth's probability must exceed the market price by **≥10% raw edge** (≥8% net after fees).
- **Sizing:** Fractional Kelly based on edge. Max 5% of bankroll per trade.
- **Hold:** Hold to resolution. Time stop at 60 seconds before close.

**Why it works:** Participants are reluctant to risk 85¢ to make 15¢ even on favorable bets near expiry.
Synth's forecast identifies which high-prob contracts are still mispriced. Convergence is near-certain in 20 min.

---

## Config Profile

The live config is in `polybot/config.py` under `LATE_WINDOW_PROFILE`.
Run the scanner with: `python -m polybot.scanner --profile late_window`

Key params:
- `max_seconds_to_event_end = 1200` (20 min)
- `min_entry_price = 0.75` (75¢+)
- `min_entry_edge = 0.08` (8% net)
- `min_synth_conviction = 0.82` (Synth must say 82%+)
- `synth_horizons_sec = [3600]` (1H only)
- `execution_mode = taker` (guaranteed fill in short window)

---

## Risk Limits

| Limit | Value |
|---|---|
| Max position size | 5% of bankroll |
| Max total exposure | 25% of bankroll |
| Max per-asset exposure | 10% of bankroll |
| Max open positions | 5 |
| Max trades per scan | 2 |
| Position cooldown | 300 seconds |

**Only trade BTC and ETH.** No other assets.

---

## Log Files (shared ledger)

| File | Written by | Read by |
|---|---|---|
| `polybot/logs/fills.jsonl` | Executor | Risk Manager, Performance Monitor |
| `polybot/logs/positions.jsonl` | Executor | Risk Manager, Position Watcher |
| `polybot/logs/trade_log.jsonl` | Analyst | Performance Monitor |
| `polybot/logs/exit_signals.jsonl` | Position Watcher | Performance Monitor |
| `polybot/data/calibration/observations.jsonl` | All | Analyst |
| `polybot/logs/reports/` | Performance Monitor | Orchestrator |
| `agents/signal_queue.json` | Analyst | Risk Manager |
| `agents/decision_queue.json` | Risk Manager | Executor |

---

## Agent Schedule

| Agent | Trigger | Job |
|---|---|---|
| **Analyst** | Every 6 min (cron) | Scan Synth, find edge, write to signal_queue |
| **Risk Manager** | Every 6 min (triggered by Analyst) + 15 min snapshot | Size + approve/reject signals, write to decision_queue |
| **Executor** | Triggered by Risk Manager approval | Place paper/live order, confirm fill |
| **Performance Monitor** | 7am, 1pm, 10pm daily + Sunday weekly | TCA, win rate, P&L report |
| **Orchestrator** | Event-driven | Route messages, post summaries to user |

---

## Current Status

- **Mode:** Paper trading (`PAPER_TRADE_MODE=true`)
- **Bankroll:** $1,000 (paper)
- **Assets:** BTC, ETH
- **Profile:** `late_window`
- **Live trading:** NOT enabled — requires explicit `ENABLE_LIVE_TRADING=true`
