"""The definitive calibration test: can a calibrator trained on 3 weeks make
money on the held-out 4th week? Plus walk-forward across all weeks.

Decisive baseline stays the MARKET price: if the calibrated model can't beat
the market's own Brier out-of-sample, there is no information to calibrate.
No new API calls (cached). Run: python -m polybot.calib_oos
"""
from __future__ import annotations

from datetime import timedelta
import numpy as np

from .strategy_lab import build_dataset, _stats
from .strategy_ml import _features, _fit, _predict, _brier

START, END = "2026-06-05T21:00:00Z", "2026-07-03T21:00:00Z"


def _week(recs):
    o = recs[0]["t"]
    out = {}
    for r in recs:
        out.setdefault(int((r["t"] - o).total_seconds() // (7 * 86400)), []).append(r)
    return out


def fit_bins(recs):
    from collections import defaultdict
    b = defaultdict(lambda: [0, 0])
    for r in recs:
        k = int(r["p_up"] * 10)
        b[k][0] += 1 if r["outcome"] == "UP" else 0
        b[k][1] += 1
    return {k: v[0] / v[1] for k, v in b.items() if v[1] >= 15}


def trade(recs, p_pred, thr, slip=0.005):
    t = []
    for r, cp in zip(recs, p_pred):
        if cp - r["yes_ask"] >= thr:
            t.append(("UP", min(0.999, r["yes_ask"] + slip), r["outcome"]))
        elif (1 - cp) - r["no_ask"] >= thr:
            t.append(("DOWN", min(0.999, r["no_ask"] + slip), r["outcome"]))
    return _stats(t)


def main() -> int:
    recs = build_dataset(START, END)
    wk = _week(recs)
    ids = sorted(wk)
    train = [r for w in ids[:-1] for r in wk[w]]
    test = wk[ids[-1]]
    print(f"Train weeks {ids[:-1]} (n={len(train)}) -> Test week {ids[-1]} (n={len(test)})\n")

    # ---- logistic model ----
    Xtr, ytr = _features(train); Xte, yte = _features(test)
    model = _fit(Xtr, ytr)
    p_model, p_synth, p_mkt = _predict(model, Xte), Xte[:, 0], Xte[:, 2]
    print("=== OUT-OF-SAMPLE forecast accuracy on held-out week (Brier, lower=better) ===")
    print(f"  raw Synth : {_brier(p_synth, yte):.4f}")
    print(f"  MARKET    : {_brier(p_mkt, yte):.4f}   <- must beat this")
    print(f"  model     : {_brier(p_model, yte):.4f}   -> beats market? "
          f"{'YES' if _brier(p_model, yte) < _brier(p_mkt, yte) else 'NO'}\n")

    print("=== trading the calibrated model on the held-out week ===")
    print(f"{'method':22s}{'thr':>6}{'trades':>8}{'win%':>7}{'avg/$':>8}{'pnl':>8}")
    binmap = fit_bins(train)
    for thr in (0.05, 0.10, 0.15):
        pb = np.array([binmap.get(int(r["p_up"] * 10), r["p_up"]) for r in test])
        s = trade(test, pb, thr)
        print(f"{'bin-calibrated':22s}{thr:>6}{s['n']:>8}{s['win']*100:>6.0f}%{s['avg']:>+8.2f}{s['pnl']:>+8.0f}")
    for thr in (0.05, 0.10, 0.15):
        s = trade(test, p_model, thr)
        print(f"{'logistic-model':22s}{thr:>6}{s['n']:>8}{s['win']*100:>6.0f}%{s['avg']:>+8.2f}{s['pnl']:>+8.0f}")

    # ---- walk-forward: train on all prior weeks, test each subsequent week ----
    print("\n=== WALK-FORWARD (train on prior weeks, test each week) logistic thr=0.10 ===")
    allwf = []
    for i in range(1, len(ids)):
        tr = [r for w in ids[:i] for r in wk[w]]
        te = wk[ids[i]]
        m = _fit(*_features(tr))
        pm = _predict(m, _features(te)[0])
        s = trade(te, pm, 0.10)
        allwf.append(s)
        print(f"  test week {ids[i]}: trades={s['n']} win={s['win']*100:.0f}% avg/$={s['avg']:+.3f} pnl={s['pnl']:+.0f}")
    tot_n = sum(s["n"] for s in allwf)
    tot_pnl = sum(s["pnl"] for s in allwf)
    print(f"  AGGREGATE walk-forward: trades={tot_n} pnl={tot_pnl:+.0f}  avg/$={tot_pnl/(tot_n*37.5) if tot_n else 0:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
