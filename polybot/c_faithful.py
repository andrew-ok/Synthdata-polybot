"""Faithful backtest of the Synth X-post strategy across the 4 cached weeks.

THE trade per the post: hourly up/down, ~20 min before close, the favorite is
trading high (e.g. 85c) but not fully converged; buy the FAVORITE side iff
Synth's probability for that side >= ask + threshold ("85c worth 92c").

Needs late-window (T-20min) hourly snapshots: week 4 is cached; weeks 1-3 are
fetched fresh (~1,000 calls). Outcomes come from the cached early-snapshot
start-price chain (same as all prior tests).

Scores per week + variants (edge threshold x favorite band) and a contrarian
control (what my old unfaithful C did) for contrast.

Run: python -m polybot.c_faithful
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import CONFIG
from .synth_client import SynthInsightsClient
from .backtester import _walk, _iso_zulu, _ENTRY_OFFSET_SEC
from .strategy_lab import _stats

START, END = "2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z"
LATE_OFFSET = 2400.0          # T-20min into a 60-min window
SLIP = 0.005
STEP = 3600


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


def build() -> List[Dict]:
    client = SynthInsightsClient()
    start = datetime.fromisoformat(START.replace("Z", "+00:00"))
    end = datetime.fromisoformat(END.replace("Z", "+00:00"))
    events = _walk(start, end, STEP)
    recs: List[Dict] = []
    for asset in CONFIG.synth_assets:
        outcomes = _collect(client, asset, events, _ENTRY_OFFSET_SEC, _ENTRY_OFFSET_SEC + 90)  # cached
        late = _collect(client, asset, events, LATE_OFFSET, 3600)
        for w in sorted(late):
            opp = late[w]["opp"]
            if opp is None:
                continue
            # late snapshot must actually be LATE (>= 30 min in) for the thesis
            if late[w]["lag"] < 1800:
                continue
            a = outcomes.get(w, {}).get("sp")
            nx = outcomes.get(w + timedelta(seconds=STEP))
            b = nx.get("sp") if nx else None
            if not a or not b or a == b:
                continue
            ya, na = opp.best_ask_price, _no_ask(opp)
            if ya is None or na is None:
                continue
            recs.append({
                "asset": asset, "t": w, "p_up": opp.synth_probability_up,
                "yes_ask": ya, "no_ask": na,
                "lag_min": late[w]["lag"] / 60.0,
                "outcome": "UP" if b > a else "DOWN",
            })
    recs.sort(key=lambda r: r["t"])
    return recs


def faithful(recs, edge, lo, hi):
    """Buy the FAVORITE side iff its ask is in [lo,hi] and Synth prob for that
    side >= ask + edge."""
    trades = []
    for r in recs:
        for side, ask, p in (("UP", r["yes_ask"], r["p_up"]), ("DOWN", r["no_ask"], 1 - r["p_up"])):
            if lo <= ask <= hi and p - ask >= edge:
                trades.append((side, min(0.999, ask + SLIP), r["outcome"]))
                break
    return _stats(trades)


def contrarian(recs, edge):
    """Control: my old unfaithful C — whichever side shows edge (usually cheap)."""
    trades = []
    for r in recs:
        if r["p_up"] - r["yes_ask"] >= edge:
            trades.append(("UP", min(0.999, r["yes_ask"] + SLIP), r["outcome"]))
        elif (1 - r["p_up"]) - r["no_ask"] >= edge:
            trades.append(("DOWN", min(0.999, r["no_ask"] + SLIP), r["outcome"]))
    return _stats(trades)


def main() -> int:
    import sys
    global START, END
    if len(sys.argv) >= 3:
        START, END = sys.argv[1], sys.argv[2]
    recs = build()
    if not recs:
        print("no data")
        return 1
    o = recs[0]["t"]
    wk: Dict[int, List] = {}
    for r in recs:
        wk.setdefault(int((r["t"] - o).total_seconds() // (7 * 86400)), []).append(r)
    ids = sorted(wk)
    avg_lag = sum(r["lag_min"] for r in recs) / len(recs)
    print(f"{len(recs)} late-window hourly observations, {len(ids)} weeks, "
          f"avg snapshot at {avg_lag:.0f} min into the 60-min window\n")

    variants = [
        ("FAITHFUL post: fav .70-.95, edge>=.07", lambda R: faithful(R, .07, .70, .95)),
        ("FAITHFUL variant: edge>=.05", lambda R: faithful(R, .05, .70, .95)),
        ("FAITHFUL variant: edge>=.10", lambda R: faithful(R, .10, .70, .95)),
        ("FAITHFUL wide band .60-.97", lambda R: faithful(R, .07, .60, .97)),
        ("control: old unfaithful C (either side, .10)", lambda R: contrarian(R, .10)),
    ]
    wcols = "".join(f"{'W'+str(w+1):>16s}" for w in ids)
    print(f"{'variant':44s}{wcols}{'   AGG (n, win%, avg/$)':>26s}")
    print("-" * (44 + 16 * len(ids) + 26))
    for name, fn in variants:
        cells = []
        for w in ids:
            s = fn(wk[w])
            cells.append(f"{s['n']:>4}|{s['win']*100:>3.0f}%|{s['avg']:>+5.2f}" if s["n"] else f"{'--':>16s}")
        agg = fn(recs)
        print(f"{name:44s}{''.join(f'{c:>16s}' for c in cells)}"
              f"{agg['n']:>8}, {agg['win']*100:>3.0f}%, {agg['avg']:>+5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
