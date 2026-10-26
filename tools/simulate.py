"""Economy check: does researching the market beat just checking in?

Runs the real price engine (kabu_market/prices.py) and the game's own constants.
One player per group, 16 weeks, no loans, no streak bonus. A "researching" player
follows simple folk strategies; a random player buys and sells at random.

    python tools/simulate.py [runs]
"""

import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabu_market import game  # noqa: E402
from kabu_market.prices import PriceEngine, week_start  # noqa: E402

WEEKS = 16
BASES = {p.kind: p.base for p in game.PRODUCTS}


def buy_cost(price, units):
    return game.KabuMarket.buy_cost(price, 1.0, units)[0]


def sell_income(price, units):
    return game.KabuMarket.sell_income(price, 1.0, units)[0]


def affordable(price, budget):
    units = 0
    while units < game.MAX_UNITS and buy_cost(price, units + 1) <= budget:
        units += 1
    return units


def play(seed, smart):
    engine = PriceEngine(f"sim{seed}", 740000)
    rng = random.Random(seed)
    start = week_start(740000)
    coins, pnl = game.STARTER_COINS, {"turnip": 0, "tomato": 0, "potato": 0}
    held = {}  # kind -> (units, cost, day bought, price paid)
    for day in range(start, start + WEEKS * 7):
        coins += rng.randint(*game.CHECKIN_RANGE)
        for half in (0, 1):
            prices = {k: getattr(engine, k)(BASES[k], day, half) for k in BASES}
            # sell first
            for kind, (units, cost, bought, paid) in list(held.items()):
                price, age = prices[kind], day - bought
                if kind == "turnip":
                    rotten = age >= game.TURNIP_SHELF_DAYS
                    sell = price >= 1.3 * paid or age >= 6 if smart else rng.random() < 0.15
                elif kind == "tomato":
                    rotten, sell = (
                        False,
                        (price >= 1.2 * paid or age >= 3) if smart else rng.random() < 0.15,
                    )
                else:
                    rotten, sell = False, price >= BASES[kind] if smart else rng.random() < 0.15
                if rotten:
                    pnl[kind] -= cost
                    del held[kind]
                elif sell:
                    income = sell_income(price, units)
                    coins += income
                    pnl[kind] += income - cost
                    del held[kind]
            # then buy (turnips are never sold below this week's Sunday price)
            for kind, price in prices.items():
                if kind in held:
                    continue
                if kind == "turnip":
                    price = max(price, engine.turnip(BASES[kind], week_start(day), 0))
                if smart:
                    if kind == "turnip":
                        want = day == week_start(day) and half == 0
                    elif kind == "tomato":
                        news = engine.news(day)
                        want = half == 0 and news is not None and news[1] > 0
                    else:
                        want = price < 0.7 * BASES[kind]
                    share = 0.6 if kind == "turnip" else 0.3
                else:
                    want, share = rng.random() < 0.1, rng.random()
                units = affordable(price, coins * share) if want else 0
                if units:
                    cost = buy_cost(price, units)
                    coins -= cost
                    held[kind] = (units, cost, day, price)
    days = WEEKS * 7
    return sum(pnl.values()) / days, {k: v / days for k, v in pnl.items()}


def main(runs):
    smart = [play(seed, True) for seed in range(runs)]
    naive = [play(seed, False) for seed in range(runs)]
    mean = lambda rows: sum(r[0] for r in rows) / len(rows)  # noqa: E731
    by_kind = {k: sum(r[1][k] for r in smart) / runs for k in BASES}
    checkin = sum(game.CHECKIN_RANGE) / 2
    print(f"签到日均 {checkin:.1f}（另有连签奖励，未计入）")
    print(
        f"会研究的玩家交易日均 {mean(smart):+.1f}，其中 "
        + "、".join(f"{k} {v:+.1f}" for k, v in by_kind.items())
    )
    print(f"乱买乱卖的玩家交易日均 {mean(naive):+.1f}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
