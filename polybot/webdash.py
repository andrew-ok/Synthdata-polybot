"""Local web dashboard.

`python -m polybot.webdash` → http://localhost:8000

Shows:
- Live opportunities + edge (from a scan on every load)
- Cumulative paper-trade performance from polybot/logs/fills.jsonl
- Trade count, win/loss rate, and the YES (Up) vs NO (Down) split
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template_string

from .clob_enrichment import enrich_real_clob
from .config import CONFIG
from .risk_manager import RiskManager
from .signal_engine import evaluate
from .synth_client import SynthInsightsClient, configured_horizons

log = logging.getLogger(__name__)

app = Flask(__name__)
_LOCK = threading.Lock()


def _load_fills() -> List[Dict[str, Any]]:
    path = os.path.join(CONFIG.log_dir, "fills.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _compute_perf(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(fills)
    if not n:
        return {
            "trades": 0, "wins": 0, "losses": 0, "open": 0,
            "win_rate": 0.0, "realized_pnl": 0.0,
            "yes": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "no":  {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "by_asset": {},
            "by_horizon": {},
        }

    wins = losses = open_ = 0
    pnl_total = 0.0
    by_side = {"YES": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
               "NO":  {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}}
    by_asset: Dict[str, Dict[str, float]] = {}
    by_horizon: Dict[str, Dict[str, float]] = {}

    for f in fills:
        side = "YES" if (f.get("side") or "").upper() == "UP" else "NO"
        outcome = (f.get("resolved_outcome") or "").upper() if f.get("resolved_outcome") else None
        pnl = float(f.get("realized_pnl", 0.0))
        contracts = float(f.get("contracts", 0.0))
        fill_px = float(f.get("fill_price", 0.0))
        notional = float(f.get("notional_usd", contracts * fill_px))

        if outcome in ("UP", "DOWN"):
            # Resolved
            won = (side == "YES" and outcome == "UP") or (side == "NO" and outcome == "DOWN")
            if "realized_pnl" not in f:
                pnl = (contracts * 1.0 - notional) if won else (-notional)
            if won:
                wins += 1
                by_side[side]["wins"] += 1
            else:
                losses += 1
                by_side[side]["losses"] += 1
            pnl_total += pnl
            by_side[side]["pnl"] += pnl
        else:
            open_ += 1
        by_side[side]["trades"] += 1

        a = f.get("asset") or "-"
        by_asset.setdefault(a, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_asset[a]["trades"] += 1
        if outcome in ("UP", "DOWN") and ((side == "YES" and outcome == "UP") or (side == "NO" and outcome == "DOWN")):
            by_asset[a]["wins"] += 1
        by_asset[a]["pnl"] += pnl if outcome else 0.0

        h = f.get("horizon") or "-"
        by_horizon.setdefault(h, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_horizon[h]["trades"] += 1
        if outcome in ("UP", "DOWN") and ((side == "YES" and outcome == "UP") or (side == "NO" and outcome == "DOWN")):
            by_horizon[h]["wins"] += 1
        by_horizon[h]["pnl"] += pnl if outcome else 0.0

    resolved = wins + losses
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "open": open_,
        "win_rate": (wins / resolved) if resolved else 0.0,
        "realized_pnl": round(pnl_total, 2),
        "yes": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in by_side["YES"].items()},
        "no":  {k: (round(v, 2) if isinstance(v, float) else v) for k, v in by_side["NO"].items()},
        "by_asset": {a: {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()} for a, d in by_asset.items()},
        "by_horizon": {h: {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()} for h, d in by_horizon.items()},
    }


def _live_scan() -> Dict[str, Any]:
    client = SynthInsightsClient()
    horizons = configured_horizons()
    opps = client.fetch_all(assets=CONFIG.synth_assets, horizons=horizons)
    opps = enrich_real_clob(opps)
    signals = evaluate(opps)
    risk = RiskManager()
    decisions = risk.evaluate(signals)
    accepted = [d for d in decisions if d.accepted]

    def _row(d):
        s = d.signal
        return {
            "asset": getattr(s, "asset", "-"),
            "horizon": getattr(s, "horizon", "-"),
            "side": "YES" if s.side == "UP" else "NO",
            "synth_p": round(s.synth_probability, 4),
            "calibrated_p": round(getattr(s, "calibrated_probability", s.synth_probability), 4),
            "ask": round(s.execution_price, 4),
            "raw_edge": round(s.raw_edge, 4),
            "calibrated_edge": round(getattr(s, "calibrated_edge", s.raw_edge), 4),
            "net_edge": round(s.net_edge, 4),
            "model_confidence": round(getattr(s, "model_confidence", 0.0), 4),
            "expected_value_score": round(getattr(s, "expected_value_score", s.net_edge), 4),
            "confidence_score": round(getattr(s, "confidence_score", 0.0), 4),
            "liquidity_score": round(getattr(s, "liquidity_score", 0.0), 4),
            "regime_score": round(getattr(s, "regime_score", 1.0), 4),
            "score": round(getattr(s, "score", s.net_edge), 4),
            "spread": s.spread,
            "liquidity": round(s.liquidity, 2),
            "size_usd": round(d.position_size_usd, 2) if d.accepted else None,
            "status": "ACCEPT" if d.accepted else "skip",
            "reason": d.reason,
            "slug": getattr(s, "slug", ""),
            "event_key": getattr(s, "event_key", ""),
            "market_url": getattr(s, "market_url", ""),
        }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n_opps": len(opps),
        "n_signals": len(signals),
        "n_accepted": len(accepted),
        "accepted": [_row(d) for d in decisions if d.accepted],
        "skipped":  [_row(d) for d in decisions if not d.accepted][:50],
    }


@app.route("/api/state")
def api_state():
    with _LOCK:
        try:
            live = _live_scan()
        except Exception as exc:
            log.exception("live scan failed")
            live = {"error": str(exc), "accepted": [], "skipped": []}
        perf = _compute_perf(_load_fills())
        return jsonify({
            "config": {
                "paper_mode": CONFIG.paper_trade_mode,
                "bankroll_usd": CONFIG.bankroll_usd,
                "min_entry_edge": CONFIG.min_entry_edge,
                "min_exit_edge": CONFIG.min_exit_edge,
                "assets": CONFIG.synth_assets,
                "horizons": configured_horizons(),
            },
            "live": live,
            "perf": perf,
        })


_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Polybot — Polymarket × Synth</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg:#0b0d10; --fg:#e8edf2; --muted:#8892a0; --card:#13171c;
    --good:#3ddc97; --bad:#ff6b6b; --warn:#f5d76e; --accent:#6cb6ff;
    --border:#1f2630;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background:var(--bg); color:var(--fg); padding:24px;
  }
  h1 { margin:0 0 6px; font-size:20px; letter-spacing:.5px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:18px; }
  .grid { display:grid; gap:14px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:18px; }
  .card {
    background:var(--card); border:1px solid var(--border);
    border-radius:10px; padding:14px;
  }
  .card .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
  .card .value { font-size:22px; margin-top:6px; font-weight:600; }
  .card .sub { font-size:11px; margin-top:4px; color:var(--muted); }
  .good { color:var(--good); }
  .bad { color:var(--bad); }
  .warn { color:var(--warn); }
  .row2 { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
  @media (max-width: 880px) { .row2 { grid-template-columns: 1fr; } }
  table {
    width:100%; border-collapse: collapse; font-size:12px;
  }
  th, td {
    text-align:left; padding:7px 8px; border-bottom:1px solid var(--border);
    white-space: nowrap; overflow:hidden; text-overflow: ellipsis;
  }
  th { color:var(--muted); font-weight:500; }
  tr:hover td { background: rgba(108,182,255,.05); }
  .pill {
    display:inline-block; padding:2px 6px; border-radius:6px;
    font-size:10px; font-weight:600; letter-spacing:.05em;
  }
  .pill.yes { background:rgba(61,220,151,.15); color:var(--good); }
  .pill.no  { background:rgba(255,107,107,.15); color:var(--bad); }
  .pill.accept { background:rgba(108,182,255,.15); color:var(--accent); }
  .pill.skip { background:rgba(136,146,160,.15); color:var(--muted); }
  h2 { font-size:13px; margin:18px 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; }
  .header-row { display:flex; justify-content:space-between; align-items:baseline; }
  .small { font-size:11px; color:var(--muted); }
  .truncate { max-width:340px; overflow:hidden; text-overflow:ellipsis; }
</style>
</head>
<body>
  <div class="header-row">
    <div>
      <h1>POLYBOT — paper trading</h1>
      <div class="sub" id="meta">loading…</div>
    </div>
    <div class="small" id="asof">—</div>
  </div>

  <div class="grid" id="cards"></div>

  <div class="row2">
    <div class="card">
      <h2 style="margin-top:0">YES (Up) trades</h2>
      <div id="yescell" class="small">no fills yet</div>
    </div>
    <div class="card">
      <h2 style="margin-top:0">NO (Down) trades</h2>
      <div id="nocell" class="small">no fills yet</div>
    </div>
  </div>

  <h2>Accepted opportunities <span id="acceptedCount" class="small"></span></h2>
  <div class="card" style="overflow-x:auto;">
    <table id="acceptedTbl">
      <thead><tr>
        <th>Asset</th><th>Horizon</th><th>Side</th><th>Synth p</th><th>Fair p</th><th>Ask</th>
        <th>Raw edge</th><th>Net edge</th><th>Conf</th><th>Score</th><th>Spread</th><th>Liquidity ($)</th><th>Size ($)</th><th>Event</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <h2>Recent skips <span id="skippedCount" class="small"></span></h2>
  <div class="card" style="overflow-x:auto;">
    <table id="skippedTbl">
      <thead><tr>
        <th>Asset</th><th>Horizon</th><th>Side</th><th>Synth p</th><th>Fair p</th><th>Ask</th>
        <th>Raw edge</th><th>Net edge</th><th>Conf</th><th>Score</th><th>Spread</th><th>Liquidity ($)</th><th>Reason</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
function num(x, d=4) { return (x===null||x===undefined||isNaN(x))?"—":Number(x).toFixed(d); }
function money(x) { return (x===null||x===undefined||isNaN(x))?"—":"$"+Number(x).toFixed(2); }
function pct(x) { return (x===null||x===undefined||isNaN(x))?"—":(100*Number(x)).toFixed(1)+"%"; }

function card(label, value, sub, cls="") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="sub">${sub||""}</div></div>`;
}

function pnlClass(v) { return v>0 ? "good" : (v<0 ? "bad" : ""); }

function renderRows(tbody, rows, includeSize) {
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr><td colspan="13" style="color:var(--muted);text-align:center;padding:18px">no rows</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const side = r.side === "YES"
      ? '<span class="pill yes">YES</span>'
      : '<span class="pill no">NO</span>';
    const lastCol = includeSize
      ? `<td>${money(r.size_usd)}</td><td class="truncate" title="${r.event_key||r.slug||''}">${r.market_url ? `<a href="${r.market_url}" target="_blank" rel="noreferrer">${r.event_key||r.slug||''}</a>` : (r.event_key||r.slug||'')}</td>`
      : `<td class="truncate" title="${r.reason||''}">${r.reason||''}</td>`;
    return `<tr>
      <td>${r.asset||"-"}</td>
      <td>${r.horizon||"-"}</td>
      <td>${side}</td>
      <td>${num(r.synth_p)}</td>
      <td>${num(r.calibrated_p)}</td>
      <td>${num(r.ask)}</td>
      <td>${num(r.raw_edge)}</td>
      <td><b>${num(r.net_edge)}</b></td>
      <td>${num(r.confidence_score,3)}</td>
      <td><b>${num(r.score,4)}</b></td>
      <td>${num(r.spread,3)}</td>
      <td>${money(r.liquidity)}</td>
      ${lastCol}
    </tr>`;
  }).join("");
}

function sideCell(s) {
  if (!s || !s.trades) return "no fills yet";
  const wr = s.trades ? (s.wins / Math.max(1, s.wins+s.losses)) : 0;
  return `
    <div style="display:flex;gap:24px;flex-wrap:wrap">
      <div><div class="label">Trades</div><div class="value">${s.trades}</div></div>
      <div><div class="label">Wins</div><div class="value good">${s.wins}</div></div>
      <div><div class="label">Losses</div><div class="value bad">${s.losses}</div></div>
      <div><div class="label">Win rate</div><div class="value">${pct(wr)}</div></div>
      <div><div class="label">PnL</div><div class="value ${pnlClass(s.pnl)}">${money(s.pnl)}</div></div>
    </div>`;
}

async function refresh() {
  let data;
  try {
    const r = await fetch("/api/state", { cache:"no-store" });
    data = await r.json();
  } catch (e) {
    document.getElementById("meta").textContent = "fetch error: " + e;
    return;
  }
  const cfg = data.config, perf = data.perf, live = data.live;

  document.getElementById("meta").textContent =
    `assets=${cfg.assets.join("/")} · horizons=${cfg.horizons.join(",")} · entry edge≥${cfg.min_entry_edge} · exit edge≥${cfg.min_exit_edge} · bankroll $${cfg.bankroll_usd}` +
    (cfg.paper_mode ? " · PAPER" : " · LIVE");
  document.getElementById("asof").textContent = "as of " + (live.as_of || new Date().toISOString());

  document.getElementById("cards").innerHTML =
    card("Total trades", perf.trades, `${perf.open} open`) +
    card("Realized PnL", money(perf.realized_pnl), null, pnlClass(perf.realized_pnl)) +
    card("Win rate", pct(perf.win_rate), `${perf.wins}W / ${perf.losses}L`) +
    card("Wins", perf.wins, null, "good") +
    card("Losses", perf.losses, null, "bad") +
    card("Live opportunities", live.n_opps||0, `${live.n_signals||0} signals · ${live.n_accepted||0} accepted`);

  document.getElementById("yescell").innerHTML = sideCell(perf.yes);
  document.getElementById("nocell").innerHTML = sideCell(perf.no);

  document.getElementById("acceptedCount").textContent = `(${(live.accepted||[]).length})`;
  document.getElementById("skippedCount").textContent  = `(${(live.skipped||[]).length} shown)`;

  renderRows(document.querySelector("#acceptedTbl tbody"), live.accepted, true);
  renderRows(document.querySelector("#skippedTbl tbody"),  live.skipped,  false);
}

refresh();
setInterval(refresh, 300000);   // 5 min — saves tokens vs a forgotten open tab
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(_HTML)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    host = os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("POLYBOT_DASH_PORT", "8000"))
    log.info("Polybot dashboard → http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
