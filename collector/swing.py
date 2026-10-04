"""
swing.py: ported verbatim from the fundamentals Screener (Stock Fundamental Analysis v3/swing.py);
only the money formatting in describe() changed. Keep the two in step.

swing.py — which names oscillate, and where they have turned before.

Every other screen in this app asks what a business is worth. This one does
not ask that at all, and saying so plainly matters: nothing here is a view on
a company. It measures how a price has behaved and where it has reversed, for
holding periods of days to weeks.

Two questions, kept separate
----------------------------
**Does this name swing?** Not "is it volatile" — a stock that falls 60% in a
straight line is extremely volatile and completely unsuited to this. What is
wanted is amplitude *without* direction: a big weekly range that keeps
returning. That is the efficiency ratio. Net distance travelled divided by
total distance travelled is near 1 for a trend and near 0 for chop, so
`swing_chop` is its complement. Paired with amplitude, because a flat stock
also goes nowhere efficiently.

**Where has it turned?** Prior swing lows, clustered into levels. A level that
has been touched once is a coincidence; the count is reported so the
difference is visible.

What this deliberately does not do
----------------------------------
It does not say a level will hold. What it says is what happened the last
times price arrived there — how many touches, and the distribution of returns
over the following two weeks. A level with three touches and a +1% median is
reported exactly as faithfully as one with six touches and +9%, because the
whole value of a base rate is destroyed the moment it is only shown when it
flatters the setup.

Forward returns are computed only from touches old enough to have resolved.
The current touch is never in its own statistics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Trading days either side of a bar that must be higher for it to be a swing
#: low. Five is about a week each way: long enough that ordinary noise does not
#: mint a pivot, short enough to find the turns inside a range rather than only
#: the major bottoms.
PIVOT_WINDOW = 5

#: Qualifying bars this many apart or fewer belong to the same visit. Small
#: on purpose: it exists to collapse a run of adjacent bars resting at the
#: bottom of a range, not to merge separate trips to the same price. Set it to
#: PIVOT_WINDOW and a stock oscillating on a six-day cycle has every trough
#: merged into one.
RUN_GAP = 3

#: Two lows this close together in price are the same level. Expressed as a
#: fraction of price rather than an absolute, and deliberately wider than a
#: tick: traders act on round numbers and prior lows approximately, not to the
#: cent, and a tolerance below ~1.5% splits one real level into three.
LEVEL_TOLERANCE = 0.025

#: How near price must be to call it "at" the level now.
AT_LEVEL = 0.04

#: Trading days measured after a touch. Two to three weeks — the horizon a
#: swing is actually held for. Both are kept because a level that works over a
#: week and fades over a month is a different animal from one that keeps going.
FORWARD_DAYS = (10, 21)

#: Bars needed before any of this is meaningful.
MIN_BARS = 180

#: Window for the swing measures. Roughly six months: long enough for several
#: complete oscillations, short enough that a name which stopped swinging a
#: year ago does not still qualify on last year's behaviour.
WINDOW = 126

#: Window for finding levels. Two years — long enough to accumulate several
#: touches, short enough that the levels are ones this market still remembers.
#: Run over the full stored history instead and a large cap accumulates twenty
#: to thirty "levels", at which point price is always near one and the measure
#: has stopped saying anything.
LEVEL_WINDOW = 504

#: Touches needed before a cluster is called a level. Two lows at a similar
#: price is where most price series spend their time; it is the third visit
#: that distinguishes a level from a coincidence, and it is also the point at
#: which the forward returns stop being an anecdote.
MIN_TOUCHES = 3

#: Beyond this, the nearest level is not information. NBIS sits 172% above its
#: nearest prior low; reporting that as "the level" would be arithmetically
#: true and completely useless, so no level is reported at all instead.
MAX_LEVEL_DISTANCE = 0.15


@dataclass
class Level:
    """One price level, and what happened the times price reached it."""
    price: float
    touches: int
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    #: Forward return after each resolved touch, keyed by horizon.
    forward: dict = field(default_factory=dict)

    def median(self, days: int) -> float | None:
        values = self.forward.get(days) or []
        return float(np.median(values)) if values else None

    def hit_rate(self, days: int) -> float | None:
        values = self.forward.get(days) or []
        return (sum(1 for v in values if v > 0) / len(values)) if values else None

    def resolved(self, days: int) -> int:
        return len(self.forward.get(days) or [])


def efficiency_ratio(close: pd.Series, window: int = WINDOW) -> float | None:
    """Net distance travelled over total distance travelled, in [0, 1].

    Kaufman's ratio. Near 1 means the move went somewhere — a trend. Near 0
    means it covered the same ground repeatedly, which is the behaviour this
    module is looking for.

    Measured on **weekly** closes, not daily. On daily bars the denominator is
    dominated by day-to-day noise that no swing is held through, and the ratio
    collapses towards zero for everything: NVDA and Coca-Cola both scored 0.90
    "choppy", which is not a measurement, it is a constant. Weekly sampling
    asks the question actually being asked — do the weekly moves go anywhere.
    """
    values = close.dropna().tail(window + 1)
    if len(values) < window // 2:
        return None
    weekly = values.resample("W").last().dropna()
    if len(weekly) < 8:
        return None
    travelled = float(weekly.diff().abs().sum())
    if travelled <= 0:
        return None
    net = abs(float(weekly.iloc[-1] - weekly.iloc[0]))
    return min(1.0, net / travelled)


def weekly_amplitude(prices: pd.DataFrame, window: int = WINDOW) -> float | None:
    """Median weekly high-to-low range, as a fraction of that week's close.

    The plain statement of "how much does it move in a week". Taken as a
    median rather than a mean so one earnings gap does not define the name.
    """
    if not {"High", "Low", "Close"} <= set(prices.columns):
        return None
    recent = prices.tail(window)
    if len(recent) < 20:
        return None
    weekly = recent.resample("W").agg({"High": "max", "Low": "min",
                                       "Close": "last"}).dropna()
    if len(weekly) < 8:
        return None
    span = (weekly["High"] - weekly["Low"]) / weekly["Close"].replace(0, np.nan)
    span = span.replace([np.inf, -np.inf], np.nan).dropna()
    return float(span.median()) if not span.empty else None


def reversals(close: pd.Series, window: int = WINDOW) -> int | None:
    """How many times the weekly direction changed. Literally down-up-down-up.

    Reported alongside the efficiency ratio rather than instead of it: this
    counts turns and ignores their size, so a name inching sideways scores
    well on it. The two together say the moves are both frequent and large.
    """
    values = close.dropna().tail(window)
    if len(values) < 20:
        return None
    weekly = values.resample("W").last().dropna()
    if len(weekly) < 6:
        return None
    direction = np.sign(weekly.diff().dropna())
    direction = direction[direction != 0]
    if len(direction) < 2:
        return 0
    return int((direction != direction.shift(1)).iloc[1:].sum())


def swing_lows(prices: pd.DataFrame, window: int = PIVOT_WINDOW) -> pd.Series:
    """Bars that are the lowest point for `window` bars either side.

    The forward half of that test is why a pivot cannot be identified until
    `window` bars after it happens. That is a property of the definition, not
    a lag to engineer away: a low is only known to be a low once price has
    left it.
    """
    if "Low" not in prices.columns:
        return pd.Series(dtype=float)
    low = pd.to_numeric(prices["Low"], errors="coerce").dropna()
    if len(low) < 2 * window + 1:
        return pd.Series(dtype=float)
    span = 2 * window + 1
    local = low.rolling(span, center=True).min()
    found = low[(low <= local) & local.notna()]
    if found.empty:
        return found

    # Adjacent bars all qualify when price rests at the bottom of a range, and
    # counting each of them makes one visit look like twenty — a flat base
    # produced 295 "touches" of a single level. A run of pivots inside one
    # window is one visit; the lowest bar in the run represents it.
    positions = {d: i for i, d in enumerate(low.index)}
    keep, run = [], []

    def close_run() -> None:
        if run:
            keep.append(min(run, key=lambda d: float(found.loc[d])))

    previous = None
    for when in found.index:
        if previous is not None and positions[when] - positions[previous] > RUN_GAP:
            close_run()
            run = []
        run.append(when)
        previous = when
    close_run()
    return found.loc[sorted(keep)]


def swing_highs(prices: pd.DataFrame, window: int = PIVOT_WINDOW) -> pd.Series:
    """Bars that are the highest point for `window` bars either side.

    The mirror of `swing_lows`, and kept separate rather than parameterised
    because what the two mean is not symmetric. A support level is where
    buyers appeared and the base rate is "what happened next". A resistance
    level is where a rally stopped — which is a target if you are already
    long, and a different trade entirely if it breaks. Reporting both under
    one name would invite reading a bounce statistic against a ceiling.
    """
    if "High" not in prices.columns:
        return pd.Series(dtype=float)
    high = pd.to_numeric(prices["High"], errors="coerce").dropna()
    if len(high) < 2 * window + 1:
        return pd.Series(dtype=float)
    span = 2 * window + 1
    local = high.rolling(span, center=True).max()
    found = high[(high >= local) & local.notna()]
    if found.empty:
        return found

    positions = {d: i for i, d in enumerate(high.index)}
    keep, run = [], []

    def close_run() -> None:
        if run:
            keep.append(max(run, key=lambda d: float(found.loc[d])))

    previous = None
    for when in found.index:
        if previous is not None and positions[when] - positions[previous] > RUN_GAP:
            close_run()
            run = []
        run.append(when)
        previous = when
    close_run()
    return found.loc[sorted(keep)]


def cluster_levels(pivots: pd.Series,
                   tolerance: float = LEVEL_TOLERANCE) -> list[Level]:
    """Group nearby pivot lows into levels, cheapest first.

    Single-linkage on price: each pivot either joins the open cluster or
    starts a new one. Simple on purpose — the alternative is a distance metric
    with parameters nobody can justify, on data where "about the same price"
    is genuinely the whole of the idea.
    """
    if pivots.empty:
        return []
    ordered = pivots.sort_values()
    levels: list[Level] = []
    bucket: list[tuple] = []

    def flush() -> None:
        if not bucket:
            return
        prices = [p for p, _ in bucket]
        dates = [d for _, d in bucket]
        levels.append(Level(price=float(np.median(prices)), touches=len(bucket),
                            first=min(dates), last=max(dates)))

    for price, when in zip(ordered.values, ordered.index):
        if bucket and price > bucket[0][0] * (1 + tolerance):
            flush()
            bucket = []
        bucket.append((float(price), when))
    flush()
    return levels


def measure_forward(level: Level, close: pd.Series, pivots: pd.Series,
                    tolerance: float = LEVEL_TOLERANCE) -> None:
    """Fill in what happened after each resolved touch of this level.

    Only touches with a full horizon of prices behind them are counted, so
    the statistics never include the touch being considered right now.
    """
    touched = pivots[(pivots >= level.price * (1 - tolerance))
                     & (pivots <= level.price * (1 + tolerance))]
    # The clustering counts pivots that chained together from the lowest one;
    # this band is centred on the level's own price and so can take in a few
    # more. The band is the honest count, because it is the one the forward
    # returns are measured over — reporting "turned here 3 times" beside
    # "7 resolved touches" invites the reader to divide one by the other.
    level.touches = max(level.touches, len(touched))
    if len(touched):
        level.first, level.last = touched.index.min(), touched.index.max()
    positions = {d: i for i, d in enumerate(close.index)}
    for days in FORWARD_DAYS:
        moves = []
        for when in touched.index:
            start = positions.get(when)
            if start is None or start + days >= len(close):
                continue
            entry = float(close.iloc[start])
            if entry <= 0:
                continue
            moves.append(float(close.iloc[start + days]) / entry - 1)
        level.forward[days] = moves


def analyse(prices: pd.DataFrame | None, close: pd.Series | None = None) -> dict:
    """Every swing measure for one name, as a plain dict.

    `close` is passed in already split-adjusted, because every measure here
    compares one date against another and a split left in the series would
    invent both the amplitude and the levels.
    """
    row: dict = {}
    if prices is None or getattr(prices, "empty", True):
        return row
    if close is None or close.empty or len(close) < MIN_BARS:
        return row

    # Levels are built on the adjusted basis too, so the highs and lows have
    # to be put on it. The ratio of adjusted to raw close on each bar is
    # exactly the adjustment that was applied to it.
    frame = prices.reindex(close.index)
    raw_close = pd.to_numeric(frame.get("Close"), errors="coerce")
    scale = (close / raw_close).replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
    adjusted = pd.DataFrame({"Close": close}, index=close.index)
    for name in ("High", "Low"):
        if name in frame.columns:
            adjusted[name] = pd.to_numeric(frame[name], errors="coerce") * scale

    ratio = efficiency_ratio(close)
    if ratio is not None:
        row["swing_efficiency"] = ratio
        row["swing_chop"] = 1.0 - ratio
    amplitude = weekly_amplitude(adjusted)
    if amplitude is not None:
        row["swing_amplitude"] = amplitude
    turns = reversals(close)
    if turns is not None:
        row["swing_reversals"] = turns

    price = float(close.iloc[-1])
    # Overhead first, and unconditionally. Support and resistance are separate
    # questions: a name can sit far from any floor it has held while having a
    # ceiling directly above it, and running this inside the support block
    # meant every such name reported no resistance at all.
    row.update(_overhead(adjusted, close, price))

    # Levels come from the recent window; the forward returns are measured
    # against the full series, so a touch near the start of the window still
    # has its outcome.
    pivots = swing_lows(adjusted.tail(LEVEL_WINDOW))
    if pivots.empty:
        row["swing_note"] = describe(row)
        return row
    levels = [lv for lv in cluster_levels(pivots) if lv.touches >= MIN_TOUCHES]
    row["bounce_levels_found"] = len(levels)
    if not levels:
        row["swing_note"] = describe(row)
        return row

    for level in levels:
        measure_forward(level, close, pivots)

    # The level price is nearest to, whether just above or just below it.
    # Restricting to levels below would miss the case that matters most —
    # price sitting a hair under one it has repeatedly bounced from.
    nearest = min(levels, key=lambda lv: abs(price / lv.price - 1))
    distance = price / nearest.price - 1
    if abs(distance) > MAX_LEVEL_DISTANCE:
        # Near nothing. The swing measures still stand on their own.
        row["swing_note"] = describe(row)
        return row
    row["bounce_level"] = nearest.price
    row["bounce_touches"] = nearest.touches
    row["dist_to_bounce"] = distance
    row["at_bounce_level"] = bool(abs(distance) <= AT_LEVEL)
    if nearest.last is not None:
        row["bounce_last_touch"] = nearest.last.date().isoformat()
    for days in FORWARD_DAYS:
        row[f"bounce_median_{days}d"] = nearest.median(days)
        row[f"bounce_hit_rate_{days}d"] = nearest.hit_rate(days)
        row[f"bounce_resolved_{days}d"] = nearest.resolved(days)

    row["swing_note"] = describe(row)
    return row


def _overhead(adjusted: pd.DataFrame, close: pd.Series, price: float) -> dict:
    """The nearest resistance above the current price.

    Deliberately thinner than the support side: no forward returns. What
    followed a *low* is a base rate for a bounce you might take. What followed
    a *high* is a mixture of two different outcomes — the rally stalled, or it
    broke through — and averaging them describes neither. So this reports
    where the ceiling is and how many times it held, and stops.

    Only levels above the price are reported. A former ceiling that price has
    already cleared is not overhead, and calling it resistance would be
    describing the past.
    """
    out: dict = {}
    peaks = swing_highs(adjusted.tail(LEVEL_WINDOW))
    if peaks.empty:
        return out
    levels = [lv for lv in cluster_levels(peaks) if lv.touches >= MIN_TOUCHES]
    out["resistance_levels_found"] = len(levels)
    above = [lv for lv in levels if lv.price > price]
    if not above:
        # At or through every ceiling it has. That is information, and the
        # absence of a number is how it is said.
        out["clear_of_resistance"] = True
        return out
    out["clear_of_resistance"] = False

    nearest = min(above, key=lambda lv: lv.price / price - 1)
    # Count touches over the same band the level was built from, so "held 4
    # times" and the level price describe the same set of bars.
    band = (nearest.price * (1 - LEVEL_TOLERANCE),
            nearest.price * (1 + LEVEL_TOLERANCE))
    touched = peaks[(peaks >= band[0]) & (peaks <= band[1])]
    out["resistance_level"] = nearest.price
    out["resistance_touches"] = max(nearest.touches, len(touched))
    out["room_to_resistance"] = nearest.price / price - 1
    if len(touched):
        out["resistance_last_touch"] = touched.index.max().date().isoformat()
    return out


def describe(row: dict) -> str:
    """One sentence saying what the numbers above amount to.

    Written to be legible without the table: the hit rate is meaningless
    without the sample size behind it, so both are always stated together.
    """

    bits = []
    amplitude = row.get("swing_amplitude")
    turns = row.get("swing_reversals")
    if amplitude is not None:
        text = f"swings {amplitude:.1%} in a typical week"
        if turns:
            text += f", changing direction {turns} times in six months"
        bits.append(text)

    level = row.get("bounce_level")
    touches = row.get("bounce_touches")
    distance = row.get("dist_to_bounce")
    if level is not None and distance is not None:
        where = ("at" if abs(distance) <= AT_LEVEL
                 else f"{abs(distance):.0%} {'above' if distance > 0 else 'below'}")
        bits.append(f"{where} a level it turned at {touches} times "
                    f"({_money(level)})")

    resolved = row.get("bounce_resolved_10d") or 0
    median = row.get("bounce_median_10d")
    rate = row.get("bounce_hit_rate_10d")
    if resolved and median is not None and rate is not None:
        bits.append(f"after the {resolved} resolved touches the next two weeks "
                    f"returned {median:+.1%} at the median, {rate:.0%} positive")

    ceiling = row.get("resistance_level")
    room = row.get("room_to_resistance")
    if ceiling is not None and room is not None:
        bits.append(f"{room:.0%} of room to the next ceiling "
                    f"({_money(ceiling)}, held "
                    f"{row.get('resistance_touches', 0)} times)")
    elif row.get("clear_of_resistance"):
        bits.append("above every level it has stalled at in two years")
    return "; ".join(bits)


def _money(v) -> str:
    return f"${v:,.2f}" if v < 1000 else f"${v:,.0f}"
