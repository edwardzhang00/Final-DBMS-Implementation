"""Pieces every operator shares: schemas, frame accounting, temp runs, hashing, sort keys."""

import struct
from decimal import Decimal, ROUND_HALF_UP

from ..ra.operators import Column, resolve_attr, EvalError
from ..ra.tree import AttrRef
from ..storage.page import PageBuilder, check_record_fits, live_records, USABLE_SPACE, SLOT_SIZE
from ..storage.record import ColumnDef, encode


FOUR_PLACES = Decimal("0.0001")


class ExecError(Exception):
    pass


class Schema:
    """Ordered qualified columns with their types."""

    def __init__(self, columns, defs):
        self.columns = columns
        self.defs = defs

    @classmethod
    def for_table(cls, table, table_schema):
        return cls([Column(table, c.name) for c in table_schema],
                   [ColumnDef(c.name, c.type, c.length) for c in table_schema])

    def resolve(self, attr_ref):
        try:
            return resolve_attr(attr_ref, self.columns)
        except EvalError as e:
            raise ExecError(str(e)) from None

    def name(self, i):
        return self.columns[i].text

    def ref(self, i):
        column = self.columns[i]
        return AttrRef(column.rel_name, column.attr_name)

    def types(self):
        return [d.type for d in self.defs]

    def to_json(self):
        return [ColumnDef(self.name(i), d.type, d.length).to_json() for i, d in enumerate(self.defs)]

    def check_unique(self):
        seen = set()
        for i, c in enumerate(self.columns):
            key = (c.rel_name, c.attr_name)
            if key in seen:
                raise ExecError(f"Two columns named {self.name(i)} in one schema")
            seen.add(key)

    def __add__(self, other):
        return Schema(self.columns + other.columns, self.defs + other.defs)

    def __len__(self):
        return len(self.columns)


class FrameCounter:
    """How many pages a sequence of records occupies when packed into slotted pages."""

    def __init__(self):
        self.pages = 0
        self.free = 0

    def pages_with(self, length):
        return self.pages + (1 if self.pages == 0 or length + SLOT_SIZE > self.free else 0)

    def add(self, length):
        check_record_fits(length)
        if self.pages == 0 or length + SLOT_SIZE > self.free:
            self.pages += 1
            self.free = USABLE_SPACE
        self.free -= length + SLOT_SIZE

    def reset(self):
        self.pages = 0
        self.free = 0


class Run:
    """A temporary file of encoded tuples, written page by page."""

    def __init__(self, temp, count):
        self.temp = temp
        self.count = count

    @property
    def pages(self):
        return self.temp.pages


class RunWriter:
    """Packs tuples into one page image (one frame) and appends full pages to a temp file."""

    def __init__(self, op, defs):
        self.op = op
        self.defs = defs
        self.temp = op.new_temp()
        self.builder = PageBuilder()
        self.count = 0

    def add(self, row):
        record = encode(self.defs, row)
        if not self.builder.fits(len(record)):
            self.flush()
        self.builder.add(record)
        self.count += 1

    def flush(self):
        if self.builder.count:
            self.temp.append_page(self.builder.page)
            self.op.page_writes += 1
            self.builder.reset()

    def finish(self):
        self.flush()
        return Run(self.temp, self.count)


def read_run(op, run, decode):
    """Yield the tuples of a run, one page (one frame) in memory at a time."""
    for page_id in range(run.pages):
        page = run.temp.read_page(page_id)
        op.page_reads += 1
        rows = [decode(record) for _, record in live_records(page)]
        yield from rows


FNV_OFFSET = 0xcbf29ce484222325
FNV_PRIME = 0x100000001b3
MASK = 0xFFFFFFFFFFFFFFFF
INT_KEY = struct.Struct("<q")
REAL_KEY = struct.Struct("<d")


def key_bytes(values):
    parts = []
    for v in values:
        if isinstance(v, str):
            parts.append(b"S" + v.encode("utf-8") + b"\x00")
        elif isinstance(v, float) and not v.is_integer():
            parts.append(b"R" + REAL_KEY.pack(v))
        elif isinstance(v, float):
            # Equal values must hash alike: an AVG of 40.0 joins or groups with an INT 40.
            parts.append(b"I" + INT_KEY.pack(int(v)))
        else:
            parts.append(b"I" + INT_KEY.pack(v))
    return b"".join(parts)


def fnv_hash(values, seed):
    """FNV-1a over the key bytes, salted by seed. Deterministic across runs,
    unlike Python's hash() for strings."""
    h = FNV_OFFSET
    for byte in bytes([seed & 0xFF]) + key_bytes(values):
        h = ((h ^ byte) * FNV_PRIME) & MASK
    return h


def sort_key_class(descending, counter):
    """Key wrapper whose comparisons are counted into counter[0]."""

    class SortKey:
        __slots__ = ("values",)

        def __init__(self, values):
            self.values = values

        def __lt__(self, other):
            counter[0] += 1
            for a, b, desc in zip(self.values, other.values, descending):
                if a != b:
                    return a > b if desc else a < b
            return False

    return SortKey


def format_value(v):
    """REAL values (AVG results, kept at full precision) print with four decimal
    places, a half rounded away from zero as PostgreSQL's round() does. repr gives
    the shortest decimal that is this float, so 20.53125 rounds to 20.5313."""
    if isinstance(v, float):
        return str(Decimal(repr(v)).quantize(FOUR_PLACES, ROUND_HALF_UP))
    return str(v)
