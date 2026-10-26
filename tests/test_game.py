"""Game rules, offline."""

import math
import random
from datetime import datetime

import pytest

from kabu_market import game as K
from kabu_market import Event, KabuMarket, Scope
from kabu_market.prices import PATTERN_NAMES, TZ, PriceEngine, slot_of, week_start

GROUP, OTHER = "30005", "30006"


def ev(clock, text, *, sender="u1", peer=GROUP, kind="group", name="", mentions=()):
    return Event(
        Scope("bot", kind, peer), sender, text, sender_name=name, mentions=tuple(mentions)
    )


def go(clock, weekday, hour=9):
    """Move the clock forward to the next given weekday (Mon=0 … Sun=6) at ``hour``."""
    moment = datetime.fromtimestamp(clock.now(), TZ)
    days = (weekday - moment.weekday()) % 7
    target = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    target = datetime.fromordinal(target.toordinal() + days).replace(
        hour=hour, tzinfo=TZ
    )
    if target.timestamp() <= clock.now():
        target = datetime.fromordinal(target.toordinal() + 7).replace(
            hour=hour, tzinfo=TZ
        )
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


def held(market, item, user="u1"):
    return sum(r["quantity"] for r in market._stock("bot", user, item=item))


# -- check-in ---------------------------------------------------------------------


def test_checkin_once_a_day_across_all_groups_with_streak(market, clock):
    go(clock, 0)
    assert "签到成功" in market.respond(ev(clock, "/kabu 签到"))
    first = acct(market)["coins"] - K.STARTER_COINS
    assert K.CHECKIN_RANGE[0] <= first <= K.CHECKIN_RANGE[1]
    assert "已经签到过了" in market.respond(ev(clock, "/kabu 签到", peer=OTHER))
    assert "已经签到过了" in market.respond(
        ev(clock, "/kabu 签到", kind="private", peer="u1")
    )
    clock.advance(K.DAY)
    assert "已连签 2 天" in market.respond(ev(clock, "/kabu 签到"))
    clock.advance(2 * K.DAY)  # missed one day: the weekly skip keeps the streak
    text = market.respond(ev(clock, "/kabu 签到"))
    assert "已连签 3 天" in text and "补签" in text
    clock.advance(2 * K.DAY)
    if week_start(market.day(clock.now())) == week_start(
        market.day(clock.now() - 2 * K.DAY)
    ):
        assert "已连签 1 天" in market.respond(ev(clock, "/kabu 签到"))


def test_wallet_and_goods_follow_the_player_across_groups(market, clock):
    go(clock, 6)
    give(market, 1000)
    market.respond(ev(clock, "/kabu buy d 3"))
    spent = 1000 - acct(market)["coins"]
    assert spent > 0
    text = market.respond(ev(clock, "/kabu bag", peer=OTHER))
    assert f"金币 {1000 - spent}" in text and "3 个大头菜" in text


def test_buy_in_one_group_sell_in_another_at_that_groups_price(market, clock):
    """Cross-group arbitrage is a promise in the docs: it must actually work."""
    go(clock, 1)
    give(market, 100_000)
    give(market, 100_000, "whale")
    key, other = (
        Scope("bot", "group", GROUP).key,
        Scope("bot", "group", OTHER).key,
    )
    market.respond(
        ev(clock, "/kabu buy x 200", sender="whale", peer=OTHER)
    )  # pumps OTHER
    assert market.impact(other, "西红柿", clock.now()) > market.impact(
        key, "西红柿", clock.now()
    )
    market.respond(ev(clock, "/kabu buy x 10"))  # cheap here
    pumped = market.impact(other, "西红柿", clock.now())
    text = market.respond(
        ev(clock, "/kabu sell x 10", peer=OTHER)
    )  # sell into the pumped group
    assert "卖出 10 个西红柿" in text and held(market, "西红柿") == 0
    assert (
        market.impact(other, "西红柿", clock.now()) < pumped
    )  # the arbitrage closes the gap


# -- turnip rhythm and rot ----------------------------------------------------------


def test_turnips_trade_every_day_and_rot_after_14_days(market, clock):
    give(market, 1000)
    go(clock, 2, hour=10)  # Wednesday: buy and sell on a weekday
    assert "买了 2 个大头菜" in market.respond(ev(clock, "/kabu 买 大头菜 2"))
    assert "卖出 1 个大头菜" in market.respond(ev(clock, "/kabu 卖 大头菜 1"))
    go(clock, 6, hour=10)  # Sunday: buying works too
    assert "买了 2 个大头菜" in market.respond(ev(clock, "/kabu 买 大头菜 2"))
    assert held(market, "大头菜") == 3
    clock.advance(10 * K.DAY + 3600)  # past the Wednesday lot's 14 days, not Sunday's
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
    for item in ("大头菜", "西红柿"):
        market.respond(ev(clock, f"/kabu 买 {item} 3"))
    text = market.respond(ev(clock, "/kabu 清仓"))
    for item in ("大头菜", "西红柿"):
        assert f"卖出 3 个{item}" in text and held(market, item) == 0
    assert "没有存货" in market.respond(ev(clock, "/kabu 清仓", peer=OTHER))


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
    market.respond(ev(clock, "/kabu 买 西红柿 50"))
    assert market.impact(key, "西红柿", clock.now()) == pytest.approx(K.IMPACT_STEP**50)
    market.respond(ev(clock, "/kabu 卖 西红柿 50"))
    assert acct(market)["coins"] < 100_000
    assert market.impact(key, "西红柿", clock.now()) == pytest.approx(1.0)
    for price in (2.0, 10.0, 30.0):
        for start in (K.IMPACT_BOUNDS[0], 0.9, 1.0, 3.0, K.IMPACT_BOUNDS[1]):
            for units in (1, 10, 100, 500):
                cost, after = KabuMarket.buy_cost(price, start, units)
                assert KabuMarket.sell_income(price, after, units)[0] < cost
                # the other order: selling first, then buying the same goods back
                income, low = KabuMarket.sell_income(price, start, units)
                assert KabuMarket.buy_cost(price, low, units)[0] > income


def test_impact_fades_and_is_per_group(market, clock):
    go(clock, 0)
    give(market, 100_000)
    key = Scope("bot", "group", GROUP).key
    other = Scope("bot", "group", OTHER).key
    market.respond(ev(clock, "/kabu 买 西红柿 100"))
    start = market.impact(key, "西红柿", clock.now())
    assert market.impact(other, "西红柿", clock.now()) == 1.0
    clock.advance(K.IMPACT_HALF_LIFE)
    assert market.impact(key, "西红柿", clock.now()) - 1 == pytest.approx(
        (start - 1) / 2
    )


# -- prices -------------------------------------------------------------------------


def test_prices_are_two_a_day_deterministic_and_survive_restart(market, store, clock):
    go(clock, 1, hour=9)
    tomato = market.products["西红柿"]
    morning = market.price(tomato, clock.now())
    assert market.price(tomato, clock.now() + 3600) == morning
    assert KabuMarket(store, clock).price(tomato, clock.now()) == morning
    clock.advance(6 * 3600)
    assert market.price(tomato, clock.now()) != morning


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
    market.respond(ev(clock, "/kabu 买 西红柿 全部"))
    with market.store.transaction():  # the potato price collapses
        a = market.account("bot", "u1", clock.now())
        a["debt"] = 5000
        market.save(a)
    text = market.respond(
        ev(clock, "/kabu 仓库", sender="u2")
    )  # anyone's command runs margin calls
    assert "强制平仓" not in text
    assert held(market, "西红柿") == 0
    assert "强制平仓" in market.respond(ev(clock, "/kabu 仓库"))
    debt = acct(market)["debt"]
    clock.advance(K.DAY)
    debt = acct(market)["debt"]
    market.respond(ev(clock, "/kabu 签到"))
    a = acct(market)
    assert a["debt"] < debt and a["coins"] > 0
    kept = a["coins"]
    market.respond(ev(clock, "/kabu 仓库", sender="u2"))  # nothing left to sell
    assert acct(market)["coins"] == kept


def test_goods_in_an_open_offer_still_count_and_are_sold_in_a_margin_call(
    market, clock
):
    go(clock, 0)
    give(market, 100)
    market.respond(ev(clock, "/kabu", sender="u2", name="阿二"))
    market.respond(ev(clock, "/kabu 借 100"))
    market.respond(ev(clock, "/kabu 买 西红柿 全部"))
    a = acct(market)
    worth = market.net_worth(a, clock.now())
    unit = math.ceil(market.price(market.products["西红柿"], clock.now()))
    n = held(market, "西红柿")
    market.respond(ev(clock, f"/kabu 卖给 西红柿 {n} 单价 {unit}", mentions=["u2"]))
    assert held(market, "西红柿") == 0
    assert market.net_worth(acct(market), clock.now()) == pytest.approx(worth)
    assert "强制平仓" not in market.respond(ev(clock, "/kabu 仓库", sender="u2"))
    with market.store.transaction():
        a = market.account("bot", "u1", clock.now())
        a["debt"] = 5000
        market.save(a)
    market.respond(ev(clock, "/kabu 仓库", sender="u2"))
    assert "强制平仓" in market.respond(ev(clock, "/kabu 仓库"))
    assert not market._offers_by("bot", "u1") and held(market, "西红柿") == 0
    assert "没有" in market.respond(ev(clock, "/kabu 接受 1", sender="u2"))


# -- player trades ------------------------------------------------------------------


def test_offer_escrows_goods_and_accept_moves_coins_with_fee(market, clock):
    go(clock, 0)
    give(market, 10_000, "u1")
    give(market, 10_000, "u2")
    market.respond(ev(clock, "/kabu 买 西红柿 10", sender="u1"))
    market.respond(ev(clock, "/kabu", sender="u2", name="阿二"))
    price = market.price(market.products["西红柿"], clock.now())
    unit = max(1, math.ceil(price * 0.9))
    text = market.respond(
        ev(clock, f"/kabu 卖给 西红柿 4 单价 {unit}", sender="u1", mentions=["u2"])
    )
    assert "报价 #1" in text and "@阿二" in text
    assert held(market, "西红柿", "u1") == 6
    assert "不是给你的" in market.respond(ev(clock, "/kabu 接受 1", sender="u3"))
    seller_before = acct(market, "u1")["coins"]
    assert "成交" in market.respond(ev(clock, "/kabu 接受 1", sender="u2"))
    assert held(market, "西红柿", "u2") == 4
    assert acct(market, "u2")["coins"] == 10_000 - 4 * unit
    assert acct(market, "u1")["coins"] - seller_before == math.floor(4 * unit * 0.95)


def test_offer_band_self_trade_withdraw_and_expiry(market, clock):
    go(clock, 0)
    give(market, 10_000)
    market.respond(ev(clock, "/kabu 买 西红柿 10"))
    price = market.price(market.products["西红柿"], clock.now())
    assert "50%–200%" in market.respond(
        ev(clock, "/kabu 卖给 西红柿 1 单价 99999", mentions=["u2"])
    )
    assert "用法" in market.respond(
        ev(clock, "/kabu 卖给 西红柿 1 单价 10", mentions=["u1"])
    )
    assert "用法" in market.respond(
        ev(clock, "/kabu 卖给 西红柿 1 单价 10", mentions=["bot"])
    )
    unit = math.ceil(price)
    market.respond(ev(clock, f"/kabu 卖给 西红柿 3 单价 {unit}", mentions=["u2"]))
    assert "已撤回" in market.respond(ev(clock, "/kabu 撤回 1"))
    assert held(market, "西红柿") == 10
    market.respond(ev(clock, f"/kabu 卖给 西红柿 3 单价 {unit}", mentions=["u2"]))
    clock.advance(K.OFFER_TTL + 1)
    market.respond(ev(clock, "/kabu 市场", sender="u9"))
    assert held(market, "西红柿") == 10
    assert "过期" in market.respond(ev(clock, "/kabu 接受 2", sender="u2"))
    assert "只能在群里" in market.respond(
        ev(clock, "/kabu 卖给 西红柿 1 单价 10", kind="private", peer="u1")
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
        assert market.price(market.products["西红柿"], clock.now()) > 0
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
    market.respond(ev(clock, "/kabu 买 西红柿 3"))
    facts = market.knowledge(ev(clock, "我该不该卖西红柿"))
    assert facts["asker"]["goods"] == {"西红柿": 3}
    assert "opinion" in facts["note"] and "/kabu" in facts["rules"]


def test_rumours_never_tell_players_how_reliable_they_are(market, clock, monkeypatch):
    monkeypatch.setattr(K, "TIP_CHANCE", 1.0)
    go(clock, 0)
    text = market.respond(ev(clock, "/kabu 签到"))
    assert "小道消息：听说" in text
    for hint in ("假", "不一定", "真"):
        assert hint not in text and hint not in K.help_text()


def test_short_ascii_commands_and_item_codes(market, clock):
    go(clock, 0)
    assert "签到成功" in market.respond(ev(clock, "/kabu sign"))
    give(market, 10_000)
    assert "买了 3 个大头菜" in market.respond(ev(clock, "/kabu buy d 3"))
    assert "买了 2 个西红柿" in market.respond(ev(clock, "/kabu B X*2"))
    assert "改成种了" in market.respond(ev(clock, "/kabu buy m"))
    assert "卖出 1 个大头菜" in market.respond(ev(clock, "/kabu sell d 1"))
    assert "大头菜（d）" in market.respond(ev(clock, "/kabu mkt"))
    assert "2 个大头菜" in market.respond(ev(clock, "/kabu bag"))
    assert "借到 100" in market.respond(ev(clock, "/kabu loan 100"))
    assert "还款 100" in market.respond(ev(clock, "/kabu repay all"))
    unit = math.ceil(market.price(market.products["西红柿"], clock.now()))
    text = market.respond(ev(clock, f"/kabu offer x 1 {unit}", mentions=["u2"]))
    assert "/kabu ok 1" in text
    assert "成交" in market.respond(ev(clock, "/kabu ok 1", sender="u2"))
    market.respond(
        ev(
            clock,
            f"/kabu offer x 1 {math.ceil(market.price(market.products['西红柿'], clock.now()))}",
            mentions=["u2"],
        )
    )
    assert "已撤回" in market.respond(ev(clock, "/kabu cancel 2"))
    assert "本群净资产排行" in market.respond(ev(clock, "/kabu rank"))
    assert "卖出" in market.respond(ev(clock, "/kabu dump"))
    help_text = market.respond(ev(clock, "/kabu help"))
    assert "/kabu sign" in help_text and "/kabu buy d 5" in help_text
    assert "大头菜（d）" in market.respond(ev(clock, "/kabu market"))


def test_bare_kabu_is_a_short_card_not_the_full_rules(market, clock):
    text = market.respond(ev(clock, "/kabu", sender="newbie"))
    assert "送你 100 金币" in text and "你有 100 金币" in text
    assert "大头菜(d)" in text and "/kabu help" in text
    assert len(text.splitlines()) <= 4  # welcome + three-line card
    assert "强制平仓" not in text  # the full rules live in /kabu help
    assert "没有这种商品" in market.respond(ev(clock, "/kabu buy q 1"))


# -- potatoes: a sealed planting pool -------------------------------------------------


def plant_time(market, clock):
    """A moment safely before this period's random planting close."""
    period = market.period(clock.now())
    close = market.planting_closes_at(Scope("bot", "group", GROUP).key, period)
    if clock.now() >= close:  # move into the next period's open window
        clock.timestamp = market.harvest_at(period) + 60
    return market.period(clock.now())


def to_harvest(market, clock, period):
    clock.timestamp = market.harvest_at(period) + 60


def test_tomatoes_never_rot_and_potatoes_are_not_a_market_good(market, clock):
    go(clock, 0)
    give(market, 10_000)
    market.respond(ev(clock, "/kabu buy x 2"))
    clock.advance(60 * K.DAY)
    assert "2 个西红柿" in market.respond(ev(clock, "/kabu bag"))
    assert "改成种了" in market.respond(ev(clock, "/kabu buy m 3"))
    assert "改成种了" in market.respond(ev(clock, "/kabu 买 马铃薯 3"))


def test_plant_reply_hides_the_amount_and_respects_the_cap(market, clock):
    plant_time(market, clock)
    give(market, 1000)
    text = market.respond(ev(clock, "/kabu plant 13"))
    assert "种好了" in text and "撤回" in text and "13" not in text
    assert acct(market)["coins"] == 1000 - 13 * K.SEED_COST
    assert f"最多还能种 {K.PLANT_CAP - 13}" in market.respond(
        ev(clock, f"/kabu plant {K.PLANT_CAP}")
    )
    market.respond(ev(clock, f"/kabu plant {K.PLANT_CAP - 13}"))
    assert "种满" in market.respond(ev(clock, "/kabu plant 1"))
    assert f"地里有 {K.PLANT_CAP} 个土豆" in market.respond(ev(clock, "/kabu bag"))
    assert "只能在群里种" in market.respond(
        ev(clock, "/kabu plant 1", kind="private", peer="u1")
    )


def test_harvest_prices_by_total_planted_and_tells_each_planter(market, clock):
    period = plant_time(market, clock)
    give(market, 1000, "u1")
    give(market, 1000, "u2")
    market.respond(ev(clock, "/kabu plant 40", sender="u1"))
    market.respond(ev(clock, "/kabu plant 24", sender="u2"))
    key = Scope("bot", "group", GROUP).key
    # nobody can see this period's total before the harvest
    assert "共种" not in market.respond(ev(clock, "/kabu market", sender="u3"))
    potato = market.knowledge(ev(clock, "土豆种了多少", sender="u3"))["potato"]
    assert set(potato) == {
        "seed_cost",
        "next_harvest",
        "players_this_period",
        "demand_per_player",
    }  # no running total, no last harvest yet
    assert potato["players_this_period"] == 3  # u3 plays but does not plant
    to_harvest(market, clock, period)
    depth = market.harvest_demand(3)
    assert depth == K.HARVEST_PER_PLAYER * 3
    price, weather = market.harvest_price(key, period, 64, depth)
    assert price == pytest.approx(K.SEED_COST * K.HARVEST_K * depth / (depth + 64))
    mine = {u: market.plot_price(key, period, u, price, weather) for u in ("u1", "u2")}
    for u in mine:
        assert price * 0.5 <= mine[u] <= price * 1.8
    assert mine["u1"] != mine["u2"]  # each plot grows differently
    text = market.respond(ev(clock, "/kabu bag", sender="u1"))
    assert "土豆收获了：本群这期一共种了 64 个（2 人）" in text
    assert f"最高 {K.fmt(max(mine.values()))}、最低 {K.fmt(min(mine.values()))}" in text
    assert acct(market, "u1")["coins"] == 1000 - 40 * K.SEED_COST + math.floor(
        40 * mine["u1"] + 0.5
    )
    assert acct(market, "u2")["coins"] == 1000 - 24 * K.SEED_COST + math.floor(
        24 * mine["u2"] + 0.5
    )
    assert "上期本群共种 64 个" in market.respond(ev(clock, "/kabu market"))
    assert "收获了" not in market.respond(
        ev(clock, "/kabu bag", sender="u1")
    )  # told once


def test_lonely_planter_is_refunded(market, clock):
    period = plant_time(market, clock)
    give(market, 500)
    market.respond(ev(clock, "/kabu plant 5"))
    to_harvest(market, clock, period)
    assert "已退回" in market.respond(ev(clock, "/kabu bag"))
    assert acct(market)["coins"] == 500


def test_planting_stops_at_a_secret_random_time_before_harvest(market, clock):
    period = plant_time(market, clock)
    key = Scope("bot", "group", GROUP).key
    close = market.planting_closes_at(key, period)
    assert (
        market.harvest_at(period) - K.PLANT_CLOSE_WINDOW
        <= close
        <= market.harvest_at(period)
    )
    closes = {
        round(market.planting_closes_at(key, p)) % 86400
        for p in range(period, period + 20)
    }
    assert len(closes) > 10  # different every period
    clock.timestamp = close + 1
    give(market, 500)
    assert "停止播种" in market.respond(ev(clock, "/kabu plant 3"))


def test_planted_seeds_count_in_net_worth(market, clock):
    plant_time(market, clock)
    give(market, 300)
    market.respond(ev(clock, "/kabu plant 10"))
    assert "净资产 300" in market.respond(ev(clock, "/kabu bag"))


def test_tips_cover_every_good_and_are_half_false(market, clock):
    go(clock, 1)
    market.random = random.Random(3)
    said = set()
    for _ in range(400):
        text = market.tip(clock.now())
        said.add(
            "土豆"
            if "土豆" in text
            else next(p.name for p in K.PRODUCTS if p.name in text)
        )
    assert said == {"大头菜", "西红柿", "土豆"}
    tomato = market.products["西红柿"]
    rising = market.price(tomato, clock.now() + 2 * K.DAY) > market.price(
        tomato, clock.now()
    )
    market.random = random.Random(11)
    hits = []
    for _ in range(600):
        text = market.tip(clock.now())
        if "西红柿" in text:
            hits.append(("要涨" in text) == rising)
    assert 0.4 < sum(hits) / len(hits) < 0.6


def test_demand_counts_players_of_this_period_not_seeds_planted(market, clock):
    period = plant_time(market, clock)
    key = Scope("bot", "group", GROUP).key
    for user in ("u1", "u2"):
        give(market, 1000, user)
    market.respond(ev(clock, "/kabu", sender="u3"))  # plays, plants nothing
    market.respond(ev(clock, "/kabu", sender="u4", peer=OTHER))  # another group
    assert market.active_players(key, period) == 1
    for seeds, user in ((20, "u1"), (5, "u2")):
        market.respond(ev(clock, f"/kabu plant {seeds}", sender=user))
    assert market.active_players(key, period) == 3
    assert "这期已有 3 人在本群玩，玩的人越多土豆卖得越贵" in market.respond(
        ev(clock, "/kabu market", sender="u3")
    )
    # splitting one stake over more planters adds no demand by itself
    assert KabuMarket.harvest_demand(3) == 3 * K.HARVEST_PER_PLAYER
    to_harvest(market, clock, period + 1)  # someone seen in a later period only
    assert market.active_players(key, period + 1) <= 1


def test_harvest_rounds_payouts_so_small_stakes_can_win(market, clock):
    period = plant_time(market, clock)
    give(market, 1000, "u1")
    give(market, 1000, "u2")
    market.respond(ev(clock, "/kabu plant 3", sender="u1"))
    market.respond(ev(clock, "/kabu plant 3", sender="u2"))
    key = Scope("bot", "group", GROUP).key
    to_harvest(market, clock, period)
    price, weather = market.harvest_price(key, period, 6, market.harvest_demand(2))
    mine = market.plot_price(key, period, "u1", price, weather)
    market.respond(ev(clock, "/kabu bag", sender="u1"))
    assert acct(market, "u1")["coins"] == 1000 - 6 + math.floor(3 * mine + 0.5)


def test_turnips_are_never_sold_below_this_weeks_sunday_price(market, clock):
    turnip = market.products["大头菜"]
    go(clock, 5, 9)  # a Saturday (Mon=0)
    for _ in range(30):  # find a Saturday whose price sits below its Sunday price
        sunday = market.engine.turnip(
            turnip.base, week_start(market.day(clock.now())), 0
        )
        if market.price(turnip, clock.now()) < sunday * 0.95:
            break
        clock.advance(7 * K.DAY)
    else:
        pytest.fail("no cheap Saturday in 30 weeks")
    assert market.ask(turnip, clock.now()) == pytest.approx(sunday)
    give(market, 10_000)
    market.respond(ev(clock, "/kabu buy d 1"))
    assert acct(market)["coins"] == 10_000 - math.ceil(sunday - 1e-9)
    assert "买入要" in market.respond(ev(clock, "/kabu market"))
    assert "（买 " in market.respond(ev(clock, "/kabu"))
    # tomatoes, and turnips at or above the Sunday price, cost the shown price
    tomato = market.products["西红柿"]
    assert market.ask(tomato, clock.now()) == market.price(tomato, clock.now())
    go(clock, 6, 9)  # Sunday: the buy price is today's price
    assert market.ask(turnip, clock.now()) == market.price(turnip, clock.now())
