"""/kabu market mini-game: a cross-group wallet, three goods, loans and player trades.

Gameplay is inspired by cc004/market and the Animal Crossing turnip market idea; this
module and prices.py are independent implementations that copy no code. It uses no
network. Coins are virtual and cannot be exchanged for anything real. See README.md.
"""

import json
import math
import random
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from .prices import PATTERN_NAMES, TZ, PriceEngine, slot_of, week_start

COMMAND = re.compile(r"/kabu(?:\s+(.*))?", re.S)
DAY = 86400
CHECKIN_RANGE = (10, 15)  # coins per Shanghai day, once across all groups
# Each unit bought pushes the price up by IMPACT_STEP and each unit sold divides it back, so
# buying and selling walk the same curve: no buy/sell order can ever net coins. Selling to
# the system then pays SELL_SPREAD less, which is what makes any round trip lose.
IMPACT_STEP, SELL_SPREAD = 1.002, 0.03
IMPACT_HALF_LIFE = 6 * 3600
IMPACT_BOUNDS = (0.1, 10.0)
MAX_UNITS = 9999
LOAN_RATE = 0.01  # daily, compounded per Shanghai day
LEVERAGE = (
    4.0  # total position / net assets; debt may reach (LEVERAGE - 1) × net assets
)
MARGIN = 0.2  # forced sale when net worth < MARGIN × debt
TRADE_FEE = 0.05  # paid by the seller, removed from circulation
TRADE_BAND = (0.5, 2.0)  # unit price relative to the current market price
OFFER_TTL = 1800
TIP_CHANCE, TIP_TRUTH = 0.2, 0.5
STARTER_COINS = 100  # once per person (all groups), on the first /kabu
SEED_KEY = "kabu_seed_v2"
TURNIP_SHELF_DAYS = 14
STREAK_BONUS = (7, 30)  # every 7th consecutive check-in day adds 30 coins
# Potatoes are a sealed planting pool, not a market good. Harvest every 3 days at 20:00;
# price per potato = SEED_COST * HARVEST_K * D / (D + total planted) * weather, where
# D = HARVEST_PER_PLAYER x players who used /kabu in this group during the period,
# planting or not. Demand must not grow with what is planted: when it did, every small
# planter subsidised itself and splitting one stake over alt accounts paid (an external
# audit found +80 instead of -4 for the same 100 seeds). Fewer than 2 planters: refund.
# Calibrated by a multi-player simulation with wallets and rounded payouts, for fun as
# much as balance: ordinary players break even in a mixed group (winning ~4 periods in
# 10), random planting does not pay, greedy full planting does not pay, and a group that
# agrees to plant with restraint wins ~3 periods in 4. Nobody sees this period's total.
# PLANT_CAP is per person per period in one group.
SEED_COST, PLANT_CAP = 2, 100
PLANT_CLOSE_WINDOW = (
    2 * 3600
)  # planting stops at a random moment this long before harvest
HARVEST_DAYS, HARVEST_K, HARVEST_PER_PLAYER = 3, 1.8, 45
HARVEST_EPOCH = datetime(2026, 1, 1, 20, 0, tzinfo=TZ).timestamp()
POTATO = "马铃薯"
WEEKDAYS = "一二三四五六日"
BUY_WORDS, SELL_WORDS = {"buy", "b", "买", "买入"}, {"sell", "s", "卖", "卖出"}
ALL_WORDS = {"all", "全部"}
# one-letter codes so players never switch input methods
ITEM_CODES = {"d": "大头菜", "x": "西红柿", "m": "马铃薯"}
TURNIP_TIPS = (
    "这周大头菜会忽高忽低",
    "这周大头菜要冲一波大的",
    "这周大头菜行情不妙，会一路往下走",
    "这周大头菜会小涨一下",
)


@dataclass(frozen=True)
class Product:
    name: str
    base: float
    kind: str  # turnip | tomato (price shapes in prices.py)
    trait: str


PRODUCTS = (
    Product(
        "大头菜",
        20.0,
        "turnip",
        "每周走势不同（这周冲高还是一路跌要自己看）；每天都能买卖，但买入价不低于本周日的价；买入 14 天后烂掉",
    ),
    Product(
        "西红柿",
        30.0,
        "tomato",
        "跟着新闻走：新闻公开，但涨跌多少、哪天反映、会不会反着走都说不准；不会坏",
    ),
)
POTATO_TRAIT = (
    "不在市场买卖，用 /kabu plant 种。每 3 天晚上 8 点收获，本群这期一共种得越多，"
    "每个卖得越便宜；这期在本群玩的人越多，卖得越贵；各家地里收成不同，"
    "同一期有人赚有人亏；别人种了多少，收获时才揭晓"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kabu_accounts (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    coins INTEGER NOT NULL DEFAULT 0 CHECK (coins >= 0),
    debt INTEGER NOT NULL DEFAULT 0 CHECK (debt >= 0),
    debt_day INTEGER,
    checkin_day INTEGER,
    streak INTEGER NOT NULL DEFAULT 0,
    skip_week INTEGER,
    notice TEXT,
    PRIMARY KEY (bot_id, user_id)
);
CREATE TABLE IF NOT EXISTS kabu_players (
    scope_key TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (scope_key, user_id)
);
CREATE TABLE IF NOT EXISTS kabu_plants (
    scope_key TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    period INTEGER NOT NULL,
    seeds INTEGER NOT NULL CHECK (seeds > 0),
    PRIMARY KEY (scope_key, user_id, period)
);
CREATE TABLE IF NOT EXISTS kabu_harvests (
    scope_key TEXT NOT NULL,
    period INTEGER NOT NULL,
    total INTEGER NOT NULL,
    planters INTEGER NOT NULL,
    price REAL NOT NULL,
    weather REAL NOT NULL,
    PRIMARY KEY (scope_key, period)
);
CREATE TABLE IF NOT EXISTS kabu_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    bought_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS kabu_stock_owner ON kabu_stock(bot_id, user_id, scope_key, item, bought_at);
CREATE TABLE IF NOT EXISTS kabu_impact (
    scope_key TEXT NOT NULL,
    item TEXT NOT NULL,
    impact REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope_key, item)
);
CREATE TABLE IF NOT EXISTS kabu_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    seller TEXT NOT NULL,
    buyer TEXT NOT NULL,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    lots TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def fmt(value):
    return f"{value:.0f}" if value >= 100 else f"{value:.2f}"


def fmt_quote(sell, buy):
    """One number when buying and selling cost the same, else 'sell（买 buy）'."""
    return fmt(sell) if fmt(buy) == fmt(sell) else f"{fmt(sell)}（买 {fmt(buy)}）"


def time_left(seconds):
    if seconds >= DAY:
        return f"{int(seconds // DAY)} 天后"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} 小时后"
    return "1 小时内"


def code_of(name):
    return next(code for code, item in ITEM_CODES.items() if item == name)


def help_text():
    goods = "\n".join(f"· {p.name}（{code_of(p.name)}）：{p.trait}" for p in PRODUCTS)
    goods += f"\n· {POTATO}（m）：{POTATO_TRAIT}"
    return (
        "大头菜市场（虚拟金币，所有群通用一个钱包）\n"
        f"第一次玩送 {STARTER_COINS} 金币。商品代号：大头菜 d、西红柿 x、马铃薯 m\n"
        f"/kabu sign：签到，每天一次，领 {CHECKIN_RANGE[0]}–{CHECKIN_RANGE[1]} 金币，偶尔附带小道消息\n"
        "/kabu market 看价格（还会揭晓上周大头菜的走势、回顾已兑现的西红柿新闻）　/kabu bag 看仓库　/kabu rank 排行\n"
        "/kabu buy d 5　/kabu sell d 5（数量可写 all）　/kabu dump 清仓\n"
        f"/kabu plant 10：种土豆，种薯 {SEED_COST} 金币一个，每人每期最多 {PLANT_CAP} 个，只能在群里种；种得越多，你自己的收获价也越低\n"
        "/kabu loan 500　/kabu repay 500：可以多次借，欠款合计最多到净资产的 3 倍（4 倍杠杆），日息 1%，资不抵债会被强制平仓\n"
        "/kabu offer @群友 d 5 25：向群友报价（单价 25），对方 /kabu ok 编号 成交，/kabu cancel 编号 撤回；卖方付 5% 手续费\n"
        "每天上午、下午各一个价；在本群买得越多价格越高，卖得越多价格越低，波动会慢慢回落。货物跟着你走，在哪个群都能卖。\n"
        "商品：\n" + goods
    )


class KabuMarket:
    def __init__(self, store, clock, *, random_source=None):
        self.store, self.clock = store, clock
        self.random = random_source or random.SystemRandom()
        self.products = {p.name: p for p in PRODUCTS}
        self.store.db.executescript(SCHEMA)
        self._engine = None

    @staticmethod
    def matches(text):
        return COMMAND.fullmatch(text.strip()) is not None

    @staticmethod
    def day(now):
        return slot_of(now)[0]

    # -- prices -------------------------------------------------------------------

    @property
    def engine(self):
        if self._engine is None:
            seed = self.store.get(SEED_KEY)
            if not isinstance(seed, dict):
                # Server-local salt: prices survive restarts and are unknowable to players.
                seed = {
                    "salt": secrets.token_hex(16),
                    "start_day": self.day(self.clock.now()),
                }
                if (
                    self.store.db.in_transaction
                ):  # e.g. first price read in a margin call
                    self.store.put(SEED_KEY, seed, self.clock.now())
                else:
                    with self.store.transaction():
                        self.store.put(SEED_KEY, seed, self.clock.now())
            self._engine = PriceEngine(seed["salt"], seed["start_day"])
        return self._engine

    def price(self, product, now):
        """System price before this group's impact."""
        day, half = slot_of(now)
        return getattr(self.engine, product.kind)(product.base, day, half)

    def ask(self, product, now):
        """System price when the system sells to a player, before impact.

        Turnips are never sold below this week's Sunday price (as with the Sunday seller
        in the original game), while selling to the system follows the weekly pattern.
        Without this floor a cheap Saturday followed by the Sunday reset was a near-sure
        overnight profit (an audit saw 9576 such trades and not one loss).
        """
        price = self.price(product, now)
        if product.kind != "turnip":
            return price
        day, _half = slot_of(now)
        return max(price, self.engine.turnip(product.base, week_start(day), 0))

    def quote(self, product, scope_key, now):
        """What a player sees: (sell-to-system price, buy price) with this group's impact."""
        impact = self.impact(scope_key, product.name, now)
        return self.price(product, now) * impact, self.ask(product, now) * impact

    def impact(self, scope_key, item, now):
        row = self.store.db.execute(
            "SELECT impact, updated_at FROM kabu_impact WHERE scope_key=? AND item=?",
            (scope_key, item),
        ).fetchone()
        if row is None:
            return 1.0
        fade = 0.5 ** (max(0.0, now - row["updated_at"]) / IMPACT_HALF_LIFE)
        return 1.0 + (row["impact"] - 1.0) * fade

    def _set_impact(self, scope_key, item, value, now):
        value = max(IMPACT_BOUNDS[0], min(IMPACT_BOUNDS[1], value))
        self.store.db.execute(
            "INSERT INTO kabu_impact VALUES(?,?,?,?) ON CONFLICT(scope_key,item) DO UPDATE SET impact=excluded.impact,updated_at=excluded.updated_at",
            (scope_key, item, value, now),
        )

    @staticmethod
    def buy_cost(price, impact, units):
        """Each unit is paid at the current price, then pushes the price up."""
        total = 0.0
        for _ in range(units):
            total += price * impact
            impact = min(IMPACT_BOUNDS[1], impact * IMPACT_STEP)
        return math.ceil(total - 1e-9), impact

    @staticmethod
    def sell_income(price, impact, units):
        """Each unit first pulls the price back one step, then is paid at that price.

        That is exactly the price the last unit bought was paid at, so the curve alone is
        reversible both ways (sell-then-buy cannot mint coins); the spread makes it a loss.
        """
        total = 0.0
        for _ in range(units):
            impact = max(IMPACT_BOUNDS[0], impact / IMPACT_STEP)
            total += price * impact
        return math.floor(total * (1 - SELL_SPREAD) + 1e-9), impact

    # -- accounts -----------------------------------------------------------------

    def account(self, bot, user, now):
        """Inside a transaction: the account row with interest accrued to today."""
        query = "SELECT * FROM kabu_accounts WHERE bot_id=? AND user_id=?"
        row = self.store.db.execute(query, (bot, user)).fetchone()
        if row is None:
            self.store.db.execute(
                "INSERT INTO kabu_accounts(bot_id,user_id) VALUES(?,?)", (bot, user)
            )
            row = self.store.db.execute(query, (bot, user)).fetchone()
        return self._accrue(dict(row), now)

    def _accrue(self, acct, now):
        today = self.day(now)
        if acct["debt"] and acct["debt_day"] is not None and today > acct["debt_day"]:
            days = today - acct["debt_day"]
            acct["debt"] = math.ceil(acct["debt"] * (1 + LOAN_RATE) ** days)
        if acct["debt"]:
            acct["debt_day"] = today
        return acct

    def knowledge(self, event):
        """Read-only facts for the chat model when a conversation touches /kabu.

        Writes nothing: no account is created, no interest is saved, no offer expires.
        """
        now = self.clock.now()
        key = event.scope.key
        prices = {}
        for p in PRODUCTS:
            sell, buy = self.quote(p, key, now)
            prices[p.name] = {"sell_to_system": round(sell, 2), "buy": round(buy, 2)}
        day = self.day(now)
        news = [
            f"{label}：{headline}"
            for when, label in ((day, "今天"), (day - 1, "昨天"))
            if (headline := self.engine.headline(when))
        ]
        facts = {
            "note": (
                "Trusted read-only state of this chat's /kabu market mini-game (virtual coins). "
                "Explain rules and quote numbers only from here. You may discuss strategy and give "
                "your opinion on the asker's positions, framed as an opinion: you cannot see "
                "this week's hidden pattern or how a news item will land, so never promise a "
                "future price (turnip_last_week and tomato_news_landed are past, already "
                "public facts you may discuss). "
                "Players trade only by sending /kabu commands themselves: never claim you bought, "
                "sold, lent or checked in for anyone. 小道消息 rumours are unverified: never tell "
                "players how often they are true, and do not confirm or deny one. A stated "
                "investing preference may be remembered under the usual memory rules; these "
                "numbers change every half day, so do not store them as memories. "
                "Nobody, including you, knows how many potatoes this chat planted this period "
                "until the harvest: never guess or hint at it."
            ),
            "rules": help_text(),
            "prices_now_in_this_chat": prices,
            "tomato_news": news,
            # past facts only: last week's revealed turnip pattern, the latest landed news
            "turnip_last_week": self.turnip_recap(now),
            "tomato_news_landed": self.tomato_recap(now),
            "potato": self.potato_public(key, now),
        }
        row = self.store.db.execute(
            "SELECT * FROM kabu_accounts WHERE bot_id=? AND user_id=?",
            (event.scope.bot_id, event.sender),
        ).fetchone()
        if row is not None:
            acct = self._accrue(dict(row), now)
            here = {}
            for lot in self._stock(event.scope.bot_id, event.sender):
                here[lot["item"]] = here.get(lot["item"], 0) + lot["quantity"]
            facts["asker"] = {
                "coins": acct["coins"],
                "debt": acct["debt"],
                "net_assets": math.floor(self.net_worth(acct, now)),
                "goods": here,
                "potatoes_planted_unharvested": self._planted(
                    event.scope.bot_id, event.sender
                ),
                "checkin_streak": acct["streak"],
            }
        return facts

    def save(self, acct):
        self.store.db.execute(
            "UPDATE kabu_accounts SET coins=?,debt=?,debt_day=?,checkin_day=?,streak=?,skip_week=?,notice=? WHERE bot_id=? AND user_id=?",
            (
                acct["coins"],
                acct["debt"],
                acct["debt_day"] if acct["debt"] else None,
                acct["checkin_day"],
                acct["streak"],
                acct["skip_week"],
                acct["notice"],
                acct["bot_id"],
                acct["user_id"],
            ),
        )

    def _stock(self, bot, user, scope_key=None, item=None):
        sql = "SELECT * FROM kabu_stock WHERE bot_id=? AND user_id=?"
        args = [bot, user]
        if scope_key is not None:
            sql += " AND scope_key=?"
            args.append(scope_key)
        if item is not None:
            sql += " AND item=?"
            args.append(item)
        return self.store.db.execute(sql + " ORDER BY bought_at, id", args).fetchall()

    def _rot(self, bot, user, now):
        """Inside a transaction: drop rotten lots everywhere, return {item: units}."""
        where = "bot_id=? AND user_id=? AND expires_at IS NOT NULL AND expires_at<=?"
        rows = self.store.db.execute(
            f"SELECT item, sum(quantity) AS n FROM kabu_stock WHERE {where} GROUP BY item",
            (bot, user, now),
        ).fetchall()
        self.store.db.execute(f"DELETE FROM kabu_stock WHERE {where}", (bot, user, now))
        return {r["item"]: r["n"] for r in rows}

    def net_worth(self, acct, now):
        """Coins plus every unrotten lot at the current system price, minus debt."""
        value = acct["coins"] - acct["debt"]
        for lot in self._stock(acct["bot_id"], acct["user_id"]):
            product = self.products.get(lot["item"])
            if product is None or (
                lot["expires_at"] is not None and lot["expires_at"] <= now
            ):
                continue
            value += self.price(product, now) * lot["quantity"]
        # goods waiting in an open offer are still the seller's until someone accepts
        for offer in self._offers_by(acct["bot_id"], acct["user_id"]):
            product = self.products.get(offer["item"])
            if product is not None:
                for lot in json.loads(offer["lots"]):
                    if lot["expires_at"] is None or lot["expires_at"] > now:
                        value += self.price(product, now) * lot["quantity"]
        return value + SEED_COST * self._planted(acct["bot_id"], acct["user_id"])

    def _offers_by(self, bot, seller):
        return self.store.db.execute(
            "SELECT * FROM kabu_offers WHERE bot_id=? AND seller=?", (bot, seller)
        ).fetchall()

    def _take(self, bot, user, scope_key, item, units, now):
        """Inside a transaction: remove ``units`` oldest-first; return the taken lots."""
        taken, left = [], units
        for lot in self._stock(bot, user, scope_key, item):
            if left == 0:
                break
            if lot["expires_at"] is not None and lot["expires_at"] <= now:
                continue
            take = min(left, lot["quantity"])
            if take == lot["quantity"]:
                self.store.db.execute("DELETE FROM kabu_stock WHERE id=?", (lot["id"],))
            else:
                self.store.db.execute(
                    "UPDATE kabu_stock SET quantity=quantity-? WHERE id=?",
                    (take, lot["id"]),
                )
            taken.append(
                {
                    "quantity": take,
                    "bought_at": lot["bought_at"],
                    "expires_at": lot["expires_at"],
                }
            )
            left -= take
        return taken

    def _put(self, bot, user, scope_key, item, lots):
        for lot in lots:
            self.store.db.execute(
                "INSERT INTO kabu_stock(scope_key,bot_id,user_id,item,quantity,bought_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    scope_key,
                    bot,
                    user,
                    item,
                    lot["quantity"],
                    lot["bought_at"],
                    lot["expires_at"],
                ),
            )

    # -- maintenance --------------------------------------------------------------

    def maintain(self, event, now):
        """Record the player, return expired offers and run margin calls for this Bot."""
        bot = event.scope.bot_id
        with self.store.transaction():
            # First /kabu ever (any group): open the wallet with a starter grant.
            self.store.db.execute(
                "INSERT OR IGNORE INTO kabu_accounts(bot_id,user_id,coins,notice) VALUES(?,?,?,?)",
                (
                    bot,
                    event.sender,
                    STARTER_COINS,
                    f"欢迎来到大头菜市场！送你 {STARTER_COINS} 金币当本钱。",
                ),
            )
            self.store.db.execute(
                "INSERT INTO kabu_players VALUES(?,?,?,?,?) ON CONFLICT(scope_key,user_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE kabu_players.name END,seen_at=excluded.seen_at",
                (
                    event.scope.key,
                    bot,
                    event.sender,
                    event.sender_name.strip()[:24],
                    now,
                ),
            )
            expired = self.store.db.execute(
                "SELECT * FROM kabu_offers WHERE bot_id=? AND expires_at<=?", (bot, now)
            ).fetchall()
            for offer in expired:
                lots = json.loads(offer["lots"])
                self._put(bot, offer["seller"], offer["scope_key"], offer["item"], lots)
                self.store.db.execute(
                    "DELETE FROM kabu_offers WHERE id=?", (offer["id"],)
                )
            self._harvest(bot, now)
            debtors = self.store.db.execute(
                "SELECT user_id FROM kabu_accounts WHERE bot_id=? AND debt>0", (bot,)
            ).fetchall()
            for row in debtors:
                acct = self.account(bot, row["user_id"], now)
                self._rot(bot, acct["user_id"], now)
                # With no goods left there is nothing to sell: coins kept from check-ins
                # stay usable, as the forced-sale notice promises.
                holds = self._stock(bot, acct["user_id"]) or self._offers_by(
                    bot, acct["user_id"]
                )
                if holds and self.net_worth(acct, now) < MARGIN * acct["debt"]:
                    self._liquidate(acct, now)
                self.save(acct)

    def _liquidate(self, acct, now):
        for offer in self._offers_by(acct["bot_id"], acct["user_id"]):
            self._put(
                acct["bot_id"],
                offer["seller"],
                offer["scope_key"],
                offer["item"],
                json.loads(offer["lots"]),
            )
            self.store.db.execute("DELETE FROM kabu_offers WHERE id=?", (offer["id"],))
        income = 0
        for lot in self._stock(acct["bot_id"], acct["user_id"]):
            product = self.products.get(lot["item"])
            if product is not None:
                impact = self.impact(lot["scope_key"], lot["item"], now)
                got, after = self.sell_income(
                    self.price(product, now), impact, lot["quantity"]
                )
                income += got
                self._set_impact(lot["scope_key"], lot["item"], after, now)
            self.store.db.execute("DELETE FROM kabu_stock WHERE id=?", (lot["id"],))
        acct["coins"] += income
        paid = min(acct["coins"], acct["debt"])
        acct["coins"] -= paid
        acct["debt"] -= paid
        rest = (
            f"还欠 {acct['debt']} 金币，之后签到所得一半自动还债。"
            if acct["debt"]
            else "债务已还清。"
        )
        self._notify(
            acct, f"你资不抵债，被强制平仓：存货卖得 {income} 金币，还债 {paid}，{rest}"
        )

    @staticmethod
    def _notify(acct, text):
        acct["notice"] = (acct["notice"] + "\n" if acct["notice"] else "") + text

    # -- potatoes ---------------------------------------------------------------------

    @staticmethod
    def period(now):
        return int((now - HARVEST_EPOCH) // (HARVEST_DAYS * DAY))

    @staticmethod
    def harvest_at(period):
        return HARVEST_EPOCH + (period + 1) * HARVEST_DAYS * DAY

    @staticmethod
    def harvest_label(period):
        moment = datetime.fromtimestamp(KabuMarket.harvest_at(period), TZ)
        return f"{moment.month}月{moment.day}日晚上 8 点"

    def planting_closes_at(self, scope_key, period):
        """Candle-auction close: a secret random moment in the last PLANT_CLOSE_WINDOW.

        Plant commands are public in a group, so a fixed deadline would reward whoever
        waits to see everyone else's numbers. A random, unannounced close removes that.
        """
        offset = self.engine.rng("close", scope_key, period).uniform(
            0, PLANT_CLOSE_WINDOW
        )
        return self.harvest_at(period) - offset

    def _planted(self, bot, user):
        row = self.store.db.execute(
            "SELECT coalesce(sum(seeds),0) FROM kabu_plants WHERE bot_id=? AND user_id=?",
            (bot, user),
        ).fetchone()
        return row[0]

    @staticmethod
    def harvest_demand(players):
        return HARVEST_PER_PLAYER * players

    def active_players(self, scope_key, period):
        """Players who used /kabu in this group since the period began."""
        start = self.harvest_at(period) - HARVEST_DAYS * DAY
        return self.store.db.execute(
            "SELECT count(*) FROM kabu_players WHERE scope_key=? AND seen_at>=?",
            (scope_key, start),
        ).fetchone()[0]

    def harvest_price(self, scope_key, period, total, depth):
        """The group's price per potato before weather, and the shared weather factor."""
        weather = self.engine.rng("weather", scope_key, period).gauss(-0.011, 0.15)
        return SEED_COST * HARVEST_K * depth / (depth + total), math.exp(weather)

    def plot_price(self, scope_key, period, user, group_price, weather):
        """One planter's price: the group's, times shared weather and their own plot.

        With one price for everyone a whole group won or lost together, so nobody beat
        anybody and a loser had nothing to try differently. Each plot's luck averages 1,
        so it changes who wins this time, not who comes out ahead in the long run.
        """
        luck = self.engine.rng("plot", scope_key, period, user).gauss(-0.02, 0.2)
        return group_price * max(0.5, min(1.8, weather * math.exp(luck)))

    def _harvest(self, bot, now):
        """Inside a transaction: settle every finished period of this Bot's groups."""
        current = self.period(now)
        due = self.store.db.execute(
            "SELECT DISTINCT scope_key, period FROM kabu_plants WHERE bot_id=? AND period<?",
            (bot, current),
        ).fetchall()
        for row in due:
            key, period = row["scope_key"], row["period"]
            plants = self.store.db.execute(
                "SELECT user_id, seeds FROM kabu_plants WHERE scope_key=? AND period=?",
                (key, period),
            ).fetchall()
            total = sum(r["seeds"] for r in plants)
            players = max(self.active_players(key, period), len(plants))
            depth = self.harvest_demand(players)
            refund = len(plants) < 2
            if refund:
                price, weather = float(SEED_COST), 1.0
                each = {p["user_id"]: price for p in plants}
            else:
                price, weather = self.harvest_price(key, period, total, depth)
                each = {
                    p["user_id"]: self.plot_price(
                        key, period, p["user_id"], price, weather
                    )
                    for p in plants
                }
            best, worst = max(each.values()), min(each.values())
            for plant in plants:
                acct = self.account(bot, plant["user_id"], now)
                cost = plant["seeds"] * SEED_COST
                mine = each[plant["user_id"]]
                # rounded, not floored: flooring made a 3-seed planter at 2.05 a seed
                # get 6 coins back for 6 spent, so small stakes almost never won
                income = math.floor(plant["seeds"] * mine + 0.5)
                acct["coins"] += income
                if refund:
                    self._notify(
                        acct,
                        f"这期土豆没开张：本群只有你一个人种，{plant['seeds']} 个种薯的 {cost} 金币已退回。",
                    )
                else:
                    gain = income - cost
                    verdict = f"赚了 {gain}" if gain >= 0 else f"亏了 {-gain}"
                    self._notify(
                        acct,
                        f"土豆收获了：本群这期一共种了 {total} 个（{len(plants)} 人），"
                        f"天气系数 {weather:.2f}；各家地里收成不同，"
                        f"每个卖价最高 {fmt(best)}、最低 {fmt(worst)}。"
                        f"你种了 {plant['seeds']} 个，每个卖 {fmt(mine)}，"
                        f"收入 {income} 金币，{verdict}。",
                    )
                self.save(acct)
            self.store.db.execute(
                "INSERT OR REPLACE INTO kabu_harvests VALUES(?,?,?,?,?,?)",
                (
                    key,
                    period,
                    total,
                    len(plants),
                    sum(p["seeds"] * each[p["user_id"]] for p in plants) / total,
                    weather,
                ),
            )
            self.store.db.execute(
                "DELETE FROM kabu_plants WHERE scope_key=? AND period=?", (key, period)
            )

    def potato_public(self, scope_key, now):
        """What anyone may know: the next harvest time and the last harvest here."""
        period = self.period(now)
        last = self.store.db.execute(
            "SELECT * FROM kabu_harvests WHERE scope_key=? ORDER BY period DESC LIMIT 1",
            (scope_key,),
        ).fetchone()
        facts = {
            "seed_cost": SEED_COST,
            "next_harvest": self.harvest_label(period),
            "players_this_period": self.active_players(scope_key, period),
            "demand_per_player": HARVEST_PER_PLAYER,
        }
        if last is not None:
            facts["last_harvest"] = {
                "total_planted": last["total"],
                "planters": last["planters"],
                "price_each": round(last["price"], 2),
            }
        return facts

    def plant(self, event, args, now):
        if event.scope.kind != "group":
            return "土豆只能在群里种：收获价看全群一共种了多少，得和群友一起玩。"
        amount = self._amount(args)
        if isinstance(amount, str):
            return f"用法：/kabu plant 10（种薯 {SEED_COST} 金币一个，每期最多 {PLANT_CAP} 个）"
        bot, user, key = event.scope.bot_id, event.sender, event.scope.key
        period = self.period(now)
        if now >= self.planting_closes_at(key, period):
            return (
                f"这期已经停止播种了（停止时间每期随机，在收获前 2 小时内）。"
                f"{self.harvest_label(period)}收获后，下期再种。"
            )
        with self.store.transaction():
            acct = self.account(bot, user, now)
            row = self.store.db.execute(
                "SELECT seeds FROM kabu_plants WHERE scope_key=? AND user_id=? AND period=?",
                (key, user, period),
            ).fetchone()
            have = row["seeds"] if row else 0
            # The cap stops one rich player from flooding the whole group's harvest.
            room = PLANT_CAP - have
            if room <= 0:
                return f"这期你在本群已经种满 {PLANT_CAP} 个了，等收获吧。"
            seeds = min(room, acct["coins"] // SEED_COST) if amount is None else amount
            if amount is not None and amount > room:
                return f"这期你在本群最多还能种 {room} 个。"
            cost = seeds * SEED_COST
            if seeds <= 0 or cost > acct["coins"]:
                return f"金币不够：种 {max(seeds, 1)} 个要 {max(cost, SEED_COST)} 金币，你有 {acct['coins']}。"
            acct["coins"] -= cost
            self.save(acct)
            self.store.db.execute(
                "INSERT INTO kabu_plants VALUES(?,?,?,?,?) ON CONFLICT(scope_key,user_id,period) DO UPDATE SET seeds=seeds+excluded.seeds",
                (key, bot, user, period, seeds),
            )
        # The reply never repeats the amount: the plant command itself is the only leak,
        # and the player decides whether to recall it.
        return (
            "种好了。想让别人不知道你种了多少，可以撤回刚才那条消息。"
            f"{self.harvest_label(period)}收获；停止播种的时间每期随机，在收获前 2 小时内。"
        )

    # -- commands -----------------------------------------------------------------

    def respond(self, event):
        now = self.clock.now()
        self.maintain(event, now)
        match = COMMAND.fullmatch(event.text.strip())
        args = (match.group(1) or "").split() if match else []
        sub = args[0].lower() if args else "card"
        handlers = {
            "sign": self.checkin,
            "mkt": self.market,
            "m": self.market,
            "market": self.market,
            "bag": self.inventory,
            "rank": self.ranking,
            "loan": self.borrow,
            "repay": self.repay,
            "offer": self.offer,
            "dump": self.clear_out,
            "plant": self.plant,
            "p": self.plant,
            "种": self.plant,
            "种土豆": self.plant,
            "ok": self.accept,
            "accept": self.accept,
            "cancel": self.withdraw,
            # Chinese aliases from the first versions keep working
            "签到": self.checkin,
            "市场": self.market,
            "行情": self.market,
            "价格": self.market,
            "仓库": self.inventory,
            "背包": self.inventory,
            "钱包": self.inventory,
            "排行": self.ranking,
            "借": self.borrow,
            "借款": self.borrow,
            "还": self.repay,
            "还款": self.repay,
            "卖给": self.offer,
            "清仓": self.clear_out,
            "接受": self.accept,
            "撤回": self.withdraw,
        }
        if sub == "card":
            text = self.card(event, now)
        elif sub in {"help", "h", "帮助", "玩法"}:
            text = help_text()
        elif sub in handlers:
            text = handlers[sub](event, args[1:], now)
        elif sub in BUY_WORDS | SELL_WORDS:
            parsed = self.parse_trade(args[1:])
            if isinstance(parsed, str):
                text = parsed
            else:
                trade = self.buy if sub in BUY_WORDS else self.sell
                text = trade(event, *parsed, now)
        else:
            text = "没有这个用法。发 /kabu help 看玩法。"
        with self.store.transaction():
            acct = self.account(event.scope.bot_id, event.sender, now)
            if acct["notice"]:
                text = acct["notice"] + "\n" + text
                acct["notice"] = None
                self.save(acct)
        name = event.sender_name.strip()
        if event.scope.kind == "group" and name:
            text = "@" + name + " " + text
        return text

    def item_name(self, word):
        """Item code (d / x / m, any case) or full name -> product name, else None."""
        name = ITEM_CODES.get(word.lower(), word)
        return name if name in self.products else None

    def parse_trade(self, args):
        joined = " ".join(args)
        match = re.fullmatch(r"(\S+?)\s*(?:[*×]\s*|\s+)(\d+|全部|all)", joined, re.I)
        word, amount = (match.group(1), match.group(2)) if match else (joined, "1")
        item = self.item_name(word)
        if ITEM_CODES.get(word.lower(), word) == POTATO:
            return f"马铃薯不在市场买卖，改成种了：/kabu plant 10（种薯 {SEED_COST} 金币一个）"
        if item is None:
            names = "、".join(f"{name} {code}" for code, name in ITEM_CODES.items())
            return (
                f"没有这种商品。可以买卖：{names}。" if word else "用法：/kabu buy d 5"
            )
        if amount.lower() in ALL_WORDS:
            return item, None
        units = int(amount)
        if not 1 <= units <= MAX_UNITS:
            return f"每次数量要在 1–{MAX_UNITS} 之间。"
        return item, units

    @staticmethod
    def _amount(args):
        if not args:
            return "missing"
        if args[0].lower() in ALL_WORDS:
            return None
        return int(args[0]) if args[0].isdigit() and int(args[0]) > 0 else "invalid"

    def tip(self, now):
        """A rumour about any good; true or false with equal odds."""
        day, half = slot_of(now)
        product = self.random.choice(PRODUCTS + (None,))
        truthful = self.random.random() < TIP_TRUTH
        if product is None:  # potatoes: is this period busier than the last one?
            return self._potato_tip(now, truthful)
        if product.kind == "turnip":
            truth = self.engine.pattern(week_start(day))
            others = [p for p in range(len(TURNIP_TIPS)) if p != truth]
            said = truth if truthful else self.random.choice(others)
            return TURNIP_TIPS[said]
        now_price = self.price(product, now)
        later = self.price(product, now + 2 * DAY)
        rising = later > now_price
        if not truthful:
            rising = not rising
        word = "要涨" if rising else "要跌"
        return f"{product.name}过两天{word}"

    def _potato_tip(self, now, truthful):
        """Is this period, so far, busier than the last harvest (all groups together)?"""
        period = self.period(now)
        current = self.store.db.execute(
            "SELECT coalesce(sum(seeds),0) FROM kabu_plants WHERE period=?", (period,)
        ).fetchone()[0]
        last = self.store.db.execute(
            "SELECT coalesce(sum(total),0) FROM kabu_harvests WHERE period=?",
            (period - 1,),
        ).fetchone()[0]
        busy = current > last
        if not truthful:
            busy = not busy
        return "这期大家都在抢着种土豆" if busy else "这期没什么人种土豆"

    def checkin(self, event, _args, now):
        bot, user = event.scope.bot_id, event.sender
        today = self.day(now)
        week = week_start(today)
        lines = []
        with self.store.transaction():
            acct = self.account(bot, user, now)
            last = acct["checkin_day"]
            if last == today:
                return f"今天已经签到过了，明天再来。你现在有 {acct['coins']} 金币。"
            if last == today - 1:
                acct["streak"] += 1
            elif last == today - 2 and acct["skip_week"] != week:
                acct["streak"] += 1
                acct["skip_week"] = week
                lines.append("昨天断签，本周的一次补签已自动用掉。")
            else:
                acct["streak"] = 1
            gained = self.random.randint(*CHECKIN_RANGE)
            bonus = STREAK_BONUS[1] if acct["streak"] % STREAK_BONUS[0] == 0 else 0
            gained += bonus
            repaid = min(acct["debt"], gained // 2)
            acct["debt"] -= repaid
            acct["coins"] += gained - repaid
            acct["checkin_day"] = today
            self.save(acct)
            soon = self.store.db.execute(
                "SELECT item, sum(quantity) AS n FROM kabu_stock WHERE bot_id=? AND user_id=? AND expires_at IS NOT NULL AND expires_at>? AND expires_at<=? GROUP BY item",
                (bot, user, now, now + DAY),
            ).fetchall()
        head = f"签到成功，获得 {gained} 金币"
        if bonus:
            head += f"（含连签 {acct['streak']} 天奖励 {bonus}）"
        if repaid:
            head += f"（其中 {repaid} 自动还债）"
        head += f"，现在共有 {acct['coins']} 金币，已连签 {acct['streak']} 天。"
        lines.insert(0, head)
        for row in soon:
            lines.append(f"提醒：你有 {row['n']} 个{row['item']}一天内就要烂了。")
        if self.random.random() < TIP_CHANCE:
            lines.append("小道消息：听说" + self.tip(now) + "。")
        return "\n".join(lines)

    def card(self, event, now):
        """What a bare /kabu shows: coins, prices here, the commonest commands."""
        with self.store.transaction():
            coins = self.account(event.scope.bot_id, event.sender, now)["coins"]
        prices = "　".join(
            f"{p.name}({code_of(p.name)}) "
            + fmt_quote(*self.quote(p, event.scope.key, now))
            for p in PRODUCTS
        )
        prices += f"　{POTATO}(m) 种薯 {SEED_COST}"
        return (
            f"你有 {coins} 金币｜{prices}\n"
            "常用：/kabu sign 签到 · buy d 5 · sell d 5 · plant 5 种土豆 · market 行情\n"
            "发 /kabu help 看全部玩法"
        )

    def market(self, event, _args, now):
        _day, half = slot_of(now)
        lines = []
        for product in PRODUCTS:
            sell, buy = self.quote(product, event.scope.key, now)
            line = f"{product.name}（{code_of(product.name)}）：{fmt(sell)} 金币/个"
            if fmt(buy) != fmt(sell):
                line += f"，买入要 {fmt(buy)}（不低于本周日价）"
            lines.append(line)
        public = self.potato_public(event.scope.key, now)
        potato = (
            f"{POTATO}（m）：种薯 {SEED_COST} 金币/个，{public['next_harvest']}收获；"
            f"这期已有 {public['players_this_period']} 人在本群玩，"
            f"需求 D 目前是 {self.harvest_demand(public['players_this_period'])}"
        )
        last = public.get("last_harvest")
        if last:
            potato += f"；上期本群共种 {last['total_planted']} 个，平均每个卖 {fmt(last['price_each'])}"
        lines.append(potato)
        day = self.day(now)
        landed = self.tomato_recap(now)
        outcome = ""
        if landed:
            outcome = (
                f"已兑现：从 {fmt(landed['from'])} 到 {fmt(landed['to'])}"
                f"（{landed['change']:+.0%}）"
                + (f"，{landed['reversed']}" if landed["reversed"] else "")
            )
        for when_day, label in ((day, "今天"), (day - 1, "昨天")):
            headline = self.engine.headline(when_day)
            if headline:
                line = f"{label}的西红柿新闻：{headline}"
                if landed and landed["day"] == when_day:
                    line += f"——{outcome}"
                    landed = None
                lines.append(line)
        if landed:  # an older headline that landed since
            lines.append(
                f"{landed['published']}的西红柿新闻「{landed['headline']}」{outcome}"
            )
        recap = self.turnip_recap(now)
        if recap:
            lines.append(
                f"上周大头菜揭晓：「{recap['pattern']}」，最高 {fmt(recap['high'])}"
                f"（{recap['high_at']}），最低 {fmt(recap['low'])}（{recap['low_at']}）"
            )
        when = "上午" if half == 0 else "下午"
        return (
            f"本群{when}的市场价格：\n"
            + "\n".join(lines)
            + "\n在本群买得越多价格越高，卖得越多价格越低。发 /kabu help 看各商品的脾气。"
        )

    # -- after-the-fact reveals: a loser should see what they could have done ---------

    def turnip_recap(self, now):
        """Last week's hidden turnip pattern and its best and worst system prices."""
        sunday = week_start(self.day(now)) - 7
        if sunday < self.engine.start_week:
            return None
        base = self.products["大头菜"].base
        slots = []
        for step in range(12):  # Monday morning .. Saturday afternoon
            day, half = sunday + 1 + step // 2, step % 2
            label = f"周{WEEKDAYS[datetime.fromordinal(day).weekday()]}{'上午' if half == 0 else '下午'}"
            slots.append((self.engine.turnip(base, day, half), label))
        (high, high_at), (low, low_at) = max(slots), min(slots)
        return {
            "pattern": PATTERN_NAMES[self.engine.pattern(sunday)],
            "high": high,
            "high_at": high_at,
            "low": low,
            "low_at": low_at,
        }

    def tomato_recap(self, now):
        """The latest tomato headline of the last few days whose effect has landed.

        Only past prices are used (the day before the headline vs. the landing morning),
        so this reveals nothing a careful player could not have worked out.
        """
        today = self.day(now)
        base = self.products["西红柿"].base
        for day in range(today, today - 4, -1):
            event = self.engine.news(day)
            if event is None or event[3] > today:
                continue
            _index, sign, _effect, lands = event
            before = self.engine.tomato(base, day - 1, 1)
            after = self.engine.tomato(base, lands, 0)
            change = after / before - 1
            reversed_ = ""
            if change * sign < 0:
                reversed_ = "利好出尽，反而跌了" if sign > 0 else "利空出尽，反而涨了"
            return {
                "day": day,
                "headline": self.engine.headline(day),
                "published": {0: "今天", 1: "昨天", 2: "前天"}.get(
                    today - day, f"{today - day} 天前"
                ),
                "from": before,
                "to": after,
                "change": change,
                "reversed": reversed_,
            }
        return None

    def inventory(self, event, _args, now):
        bot, user = event.scope.bot_id, event.sender
        with self.store.transaction():
            rotten = self._rot(bot, user, now)
            acct = self.account(bot, user, now)
            self.save(acct)
            lots = self._stock(bot, user)
            worth = self.net_worth(acct, now)
        text = f"金币 {acct['coins']}"
        if acct["debt"]:
            text += f"，欠款 {acct['debt']}（日息 1%）"
        text += f"，净资产 {math.floor(worth)}（所有群合计，已扣欠款）。"
        if lots:
            text += "\n存货（跟着你走，在哪个群都能卖）："
            for lot in lots:
                line = f"\n{lot['quantity']} 个{lot['item']}"
                if lot["expires_at"] is not None:
                    line += f"，{time_left(lot['expires_at'] - now)}烂掉"
                text += line
        else:
            text += "\n没有存货。"
        planted = self.store.db.execute(
            "SELECT period, sum(seeds) AS n FROM kabu_plants WHERE bot_id=? AND user_id=? GROUP BY period",
            (bot, user),
        ).fetchall()
        for row in planted:
            text += (
                f"\n地里有 {row['n']} 个土豆，{self.harvest_label(row['period'])}收获。"
            )
        if rotten:
            gone = "、".join(f"{n} 个{i}" for i, n in rotten.items())
            text += f"\n刚刚烂掉：{gone}。"
        if not acct["coins"] and not lots and not acct["debt"]:
            text += "\n发 /kabu sign 领今天的金币。"
        return text

    def buy(self, event, item, units, now):
        product = self.products[item]
        price = self.ask(product, now)
        bot, user, key = event.scope.bot_id, event.sender, event.scope.key
        with self.store.transaction():
            acct = self.account(bot, user, now)
            impact = self.impact(key, item, now)
            if units is None:  # “全部”: as many as the coins allow
                units = 0
                while (
                    units < MAX_UNITS
                    and self.buy_cost(price, impact, units + 1)[0] <= acct["coins"]
                ):
                    units += 1
                if units == 0:
                    return f"你的 {acct['coins']} 金币一个{item}都买不起。"
            cost, after = self.buy_cost(price, impact, units)
            if cost > acct["coins"]:
                return f"金币不够：买 {units} 个{item}要 {cost} 金币，你只有 {acct['coins']} 金币。"
            acct["coins"] -= cost
            self.save(acct)
            self._set_impact(key, item, after, now)
            expires = (
                now + TURNIP_SHELF_DAYS * DAY if product.kind == "turnip" else None
            )
            lot = {"quantity": units, "bought_at": now, "expires_at": expires}
            self._put(bot, user, key, item, [lot])
        tail = f"，{TURNIP_SHELF_DAYS} 天后烂掉" if product.kind == "turnip" else ""
        return (
            f"花 {cost} 金币买了 {units} 个{item}{tail}，剩 {acct['coins']} 金币。"
            f"本群现价涨到 {fmt(price * after)}。"
        )

    def sell(self, event, item, units, now):
        product = self.products[item]
        price = self.price(product, now)
        bot, user, key = event.scope.bot_id, event.sender, event.scope.key
        with self.store.transaction():
            self._rot(bot, user, now)
            held = sum(lot["quantity"] for lot in self._stock(bot, user, item=item))
            if held == 0:
                return f"你没有可卖的{item}。"
            units = min(held, MAX_UNITS) if units is None else units
            if units > held:
                return f"你只有 {held} 个{item}。"
            self._take(bot, user, None, item, units, now)
            impact = self.impact(key, item, now)
            income, after = self.sell_income(price, impact, units)
            acct = self.account(bot, user, now)
            acct["coins"] += income
            self.save(acct)
            self._set_impact(key, item, after, now)
        return (
            f"卖出 {units} 个{item}，得到 {income} 金币，现在共有 {acct['coins']} 金币。"
            f"本群现价跌到 {fmt(price * after)}。"
        )

    def clear_out(self, event, _args, now):
        """Sell everything the player holds at this group's prices, one line per good."""
        bot, user = event.scope.bot_id, event.sender
        with self.store.transaction():
            self._rot(bot, user, now)
            goods = [p.name for p in PRODUCTS if self._stock(bot, user, item=p.name)]
        if not goods:
            return "你没有存货。"
        return "\n".join(self.sell(event, item, None, now) for item in goods)

    def borrow(self, event, args, now):
        amount = self._amount(args)
        if isinstance(amount, str):
            return "用法：/kabu loan 500（或 /kabu loan all）"
        with self.store.transaction():
            acct = self.account(event.scope.bot_id, event.sender, now)
            cap = math.floor(self.net_worth(acct, now) * (LEVERAGE - 1))
            room = max(0, cap - acct["debt"])
            amount = room if amount is None else amount
            if amount <= 0 or amount > room:
                return f"最多再借 {room} 金币（欠款合计不能超过净资产的 3 倍）。"
            acct["coins"] += amount
            acct["debt"] += amount
            acct["debt_day"] = self.day(now)
            self.save(acct)
        return (
            f"借到 {amount} 金币，现在欠款 {acct['debt']}，日息 1%。"
            f"还能接着借，直到欠款合计到净资产的 3 倍；净资产跌到欠款的 {MARGIN:.0%} 以下会被强制平仓。"
        )

    def repay(self, event, args, now):
        amount = self._amount(args)
        if isinstance(amount, str):
            return "用法：/kabu repay 500（或 /kabu repay all）"
        with self.store.transaction():
            acct = self.account(event.scope.bot_id, event.sender, now)
            if not acct["debt"]:
                return "你现在没有欠款。"
            wanted = acct["debt"] if amount is None else amount
            pay = min(acct["debt"], acct["coins"], wanted)
            if pay <= 0:
                return "你手上没有金币可以还。"
            acct["coins"] -= pay
            acct["debt"] -= pay
            self.save(acct)
        left = f"还欠 {acct['debt']}。" if acct["debt"] else "债务已还清。"
        return f"还款 {pay} 金币，{left}剩 {acct['coins']} 金币。"

    def _name(self, scope_key, user):
        row = self.store.db.execute(
            "SELECT name FROM kabu_players WHERE scope_key=? AND user_id=?",
            (scope_key, user),
        ).fetchone()
        return row["name"] if row and row["name"] else "对方"

    def offer(self, event, args, now):
        usage = "用法：/kabu offer @群友 d 5 25（商品、数量、单价；只能 @ 一位别人）"
        if event.scope.kind != "group":
            return "只能在群里卖给群友。"
        bot, seller, key = event.scope.bot_id, event.sender, event.scope.key
        targets = [m for m in event.mentions if m and m != bot]
        if len(targets) != 1 or targets[0] == seller:
            return usage
        rest = [a for a in args if a not in {"单价", "price", "@"}]
        if (
            len(rest) != 3
            or self.item_name(rest[0]) is None
            or not rest[1].isdigit()
            or not rest[2].isdigit()
        ):
            return usage
        item, units, unit_price = self.item_name(rest[0]), int(rest[1]), int(rest[2])
        if not 1 <= units <= MAX_UNITS:
            return f"每次数量要在 1–{MAX_UNITS} 之间。"
        market = self.price(self.products[item], now) * self.impact(key, item, now)
        low = math.ceil(market * TRADE_BAND[0])
        high = math.floor(market * TRADE_BAND[1])
        if not low <= unit_price <= high:
            return f"单价要在现价的 50%–200% 之间，也就是 {low}–{high}。"
        with self.store.transaction():
            self._rot(bot, seller, now)
            held = sum(lot["quantity"] for lot in self._stock(bot, seller, item=item))
            if units > held:
                return f"你只有 {held} 个{item}。"
            lots = self._take(bot, seller, None, item, units, now)
            cur = self.store.db.execute(
                "INSERT INTO kabu_offers(scope_key,bot_id,seller,buyer,item,quantity,unit_price,lots,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    bot,
                    seller,
                    targets[0],
                    item,
                    units,
                    unit_price,
                    json.dumps(lots),
                    now + OFFER_TTL,
                ),
            )
            ident = cur.lastrowid
        buyer = self._name(key, targets[0])
        return (
            f"报价 #{ident}：卖给 @{buyer} {units} 个{item}，单价 {unit_price}，"
            f"共 {units * unit_price} 金币（手续费 5% 由卖方出）。"
            f"对方发 /kabu ok {ident}，30 分钟内有效。"
        )

    def _offer_row(self, event, args):
        if not args or not args[0].lstrip("#").isdigit():
            return None
        return self.store.db.execute(
            "SELECT * FROM kabu_offers WHERE id=? AND scope_key=?",
            (int(args[0].lstrip("#")), event.scope.key),
        ).fetchone()

    def accept(self, event, args, now):
        bot, buyer, key = event.scope.bot_id, event.sender, event.scope.key
        with self.store.transaction():
            offer = self._offer_row(event, args)
            if offer is None or offer["expires_at"] <= now:
                return "没有这个报价，或者已经过期。"
            if offer["buyer"] != buyer:
                return "这个报价不是给你的。"
            total = offer["quantity"] * offer["unit_price"]
            acct = self.account(bot, buyer, now)
            if acct["coins"] < total:
                return f"金币不够：要 {total}，你只有 {acct['coins']}。"
            acct["coins"] -= total
            self.save(acct)
            seller = self.account(bot, offer["seller"], now)
            seller["coins"] += math.floor(total * (1 - TRADE_FEE))
            self.save(seller)
            self._put(bot, buyer, key, offer["item"], json.loads(offer["lots"]))
            self.store.db.execute("DELETE FROM kabu_offers WHERE id=?", (offer["id"],))
        return (
            f"成交：你花 {total} 金币买到 {offer['quantity']} 个{offer['item']}"
            f"（大头菜的保质期跟着货走），剩 {acct['coins']} 金币。"
        )

    def withdraw(self, event, args, now):
        bot, user = event.scope.bot_id, event.sender
        with self.store.transaction():
            offer = self._offer_row(event, args)
            if offer is None or offer["seller"] != user:
                return "没有你发出的这个报价。"
            lots = json.loads(offer["lots"])
            self._put(bot, user, offer["scope_key"], offer["item"], lots)
            self.store.db.execute("DELETE FROM kabu_offers WHERE id=?", (offer["id"],))
        return f"报价 #{offer['id']} 已撤回，货已退回仓库。"

    def ranking(self, event, _args, now):
        bot, key = event.scope.bot_id, event.scope.key
        rows = []
        with self.store.transaction():
            players = self.store.db.execute(
                "SELECT user_id, name FROM kabu_players WHERE scope_key=?", (key,)
            ).fetchall()
            for player in players:
                acct = self.account(bot, player["user_id"], now)
                worth = math.floor(self.net_worth(acct, now))
                rows.append((worth, player["name"] or "某位群友"))
        rows.sort(reverse=True)
        lines = [
            f"{i}. {name}：{worth}" for i, (worth, name) in enumerate(rows[:10], 1)
        ]
        return "本群净资产排行（所有群合计，已扣欠款）：\n" + "\n".join(lines)
