# OpenClaw MacBook handoff — live-test the seven Strategy C books

You are Claude Code on the OpenClaw MacBook. Your job: run a 48–72 hour live
paper test of seven pre-validated strategies, **exactly as implemented on the
`thinkpadsynthbot` branch**. You are an operator here, not a designer.

## Prime directive: DO NOT MODIFY THE STRATEGIES

Every parameter below was chosen from an 8-week backtest (May 8 – Jul 3 2026,
2,400+ hourly windows) with per-week consistency checks, and several were
learned from live incidents. The entire value of this test is comparing seven
FIXED rulebooks on identical forward data. Specifically:

- Do NOT change any threshold, band, gate, or sizing rule.
- Do NOT add, remove, or "improve" strategies, even if one looks broken or
  keeps losing. Losing weeks are expected (see stats below); that is data.
- Do NOT introduce a calibration layer. The strategies use RAW Synth
  probabilities by design (`Calibrator(observations=[])` in strategy_c.py).
  A polluted calibration DB once silently zeroed all trading for 2 days —
  the live DB must contain only live_paper observations, nothing else.
- Do NOT run historical backtests here (they burn the shared Synth quota).
- PAPER ONLY: `PAPER_TRADE_MODE=true`. Never implement or enable live orders.
- If you find an actual BUG (crash, missed settlement, wrong ledger), fix the
  bug without touching strategy semantics, commit it clearly labeled, and note
  it in your report.

## What the system is

Polymarket runs hourly BTC/ETH up/down markets. Synth (api.synthdata.co)
publishes a probability for each. All seven books trade one underlying idea:
**late in an hourly window (last ~20 min), buy the FAVORITE side (priced
0.70–0.92) — high-probability contracts carry a risk premium because traders
dislike risking ~85c to win ~15c.** Synth's role differs per book: sometimes
a trigger (edge threshold), sometimes only a veto. Positions are held to
resolution; settlement derives outcomes from consecutive window start prices
(validated 100% vs 203 official Polymarket Gamma resolutions).

The scanner runs every 15 minutes; only the **:47 scan** falls inside the
late-window entry zone, so entries happen once per hour per asset at most.
The **:02/:17/:32 scans still matter**: they settle positions, and the :02
scan records each hour's early price move (`polybot/logs/early_moves.json`)
which the HVOL book needs at :47. Never "optimize" the cadence down to one
scan per hour — that silently breaks HVOL and delays settlement.

## Shared candidate gates (polybot/strategy_c.py, evaluate_c)

A market becomes a candidate only if ALL hold:
- 1H horizon; wall-clock time to window end in [120s, 1200s] (measured on OUR
  clock — payload clocks have been observed minutes stale)
- favorite side ask in [0.70, 0.92]
- edge = (raw Synth probability for that side) − ask ≥ −0.02
- book liquidity ≥ $200 (real depth within 2c of ask, from CLOB enrichment)
- Synth forecast age ≤ 900s
- one position per market window per book; correlation dedupe per scan

Each book then applies its own rule. price = favorite ask, edge as above.

## The seven books — rules, mechanisms, expectations

Ledgers live in `polybot/logs/fills_C_*.jsonl`. 8-week stats use $37.50 stakes.

1. **C_RAMP** (`fills_C_ramp.jsonl`) — take if edge ≥ 0.015 + 0.22×(price −
   0.70) (≈1.5pp at 70c → ≈5.9pp at 90c). Mechanism: the reported-edge needed
   for +EV rises with price. Kelly-sized (quarter-Kelly, 5% cap, ×0.75
   cold-start confidence → ~$37.50). 8wk: 269 trades, 84% win, +$522, 5/8
   weeks positive, maxDD ≈ −$244.
2. **C_B7075** (`fills_C_b7075.jsonl`) — price in [0.70, 0.75), edge ≥ 0.015.
   The single most consistent price bucket (6/8 weeks, +0.07/$1). Kelly.
3. **C_B8085** (`fills_C_b8085.jsonl`) — price in [0.80, 0.85), edge ≥ 0.03.
   The highest-win-rate bucket (90%, 6/8 weeks). Kelly.
4. **C_VETO** (`fills_C_veto.jsonl`) — take EVERY candidate (edge ≥ −0.02 is
   the only edge condition). Mechanism: pure favorite-buying was −$125 over 8
   weeks; adding only "skip when Synth disagrees by >2pp" made +$775/491
   trades. Synth as VETO, not trigger. **FLAT-sized $37.50** — Kelly would
   zero out negative-edge entries the mechanism intends to take (this was a
   real bug once; do not "fix" it back).
5. **C_RAMPP** (`fills_C_rampp.jsonl`) — RAMP entries + stand-down: no new
   entries for 24h after 3 resolved losses within 24h (losses cluster in
   regimes). 8wk +$590, and it flipped RAMP's worst week positive.
6. **C_VETOP** (`fills_C_vetop.jsonl`) — VETO entries + the same stand-down.
   **Best strategy found: +$854, +0.06/$1, 6/8 weeks.** FLAT-sized.
7. **C_HVOL** (`fills_C_hvol.jsonl`) — VETO entries only in hours whose early
   move (recorded ~2.5 min in) exceeded 0.10% in absolute value. Mechanism:
   intraday-momentum literature — predictability concentrates in high-vol
   windows. **Best consistency (7/8 weeks) and half the drawdown (−$151).**
   FLAT-sized. Skips trades when early state is missing (e.g. scanner was
   down at :02) — that is correct behavior, not a bug.

Expected volume: VETO-family ~7/day combined across BTC+ETH in normal
conditions; RAMP-family ~3–4/day; quiet chop days can be zero for all. ~85%
of trades will be DOWN-side favorites (Synth runs bearish); that asymmetry is
known and expected.

## Setup (macOS)

```bash
git clone https://github.com/andrew-ok/Synthdata-polybot
cd Synthdata-polybot && git checkout thinkpadsynthbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r polybot/requirements.txt
```

Create `.env` in the repo root (config.py auto-loads it; ask the user for the
SYNTH_API_KEY — do not invent one):

```
SYNTH_API_KEY=<ask the user>
PAPER_TRADE_MODE=true
BANKROLL_USD=1000
MAX_POSITION_SIZE=0.05
MAX_TOTAL_EXPOSURE=0.25
KELLY_FRACTION=0.25
MAX_KELLY_FRACTION=0.25
SYNTH_ASSETS=BTC,ETH
SYNTH_HORIZONS_SEC=900,3600
MIN_LIQUIDITY=200
MODEL_CONFIDENCE_FLOOR=0.75
REPORT_TIMEZONE=America/New_York
```

Scheduling (cron, aligned like the source machine — :02/:17/:32/:47):

```bash
crontab -e
# add:
2,17,32,47 * * * * cd $HOME/Synthdata-polybot && ./.venv/bin/python -m polybot.scanner --execute --kelly >> polybot/logs/scan_loop.log 2>&1
```

Keep the Mac awake for the whole test: `caffeinate -dims &` (or Amphetamine /
plugged in with sleep disabled). A sleeping machine = silent data gaps; this
killed multiple test windows on the source machine.

## Verify BEFORE declaring the test started (all must pass)

1. `python -c "import polybot.scanner, polybot.strategy_c"` — clean.
2. `python -c "from polybot.config import CONFIG; print(CONFIG.synth_api_key[:6])"`
   — key loads from .env.
3. One manual scan: `python -m polybot.scanner --execute --kelly` — expect
   "Opportunities: 4", no Traceback, "C variants" line present.
4. `python -c "from polybot.calibration import Calibrator; c=Calibrator(); print(c.calibrate('BTC','1H',0.92))"`
   → must print **0.92** (raw passthrough; if not, the calibration DB is
   polluted — stop and investigate, do not proceed).
5. Confirm cron fired twice in a row (`tail polybot/logs/scan_loop.log`), then
   record the official test start time.

## During the test

- Fills/settlements appear in the ledgers; every REJECTED candidate logs its
  reason to `polybot/logs/skipped_C.jsonl` — silence is diagnosable, use it.
- Report: `python -m polybot.ab_report` (all seven books side by side).
- Budget: the Synth key is SHARED with other machines. This cadence costs
  ~384 calls/day. Do not add extra scans, backtests, or probes.
- If scans stop (log mtime stale >30 min): check cron, check network, check
  `caffeinate`. Fix and note the gap — do not quietly restart the clock.

## Report back (end of test)

For EACH book: fills, settled W/L, win rate, avg return/$1, cum P&L, and any
gap periods. Plus: skipped_C reason counts, any errors, any code fixes made.
The comparison question this test answers: do VETOP's and HVOL's backtest
advantages survive live forward data?
