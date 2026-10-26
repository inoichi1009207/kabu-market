"""Stylized facts + GARCH(1,1)-t fit of daily log returns; prints one summary table.

Usage: python fit_kline.py data/kline.json
"""

import json
import sys

import numpy as np
from arch import arch_model


def acf(x, lag):
    x = x - x.mean()
    return float((x[:-lag] * x[lag:]).sum() / (x * x).sum())


def main(path):
    data = json.load(open(path, encoding="utf-8"))
    rows = []
    for symbol, item in data.items():
        close = np.array(item["close"])
        r = np.diff(np.log(close))
        r = r[np.isfinite(r)]
        sd = r.std()
        z = (r - r.mean()) / sd
        kurt = float((z**4).mean() - 3)
        tail = float((np.abs(z) > 3).mean())
        fit = arch_model(r * 100, mean="Constant", vol="GARCH", p=1, q=1, dist="t").fit(disp="off")
        p = fit.params
        rows.append(
            dict(
                symbol=symbol,
                sd=sd,
                kurt=kurt,
                tail3=tail,
                acf_r1=acf(r, 1),
                acf_abs1=acf(np.abs(r), 1),
                acf_abs5=acf(np.abs(r), 5),
                alpha=float(p["alpha[1]"]),
                beta=float(p["beta[1]"]),
                nu=float(p["nu"]),
            )
        )
    keys = ["sd", "kurt", "tail3", "acf_r1", "acf_abs1", "acf_abs5", "alpha", "beta", "nu"]
    print("symbol    " + " ".join(f"{k:>8}" for k in keys))
    for row in rows:
        print(f"{row['symbol']:9} " + " ".join(f"{row[k]:8.3f}" for k in keys))
    med = {k: float(np.median([row[k] for row in rows])) for k in keys}
    print("median    " + " ".join(f"{med[k]:8.3f}" for k in keys))
    print("persistence(alpha+beta) median:", round(med["alpha"] + med["beta"], 3))
    print("normal-distribution reference: kurt 0, tail3 0.0027")


if __name__ == "__main__":
    main(sys.argv[1])
