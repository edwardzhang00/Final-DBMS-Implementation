"""Cardinality estimation: per-column statistics carried up the plan and the
selectivity formulas of DESIGN_M4.md section 2.

Without ANALYZE statistics the catalog still knows each table's tuple and
page counts; columns then use the System R defaults below.
"""

import math

from ..ra.operators import COMPARE, NameError_
from ..ra.tree import And, Or, Not, AttrRef, is_equality
from ..storage.disk import PAGE_SIZE
from ..storage.record import FIXED_WIDTH, LENGTH

DEFAULT_EQUALITY = 0.1
DEFAULT_RANGE = 1 / 3
DEFAULT_JOIN_OTHER = 0.1


def ceil_rows(x):
    """Round up, never below 1, ignoring floating point noise (0.02 * 0.2 * 10000)."""
    return max(1, math.ceil(x - 1e-9))


def pages_for(rows, width):
    return max(1, math.ceil(rows * width / PAGE_SIZE - 1e-9))


class ColumnEstimate:
    """What is known about one output column."""

    def __init__(self, type, width, ndv=None, low=None, high=None, buckets=(), base_tuples=None):
        self.type = type
        self.width = width
        self.ndv = ndv
        self.low = low
        self.high = high
        self.buckets = buckets
        self.base_tuples = base_tuples

    def capped(self, rows):
        """The same column after a filter left `rows` tuples."""
        ndv = None if self.ndv is None else max(1, min(self.ndv, rows))
        return ColumnEstimate(self.type, self.width, ndv, self.low, self.high, self.buckets, self.base_tuples)


class Estimate:
    def __init__(self, rows, columns, pages=None):
        self.rows = rows
        self.columns = columns
        self.width = sum(c.width for c in columns)
        self.pages = pages_for(rows, self.width) if pages is None else pages


def default_width(column):
    if column.type == "VARCHAR":
        return LENGTH.size + column.length / 2
    return FIXED_WIDTH[column.type]


def table_estimate(info):
    """A seqscan: exact counts from ANALYZE, or the catalog's counts and defaults."""
    stats = info.stats
    if stats is None:
        columns = [ColumnEstimate(c.type, default_width(c)) for c in info.schema]
        return Estimate(max(1, info.num_tuples), columns, max(1, info.data_pages))
    columns = []
    for c, s in zip(info.schema, stats.columns):
        columns.append(ColumnEstimate(c.type, s.avg_width or default_width(c), s.ndv or None, s.min, s.max,
                                      s.buckets, stats.n_tuples))
    return Estimate(max(1, stats.n_tuples), columns, max(1, stats.n_pages))


def as_number(value):
    """Map a value onto a line so strings can be interpolated (first 8 bytes, big-endian)."""
    if isinstance(value, str):
        return int.from_bytes(value.encode("utf-8")[:8].ljust(8, b"\0"), "big")
    return value


def fraction_below(stat, v):
    """Estimated fraction of tuples with value < v (continuous model)."""
    if stat.buckets:
        total = sum(b.count for b in stat.buckets)
        below = 0.0
        x = as_number(v)
        for b in stat.buckets:
            lo, hi = as_number(b.lo), as_number(b.hi)
            if hi < x:
                below += b.count
            elif lo < x <= hi:
                below += b.count * (x - lo) / (hi - lo)
        return below / total
    if stat.low is not None:
        lo, hi, x = as_number(stat.low), as_number(stat.high), as_number(v)
        if hi == lo:
            return 0.0 if x <= lo else 1.0
        return min(1.0, max(0.0, (x - lo) / (hi - lo)))
    return None


def equality_selectivity(stat, v):
    if stat.buckets:
        if v < stat.low or v > stat.high:
            return 0.0
        total = 0.0
        for b in stat.buckets:
            if b.lo <= v <= b.hi:
                total += b.count / (stat.base_tuples * b.ndv)
        return min(1.0, total)
    if stat.ndv:
        return 1 / stat.ndv
    return DEFAULT_EQUALITY


def comparison_selectivity(op, stat, v):
    if op == "=":
        return equality_selectivity(stat, v)
    if op == "!=":
        return 1 - equality_selectivity(stat, v)
    below = fraction_below(stat, v)
    if below is None:
        return DEFAULT_RANGE
    return below if op in ("<", "<=") else 1 - below


def known(lookup, ref):
    stat = lookup(ref)
    if stat is None:
        raise NameError_(f"Unknown attribute {ref.text}")
    return stat


def selectivity(cond, lookup):
    """lookup(AttrRef) -> ColumnEstimate of that column in the input, or None."""
    if isinstance(cond, And):
        return selectivity(cond.left, lookup) * selectivity(cond.right, lookup)
    if isinstance(cond, Or):
        p, q = selectivity(cond.left, lookup), selectivity(cond.right, lookup)
        return p + q - p * q
    if isinstance(cond, Not):
        return 1 - selectivity(cond.cond, lookup)
    left, right, op = cond.left, cond.right, cond.op
    if isinstance(left, AttrRef) and isinstance(right, AttrRef):
        a, b = known(lookup, left), known(lookup, right)
        if op == "=":
            return 1 / max(a.ndv or 1 / DEFAULT_EQUALITY, b.ndv or 1 / DEFAULT_EQUALITY)
        if op == "!=":
            return 1 - 1 / max(a.ndv or 1 / DEFAULT_EQUALITY, b.ndv or 1 / DEFAULT_EQUALITY)
        return DEFAULT_RANGE
    if not isinstance(left, AttrRef):
        if not isinstance(right, AttrRef):
            return 1.0 if COMPARE[op](left.value, right.value) else 0.0
        left, right, op = right, left, {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
    return comparison_selectivity(op, known(lookup, left), right.value)


def join_selectivity(cond, left_lookup, right_lookup):
    """A predicate between two inputs. An equality is 1 / max(ndv), or None when a
    side has no ndv; anything else is DEFAULT_JOIN_OTHER."""
    if is_equality(cond):
        sides = []
        for ref in (cond.left, cond.right):
            stat = known(lambda r: left_lookup(r) or right_lookup(r), ref)
            sides.append(stat.ndv if stat.ndv else None)
        if None in sides:
            return None
        return 1 / max(sides)
    return DEFAULT_JOIN_OTHER
