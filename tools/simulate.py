"""Economy check: does trading beat just checking in?

Runs the real price engine (kabu_market/prices.py) and the game's own constants and
settlement (impact, the 3% sell spread, cents). One player per group, 16 weeks, no loans,
no streak bonus. A player's own price impact carries over between trades and fades at the
game's half-life, and whatever is still held at the end is sold, so the numbers are the
wallet's real result. Two players:

- "folk": turnips bought once the price is under 70% of the base, sold after a 20% gain or
  7 days; tomatoes bought on a good-news morning, sold after a 20% gain or 3 days.
- random: buys and sells at random.

    python tools/simulate.py [runs]
"""

import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabu_market import game  # noqa: E402
from kabu_market.prices import PriceEngine, week_start  # noqa: E402

M = game.KabuMarket
WEEKS = 16
BASES = {p.kind: p.base for p in game.PRODUCTS}
FADE = 0.5 ** (game.DAY / 2 / game.IMPACT_HALF_LIFE)  # impact kept per half day


def self_check():
    """Guard against a broken simulator: an instant round trip must lose."""
    for price in (2.0, 20.0, 120.0):
        cost, after = M.buy_cost(price, 1.0, 1)
        assert M.sell_income(price, after, 1)[0] < cost, "round trip should lose"
    assert 0 < FADE < 1


def play(seed, smart):
    engine = PriceEngine(f"sim{seed}", 740000)
    rng = random.Random(seed)
    start = week_start(740000)
    coins, pnl = float(game.STARTER_COINS), dict.fromkeys(BASES, 0.0)
    held = {}  # kind -> (units, cost, slot bought, price paid)
    impact = dict.fromkeys(BASES, 1.0)
    slot, prices = 0, {}
    for day in range(start, start + WEEKS * 7):
        coins += rng.randint(*game.CHECKIN_RANGE)
        for half in (0, 1):
            slot += 1
            impact = {k: 1 + (v - 1) * FADE for k, v in impact.items()}
            prices = {k: getattr(engine, k)(BASES[k], day, half) for k in BASES}
            for kind, (units, cost, bought, paid) in list(held.items()):
                price, age = prices[kind], (slot - bought) / 2
                if kind == "turnip" and age >= game.TURNIP_SHELF_DAYS:
                    pnl[kind] -= cost  # rotted
                    del held[kind]
                    continue
                if smart:
                    sell = price >= 1.2 * paid or age >= (7 if kind == "turnip" else 3)
                else:
                    sell = rng.random() < 0.15
                if sell:
                    income, impact[kind] = M.sell_income(price, impact[kind], units)
                    coins += income
                    pnl[kind] += income - cost
                    del held[kind]
            for kind, price in prices.items():
                if kind in held:
                    continue
                if smart:
                    if kind == "turnip":
                        want = price < 0.7 * BASES[kind]
                    else:
                        news = engine.news(day)
                        want = half == 0 and news is not None and news[1] > 0
                    budget = coins * 0.3
                else:
                    want, budget = rng.random() < 0.1, coins * rng.random()
                if not want:
                    continue
                units = 0
                while units < game.MAX_UNITS and M.buy_cost(price, impact[kind], units + 1)[0] <= budget:
                    units += 1
                if units:
                    cost, impact[kind] = M.buy_cost(price, impact[kind], units)
                    coins -= cost
                    held[kind] = (units, cost, slot, price)
    for kind, (units, cost, _bought, _paid) in held.items():  # sell what is left
        income, _ = M.sell_income(prices[kind], impact[kind], units)
        pnl[kind] += income - cost
    days = WEEKS * 7
    return sum(pnl.values()) / days, {k: v / days for k, v in pnl.items()}


def main(runs):
    self_check()
    smart = [play(seed, True) for seed in range(runs)]
    naive = [play(seed, False) for seed in range(runs)]
    mean = lambda rows: sum(r[0] for r in rows) / len(rows)  # noqa: E731
    by_kind = {k: sum(r[1][k] for r in smart) / runs for k in BASES}
    checkin = sum(game.CHECKIN_RANGE) / 2
    print(f"签到日均 {checkin:.1f}（另有连签奖励，未计入）")
    print(
        f"按常见打法玩的玩家交易日均 {mean(smart):+.2f}，其中 "
        + "、".join(f"{k} {v:+.2f}" for k, v in by_kind.items())
    )
    print(f"乱买乱卖的玩家交易日均 {mean(naive):+.2f}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
