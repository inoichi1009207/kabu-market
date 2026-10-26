"""Game rules, offline."""

import math
import random
from datetime import datetime

import pytest

from kabu_market import game as K
from kabu_market import Event, KabuMarket, Scope
from kabu_market.prices import TZ, PriceEngine, slot_of, week_start

GROUP, OTHER = "30005", "30006"


def ev(clock, text, *, sender="u1", peer=GROUP, kind="group", name="", mentions=()):
    return Event(Scope("bot", kind, peer), sender, text, sender_name=name, mentions=tuple(mentions))


def go(clock, weekday, hour=9):
    """Move the clock forward to the next given weekday (Mon=0 … Sun=6) at ``hour``."""
    moment = datetime.fromtimestamp(clock.now(), TZ)
    days = (weekday - moment.weekday()) % 7
    target = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    target = datetime.fromordinal(target.toordinal() + days).replace(hour=hour, tzinfo=TZ)
    if target.timestamp() <= clock.now():
        target = datetime.fromordinal(target.toordinal() + 7).replace(hour=hour, tzinfo=TZ)
    clock.timestamp = target.timestamp()


@pytest.fixture
def market(store, clock):
    return KabuMarket(store, clock, random_source=random.Random(7))


def acct(market, user="u1"):
    with market.store.transaction():
        return market.account("bot", user, market.clock.now())


def give(market, amount, user="u1"):
    with market.store.transaction():
        a = market.account("bot", user, market.clock.now())
        a["coins"] = amount
        market.save(a)


def held(market, item, user="u1", peer=GROUP):
    key = Scope("bot", "group", peer).key
    return sum(r["quantity"] for r in market._stock("bot", user, key, item))


# -- check-in ---------------------------------------------------------------------


def test_checkin_once_a_day_across_all_groups_with_streak(market, clock):
    go(clock, 0)
    assert "签到成功" in market.respond(ev(clock, "/kabu 签到"))
    first = acct(market)["coins"] - K.STARTER_COINS
    assert K.CHECKIN_RANGE[0] <= first <= K.CHECKIN_RANGE[1]
    assert "已经签到过了" in market.respond(ev(clock, "/kabu 签到", peer=OTHER))
    assert "已经签到过了" in market.respond(ev(clock, "/kabu 签到", kind="private", peer="u1"))
    clock.advance(K.DAY)
    assert "已连签 2 天" in market.respond(ev(clock, "/kabu 签到"))
    clock.advance(2 * K.DAY)  # missed one day: the weekly skip keeps the streak
    text = market.respond(ev(clock, "/kabu 签到"))
    assert "已连签 3 天" in text and "补签" in text
    clock.advance(2 * K.DAY)
    if week_start(market.day(clock.now())) == week_start(market.day(clock.now() - 2 * K.DAY)):
        assert "已连签 1 天" in market.respond(ev(clock, "/kabu 签到"))


def test_wallet_is_shared_but_goods_stay_in_their_group(market, clock):
    go(clock, 6)
    give(market, 1000)
    market.respond(ev(clock, "/kabu 买 大头菜 3"))
    assert held(market, "大头菜") == 3 and held(market, "大头菜", peer=OTHER) == 0
    spent = 1000 - acct(market)["coins"]
    assert spent > 0
    assert f"金币 {1000 - spent}" in market.respond(ev(clock, "/kabu 仓库", peer=OTHER))
    go(clock, 1)
    assert "没有可卖" in market.respond(ev(clock, "/kabu 卖 大头菜 1", peer=OTHER))


# -- turnip rhythm and rot ----------------------------------------------------------


def test_turnips_trade_every_day_and_rot_after_seven_days(market, clock):
    give(market, 1000)
    go(clock, 2, hour=10)  # Wednesday: buy and sell on a weekday
    assert "买了 2 个大头菜" in market.respond(ev(clock, "/kabu 买 大头菜 2"))
    assert "卖出 1 个大头菜" in market.respond(ev(clock, "/kabu 卖 大头菜 1"))
    go(clock, 6, hour=10)  # Sunday: buying works too
    assert "买了 2 个大头菜" in market.respond(ev(clock, "/kabu 买 大头菜 2"))
    assert held(market, "大头菜") == 3
    clock.advance(3 * K.DAY + 3600)  # past the Wednesday lot's 7 days, not Sunday's
    assert "刚刚烂掉：1 个大头菜" in market.respond(ev(clock, "/kabu 仓库"))
    assert held(market, "大头菜") == 2


def test_first_kabu_ever_grants_starter_coins_once(market, clock):
    text = market.respond(ev(clock, "/kabu", sender="new"))
    assert f"送你 {K.STARTER_COINS} 金币" in text
    assert acct(market, "new")["coins"] == K.STARTER_COINS
    other = market.respond(ev(clock, "/kabu 仓库", sender="new", peer=OTHER))
    assert "送你" not in other and f"金币 {K.STARTER_COINS}" in other


def test_clear_out_sells_everything_here(market, clock):
    go(clock, 6)
    give(market, 10_000)
    for item in ("大头菜", "西红柿", "马铃薯"):
        market.respond(ev(clock, f"/kabu 买 {item} 3"))
    text = market.respond(ev(clock, "/kabu 清仓"))
    for item in ("大头菜", "西红柿", "马铃薯"):
        assert f"卖出 3 个{item}" in text and held(market, item) == 0
    assert "没有存货" in market.respond(ev(clock, "/kabu 清仓", peer=OTHER))


def test_tomatoes_and_potatoes_never_rot(market, clock):
    go(clock, 0)
    give(market, 10_000)
    market.respond(ev(clock, "/kabu 买 西红柿 2"))
    market.respond(ev(clock, "/kabu 买 马铃薯 2"))
    clock.advance(60 * K.DAY)
    text = market.respond(ev(clock, "/kabu 仓库"))
    assert "2 个西红柿" in text and "2 个马铃薯" in text and "烂" not in text


def test_tomato_news_is_public_moves_price_but_not_always_as_headlined():
    from kabu_market.prices import NEWS_DOWN, NEWS_UP

    engine = PriceEngine("news", 740000)
    events = [(d, engine.news(d)) for d in range(740000, 740000 + 800)]
    published = [(d, e) for d, e in events if e is not None]
    assert 0.15 < len(published) / len(events) < 0.35
    reversed_ = sum(1 for _d, e in published if (e[1] > 0) != (e[2] > 0))
    assert 0.15 < reversed_ / len(published) < 0.45  # "sell the news" happens
    assert all(0 <= e[3] - d <= 2 for d, e in published)  # lands within two days
    day, event = published[0]
    assert engine.headline(day) in (NEWS_UP if event[1] > 0 else NEWS_DOWN)
    # a big real effect shows up as a jump in the price on its landing day
    big = next(e for _d, e in published if abs(e[2]) > 0.35)
    lands = big[3]
    before = engine.tomato(30, lands - 1, 1)
    after = engine.tomato(30, lands, 0)
    assert (after > before) == (big[2] > 0)


def test_market_shows_todays_tomato_news(market, clock):
    day = market.day(clock.now())
    while market.engine.headline(day) is None:
        day += 1
    clock.timestamp = datetime.fromordinal(day).replace(hour=9, tzinfo=TZ).timestamp()
    text = market.respond(ev(clock, "/kabu 市场"))
    assert "今天的西红柿新闻：" + market.engine.headline(day) in text


# -- market impact ------------------------------------------------------------------


def test_buys_push_price_up_sells_push_down_and_round_trip_loses(market, clock):
    go(clock, 0)
    give(market, 100_000)
    key = Scope("bot", "group", GROUP).key
    market.respond(ev(clock, "/kabu 买 马铃薯 50"))
    assert market.impact(key, "马铃薯", clock.now()) == pytest.approx(K.BUY_STEP**50)
    market.respond(ev(clock, "/kabu 卖 马铃薯 50"))
    assert acct(market)["coins"] < 100_000
    for units in (1, 10, 500):
        cost, after = KabuMarket.buy_cost(10.0, 1.0, units)
        assert KabuMarket.sell_income(10.0, after, units)[0] < cost


def test_impact_fades_and_is_per_group(market, clock):
    go(clock, 0)
    give(market, 100_000)
    key = Scope("bot", "group", GROUP).key
    other = Scope("bot", "group", OTHER).key
    market.respond(ev(clock, "/kabu 买 马铃薯 100"))
    start = market.impact(key, "马铃薯", clock.now())
    assert market.impact(other, "马铃薯", clock.now()) == 1.0
    clock.advance(K.IMPACT_HALF_LIFE)
    assert market.impact(key, "马铃薯", clock.now()) - 1 == pytest.approx((start - 1) / 2)


# -- prices -------------------------------------------------------------------------


def test_prices_are_two_a_day_deterministic_and_survive_restart(market, store, clock):
    go(clock, 1, hour=9)
    potato = market.products["马铃薯"]
    morning = market.price(potato, clock.now())
    assert market.price(potato, clock.now() + 3600) == morning
    assert KabuMarket(store, clock).price(potato, clock.now()) == morning
    clock.advance(6 * 3600)
    assert market.price(potato, clock.now()) != morning


def test_turnip_week_shapes_and_markov_patterns():
    engine = PriceEngine("t", 740000)
    seen = set()
    for week in range(200):
        sunday = week_start(740000) + 7 * week
        pattern = engine.pattern(sunday)
        seen.add(pattern)
        buy, seq = engine.turnip_week(sunday)
        assert 0.9 <= buy <= 1.1 and len(seq) == 12 and min(seq) >= 0.2
        if pattern == 1:
            assert max(seq) >= 2.0
        if pattern == 2:
            assert all(b < a for a, b in zip(seq, seq[1:]))
    assert seen == {0, 1, 2, 3}
    patterns = [engine.pattern(week_start(740000) + 7 * w) for w in range(200)]
    repeats = sum(a == b == 1 for a, b in zip(patterns, patterns[1:]))
    assert repeats < 10  # a large spike rarely follows a large spike


def test_tomato_and_potato_stay_in_bounds_and_move():
    engine = PriceEngine("t", 740000)
    tomatoes = [engine.tomato(30, 740000 + d, h) for d in range(400) for h in (0, 1)]
    potatoes = [engine.potato(12, 740000 + d, h) for d in range(400) for h in (0, 1)]
    assert 30 * math.exp(-1.5) < min(tomatoes) and max(tomatoes) < 30 * math.exp(1.5)
    assert 12 * math.exp(-1.7) < min(potatoes) and max(potatoes) < 12 * math.exp(1.7)
    assert max(tomatoes) / min(tomatoes) > 1.5  # news moves it, not a flat line


# -- loans --------------------------------------------------------------------------


def test_borrow_limit_interest_and_repay(market, clock):
    go(clock, 0)
    give(market, 1000)
    assert "最多再借 3000" in market.respond(ev(clock, "/kabu 借 3001"))
    assert "借到 3000" in market.respond(ev(clock, "/kabu 借 3000"))
    assert "最多再借 0" in market.respond(ev(clock, "/kabu 借 1"))
    clock.advance(2 * K.DAY)
    assert acct(market)["debt"] == math.ceil(3000 * 1.01**2)
    market.respond(ev(clock, "/kabu 还 全部"))
    assert acct(market)["debt"] == 0


def test_several_loans_until_debt_equals_net_assets(market, clock):
    go(clock, 0)
    give(market, 600)
    for _ in range(3):
        assert "还能接着借" in market.respond(ev(clock, "/kabu 借 600"))
    assert acct(market)["debt"] == 1800
    assert "最多再借 0" in market.respond(ev(clock, "/kabu 借 1"))
    assert "净资产 600" in market.respond(ev(clock, "/kabu 仓库"))


def test_margin_call_liquidates_and_checkin_repays_half(market, clock):
    go(clock, 0)
    give(market, 100)
    market.respond(ev(clock, "/kabu 借 100"))
    market.respond(ev(clock, "/kabu 买 马铃薯 全部"))
    with market.store.transaction():  # the potato price collapses
        a = market.account("bot", "u1", clock.now())
        a["debt"] = 5000
        market.save(a)
    text = market.respond(
        ev(clock, "/kabu 仓库", sender="u2")
    )  # anyone's command runs margin calls
    assert "强制平仓" not in text
    assert held(market, "马铃薯") == 0
    assert "强制平仓" in market.respond(ev(clock, "/kabu 仓库"))
    debt = acct(market)["debt"]
    clock.advance(K.DAY)
    debt = acct(market)["debt"]
    market.respond(ev(clock, "/kabu 签到"))
    a = acct(market)
    assert a["debt"] < debt and a["coins"] > 0


# -- player trades ------------------------------------------------------------------


def test_offer_escrows_goods_and_accept_moves_coins_with_fee(market, clock):
    go(clock, 0)
    give(market, 10_000, "u1")
    give(market, 10_000, "u2")
    market.respond(ev(clock, "/kabu 买 马铃薯 10", sender="u1"))
    market.respond(ev(clock, "/kabu", sender="u2", name="阿二"))
    price = market.price(market.products["马铃薯"], clock.now())
    unit = max(1, math.ceil(price * 0.9))
    text = market.respond(
        ev(clock, f"/kabu 卖给 马铃薯 4 单价 {unit}", sender="u1", mentions=["u2"])
    )
    assert "报价 #1" in text and "@阿二" in text
    assert held(market, "马铃薯", "u1") == 6
    assert "不是给你的" in market.respond(ev(clock, "/kabu 接受 1", sender="u3"))
    seller_before = acct(market, "u1")["coins"]
    assert "成交" in market.respond(ev(clock, "/kabu 接受 1", sender="u2"))
    assert held(market, "马铃薯", "u2") == 4
    assert acct(market, "u2")["coins"] == 10_000 - 4 * unit
    assert acct(market, "u1")["coins"] - seller_before == math.floor(4 * unit * 0.95)


def test_offer_band_self_trade_withdraw_and_expiry(market, clock):
    go(clock, 0)
    give(market, 10_000)
    market.respond(ev(clock, "/kabu 买 马铃薯 10"))
    price = market.price(market.products["马铃薯"], clock.now())
    assert "50%–200%" in market.respond(
        ev(clock, "/kabu 卖给 马铃薯 1 单价 99999", mentions=["u2"])
    )
    assert "用法" in market.respond(ev(clock, "/kabu 卖给 马铃薯 1 单价 10", mentions=["u1"]))
    assert "用法" in market.respond(ev(clock, "/kabu 卖给 马铃薯 1 单价 10", mentions=["bot"]))
    unit = math.ceil(price)
    market.respond(ev(clock, f"/kabu 卖给 马铃薯 3 单价 {unit}", mentions=["u2"]))
    assert "已撤回" in market.respond(ev(clock, "/kabu 撤回 1"))
    assert held(market, "马铃薯") == 10
    market.respond(ev(clock, f"/kabu 卖给 马铃薯 3 单价 {unit}", mentions=["u2"]))
    clock.advance(K.OFFER_TTL + 1)
    market.respond(ev(clock, "/kabu 市场", sender="u9"))
    assert held(market, "马铃薯") == 10
    assert "过期" in market.respond(ev(clock, "/kabu 接受 2", sender="u2"))
    assert "只能在群里" in market.respond(
        ev(clock, "/kabu 卖给 马铃薯 1 单价 10", kind="private", peer="u1")
    )


# -- ranking, tips, syntax ----------------------------------------------------------


def test_ranking_orders_group_players_by_net_worth(market, clock):
    go(clock, 0)
    give(market, 500, "u1")
    give(market, 900, "u2")
    market.respond(ev(clock, "/kabu", sender="u1", name="甲"))
    market.respond(ev(clock, "/kabu", sender="u2", name="乙"))
    market.respond(ev(clock, "/kabu", sender="u3", name="丙", peer=OTHER))
    text = market.respond(ev(clock, "/kabu 排行"))
    assert text.index("乙：900") < text.index("甲：500") and "丙" not in text


def test_tips_cover_every_good_and_are_half_false(market, clock):
    go(clock, 1)
    seen, truths = set(), 0
    rng = random.Random(3)
    market.random = rng
    for _ in range(400):
        text = market.tip(clock.now())
        seen.add(next(p.name for p in K.PRODUCTS if p.name in text))
    assert seen == {p.name for p in K.PRODUCTS}
    # potato/tomato rumours: about half say the opposite of what actually happens
    potato = market.products["马铃薯"]
    rising = market.price(potato, clock.now() + 2 * K.DAY) > market.price(potato, clock.now())
    market.random = random.Random(11)
    said = []
    for _ in range(600):
        text = market.tip(clock.now())
        if "马铃薯" in text:
            said.append(("要涨" in text) == rising)
    truths = sum(said) / len(said)
    assert 0.4 < truths < 0.6


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/kabu 买 大头菜*3", ("大头菜", 3)),
        ("/kabu 买 大头菜 3", ("大头菜", 3)),
        ("/kabu 买 大头菜", ("大头菜", 1)),
        ("/kabu 卖 西红柿 全部", ("西红柿", None)),
    ],
)
def test_trade_syntax(market, text, expected):
    assert market.parse_trade(text.split()[2:]) == expected


def test_bad_input_mention_prefix_and_matching(market, clock):
    assert "没有这种商品" in market.respond(ev(clock, "/kabu 买 霓裳花 1"))
    assert "没有这个用法" in market.respond(ev(clock, "/kabu 抽奖"))
    assert market.respond(ev(clock, "/kabu", name="阿菜")).startswith("@阿菜 ")
    private = ev(clock, "/kabu", kind="private", peer="u1", name="阿菜")
    assert not market.respond(private).startswith("@")
    assert KabuMarket.matches("/kabu") and KabuMarket.matches(" /kabu 市场 ")
    assert not KabuMarket.matches("/kabuu") and not KabuMarket.matches("kabu")
    assert slot_of(clock.now())[1] in (0, 1)


def test_first_price_read_inside_a_transaction_creates_the_seed(store, clock):
    market = KabuMarket(store, clock)
    with store.transaction():
        assert market.price(market.products["马铃薯"], clock.now()) > 0
    assert store.get(K.SEED_KEY)["salt"]


def test_seventh_consecutive_checkin_pays_the_streak_bonus(market, clock):
    go(clock, 0)
    for day in range(1, 8):
        text = market.respond(ev(clock, "/kabu 签到"))
        assert ("连签 7 天奖励 30" in text) == (day == 7)
        clock.advance(K.DAY)


def test_knowledge_is_read_only_and_describes_this_chat(market, store, clock):
    facts = market.knowledge(ev(clock, "大头菜现在多少钱", sender="ghost"))
    assert "asker" not in facts and set(facts["prices_now_in_this_chat"]) == {
        p.name for p in K.PRODUCTS
    }
    assert store.db.execute("SELECT count(*) FROM kabu_accounts").fetchone()[0] == 0
    give(market, 500)
    market.respond(ev(clock, "/kabu 买 马铃薯 3"))
    facts = market.knowledge(ev(clock, "我该不该卖马铃薯"))
    assert facts["asker"]["goods_in_this_chat"] == {"马铃薯": 3}
    assert "opinion" in facts["note"] and "/kabu" in facts["rules"]
