"""Iterator operators: open / next / close / counters.

Frame budget. The whole plan gets B frames. Every operator receives two
numbers for itself and its subtree (configure):
  open_frames    what it may hold during open(), where blocking work happens.
                 A parent opens its input before reserving anything itself, so
                 an input's blocking work gets the parent's whole budget.
  stream_frames  what it may hold while it returns tuples, alongside the
                 buffers its parent holds at the same time.
A pipeline (seqscans under filter, project, rename, limit and union_all) holds
one frame, the page its scan has pinned. A blocking operator lets a blocking
input stream with half of its budget (at least the input's minimum) and keeps
the rest. Its own buffers are reserved from the buffer pool, which never lets
pinned pages plus reserved frames exceed B.
"""

from ..ra.operators import Column, compile_condition, type_class, EvalError
from ..storage.catalog import CatalogError
from ..storage.record import ColumnDef, common_definition, decoder
from .runtime import ExecError, Schema, RunWriter, read_run


class Operator:
    op = None
    pipelined = True

    def __init__(self, ctx, path, children):
        self.ctx = ctx
        self.path = path
        self.children = children
        self.tuples_out = self.comparisons = self.page_reads = self.page_writes = 0
        self.schema = None
        self.open_frames = self.stream_frames = None
        self.reserved = 0
        self.temps = []
        self.internals = []
        self.gen = None
        self.is_open = False

    def bind(self):
        for child in self.children:
            child.bind()
        self.bind_self()

    def mark_readers(self, reader_frames, may_spill):
        """reader_frames: how many more frames the operator reading this one's tuples
        holds while they stream than while this one opens. A blocking operator holds
        at least one buffer frame while its input streams; a pipelined one passes on
        what its own reader holds. may_spill: whether a union_all may copy its right
        input to a temporary file to need fewer frames."""
        for child in self.children:
            child.mark_readers(reader_frames if self.pipelined else 1, may_spill)

    def bind_self(self):
        try:
            self.schema = self.derive_schema()
            self.schema.check_unique()
        except (ExecError, EvalError) as e:
            raise ExecError(f"Node '{self.op}': {e} [at {self.path}]") from None

    def derive_schema(self):
        return self.children[0].schema

    def is_pipeline(self):
        return self.pipelined and all(child.is_pipeline() for child in self.children)

    def min_open(self):
        return max(child.min_open() for child in self.children)

    def min_stream(self):
        return max(child.min_stream() for child in self.children)

    def min_frames(self):
        """B this operator needs as the root of a plan."""
        return max(self.min_open(), self.min_stream())

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        for child in self.children:
            child.configure(open_frames, stream_frames)

    def open(self):
        self.is_open = True
        for child in self.children:
            child.open()
        self.gen = self.produce()

    def next(self):
        row = next(self.gen, None)
        if row is not None:
            self.tuples_out += 1
        return row

    def close(self):
        if not self.is_open:
            return
        self.is_open = False
        if self.gen is not None:
            self.gen.close()
        if self.reserved:
            self.release(self.reserved)
        for temp in list(self.temps):
            self.drop_temp(temp)
        for internal in self.internals:
            internal.close()
        for child in self.children:
            child.close()

    def counters(self):
        """This node's work, including the helpers (such as sorts) it runs internally."""
        out = {"tuples_out": self.tuples_out, "comparisons": self.comparisons,
               "page_reads": self.page_reads, "page_writes": self.page_writes}
        for internal in self.internals:
            inner = internal.counters()
            for key in ("comparisons", "page_reads", "page_writes"):
                out[key] += inner[key]
        return out

    def label(self):
        return self.op

    def reserve(self, count):
        self.ctx.pool.reserve(count)
        self.reserved += count

    def release(self, count):
        self.ctx.pool.release(count)
        self.reserved -= count

    def new_temp(self):
        temp = self.ctx.new_temp()
        self.temps.append(temp)
        return temp

    def write_run(self, rows, defs):
        """Write rows to a new temporary file through one page image; the caller holds its frame."""
        writer = RunWriter(self, defs)
        for row in rows:
            writer.add(row)
        return writer.finish()

    def materialize(self, child):
        """Copy a child's whole output to a temporary file through one writer frame."""
        child.open()
        self.reserve(1)
        run = self.write_run(drain(child), child.schema.defs)
        self.release(1)
        child.close()
        return run

    def drop_temp(self, temp):
        self.temps.remove(temp)
        temp.delete()


def share(child, frames):
    """Frames an input may hold while it streams into a blocking operator with `frames`."""
    if child.is_pipeline():
        return 1
    return max(child.min_stream(), frames // 2)


def drain(op):
    while True:
        row = op.next()
        if row is None:
            return
        yield row


class SeqScan(Operator):
    op = "seqscan"

    def __init__(self, ctx, path, table):
        super().__init__(ctx, path, [])
        self.table = table

    def derive_schema(self):
        try:
            self.heap = self.ctx.db.heap_file(self.table)
        except CatalogError as e:
            raise ExecError(str(e)) from None
        return Schema.for_table(self.table, self.heap.info.schema)

    def min_open(self):
        return 1

    def min_stream(self):
        return 1

    def label(self):
        return f"seqscan {self.table}"

    def produce(self):
        pool = self.ctx.pool
        scan = self.heap.scan()
        try:
            while True:
                reads, writes = pool.reads, pool.writes
                item = next(scan, None)
                self.page_reads += pool.reads - reads
                self.page_writes += pool.writes - writes
                if item is None:
                    return
                yield item[1]
        finally:
            scan.close()


class Filter(Operator):
    op = "filter"

    def __init__(self, ctx, path, child, condition):
        super().__init__(ctx, path, [child])
        self.condition = condition

    def derive_schema(self):
        schema = self.children[0].schema
        self.check = compile_condition(self.condition, schema.columns, schema.types())
        return schema

    def produce(self):
        child = self.children[0]
        check = self.check
        while True:
            row = child.next()
            if row is None:
                return
            self.comparisons += 1
            if check(row):
                yield row


class Project(Operator):
    op = "project"

    def __init__(self, ctx, path, child, columns, names):
        super().__init__(ctx, path, [child])
        self.columns = columns
        self.names = names

    def derive_schema(self):
        child = self.children[0].schema
        self.indices = [child.resolve(c) for c in self.columns]
        columns = []
        defs = []
        for idx, name in zip(self.indices, self.names):
            source = child.defs[idx]
            if name is None:
                columns.append(child.columns[idx])
                defs.append(source)
            else:
                columns.append(Column(None, name))
                defs.append(ColumnDef(name, source.type, source.length))
        return Schema(columns, defs)

    def produce(self):
        child = self.children[0]
        indices = self.indices
        while True:
            row = child.next()
            if row is None:
                return
            yield tuple(row[i] for i in indices)


class Rename(Operator):
    op = "rename"

    def __init__(self, ctx, path, child, to):
        super().__init__(ctx, path, [child])
        self.to = to

    def derive_schema(self):
        child = self.children[0].schema
        return Schema([Column(self.to, c.attr_name) for c in child.columns], child.defs)

    def label(self):
        return f"rename {self.to}"

    def produce(self):
        return drain(self.children[0])


class Limit(Operator):
    op = "limit"

    def __init__(self, ctx, path, child, n):
        super().__init__(ctx, path, [child])
        self.n = n

    def produce(self):
        child = self.children[0]
        for _ in range(self.n):
            row = child.next()
            if row is None:
                return
            yield row
        child.close()


def unify_schemas(left, right):
    """Set operators: same arity and type class per position; names from the left."""
    if len(left) != len(right):
        raise ExecError(f"Inputs have {len(left)} and {len(right)} columns")
    defs = []
    for i, (a, b) in enumerate(zip(left.defs, right.defs)):
        if type_class(a.type) != type_class(b.type):
            raise ExecError(f"Column {i + 1} is {a.type} on the left and {b.type} on the right")
        defs.append(common_definition(a.name, a, b))
    return Schema(left.columns, defs)


def promoted_columns(schema, inputs):
    """Positions typed REAL in the result whose values are integers in some input."""
    return [i for i, d in enumerate(schema.defs)
            if d.type == "REAL" and any(child.schema.defs[i].type != "REAL" for child in inputs)]


def promote(row, positions):
    if not positions:
        return row
    values = list(row)
    for i in positions:
        values[i] = float(values[i])
    return tuple(values)


class UnionAll(Operator):
    """The left input's tuples, then the right input's. Three ways to schedule the
    right input, in order of preference:
      lazy         opened once the left is drained, beside the frames its reader holds
                   while it streams;
      right_first  opened in this node's own open, before the reader reserves anything,
                   keeping its streaming frames while the left opens and streams;
      spill_right  copied to a temporary file in this node's own open and read back,
                   one frame, after the left: it never holds frames beside the left.
    spill_right costs a write and a read of the right input's pages, so it is offered
    only when the plan does not fit in B frames otherwise."""

    op = "union_all"
    MODES = ("lazy", "right_first", "spill_right")

    def derive_schema(self):
        schema = unify_schemas(self.children[0].schema, self.children[1].schema)
        self.promoted = promoted_columns(schema, self.children)
        return schema

    def mark_readers(self, reader_frames, may_spill):
        """Choose the way that needs the fewest frames beside the reader, the earlier
        one on a tie. The right input is weighed with what it sits beside in each way."""
        left, right = self.children
        left.mark_readers(reader_frames, may_spill)
        right_readers = {"lazy": 0, "right_first": max(left.min_open(), left.min_stream() + reader_frames),
                         "spill_right": 1}
        modes = self.MODES if may_spill else self.MODES[:2]
        needed = {}
        for mode in modes:
            right.mark_readers(right_readers[mode], may_spill)
            needed[mode] = max(self.open_needed(mode), self.stream_needed(mode) + reader_frames)
        self.mode = min(modes, key=needed.get)
        right.mark_readers(right_readers[self.mode], may_spill)

    def open_needed(self, mode):
        left, right = self.children
        if mode == "right_first":
            return max(right.min_open(), left.min_open() + right.min_stream())
        if mode == "spill_right":
            return max(right.min_open(), right.min_stream() + 1, left.min_open())
        return left.min_open()

    def stream_needed(self, mode):
        left, right = self.children
        if mode == "right_first":
            return left.min_stream() + right.min_stream()
        if mode == "spill_right":
            return left.min_stream()
        return max(left.min_stream(), right.min_open(), right.min_stream())

    def min_open(self):
        return self.open_needed(self.mode)

    def min_stream(self):
        return self.stream_needed(self.mode)

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        left, right = self.children
        if self.mode == "right_first":
            held = right.min_stream()
            right.configure(open_frames, held)
            left.configure(open_frames - held, stream_frames - held)
        elif self.mode == "spill_right":
            right.configure(open_frames, open_frames - 1)
            left.configure(open_frames, stream_frames)
        else:
            left.configure(open_frames, stream_frames)
            right.configure(stream_frames, stream_frames)

    def open(self):
        self.is_open = True
        left, right = self.children
        if self.mode == "right_first":
            right.open()
        if self.mode == "spill_right":
            self.right_run = self.materialize(right)
        left.open()
        self.gen = self.produce()

    def produce(self):
        left, right = self.children
        for row in drain(left):
            yield promote(row, self.promoted)
        left.close()
        if self.mode == "spill_right":
            self.reserve(1)
            rows = read_run(self, self.right_run, decoder(right.schema.defs))
        else:
            if self.mode == "lazy":
                right.open()
            rows = drain(right)
        for row in rows:
            yield promote(row, self.promoted)
