"""Multi-player check of the potato planting pool (kabu_market.game potato rules).

Usage: python tools/simulate_potato.py [K per_player]   (defaults are the shipped values)

group price per potato = SEED_COST * K * D / (D + total planted),  D = per_player x players
each planter's price   = group price * clamp(weather * own plot luck, 0.5, 1.8)
Payouts are kept to the cent like the game's; everyone has a wallet (100 to start,
check-ins in between).
Besides the mean per period, it reports how often each player wins and how often a
period has both winners and losers: the game should be fun, not only balanced.
"""

import math
import random
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabu_market import game  # noqa: E402

COST, CAP = game.SEED_COST, game.PLANT_CAP
K = float(sys.argv[1]) if len(sys.argv) > 1 else game.HARVEST_K
PER_PLAYER = float(sys.argv[2]) if len(sys.argv) > 2 else game.HARVEST_PER_PLAYER
PERIODS, SEEDS = 150, 60
CHECKIN_PER_PERIOD = 37  # about 12.5 coins a day x 3 days


def best_response(others, others_planters, depth, budget):
    """A thinking player: the amount that would have paid best against their estimate."""
    if others_planters < 1 or others < 1:  # nobody else lately: expect someone small
        others = 5
    best, best_n = 0.0, 0
    for n in range(1, min(CAP, int(budget // COST)) + 1):
        ev = n * COST * K * depth / (depth + others + n) - n * COST
        if ev > best:
            best, best_n = ev, n
    return best_n


def run(mix, seed):
    rng = random.Random(seed)
    wrng = random.Random(seed + 10_000)  # weather on its own stream
    n = len(mix)
    depth = PER_PLAYER * n  # everyone in the group plays
    coins, gain, won, played = [100] * n, [0] * n, [0] * n, [0] * n
    last = [0] * n
    memory = [rng.uniform(0.3, 0.8) for _ in mix]  # how fast each thinker forgets
    seen, seen_planters = [None] * n, [None] * n
    open_periods = split = 0
    for _ in range(PERIODS):
        plants = []
        for i, kind in enumerate(mix):
            coins[i] += CHECKIN_PER_PERIOD
            others = sum(v for j, v in enumerate(last) if j != i)
            op = sum(1 for j, v in enumerate(last) if j != i and v)
            a = memory[i]
            seen[i] = others if seen[i] is None else a * others + (1 - a) * seen[i]
            seen_planters[i] = (
                op if seen_planters[i] is None else a * op + (1 - a) * seen_planters[i]
            )
            amount = {
                "thinker": lambda: round(
                    (last[i] + best_response(seen[i] * rng.uniform(0.8, 1.2), seen_planters[i], depth, coins[i])) / 2
                ),
                "full": lambda: CAP,
                "four": lambda: 4,
                "twenty": lambda: 20,
                "sixty": lambda: 60,
                "random": lambda: rng.randint(1, CAP) if rng.random() < 0.6 else 0,
            }[kind]()
            plants.append(max(0, min(amount, CAP, int(coins[i] // COST))))
        total = sum(plants)
        planters = sum(1 for v in plants if v)
        group_price = COST * K * depth / (depth + total)
        weather = math.exp(wrng.gauss(-0.011, 0.15))
        signs = set()
        for i, seeds in enumerate(plants):
            if not seeds:
                continue
            if planters < 2:
                g = 0  # refund
            else:
                luck = math.exp(wrng.gauss(-0.02, 0.2))
                price = group_price * max(0.5, min(1.8, weather * luck))
                g = game.cents(seeds * price) - seeds * COST  # paid to the cent, as the game does
                signs.add(g > 0)
            gain[i] += g
            coins[i] += g
            played[i] += 1
            won[i] += g > 0
        if planters >= 2:
            open_periods += 1
            split += len(signs) == 2
        last = plants
    return [(g / PERIODS, w / p if p else 0.0) for g, w, p in zip(gain, won, played)], (
        split / open_periods if open_periods else 0.0
    )


NAMES = {
    "thinker": "会猜",
    "full": "每次种满",
    "four": "每次种 4",
    "twenty": "每次种 20",
    "sixty": "每次种 60",
    "random": "乱种",
}
MIXES = {
    "6 人混着玩": ["thinker", "full", "four", "twenty", "sixty", "random"],
    "6 人都动脑": ["thinker"] * 6,
    "6 人约好各种 20": ["twenty"] * 6,
    "6 人全乱种": ["random"] * 6,
    "12 人混着玩": ["thinker"] * 3 + ["full"] * 2 + ["four"] * 2 + ["sixty"] * 2 + ["random"] * 3,
}
print(f"K={K}，D=每位玩家 {PER_PLAYER}（每期人均收益 / 赢的期数）")
for label, mix in MIXES.items():
    results = [run(mix, seed) for seed in range(SEEDS)]
    by_kind = {}
    for rows, _ in results:
        for i, kind in enumerate(mix):
            by_kind.setdefault(kind, []).append(rows[i])
    cells = "，".join(
        f"{NAMES[k]} {statistics.mean(m for m, _ in v):+.1f} / {statistics.mean(w for _, w in v):.0%}"
        for k, v in by_kind.items()
    )
    split = statistics.mean(s for _, s in results)
    print(f"{label}：{cells}；同一期有人赚有人亏 {split:.0%}")
