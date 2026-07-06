"""Mechanism-driven strategy generation, tested over the 8 cached weeks.

Joins each 1H window's EARLY snapshot (~150s in: early move direction) with its
LATE snapshot (~40min in: favorite price + Synth prob) and the validated
outcome. Mechanisms (declared before looking at results):

  G1 LATE-FAV     buy any late favorite 0.70-0.92 (pure risk-premium harvest)
  G2 SYNTH-VETO   G1 unless Synth disagrees with the favorite by >2pp
  G3 PERSIST      G1 only if the favorite side was already leading early
                  (intraday momentum: first-part-predicts-last-part)
  G4 PERSIST+VETO G3 with the Synth veto
  G5 RAMP         our live baseline (reference)
  G6 RAMP+PERSIST live strategy + early-lead confirmation
  G7 RAMP+PAUSE   RAMP, stand down 24h after 3 losses within 24h

Scoring identical to all prior tests. Zero new Synth API calls.
Run: python -m polybot.strategy_gen
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import CONFIG
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _ENTRY_OFFSET_SEC
from .strategy_lab import _stats

SLIP = 0.005
STEP = 3600
LATE_OFFSET = 2400.0
PERIODS = [("2026-05-08T21:00:00Z", "2026-06-05T21:00:00Z"),
           ("2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z")]


def _no_ask(opp) -> Optional[float]:
    if opp.no_ask_price is not None:
        return opp.no_ask_price
    if opp.best_bid_price is not None:
        return max(0.0, min(1.0, 1.0 - opp.best_bid_price))
    return None


def _collect(client, asset, events, offset, max_lag):
    bw: Dict[datetime, Dict] = {}
    for ws in events:
        opp = client.fetch(asset, "1H", start_time=_iso_zulu(ws + timedelta(seconds=offset)))
        if opp is None:
            continue
        w = opp.event_start_time
        rec = bw.setdefault(w, {"sp": None, "opp": None, "lag": None})
        if opp.start_price:
            rec["sp"] = opp.start_price
        lag = (opp.current_time - w).total_seconds()
        if 0 <= lag <= max_lag and (rec["opp"] is None or lag < rec["lag"]):
            rec["opp"], rec["lag"] = opp, lag
    return bw


def build_joined() -> List[Dict]:
    client = SynthInsightsClient()
    recs: List[Dict] = []
    for s_iso, e_iso in PERIODS:
        start = datetime.fromisoformat(s_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(e_iso.replace("Z", "+00:00"))
        events = _walk(start, end, STEP)
        for asset in CONFIG.synth_assets:
            early = _collect(client, asset, events, _ENTRY_OFFSET_SEC, _ENTRY_OFFSET_SEC + 90)
            late = _collect(client, asset, events, LATE_OFFSET, 3600)
            for w in sorted(late):
                lo = late[w]["opp"]
                if lo is None or late[w]["lag"] < 1800:
                    continue
                sp = early.get(w, {}).get("sp") or late[w]["sp"]
                nxt = early.get(w + timedelta(seconds=STEP))
                spn = nxt.get("sp") if nxt else None
                if not sp or not spn or spn == sp:
                    continue
                ya, na = lo.best_ask_price, _no_ask(lo)
                if ya is None or na is None:
                    continue
                eo = early.get(w, {}).get("opp")
                early_mom = None
                if eo is not None and eo.start_price and eo.current_price:
                    early_mom = (eo.current_price - eo.start_price) / eo.start_price
                recs.append({
                    "t": w, "asset": asset,
                    "p_up": lo.synth_probability_up,
                    "yes_ask": ya, "no_ask": na,
                    "early_mom": early_mom,
                    "outcome": "UP" if spn > sp else "DOWN",
                })
    recs.sort(key=lambda r: r["t"])
    return recs


def _favorite(r):
    """(side, ask, synth_prob_for_side) for the late favorite, or None."""
    if 0.70 <= r["yes_ask"] <= 0.92:
        return ("UP", r["yes_ask"], r["p_up"])
    if 0.70 <= r["no_ask"] <= 0.92:
        return ("DOWN", r["no_ask"], 1 - r["p_up"])
    return None


def _ramp_ok(ask, prob):
    return prob - ask >= 0.015 + 0.22 * (ask - 0.70)


def gen_trades(recs, kind):
    out = []
    for r in recs:
        fav = _favorite(r)
        if fav is None:
            continue
        side, ask, prob = fav
        veto = prob < ask - 0.02
        persist = (r["early_mom"] is not None and
                   ((side == "UP" and r["early_mom"] > 0) or (side == "DOWN" and r["early_mom"] < 0)))
        take = False
        if kind == "G1":
            take = True
        elif kind == "G2":
            take = not veto
        elif kind == "G3":
            take = persist
        elif kind == "G4":
            take = persist and not veto
        elif kind == "G5":
            take = _ramp_ok(ask, prob)
        elif kind == "G6":
            take = _ramp_ok(ask, prob) and persist
        if take:
            out.append((side, min(0.999, ask + SLIP), r["outcome"], r["t"]))
    return out


def pause_overlay(trades):
    """G7: skip 24h after 3 losses inside 24h (evaluated on resolve order)."""
    kept, losses = [], []
    pause_until = None
    for s, e, o, t in trades:
        if pause_until and t < pause_until:
            continue
        kept.append((s, e, o, t))
        if s != o:
            losses.append(t)
            losses = [x for x in losses if (t - x).total_seconds() <= 86400]
            if len(losses) >= 3:
                pause_until = t + timedelta(hours=24)
                losses = []
    return kept


def main() -> int:
    recs = build_joined()
    origin = recs[0]["t"]
    n_weeks = 8
    strategies = [
        ("G1 LATE-FAV (no Synth)", gen_trades(recs, "G1")),
        ("G2 SYNTH-VETO", gen_trades(recs, "G2")),
        ("G3 PERSIST", gen_trades(recs, "G3")),
        ("G4 PERSIST+VETO", gen_trades(recs, "G4")),
        ("G5 RAMP (live baseline)", gen_trades(recs, "G5")),
        ("G6 RAMP+PERSIST", gen_trades(recs, "G6")),
        ("G7 RAMP+PAUSE(3L/24h)", pause_overlay(gen_trades(recs, "G5"))),
    ]
    hdr = "".join(f"{'W'+str(w+1):>8s}" for w in range(n_weeks))
    print(f"{len(recs)} joined 1H windows (early+late+outcome)\n")
    print(f"{'strategy':26s}{hdr}{'  wks+':>7s}{'    n':>6s}{' win%':>6s}{' avg/$':>7s}{'  cum$':>8s}")
    print("-" * (26 + 8 * n_weeks + 34))
    for name, trades in strategies:
        wk: Dict[int, List] = {}
        for s, e, o, t in trades:
            wk.setdefault(int((t - origin).total_seconds() // (7 * 86400)), []).append((s, e, o))
        row, pos, tot = "", 0, 0
        for w in range(n_weeks):
            sub = wk.get(w, [])
            if len(sub) >= 3:
                st = _stats(sub)
                row += f"{st['avg']:>+8.2f}"
                tot += 1
                pos += 1 if st["avg"] > 0 else 0
            else:
                row += f"{'--':>8s}"
        agg = _stats([(s, e, o) for s, e, o, _ in trades])
        print(f"{name:26s}{row}{pos:>4d}/{tot}{agg['n']:>6d}{agg['win']*100:>5.0f}%{agg['avg']:>+7.2f}{agg['pnl']:>+8.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
