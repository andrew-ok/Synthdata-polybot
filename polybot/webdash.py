"""Local web dashboard — Strategy B.

`python -m polybot.webdash` → http://localhost:8000

Panels:
- Summary cards (fills, open positions, realized PnL, win rate, exposure, live signals)
- Exposure gauges (total, per-asset, per-horizon vs configured limits)
- Cumulative PnL sparkline from closed positions
- Open positions table
- Exit reason breakdown
- Recent closed positions (last 20)
- Live opportunity scan (cached 60 s) — accepted and skipped
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template_string

from .clob_enrichment import enrich_real_clob
from .config import CONFIG
from .position_manager import exposure_summary, load_positions
from .risk_manager import RiskManager
from .signal_engine import evaluate
from .synth_client import SynthInsightsClient, configured_horizons

log = logging.getLogger(__name__)

app = Flask(__name__)
_LOCK = threading.Lock()
_SCAN_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_SCAN_TTL = 60.0


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_fills() -> List[Dict[str, Any]]:
    path = os.path.join(CONFIG.log_dir, "fills.jsonl")
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
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
    if not fills:
        return {
            "trades": 0, "wins": 0, "losses": 0, "open": 0,
            "win_rate": 0.0, "realized_pnl": 0.0,
            "by_asset": {}, "by_horizon": {},
        }
    wins = losses = open_ = 0
    pnl_total = 0.0
    by_asset: Dict[str, Dict[str, Any]] = {}
    by_horizon: Dict[str, Dict[str, Any]] = {}
    for f in fills:
        side = "UP" if (f.get("side") or "").upper() == "UP" else "DOWN"
        outcome = (f.get("resolved_outcome") or "").upper() if f.get("resolved_outcome") else None
        pnl = float(f.get("realized_pnl") or 0.0)
        contracts = float(f.get("contracts") or 0.0)
        fill_px = float(f.get("fill_price") or 0.0)
        notional = float(f.get("notional_usd") or contracts * fill_px)
        if outcome in ("UP", "DOWN"):
            won = side == outcome
            if "realized_pnl" not in f:
                pnl = (contracts - notional) if won else -notional
            (wins if won else losses).__add__(0)  # count below
            if won:
                wins += 1
            else:
                losses += 1
            pnl_total += pnl
        else:
            open_ += 1
        a = f.get("asset") or "-"
        by_asset.setdefault(a, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_asset[a]["trades"] += 1
        if outcome in ("UP", "DOWN") and side == outcome:
            by_asset[a]["wins"] += 1
        by_asset[a]["pnl"] += pnl if outcome else 0.0
        h = f.get("horizon") or "-"
        by_horizon.setdefault(h, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_horizon[h]["trades"] += 1
        if outcome in ("UP", "DOWN") and side == outcome:
            by_horizon[h]["wins"] += 1
        by_horizon[h]["pnl"] += pnl if outcome else 0.0
    resolved = wins + losses
    return {
        "trades": len(fills),
        "wins": wins,
        "losses": losses,
        "open": open_,
        "win_rate": wins / resolved if resolved else 0.0,
        "realized_pnl": round(pnl_total, 2),
        "by_asset": {a: {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()} for a, d in by_asset.items()},
        "by_horizon": {h: {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()} for h, d in by_horizon.items()},
    }


def _positions_data() -> Dict[str, Any]:
    positions = load_positions()
    now = datetime.now(timezone.utc)
    open_pos = sorted([p for p in positions if p.status == "open"], key=lambda p: p.entry_time, reverse=True)
    closed = sorted([p for p in positions if p.status == "closed"], key=lambda p: p.exit_time or "", reverse=True)

    def _hold_sec(p) -> int:
        try:
            if p.exit_time:
                a = datetime.fromisoformat(p.entry_time)
                b = datetime.fromisoformat(p.exit_time)
                a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
                b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
                return max(0, int((b - a).total_seconds()))
            t = datetime.fromisoformat(p.entry_time)
            t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
            return max(0, int((now - t).total_seconds()))
        except Exception:
            return 0

    def _fmt(p) -> Dict[str, Any]:
        return {
            "pid": p.position_id[:8],
            "event_key": p.event_key,
            "asset": p.asset,
            "horizon": p.horizon,
            "side": p.side,
            "entry_price": p.entry_price,
            "notional_usd": p.notional_usd,
            "entry_edge": round(p.entry_edge, 4),
            "entry_score": round(p.entry_score, 4),
            "latest_score": round(p.latest_score, 4),
            "hold_sec": _hold_sec(p),
            "exit_reason": p.exit_reason,
            "realized_pnl": round(float(p.realized_pnl or 0.0), 4),
        }

    closed_asc = sorted(closed, key=lambda p: p.exit_time or "")
    pnl_curve, running = [], 0.0
    for p in closed_asc:
        running += float(p.realized_pnl or 0.0)
        pnl_curve.append({"t": p.exit_time, "v": round(running, 4)})

    exit_reasons: Dict[str, int] = {}
    for p in closed:
        r = p.exit_reason or "-"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    exp = exposure_summary()
    return {
        "open": [_fmt(p) for p in open_pos],
        "closed_recent": [_fmt(p) for p in closed[:20]],
        "exposure": {
            "total": round(exp["total"], 2),
            "count": exp["count"],
            "max_total": round(CONFIG.bankroll_usd * CONFIG.max_total_exposure, 2),
            "by_asset": {a: round(v, 2) for a, v in exp["by_asset"].items()},
            "max_asset": round(CONFIG.bankroll_usd * CONFIG.max_asset_exposure, 2),
            "by_horizon": {h: round(v, 2) for h, v in exp["by_horizon"].items()},
            "max_horizon": round(CONFIG.bankroll_usd * CONFIG.max_horizon_exposure, 2),
        },
        "exit_reasons": exit_reasons,
        "pnl_curve": pnl_curve,
    }


def _orders_data() -> Dict[str, Any]:
    from .order_manager import load_orders
    orders = load_orders()
    pending = [o for o in orders if o.status == "PENDING"]
    filled  = [o for o in orders if o.status == "FILLED"]
    cancelled = [o for o in orders if o.status == "CANCELLED"]
    cancel_reasons: Dict[str, int] = {}
    for o in cancelled:
        k = o.cancel_reason or "-"
        cancel_reasons[k] = cancel_reasons.get(k, 0) + 1

    def _fmt_order(o) -> Dict[str, Any]:
        from dataclasses import asdict
        d = asdict(o)
        return d

    return {
        "pending": [_fmt_order(o) for o in pending],
        "filled": [_fmt_order(o) for o in filled],
        "cancelled_recent": [_fmt_order(o) for o in cancelled[-20:]],
        "counts": {
            "pending": len(pending),
            "filled": len(filled),
            "cancelled": len(cancelled),
        },
        "cancel_reasons": cancel_reasons,
    }


def _snapshot_stats() -> Dict[str, Any]:
    if not os.path.exists(CONFIG.snapshot_db_path):
        return {"total": 0, "by_side": {}}
    try:
        with sqlite3.connect(CONFIG.snapshot_db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            by_side = dict(conn.execute("SELECT side, COUNT(*) FROM snapshots GROUP BY side").fetchall())
        return {"total": total, "by_side": by_side}
    except Exception:
        return {"total": 0, "by_side": {}}


def _live_scan() -> Dict[str, Any]:
    client = SynthInsightsClient()
    opps = client.fetch_all(assets=CONFIG.synth_assets, horizons=configured_horizons())
    opps = enrich_real_clob(opps)
    signals = evaluate(opps)
    decisions = RiskManager().evaluate(signals)

    def _row(d) -> Dict[str, Any]:
        s = d.signal
        return {
            "asset": s.asset,
            "horizon": s.horizon,
            "side": "YES" if s.side == "UP" else "NO",
            "synth_p": round(s.synth_probability, 4),
            "fair_p": round(getattr(s, "calibrated_probability", s.synth_probability), 4),
            "ask": round(s.execution_price, 4),
            "raw_edge": round(s.raw_edge, 4),
            "net_edge": round(s.net_edge, 4),
            "conf": round(getattr(s, "confidence_score", 0.0), 3),
            "score": round(getattr(s, "score", s.net_edge), 4),
            "spread": s.spread,
            "liquidity": round(s.liquidity, 2),
            "size_usd": round(d.position_size_usd, 2) if d.accepted else None,
            "status": "ACCEPT" if d.accepted else "skip",
            "reason": d.reason,
            "event_key": getattr(s, "event_key", ""),
            "market_url": getattr(s, "market_url", ""),
        }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n_opps": len(opps),
        "n_signals": len(signals),
        "n_accepted": sum(1 for d in decisions if d.accepted),
        "accepted": [_row(d) for d in decisions if d.accepted],
        "skipped":  [_row(d) for d in decisions if not d.accepted][:50],
        "cached": False,
    }


def _cached_scan() -> Dict[str, Any]:
    now = time.time()
    with _LOCK:
        cached = _SCAN_CACHE["data"]
        if cached is not None and now - _SCAN_CACHE["ts"] < _SCAN_TTL:
            return {**cached, "cached": True}
    try:
        data = _live_scan()
    except Exception as exc:
        log.exception("live scan failed")
        data = {
            "error": str(exc), "accepted": [], "skipped": [],
            "as_of": datetime.now(timezone.utc).isoformat(),
            "n_opps": 0, "n_signals": 0, "n_accepted": 0, "cached": False,
        }
    with _LOCK:
        _SCAN_CACHE["data"] = data
        _SCAN_CACHE["ts"] = now
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/state")
def api_state():
    return jsonify({
        "config": {
            "paper_mode": CONFIG.paper_trade_mode,
            "bankroll_usd": CONFIG.bankroll_usd,
            "min_entry_edge": CONFIG.min_entry_edge,
            "min_exit_edge": CONFIG.min_exit_edge,
            "max_open_positions": CONFIG.max_open_positions,
            "assets": CONFIG.synth_assets,
            "horizons": configured_horizons(),
        },
        "live": _cached_scan(),
        "perf": _compute_perf(_load_fills()),
        "positions": _positions_data(),
        "snapshots": _snapshot_stats(),
        "orders": _orders_data(),
        "paper_safe": {
            "paper_trade_mode": CONFIG.paper_trade_mode,
            "enable_live_trading": CONFIG.enable_live_trading,
            "live_trading_enabled": False,
            "simulated": True,
            "real_orders_enabled": False,
        },
    })


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Polybot — Strategy B</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
:root {
  --bg:#0b0d10; --fg:#e8edf2; --muted:#8892a0; --card:#13171c;
  --good:#3ddc97; --bad:#ff6b6b; --warn:#f5d76e; --accent:#6cb6ff;
  --border:#1f2630; --up:#6cb6ff; --down:#c084fc;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--bg);color:var(--fg);padding:20px 24px}
h1{font-size:18px;letter-spacing:.5px;margin-bottom:4px}
.sub{color:var(--muted);font-size:11px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:20px 0 8px}
.header-row{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px;flex-wrap:wrap;gap:8px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.08em;margin-left:8px}
.badge.paper{background:rgba(61,220,151,.18);color:var(--good)}
.badge.live{background:rgba(255,107,107,.18);color:var(--bad)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:4px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.value{font-size:22px;font-weight:700;margin-top:5px}
.value.sm{font-size:16px}
.value.good{color:var(--good)}
.value.bad{color:var(--bad)}
.value.warn{color:var(--warn)}
.card-sub{font-size:10px;color:var(--muted);margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
th{color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase}
tr:hover td{background:rgba(108,182,255,.04)}
.pill{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;font-weight:700}
.pill.yes,.pill.up{background:rgba(108,182,255,.15);color:var(--up)}
.pill.no,.pill.down{background:rgba(192,132,252,.15);color:var(--down)}
.pill.accept{background:rgba(61,220,151,.15);color:var(--good)}
.pill.skip{background:rgba(136,146,160,.12);color:var(--muted)}
.pill.open{background:rgba(108,182,255,.12);color:var(--accent)}
.pill.closed{background:rgba(136,146,160,.12);color:var(--muted)}
.reason-pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;margin:3px 4px 3px 0}
.reason-EDGE_COLLAPSE{background:rgba(255,107,107,.12);color:var(--bad)}
.reason-MODEL_REVERSAL{background:rgba(245,215,110,.12);color:var(--warn)}
.reason-TIME_STOP{background:rgba(108,182,255,.12);color:var(--accent)}
.reason-STALE_DATA{background:rgba(136,146,160,.12);color:var(--muted)}
.reason-RESOLVED{background:rgba(61,220,151,.12);color:var(--good)}
.reason-RANK_DECAY{background:rgba(192,132,252,.12);color:var(--down)}
.reason-other{background:rgba(136,146,160,.08);color:var(--muted)}
/* exposure bars */
.exp-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.exp-label{font-size:10px;color:var(--muted);min-width:80px}
.exp-bar-wrap{flex:1;min-width:120px;max-width:300px;background:var(--border);border-radius:4px;height:8px;overflow:hidden}
.exp-bar{height:100%;border-radius:4px;transition:width .3s}
.exp-bar.low{background:var(--good)}
.exp-bar.mid{background:var(--warn)}
.exp-bar.high{background:var(--bad)}
.exp-val{font-size:10px;color:var(--muted);white-space:nowrap}
/* pnl chart */
#pnl-svg{display:block;width:100%;height:80px}
/* refresh */
.refresh-info{font-size:10px;color:var(--muted);text-align:right}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.empty{color:var(--muted);text-align:center;padding:18px;font-size:12px}
</style>
</head>
<body>

<div id="safety-banner" style="background:rgba(61,220,151,.1);border:1px solid rgba(61,220,151,.3);border-radius:8px;padding:8px 16px;margin-bottom:14px;font-size:11px;display:flex;align-items:center;gap:12px;">
  <span style="color:var(--good);font-weight:700;font-size:13px;">✓ PAPER MODE</span>
  <span style="color:var(--muted)">LIVE ORDERS DISABLED &nbsp;·&nbsp; live_trading_enabled=false &nbsp;·&nbsp; simulated=true &nbsp;·&nbsp; real_orders_enabled=false</span>
</div>
<div class="header-row">
  <div>
    <h1>POLYBOT <span id="mode-badge"></span></h1>
    <div class="sub" id="meta">loading…</div>
  </div>
  <div class="refresh-info" id="refresh-info">—</div>
</div>

<div class="grid" id="cards"></div>

<h2>Exposure</h2>
<div class="card" id="exposure-panel"><div class="empty">no open positions</div></div>

<h2>Cumulative PnL</h2>
<div class="card"><svg id="pnl-svg"></svg></div>

<h2>Open positions <span id="open-count" class="sub"></span></h2>
<div class="card" style="overflow-x:auto">
  <table id="open-tbl">
    <thead><tr>
      <th>Asset</th><th>Horizon</th><th>Side</th><th>Entry $</th><th>Notional</th>
      <th>Entry edge</th><th>Latest score</th><th>Hold</th><th>Event</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>Exit reasons</h2>
<div class="card" id="exit-reasons"><div class="empty">no closed positions yet</div></div>

<h2>Pending Paper Orders <span id="pending-count" class="sub"></span></h2>
<div class="card" style="overflow-x:auto">
  <table id="pending-tbl">
    <thead><tr>
      <th>ID</th><th>Asset</th><th>Hz</th><th>Side</th><th>Limit $</th><th>Size $</th>
      <th>Edge@order</th><th>Created</th><th>T-to-res</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>Order Lifecycle <span id="order-stats" class="sub"></span></h2>
<div class="card" id="order-lifecycle-panel"><div class="empty">no orders yet</div></div>

<h2>Recent closed <span id="closed-count" class="sub"></span></h2>
<div class="card" style="overflow-x:auto">
  <table id="closed-tbl">
    <thead><tr>
      <th>Asset</th><th>Horizon</th><th>Side</th><th>Entry $</th><th>Exit reason</th>
      <th>PnL</th><th>Hold</th><th>Event</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>Accepted opportunities <span id="accepted-count" class="sub"></span></h2>
<div class="card" style="overflow-x:auto">
  <table id="accepted-tbl">
    <thead><tr>
      <th>Asset</th><th>Horizon</th><th>Side</th><th>Synth p</th><th>Fair p</th><th>Ask</th>
      <th>Net edge</th><th>Conf</th><th>Score</th><th>Liq $</th><th>Size $</th><th>Event</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>Skipped <span id="skipped-count" class="sub"></span></h2>
<div class="card" style="overflow-x:auto">
  <table id="skipped-tbl">
    <thead><tr>
      <th>Asset</th><th>Horizon</th><th>Side</th><th>Synth p</th><th>Fair p</th><th>Ask</th>
      <th>Net edge</th><th>Conf</th><th>Score</th><th>Liq $</th><th>Reason</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
// ── helpers ────────────────────────────────────────────────────────────────
const n4   = x => (x==null||isNaN(x)) ? "—" : Number(x).toFixed(4);
const n2   = x => (x==null||isNaN(x)) ? "—" : Number(x).toFixed(2);
const pct  = x => (x==null||isNaN(x)) ? "—" : (100*Number(x)).toFixed(1)+"%";
const money= x => (x==null||isNaN(x)) ? "—" : "$"+Number(x).toFixed(2);
const pcls = v => v>0?"good":v<0?"bad":"";
const sidePill = s => {
  const upper=(s||"").toUpperCase();
  if(upper==="YES"||upper==="UP")   return '<span class="pill yes">YES</span>';
  if(upper==="NO" ||upper==="DOWN") return '<span class="pill no">NO</span>';
  return s;
};
const hms = sec => {
  if(!sec) return "—";
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  return h?`${h}h ${m}m`:(m?`${m}m ${s}s`:`${s}s`);
};
const ek = (key,url) => {
  const short = (key||"").slice(0,18)+"…";
  return url ? `<a href="${url}" target="_blank" rel="noreferrer" title="${key}">${short}</a>`
             : `<span title="${key}">${short}</span>`;
};
const noRows = cols => `<tr><td colspan="${cols}" class="empty">—</td></tr>`;

// ── cards ─────────────────────────────────────────────────────────────────
function card(label,value,sub,cls="") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="card-sub">${sub||""}</div></div>`;
}

function renderCards(cfg, perf, live, pos) {
  const expPct = pos.exposure.max_total>0 ? pos.exposure.total/pos.exposure.max_total : 0;
  const expCls = expPct>0.9?"bad":expPct>0.6?"warn":"";
  document.getElementById("cards").innerHTML =
    card("Fills", perf.trades, `${perf.open} open · ${perf.wins}W/${perf.losses}L`) +
    card("Open positions", pos.open.length, `max ${cfg.max_open_positions}`) +
    card("Realized PnL", money(perf.realized_pnl), `win rate ${pct(perf.win_rate)}`, pcls(perf.realized_pnl)) +
    card("Total exposure", money(pos.exposure.total), `of ${money(pos.exposure.max_total)} (${pct(expPct)})`, expCls) +
    card("Snapshots", live.n_opps||0, `${live.n_signals||0} signals · ${live.n_accepted||0} accepted`) +
    card("DB snapshots", (window._snaps||{}).total||0, `UP ${((window._snaps||{}).by_side||{}).UP||0} · DOWN ${((window._snaps||{}).by_side||{}).DOWN||0}`);
}

// ── exposure bars ─────────────────────────────────────────────────────────
function expBar(label, val, max) {
  if(!max) return "";
  const pct_raw = Math.min(1, val/max);
  const cls = pct_raw>0.9?"high":pct_raw>0.6?"mid":"low";
  return `<div class="exp-row">
    <div class="exp-label">${label}</div>
    <div class="exp-bar-wrap"><div class="exp-bar ${cls}" style="width:${(pct_raw*100).toFixed(1)}%"></div></div>
    <div class="exp-val">${money(val)} / ${money(max)} (${(pct_raw*100).toFixed(0)}%)</div>
  </div>`;
}

function renderExposure(exp) {
  const panel = document.getElementById("exposure-panel");
  if(!exp.total && !Object.keys(exp.by_asset).length) {
    panel.innerHTML = '<div class="empty">no open positions</div>';
    return;
  }
  let html = expBar("Total", exp.total, exp.max_total);
  for(const [a,v] of Object.entries(exp.by_asset))
    html += expBar(a, v, exp.max_asset);
  for(const [h,v] of Object.entries(exp.by_horizon))
    html += expBar(h, v, exp.max_horizon);
  panel.innerHTML = html;
}

// ── PnL sparkline ─────────────────────────────────────────────────────────
function renderPnlChart(curve) {
  const svg = document.getElementById("pnl-svg");
  if(!curve || curve.length<2) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="var(--muted)" font-size="11" font-family="monospace">no closed positions yet</text>';
    return;
  }
  const W = svg.getBoundingClientRect().width || 600, H = 80;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const vals = curve.map(p=>p.v);
  const minV = Math.min(0,...vals), maxV = Math.max(0,...vals);
  const range = maxV-minV || 0.01;
  const xS = W/(curve.length-1);
  const yS = (H-16)/range;
  const pts = curve.map((p,i)=>`${(i*xS).toFixed(1)},${(H-8-(p.v-minV)*yS).toFixed(1)}`).join(" ");
  const zero = (H-8-(0-minV)*yS).toFixed(1);
  const last = vals[vals.length-1]||0;
  const stroke = last>=0?"var(--good)":"var(--bad)";
  const lastX=(W).toFixed(0), lastY=(H-8-(last-minV)*yS).toFixed(1);
  svg.innerHTML = `
    <line x1="0" y1="${zero}" x2="${W}" y2="${zero}" stroke="var(--border)" stroke-width="1" stroke-dasharray="4,3"/>
    <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${lastX}" cy="${lastY}" r="3" fill="${stroke}"/>
    <text x="${W-4}" y="${Math.max(12,Math.min(H-4,Number(lastY)-6))}" text-anchor="end" fill="${stroke}" font-size="10" font-family="monospace">${money(last)}</text>`;
}

// ── open positions ────────────────────────────────────────────────────────
function renderOpen(rows) {
  document.getElementById("open-count").textContent = `(${rows.length})`;
  const tb = document.querySelector("#open-tbl tbody");
  if(!rows.length){tb.innerHTML=noRows(9);return;}
  tb.innerHTML = rows.map(r=>`<tr>
    <td><b>${r.asset}</b></td>
    <td>${r.horizon}</td>
    <td>${sidePill(r.side)}</td>
    <td>${n4(r.entry_price)}</td>
    <td>${money(r.notional_usd)}</td>
    <td class="${r.entry_edge>0.1?"good":""}">${n4(r.entry_edge)}</td>
    <td>${n4(r.latest_score)}</td>
    <td>${hms(r.hold_sec)}</td>
    <td>${ek(r.event_key,"")}</td>
  </tr>`).join("");
}

// ── exit reasons ──────────────────────────────────────────────────────────
function renderExitReasons(reasons) {
  const el = document.getElementById("exit-reasons");
  const entries = Object.entries(reasons).sort((a,b)=>b[1]-a[1]);
  if(!entries.length){el.innerHTML='<div class="empty">no closed positions yet</div>';return;}
  el.innerHTML = entries.map(([r,c])=>{
    const known=["EDGE_COLLAPSE","MODEL_REVERSAL","TIME_STOP","STALE_DATA","RESOLVED","RANK_DECAY"];
    const cls=known.includes(r)?`reason-${r}`:"reason-other";
    return `<span class="reason-pill ${cls}">${r} <b>${c}</b></span>`;
  }).join("");
}

// ── closed positions ──────────────────────────────────────────────────────
function renderClosed(rows) {
  document.getElementById("closed-count").textContent = `(${rows.length} shown)`;
  const tb = document.querySelector("#closed-tbl tbody");
  if(!rows.length){tb.innerHTML=noRows(8);return;}
  tb.innerHTML = rows.map(r=>`<tr>
    <td><b>${r.asset}</b></td>
    <td>${r.horizon}</td>
    <td>${sidePill(r.side)}</td>
    <td>${n4(r.entry_price)}</td>
    <td><span class="reason-pill ${["EDGE_COLLAPSE","MODEL_REVERSAL","TIME_STOP","STALE_DATA","RESOLVED","RANK_DECAY"].includes(r.exit_reason)?"reason-"+r.exit_reason:"reason-other"}">${r.exit_reason||"—"}</span></td>
    <td class="${pcls(r.realized_pnl)}">${money(r.realized_pnl)}</td>
    <td>${hms(r.hold_sec)}</td>
    <td>${ek(r.event_key,"")}</td>
  </tr>`).join("");
}

// ── scan tables ───────────────────────────────────────────────────────────
function renderScan(accepted, skipped) {
  document.getElementById("accepted-count").textContent = `(${accepted.length})`;
  document.getElementById("skipped-count").textContent  = `(${skipped.length} shown)`;

  const atb = document.querySelector("#accepted-tbl tbody");
  const stb = document.querySelector("#skipped-tbl tbody");

  if(!accepted.length){atb.innerHTML=noRows(12);}
  else atb.innerHTML = accepted.map(r=>`<tr>
    <td><b>${r.asset}</b></td><td>${r.horizon}</td><td>${sidePill(r.side)}</td>
    <td>${n4(r.synth_p)}</td><td>${n4(r.fair_p)}</td><td>${n4(r.ask)}</td>
    <td class="good"><b>${n4(r.net_edge)}</b></td><td>${n4(r.conf)}</td><td><b>${n4(r.score)}</b></td>
    <td>${money(r.liquidity)}</td><td>${money(r.size_usd)}</td>
    <td>${ek(r.event_key,r.market_url)}</td>
  </tr>`).join("");

  if(!skipped.length){stb.innerHTML=noRows(11);}
  else stb.innerHTML = skipped.map(r=>`<tr>
    <td><b>${r.asset}</b></td><td>${r.horizon}</td><td>${sidePill(r.side)}</td>
    <td>${n4(r.synth_p)}</td><td>${n4(r.fair_p)}</td><td>${n4(r.ask)}</td>
    <td>${n4(r.net_edge)}</td><td>${n4(r.conf)}</td><td>${n4(r.score)}</td>
    <td>${money(r.liquidity)}</td>
    <td style="color:var(--muted)">${r.reason||""}</td>
  </tr>`).join("");
}

// ── orders ────────────────────────────────────────────────────────────────
function renderOrders(orders) {
  const counts = orders.counts || {};
  document.getElementById("pending-count").textContent = `(${counts.pending||0})`;
  document.getElementById("order-stats").textContent =
    `${counts.filled||0} filled · ${counts.cancelled||0} cancelled`;

  const tb = document.querySelector("#pending-tbl tbody");
  const pending = orders.pending || [];
  if(!pending.length){tb.innerHTML=noRows(9);} else {
    tb.innerHTML = pending.map(o=>`<tr>
      <td style="font-family:monospace;font-size:10px">${(o.order_id||"").slice(0,8)}</td>
      <td><b>${o.asset}</b></td><td>${o.horizon}</td>
      <td>${sidePill(o.side)}</td>
      <td>${n4(o.limit_price)}</td>
      <td>${money(o.size_usd)}</td>
      <td class="${o.edge_at_order>0.1?"good":""}">${n4(o.edge_at_order)}</td>
      <td style="font-size:10px">${(o.created_at||"").slice(0,19).replace("T"," ")}</td>
      <td>${o.time_to_resolution_at_order!=null?Math.round(o.time_to_resolution_at_order)+"s":"—"}</td>
    </tr>`).join("");
  }

  const panel = document.getElementById("order-lifecycle-panel");
  const reasons = orders.cancel_reasons || {};
  const rEntries = Object.entries(reasons).sort((a,b)=>b[1]-a[1]);
  if(!rEntries.length && !counts.filled && !counts.cancelled){
    panel.innerHTML='<div class="empty">no order events yet</div>';
  } else {
    const reasonHtml = rEntries.map(([r,c])=>
      `<span class="reason-pill reason-other">${r} <b>${c}</b></span>`
    ).join("") || "<span style='color:var(--muted);font-size:11px'>none</span>";
    panel.innerHTML = `
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:10px">
        <div><div class="label">Pending</div><div class="value sm">${counts.pending||0}</div></div>
        <div><div class="label">Filled</div><div class="value sm good">${counts.filled||0}</div></div>
        <div><div class="label">Cancelled</div><div class="value sm warn">${counts.cancelled||0}</div></div>
        <div><div class="label">Fill rate</div><div class="value sm">${counts.filled||counts.cancelled?pct((counts.filled||0)/((counts.filled||0)+(counts.cancelled||0))):"—"}</div></div>
      </div>
      <div class="label" style="margin-bottom:6px">Cancel reasons</div>
      ${reasonHtml}`;
  }
}

// ── main refresh loop ─────────────────────────────────────────────────────
let _countdown = 30, _timer = null;

function startCountdown() {
  clearInterval(_timer);
  _countdown = 30;
  _timer = setInterval(()=>{
    _countdown--;
    document.getElementById("refresh-info").textContent = `next refresh in ${_countdown}s`;
    if(_countdown<=0) refresh();
  }, 1000);
}

async function refresh() {
  document.getElementById("refresh-info").textContent = "refreshing…";
  let data;
  try {
    const r = await fetch("/api/state",{cache:"no-store"});
    data = await r.json();
  } catch(e) {
    document.getElementById("meta").textContent = "fetch error: "+e;
    startCountdown();
    return;
  }

  const {config:cfg, live, perf, positions:pos, snapshots} = data;
  window._snaps = snapshots;

  const badge = cfg.paper_mode ? '<span class="badge paper">PAPER</span>' : '<span class="badge live">LIVE</span>';
  document.getElementById("mode-badge").innerHTML = badge;
  const safe = data.paper_safe || {};
  const banner = document.getElementById("safety-banner");
  if(safe.live_trading_enabled) {
    banner.style.background = "rgba(255,107,107,.15)";
    banner.style.borderColor = "rgba(255,107,107,.4)";
    banner.innerHTML = '<span style="color:var(--bad);font-weight:700;font-size:13px;">⚠ LIVE TRADING ENABLED</span><span style="color:var(--muted)">Real orders may be placed</span>';
  } else {
    banner.style.background = "rgba(61,220,151,.1)";
    banner.style.borderColor = "rgba(61,220,151,.3)";
    banner.innerHTML = '<span style="color:var(--good);font-weight:700;font-size:13px;">✓ PAPER MODE</span><span style="color:var(--muted)">LIVE ORDERS DISABLED  ·  live_trading_enabled=false  ·  simulated=true  ·  real_orders_enabled=false</span>';
  }
  document.getElementById("meta").textContent =
    `${cfg.assets.join("/")} · ${cfg.horizons.join(",")} · entry≥${cfg.min_entry_edge} · exit≥${cfg.min_exit_edge} · bankroll $${cfg.bankroll_usd}` +
    (live.cached?" · scan cached":"");

  renderCards(cfg, perf, live, pos);
  renderExposure(pos.exposure);
  renderPnlChart(pos.pnl_curve);
  renderOpen(pos.open);
  renderExitReasons(pos.exit_reasons);
  renderClosed(pos.closed_recent);
  renderScan(live.accepted||[], live.skipped||[]);
  renderOrders(data.orders || {pending:[], filled:[], cancelled_recent:[], counts:{}, cancel_reasons:{}});

  startCountdown();
}

refresh();
</script>
</body>
</html>"""


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
