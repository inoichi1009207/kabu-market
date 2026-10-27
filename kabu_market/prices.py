"""Deterministic /kabu price engine: one global base path per item, two prices a day.

Everything derives from a server-local salt, so prices survive restarts and players
cannot compute them. Two shapes (independently written; ideas only, no code copied):

- turnip: a hidden weekly pattern chosen by a Markov chain on last week's pattern
  (fluctuating / large spike / decreasing / small spike);
- tomato: driven by public news whose size, timing and even direction are uncertain;

Both carry multiplicative GARCH(1,1)-t noise. alpha/beta/nu are the median fit to
10 A-share stocks (2024-03 to 2026-10 daily closes, see tools/fit_kline.py); only the scale
differs per item.
"""

import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
GARCH_ALPHA, GARCH_BETA, GARCH_NU = 0.12, 0.78, 4.2

FLUCTUATING, LARGE_SPIKE, DECREASING, SMALL_SPIKE = range(4)
PATTERN_NAMES = ("波动", "大冲高", "一路跌", "小冲高")
NEWS_CHANCE, NEWS_REVERSAL = 0.25, 0.3
# How far a headline pushes the tomato log price. With 0.2-0.55 the swings were so big
# that, prices being pulled back to the base, even random trading made money over a year
# (+53 coins on a 50-coin stake); 0.14-0.385 brings random trading to about break-even.
# Wallet-level check: tools/kabu-sim/wallet_sweep.py in the bot repo.
NEWS_SIZE = (0.14, 0.385)
NEWS_UP = (
    "产区连日暴雨，西红柿减产",
    "西红柿出口订单大增",
    "番茄炒蛋在网上爆火，西红柿需求猛涨",
    "冷库检修，西红柿上市推迟",
)
NEWS_DOWN = (
    "今年西红柿大丰收",
    "进口西红柿大批到港",
    "传闻西红柿农药超标",
    "大型商超宣布西红柿降价促销",
)
# Row = last week's pattern, column = this week's. Same idea as the turnip market,
# our own numbers: a pattern rarely repeats, and a bad week makes a spike likely.
# Large spikes are deliberately rare (see the economy simulation).
TRANSITIONS = (
    (0.29, 0.125, 0.175, 0.41),
    (0.46, 0.025, 0.2575, 0.2575),
    (0.335, 0.20, 0.13, 0.335),
    (0.465, 0.125, 0.175, 0.235),
)


FIRST_WEEK = (0.41, 0.125, 0.175, 0.29)
FIRST = 4  # SLOT_OFFSETS row for the first week, which has no previous pattern
TURNIP_PULL, TURNIP_BOUND = 0.01, 2.0  # pull per half day; |log price / base| cap
# Tomatoes are pulled back toward their base by this share per half day. A weaker pull
# was tried and made "buy deep and hold" stronger, not weaker (deeper lows), so it stays.
TOMATO_PULL = 0.05


def _spike(rng, size, steps_up):
    """Up-moves summing to ``size``, then a fall of the same size in 1-4 random steps."""
    ups = [rng.uniform(0.5, 1.5) for _ in range(steps_up)]
    downs = [rng.uniform(0.3, 1.7) for _ in range(rng.randint(1, 4))]
    return [size * u / sum(ups) for u in ups] + [-size * d / sum(downs) for d in downs]


def week_drift(pattern, rng):
    """Raw log drift of the 14 half days of a week with this pattern (Sunday is flat)."""
    body = [0.0] * 12
    if pattern == FLUCTUATING:
        for i in range(12):
            body[i] += rng.choice((0.06, -0.06))
            if rng.random() < 0.08:  # a fake breakout, given back soon after
                jump = rng.uniform(0.12, 0.25)
                body[i] += jump
                body[min(11, i + rng.randint(1, 3))] -= jump
    elif pattern == DECREASING:
        body = [-0.05] * 12
    else:
        size = (
            rng.uniform(0.6, 1.4) if pattern == LARGE_SPIKE else rng.uniform(0.2, 0.5)
        )
        moves = _spike(rng, size, rng.randint(1, 4))
        start = rng.randint(0, 12 - len(moves))
        for i, move in enumerate(moves):
            body[start + i] += move
    return [0.0, 0.0] + body


def _slot_offsets(samples=20000):
    """-E[drift of each slot | last week's pattern]: on public information no half day
    of the week leans up or down. Sampled once with a fixed seed, so it is reproducible."""
    rng = random.Random("turnip-slot-offsets")
    rows = []
    for previous in (*TRANSITIONS, FIRST_WEEK):
        total = [0.0] * 14
        for _ in range(samples):
            pattern = rng.choices(range(4), previous)[0]
            for slot, value in enumerate(week_drift(pattern, rng)):
                total[slot] += value
        rows.append(tuple(-t / samples for t in total))
    return tuple(rows)


SLOT_OFFSETS = _slot_offsets()


def slot_of(timestamp):
    """(day ordinal, half): half 0 = before noon, 1 = from noon (Shanghai time)."""
    moment = datetime.fromtimestamp(timestamp, TZ)
    return moment.date().toordinal(), int(moment.hour >= 12)


def week_start(day):
    """Ordinal of the Sunday that starts this day's week."""
    return day - (datetime.fromordinal(day).weekday() + 1) % 7


class Noise:
    """GARCH(1,1)-t log-noise per half-day slot, computed forward from the start slot."""

    def __init__(self, rng_factory, key, scale, nu=GARCH_NU):
        self.rng_factory, self.key, self.scale, self.nu = rng_factory, key, scale, nu
        self.values = []
        self.var = scale * scale
        self.last = 0.0

    def _draw(self, slot):
        rng = self.rng_factory(self.key, slot)
        chi = rng.gammavariate(self.nu / 2, 2)
        t = rng.gauss(0, 1) / math.sqrt(max(chi, 1e-12) / self.nu)
        return max(-4.0, min(4.0, t * math.sqrt((self.nu - 2) / self.nu)))

    def at(self, index, slot0):
        omega = self.scale * self.scale * (1 - GARCH_ALPHA - GARCH_BETA)
        while len(self.values) <= index:
            self.var = omega + GARCH_ALPHA * self.last**2 + GARCH_BETA * self.var
            self.last = math.sqrt(self.var) * self._draw(slot0 + len(self.values))
            self.values.append(self.last)
        return self.values[index]


class PriceEngine:
    def __init__(self, salt, start_day):
        self.salt, self.start_day = salt, start_day
        self.start_week = week_start(start_day)
        self._patterns = []
        self._tomato = []  # tomato log level per slot
        self._news = {}
        self._weeks = {}  # sunday -> the 14 calibrated half-day drifts
        self._turnip = []  # turnip log level per slot
        self._noise = {}

    def rng(self, *parts):
        return random.Random(":".join([self.salt, *map(str, parts)]))

    def _slot_index(self, day, half):
        return max(0, (day - self.start_week) * 2 + half)

    def noise_at(self, name, scale, index, nu=GARCH_NU):
        series = self._noise.setdefault(name, Noise(self.rng, name, scale, nu))
        return series.at(index, self.start_week * 2)

    def noise(self, name, scale, day, half, nu=GARCH_NU):
        return self.noise_at(name, scale, self._slot_index(day, half), nu)

    # -- turnip: a continuous walk steered by the week's hidden pattern ----------------
    #
    # Prices used to be fixed shapes relative to each Sunday, all falling on average, and
    # every Sunday restarted near the base price: so a cheap Saturday was a near-sure
    # profit overnight, and blocking that only moved the jump to another day. Now the log
    # price walks one step per half day and never resets: each step is the week's pattern
    # drift, minus the average drift of that slot (so on public information no slot has a
    # direction worth trading), a weak pull toward the base, and GARCH noise. Patterns
    # are deliberately ambiguous: fluctuating weeks have fake breakouts, and spikes vary
    # in height, length and how fast they fall back. See tools/kabu-sim/turnip_rules.py
    # (bot repo: the economy notes in docs/KABU.md).

    def pattern(self, sunday):
        index = max(0, (sunday - self.start_week) // 7)
        while len(self._patterns) <= index:
            k = len(self._patterns)
            draw = self.rng("pattern", k).random()
            if k == 0:
                row = FIRST_WEEK
            else:
                row = TRANSITIONS[self._patterns[-1]]
            total, choice = 0.0, len(row) - 1
            for i, p in enumerate(row):
                total += p
                if draw < total:
                    choice = i
                    break
            self._patterns.append(choice)
        return self._patterns[index]

    def turnip_drift(self, sunday):
        """The 14 half-day drifts (Sunday morning .. Saturday afternoon) of a week."""
        if sunday not in self._weeks:
            index = max(0, (sunday - self.start_week) // 7)
            previous = self.pattern(sunday - 7) if index else None
            drift = week_drift(self.pattern(sunday), self.rng("turnip-week", sunday))
            offset = SLOT_OFFSETS[FIRST if previous is None else previous]
            self._weeks[sunday] = [d + o for d, o in zip(drift, offset)]
            if len(self._weeks) > 64:
                self._weeks.pop(next(iter(self._weeks)))
        return self._weeks[sunday]

    def turnip(self, base, day, half):
        index = self._slot_index(day, half)
        while len(self._turnip) <= index:
            k = len(self._turnip)
            sunday = self.start_week + (k // 14) * 7
            if k == 0:
                level = math.log(self.rng("turnip-start").uniform(0.9, 1.1))
            else:
                level = self._turnip[-1]
                level += self.turnip_drift(sunday)[k % 14] - TURNIP_PULL * level
                level += self.noise_at("turnip", 0.03, k)
            self._turnip.append(max(-TURNIP_BOUND, min(TURNIP_BOUND, level)))
        return base * math.exp(self._turnip[index])

    # -- tomato: news-driven ---------------------------------------------------------

    def news(self, day):
        """The tomato headline published on ``day``, or None.

        Returns (headline index, sign of the headline, real effect, landing day). The
        headline is public; how strongly and when it lands is not, and 30% of the time
        the market "sells the news" and the price moves the other way.
        """
        if day not in self._news:
            rng = self.rng("news", day)
            event = None
            if rng.random() < NEWS_CHANCE:
                sign = rng.choice((1, -1))
                pool = NEWS_UP if sign > 0 else NEWS_DOWN
                index = rng.randrange(len(pool))
                effect = sign * rng.uniform(*NEWS_SIZE)
                if rng.random() < NEWS_REVERSAL:
                    effect = -effect * rng.uniform(0.5, 1.0)
                event = (index, sign, effect, day + rng.randint(0, 2))
            self._news[day] = event
            if len(self._news) > 256:
                self._news.pop(next(iter(self._news)))
        return self._news[day]

    def headline(self, day):
        event = self.news(day)
        if event is None:
            return None
        index, sign, _effect, _lands = event
        return (NEWS_UP if sign > 0 else NEWS_DOWN)[index]

    def _news_jump(self, day):
        return sum(
            event[2]
            for d in range(day - 2, day + 1)
            if (event := self.news(d)) is not None and event[3] == day
        )

    def tomato(self, base, day, half, *, kappa=TOMATO_PULL):
        index = self._slot_index(day, half)
        while len(self._tomato) <= index:
            k = len(self._tomato)
            slot_day, slot_half = divmod(self.start_week * 2 + k, 2)
            previous = self._tomato[-1] if self._tomato else 0.0
            step = self.noise_at("tomato", 0.05, k)
            if slot_half == 0:
                step += self._news_jump(slot_day)
            self._tomato.append(max(-1.4, min(1.4, (1 - kappa) * previous + step)))
        return base * math.exp(self._tomato[index])
