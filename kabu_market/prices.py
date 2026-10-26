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
# Large spikes are deliberately rare: with a 14-day shelf life, holding for a spike
# must stay a gamble, not a sure win (see the economy simulation).
TRANSITIONS = (
    (0.29, 0.125, 0.175, 0.41),
    (0.46, 0.025, 0.2575, 0.2575),
    (0.335, 0.20, 0.13, 0.335),
    (0.465, 0.125, 0.175, 0.235),
)


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
        self._weeks = {}
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

    # -- turnip -------------------------------------------------------------------

    def pattern(self, sunday):
        index = max(0, (sunday - self.start_week) // 7)
        while len(self._patterns) <= index:
            k = len(self._patterns)
            draw = self.rng("pattern", k).random()
            if k == 0:
                row = (0.41, 0.125, 0.175, 0.29)
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

    def turnip_week(self, sunday):
        """Sunday buy multiplier and the 12 Mon–Sat sell multipliers of that week."""
        if sunday in self._weeks:
            return self._weeks[sunday]
        rng = self.rng("turnip-week", sunday)
        u = rng.uniform
        pattern = self.pattern(sunday)
        buy = u(0.9, 1.1)
        seq = []
        if pattern == FLUCTUATING:
            # high, falling, high, falling, high: the five run lengths sum to 12
            hi1 = rng.randint(0, 6)
            lo1 = rng.choice((2, 3))
            hi3 = rng.randint(0, 6 - hi1)
            runs = ((hi1, True), (lo1, False), (7 - hi1 - hi3, True))
            runs += ((5 - lo1, False), (hi3, True))
            for count, high in runs:
                level = u(0.6, 0.8)
                for _ in range(count):
                    if high:
                        seq.append(u(0.9, 1.4))
                    else:
                        seq.append(level)
                        level -= u(0.04, 0.10)
        elif pattern == LARGE_SPIKE:
            start = rng.randint(1, 7)
            level = u(0.85, 0.9)
            for _ in range(start):
                seq.append(level)
                level -= u(0.03, 0.05)
            seq += [u(0.9, 1.4), u(1.4, 2.0), u(2.0, 6.0), u(1.4, 2.0), u(0.9, 1.4)]
            while len(seq) < 12:
                seq.append(u(0.4, 0.9))
        elif pattern == DECREASING:
            level = u(0.85, 0.9)
            for _ in range(12):
                seq.append(level)
                level -= u(0.03, 0.05)
        else:
            start = rng.randint(0, 7)
            level = u(0.4, 0.9)
            for _ in range(start):
                seq.append(level)
                level -= u(0.03, 0.05)
            peak = u(1.4, 2.0)
            seq += [
                u(0.9, 1.4),
                u(0.9, 1.4),
                peak * u(0.85, 1.0),
                peak,
                peak * u(0.85, 1.0),
            ]
            level = u(0.4, 0.9)
            while len(seq) < 12:
                seq.append(level)
                level -= u(0.03, 0.05)
        seq = [max(0.2, v) for v in seq[:12]]
        while len(seq) < 12:
            seq.append(seq[-1] if seq else 0.8)
        self._weeks[sunday] = (buy, seq)
        if len(self._weeks) > 64:
            self._weeks.pop(next(iter(self._weeks)))
        return self._weeks[sunday]

    def turnip(self, base, day, half):
        sunday = week_start(day)
        buy, seq = self.turnip_week(sunday)
        offset = day - sunday
        weekly = buy if offset == 0 else buy * seq[(offset - 1) * 2 + half]
        return base * weekly * math.exp(self.noise("turnip", 0.03, day, half))

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
                effect = sign * rng.uniform(0.2, 0.55)
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

    def tomato(self, base, day, half, *, kappa=0.05):
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
