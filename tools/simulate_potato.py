"""Multi-player check of the potato planting pool (kabu_market.game potato rules).

Usage: python tools/simulate_potato.py [K depth cap]   (defaults are the shipped values)

price per potato = COST * K * D / (D + N) * weather,   D = DEPTH * planters
Each seed costs COST. Per-person cap CAP per period.
"""

import math
import random
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")
COST, PERIODS = 10, 200
CAP = int(sys.argv[3]) if len(sys.argv) > 3 else 25
K = float(sys.argv[1]) if len(sys.argv) > 1 else 1.8
DEPTH = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0


def weather(rng):
    return min(1.8, max(0.5, math.exp(rng.gauss(-0.03, 0.25))))


FIXED = "--fixed" in sys.argv


def price(total, planters, w):
    if planters < 2:
        return COST  # fewer than two planters: refund
    depth = DEPTH if FIXED else DEPTH * planters
    return COST * K * depth / (depth + total) * w


def best_response(others, others_planters):
    if others_planters == 0:  # nobody else last time: a human expects someone to try
        others, others_planters = 5, 1
    best, best_n = 0.0, 0
    for n in range(1, CAP + 1):
        ev = n * (price(others + n, others_planters + 1, 1.0) - COST)
        if ev > best:
            best, best_n = ev, n
    return best_n


def choose(kind, rng, others, planters):
    return {
        "one": lambda: 1,
        "small": lambda: 5,
        "big": lambda: 30,
        "allin": lambda: min(CAP, 100),  # without a cap, "all in" = 100 seeds (1000 coins)
        "random": lambda: rng.randint(1, 40) if rng.random() < 0.6 else 0,
        "adaptive": lambda: best_response(others, planters),
    }[kind]()


def run(mix, seed):
    rng = random.Random(seed)
    profit, worst = {i: 0.0 for i in range(len(mix))}, {i: 0.0 for i in range(len(mix))}
    last = {i: 0 for i in range(len(mix))}
    for _ in range(PERIODS):
        plants = {}
        for i, kind in enumerate(mix):
            others = sum(v for j, v in last.items() if j != i)
            planters = sum(1 for j, v in last.items() if j != i and v)
            plants[i] = choose(kind, rng, others, planters)
        total = sum(plants.values())
        planters = sum(1 for v in plants.values() if v >= 5)  # only staked planters deepen demand
        if total:
            each = price(total, planters, weather(rng))
            for i, n in plants.items():
                gain = n * (each - COST)
                profit[i] += gain
                worst[i] = min(worst[i], gain)
        last = plants
    return {i: (profit[i] / PERIODS, worst[i]) for i in profit}


MIXES = {
    "独自一人": ["adaptive"],
    "3 人：一个会猜 + 两个乱种": ["adaptive", "random", "random"],
    "6 人：各种玩法混着": ["adaptive", "allin", "one", "small", "big", "random"],
    "6 人：全员会猜": ["adaptive"] * 6,
    "6 人：全员每期种满 50": ["allin"] * 6,
    "6 人：全员乱种": ["random"] * 6,
    "12 人：各种玩法混着": ["adaptive"] * 3
    + ["allin"] * 2
    + ["one"] * 2
    + ["big"] * 2
    + ["random"] * 3,
}
print(f"K={K}，D=每位参与者 {DEPTH}")
for label, mix in MIXES.items():
    results = [run(mix, seed) for seed in range(40)]
    by_kind = {}
    for res in results:
        for i, kind in enumerate(mix):
            by_kind.setdefault(kind, []).append(res[i])
    cells = "，".join(
        f"{k} {statistics.mean(m for m, _ in v):+.1f}（最惨一期 {statistics.mean(w for _, w in v):+.0f}）"
        for k, v in by_kind.items()
    )
    print(f"{label}：{cells}")
