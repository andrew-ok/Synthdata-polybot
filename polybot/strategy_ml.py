"""Can a trained calibration model turn Synth's signal into a winner —
OUT-OF-SAMPLE? Honest test, no circularity.

Trains a multi-feature logistic model P(UP) on the FIRST 70% of the week
(features: Synth p_up, early momentum, market price, asset, horizon, and an
interaction), then scores it on the held-out LAST 30% it never saw.

Decisive baseline: the MARKET's own implied probability (yes_ask). If the
trained model can't beat the market's Brier score out-of-sample, there is no
edge to calibrate — full stop. Also walk-forward (expanding window) for extra
honesty. No new API calls (cached dataset).
"""
from __future__ import annotations

import numpy as np

from .strategy_lab import build_dataset, _stats, SLIP


def _features(recs):
    X = np.array([[
        r["p_up"],
        np.clip(r["mom"], -0.01, 0.01),
        r["yes_ask"],
        1.0 if r["asset"] == "ETH" else 0.0,
        1.0 if r["horizon"] == "1H" else 0.0,
        r["p_up"] * np.clip(r["mom"], -0.01, 0.01),
    ] for r in recs], dtype=float)
    y = np.array([1.0 if r["outcome"] == "UP" else 0.0 for r in recs])
    return X, y


def _fit(X, y, l2=1.0, iters=4000, lr=0.3):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xs = (X - mu) / sd
    n, d = Xs.shape
    w, b = np.zeros(d), 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(Xs @ w + b)))
        g = p - y
        w -= lr * (Xs.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return (w, b, mu, sd)


def _predict(model, X):
    w, b, mu, sd = model
    Xs = (X - mu) / sd
    return 1.0 / (1.0 + np.exp(-(Xs @ w + b)))


def _brier(p, y):
    return float(np.mean((p - y) ** 2))


def _trade(recs, p_up_pred, thr):
    trades = []
    for r, cp in zip(recs, p_up_pred):
        if cp - r["yes_ask"] >= thr:
            trades.append(("UP", r["yes_ask"], r["outcome"]))
        elif (1 - cp) - r["no_ask"] >= thr:
            trades.append(("DOWN", r["no_ask"], r["outcome"]))
    return _stats(trades)


def main() -> int:
    recs = build_dataset()
    n = len(recs)
    split = int(n * 0.7)
    train, test = recs[:split], recs[split:]
    Xtr, ytr = _features(train)
    Xte, yte = _features(test)

    model = _fit(Xtr, ytr)
    p_model = _predict(model, Xte)
    p_synth = Xte[:, 0]
    p_market = Xte[:, 2]

    print(f"Dataset {n} windows; train={len(train)} test={len(test)} (base {yte.mean():.0%} UP)\n")
    print("=== OUT-OF-SAMPLE forecast accuracy (Brier, lower=better; 0.25=coin flip) ===")
    print(f"  raw Synth p_up      : {_brier(p_synth, yte):.4f}")
    print(f"  MARKET (yes_ask)    : {_brier(p_market, yte):.4f}   <- the baseline to beat")
    print(f"  trained model       : {_brier(p_model, yte):.4f}")
    beat = _brier(p_model, yte) < _brier(p_market, yte)
    print(f"  --> model beats market out-of-sample? {'YES' if beat else 'NO'}\n")

    print("=== OUT-OF-SAMPLE trading on the trained model ===")
    print(f"{'threshold':>10}{'trades':>8}{'win%':>7}{'avg/$':>8}{'sharpe':>8}{'pnl':>8}")
    for thr in (0.05, 0.10, 0.15, 0.20):
        s = _trade(test, p_model, thr)
        print(f"{thr:>10}{s['n']:>8}{s['win']*100:>6.0f}%{s['avg']:>+8.2f}{s['sharpe']:>+8.2f}{s['pnl']:>+8.0f}")

    # Walk-forward: expanding window, retrain every 25 test points
    print("\n=== WALK-FORWARD (expanding window, retrain every 25) thr=0.10 ===")
    wf = []
    order = recs
    start_idx = int(n * 0.4)
    model_wf = None
    for i in range(start_idx, n):
        if model_wf is None or (i - start_idx) % 25 == 0:
            Xh, yh = _features(order[:i])
            model_wf = _fit(Xh, yh)
        r = order[i]
        cp = float(_predict(model_wf, _features([r])[0])[0])
        if cp - r["yes_ask"] >= 0.10:
            wf.append(("UP", r["yes_ask"], r["outcome"]))
        elif (1 - cp) - r["no_ask"] >= 0.10:
            wf.append(("DOWN", r["no_ask"], r["outcome"]))
    s = _stats(wf)
    print(f"  trades={s['n']} win={s['win']*100:.0f}% avg/$={s['avg']:+.3f} sharpe={s['sharpe']:+.2f} pnl={s['pnl']:+.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
