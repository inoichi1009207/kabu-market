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

from .prices import PriceEngine, slot_of, week_start

COMMAND = re.compile(r"/kabu(?:\s+(.*))?", re.S)
DAY = 86400
CHECKIN_RANGE = (10, 15)  # coins per Shanghai day, once across all groups
BUY_STEP, SELL_STEP = 1.002, 0.997  # per unit, applied after a buy / before a sell
IMPACT_HALF_LIFE = 6 * 3600
IMPACT_BOUNDS = (0.1, 10.0)
MAX_UNITS = 9999
LOAN_RATE = 0.01  # daily, compounded per Shanghai day
LEVERAGE = 4.0  # total position / net assets; debt may reach (LEVERAGE - 1) × net assets
MARGIN = 0.2  # forced sale when net worth < MARGIN × debt
TRADE_FEE = 0.05  # paid by the seller, removed from circulation
TRADE_BAND = (0.5, 2.0)  # unit price relative to the current market price
OFFER_TTL = 1800
TIP_CHANCE, TIP_TRUTH = 0.2, 0.5
STARTER_COINS = 100  # once per person (all groups), on the first /kabu
SEED_KEY = "kabu_seed_v2"
TURNIP_SHELF_DAYS = 7
STREAK_BONUS = (7, 30)  # every 7th consecutive check-in day adds 30 coins
BUY_WORDS, SELL_WORDS = {"买", "买入", "buy"}, {"卖", "卖出", "sell"}
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
    kind: str  # turnip | tomato | potato (price shapes in prices.py)
    trait: str


PRODUCTS = (
    Product(
        "大头菜",
        20.0,
        "turnip",
        "每周走势不同（这周冲高还是一路跌要自己看）；每天都能买卖，买入 7 天后烂掉",
    ),
    Product(
        "西红柿",
        30.0,
        "tomato",
        "跟着新闻走：新闻公开，但涨跌多少、哪天反映、会不会反着走都说不准；不会坏",
    ),
    Product("马铃薯", 12.0, "potato", "耐放不坏，价格大起大落，跌多了有回来的倾向"),
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


def time_left(seconds):
    if seconds >= DAY:
        return f"{int(seconds // DAY)} 天后"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} 小时后"
    return "1 小时内"


def help_text():
    goods = "\n".join(f"· {p.name}：{p.trait}" for p in PRODUCTS)
    return (
        "大头菜市场（虚拟金币，所有群通用一个钱包）\n"
        f"第一次玩送 {STARTER_COINS} 金币；/kabu 签到：每天一次，领 {CHECKIN_RANGE[0]}–{CHECKIN_RANGE[1]} 金币，偶尔附带小道消息\n"
        "/kabu 市场　/kabu 仓库　/kabu 排行\n"
        "/kabu 买 大头菜 5　/kabu 卖 大头菜 5（数量可写“全部”）\n"
        "/kabu 清仓：把本群的存货一次卖光\n"
        "/kabu 借 500　/kabu 还 500：可以多次借，欠款合计最多到净资产的 3 倍（4 倍杠杆），日息 1%，资不抵债会被强制平仓\n"
        "/kabu 卖给 @群友 大头菜 5 单价 25 → 对方 /kabu 接受 编号（卖方付 5% 手续费）\n"
        "每天上午、下午各一个价；在本群买得越多价格越高，卖得越多价格越低，波动会慢慢回落。\n"
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
                if self.store.db.in_transaction:  # e.g. first price read in a margin call
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
            impact = min(IMPACT_BOUNDS[1], impact * BUY_STEP)
        return math.ceil(total - 1e-9), impact

    @staticmethod
    def sell_income(price, impact, units):
        """Each unit first pushes the price down, then is paid at that price.

        Paying after the drop makes an immediate buy-then-sell round trip always lose.
        """
        total = 0.0
        for _ in range(units):
            impact = max(IMPACT_BOUNDS[0], impact * SELL_STEP)
            total += price * impact
        return math.floor(total + 1e-9), impact

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
        prices = {
            p.name: round(self.price(p, now) * self.impact(key, p.name, now), 2) for p in PRODUCTS
        }
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
                "your opinion on the asker's positions, framed as an opinion: you cannot see the "
                "hidden weekly pattern or how a news item will land, so never promise a future price. "
                "Players trade only by sending /kabu commands themselves: never claim you bought, "
                "sold, lent or checked in for anyone. 小道消息 rumours are unverified: never tell "
                "players how often they are true, and do not confirm or deny one. A stated "
                "investing preference may be remembered under the usual memory rules; these "
                "numbers change every half day, so do not store them as memories."
            ),
            "rules": help_text(),
            "prices_now_in_this_chat": prices,
            "tomato_news": news,
        }
        row = self.store.db.execute(
            "SELECT * FROM kabu_accounts WHERE bot_id=? AND user_id=?",
            (event.scope.bot_id, event.sender),
        ).fetchone()
        if row is not None:
            acct = self._accrue(dict(row), now)
            here = {}
            for lot in self._stock(event.scope.bot_id, event.sender, key):
                here[lot["item"]] = here.get(lot["item"], 0) + lot["quantity"]
            facts["asker"] = {
                "coins": acct["coins"],
                "debt": acct["debt"],
                "net_assets": math.floor(self.net_worth(acct, now)),
                "goods_in_this_chat": here,
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
            if product is None or (lot["expires_at"] is not None and lot["expires_at"] <= now):
                continue
            value += self.price(product, now) * lot["quantity"]
        return value

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
                self.store.db.execute("DELETE FROM kabu_offers WHERE id=?", (offer["id"],))
            debtors = self.store.db.execute(
                "SELECT user_id FROM kabu_accounts WHERE bot_id=? AND debt>0", (bot,)
            ).fetchall()
            for row in debtors:
                acct = self.account(bot, row["user_id"], now)
                self._rot(bot, acct["user_id"], now)
                if self.net_worth(acct, now) < MARGIN * acct["debt"]:
                    self._liquidate(acct, now)
                self.save(acct)

    def _liquidate(self, acct, now):
        income = 0
        for lot in self._stock(acct["bot_id"], acct["user_id"]):
            product = self.products.get(lot["item"])
            if product is not None:
                impact = self.impact(lot["scope_key"], lot["item"], now)
                got, after = self.sell_income(self.price(product, now), impact, lot["quantity"])
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
        acct["notice"] = f"你资不抵债，被强制平仓：存货卖得 {income} 金币，还债 {paid}，{rest}"

    # -- commands -----------------------------------------------------------------

    def respond(self, event):
        now = self.clock.now()
        self.maintain(event, now)
        match = COMMAND.fullmatch(event.text.strip())
        args = (match.group(1) or "").split() if match else []
        sub = args[0] if args else "帮助"
        handlers = {
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
        if sub in {"帮助", "help", "玩法"}:
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
            text = "没有这个用法。发 /kabu 看玩法。"
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

    def parse_trade(self, args):
        joined = " ".join(args)
        match = re.fullmatch(r"(\S+?)\s*(?:[*xX×]\s*|\s+)(\d+|全部|all)", joined)
        item, amount = (match.group(1), match.group(2)) if match else (joined, "1")
        if item not in self.products:
            names = "、".join(self.products)
            return f"没有这种商品。可以买卖：{names}。" if item else "用法：/kabu 买 大头菜 5"
        if amount in {"全部", "all"}:
            return item, None
        units = int(amount)
        if not 1 <= units <= MAX_UNITS:
            return f"每次数量要在 1–{MAX_UNITS} 之间。"
        return item, units

    @staticmethod
    def _amount(args):
        if not args:
            return "missing"
        if args[0] in {"全部", "all"}:
            return None
        return int(args[0]) if args[0].isdigit() and int(args[0]) > 0 else "invalid"

    def tip(self, now):
        """A rumour about any good; true or false with equal odds."""
        day, half = slot_of(now)
        product = self.random.choice(PRODUCTS)
        truthful = self.random.random() < TIP_TRUTH
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
                "SELECT item, sum(quantity) AS n FROM kabu_stock WHERE bot_id=? AND user_id=? AND scope_key=? AND expires_at IS NOT NULL AND expires_at>? AND expires_at<=? GROUP BY item",
                (bot, user, event.scope.key, now, now + DAY),
            ).fetchall()
        head = f"签到成功，获得 {gained} 金币"
        if bonus:
            head += f"（含连签 {acct['streak']} 天奖励 {bonus}）"
        if repaid:
            head += f"（其中 {repaid} 自动还债）"
        head += f"，现在共有 {acct['coins']} 金币，已连签 {acct['streak']} 天。"
        lines.insert(0, head)
        for row in soon:
            lines.append(f"提醒：你在本群有 {row['n']} 个{row['item']}一天内就要烂了。")
        if self.random.random() < TIP_CHANCE:
            lines.append("小道消息：听说" + self.tip(now) + "。")
        return "\n".join(lines)

    def market(self, event, _args, now):
        _day, half = slot_of(now)
        lines = []
        for product in PRODUCTS:
            impact = self.impact(event.scope.key, product.name, now)
            quote = self.price(product, now) * impact
            lines.append(f"{product.name}：{fmt(quote)} 金币/个")
        day = self.day(now)
        for when_day, label in ((day, "今天"), (day - 1, "昨天")):
            headline = self.engine.headline(when_day)
            if headline:
                lines.append(f"{label}的西红柿新闻：{headline}")
        when = "上午" if half == 0 else "下午"
        return (
            f"本群{when}的市场价格：\n"
            + "\n".join(lines)
            + "\n在本群买得越多价格越高，卖得越多价格越低。发 /kabu 看各商品的脾气。"
        )

    def inventory(self, event, _args, now):
        bot, user = event.scope.bot_id, event.sender
        with self.store.transaction():
            rotten = self._rot(bot, user, now)
            acct = self.account(bot, user, now)
            self.save(acct)
            lots = self._stock(bot, user, event.scope.key)
            worth = self.net_worth(acct, now)
        text = f"金币 {acct['coins']}"
        if acct["debt"]:
            text += f"，欠款 {acct['debt']}（日息 1%）"
        text += f"，净资产 {math.floor(worth)}（所有群合计，已扣欠款）。"
        if lots:
            text += "\n本群存货："
            for lot in lots:
                line = f"\n{lot['quantity']} 个{lot['item']}"
                if lot["expires_at"] is not None:
                    line += f"，{time_left(lot['expires_at'] - now)}烂掉"
                text += line
        else:
            text += "\n本群没有存货。"
        if rotten:
            gone = "、".join(f"{n} 个{i}" for i, n in rotten.items())
            text += f"\n刚刚烂掉：{gone}。"
        if not acct["coins"] and not lots and not acct["debt"]:
            text += "\n发 /kabu 签到 领今天的金币。"
        return text

    def buy(self, event, item, units, now):
        product = self.products[item]
        price = self.price(product, now)
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
            expires = now + TURNIP_SHELF_DAYS * DAY if product.kind == "turnip" else None
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
            held = sum(lot["quantity"] for lot in self._stock(bot, user, key, item))
            if held == 0:
                return f"你在本群没有可卖的{item}。"
            units = min(held, MAX_UNITS) if units is None else units
            if units > held:
                return f"你在本群只有 {held} 个{item}。"
            self._take(bot, user, key, item, units, now)
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
        """Sell everything sellable in this group right now, one line per good."""
        bot, user, key = event.scope.bot_id, event.sender, event.scope.key
        with self.store.transaction():
            self._rot(bot, user, now)
            goods = [p.name for p in PRODUCTS if self._stock(bot, user, key, p.name)]
        if not goods:
            return "你在本群没有存货。"
        return "\n".join(self.sell(event, item, None, now) for item in goods)

    def borrow(self, event, args, now):
        amount = self._amount(args)
        if isinstance(amount, str):
            return "用法：/kabu 借 500（或 /kabu 借 全部）"
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
            return "用法：/kabu 还 500（或 /kabu 还 全部）"
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
        usage = "用法：/kabu 卖给 @群友 大头菜 5 单价 25（只能 @ 一位别人）"
        if event.scope.kind != "group":
            return "只能在群里卖给群友。"
        bot, seller, key = event.scope.bot_id, event.sender, event.scope.key
        targets = [m for m in event.mentions if m and m != bot]
        if len(targets) != 1 or targets[0] == seller:
            return usage
        rest = [a for a in args if a != "单价"]
        if (
            len(rest) != 3
            or rest[0] not in self.products
            or not rest[1].isdigit()
            or not rest[2].isdigit()
        ):
            return usage
        item, units, unit_price = rest[0], int(rest[1]), int(rest[2])
        if not 1 <= units <= MAX_UNITS:
            return f"每次数量要在 1–{MAX_UNITS} 之间。"
        market = self.price(self.products[item], now) * self.impact(key, item, now)
        low = math.ceil(market * TRADE_BAND[0])
        high = math.floor(market * TRADE_BAND[1])
        if not low <= unit_price <= high:
            return f"单价要在现价的 50%–200% 之间，也就是 {low}–{high}。"
        with self.store.transaction():
            self._rot(bot, seller, now)
            held = sum(lot["quantity"] for lot in self._stock(bot, seller, key, item))
            if units > held:
                return f"你在本群只有 {held} 个{item}。"
            lots = self._take(bot, seller, key, item, units, now)
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
            f"对方发 /kabu 接受 {ident}，30 分钟内有效。"
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
        lines = [f"{i}. {name}：{worth}" for i, (worth, name) in enumerate(rows[:10], 1)]
        return "本群净资产排行（所有群合计，已扣欠款）：\n" + "\n".join(lines)
