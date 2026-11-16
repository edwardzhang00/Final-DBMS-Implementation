"""Block nested loop, sort-merge and Grace hash joins. Output schema is left then right."""

from ..ra.operators import compile_condition, type_class
from ..ra.tree import conjuncts, condition_text, is_equality
from ..storage.record import decoder, sizer
from .operators import Operator, share, drain
from .runtime import ExecError, FrameCounter, RunWriter, read_run, fnv_hash
from .sort import SortedInputs


def equi_join_keys(condition, left, right):
    """Positions (in left, in right) of the columns each equality conjunct compares."""
    keys = equality_pairs(condition, left, right)
    if keys is None:
        raise ExecError(f"{condition_text(condition)} is not a conjunction of left column = right column")
    return keys


def equality_pairs(condition, left, right):
    """The key positions if every conjunct is a left column = right column, else None."""
    combined = left + right
    left_keys, right_keys = [], []
    for c in conjuncts(condition):
        if not is_equality(c):
            return None
        a = combined.resolve(c.left)
        b = combined.resolve(c.right)
        if a < len(left) <= b:
            left_index, right_index = a, b - len(left)
        elif b < len(left) <= a:
            left_index, right_index = b, a - len(left)
        else:
            return None
        if type_class(left.defs[left_index].type) != type_class(right.defs[right_index].type):
            raise ExecError(f"Cannot compare {left.name(left_index)} ({left.defs[left_index].type}) with "
                            f"{right.name(right_index)} ({right.defs[right_index].type})")
        left_keys.append(left_index)
        right_keys.append(right_index)
    return left_keys, right_keys


def split_into_blocks(rows, size, block_frames):
    """The rows in consecutive blocks, each as many as fit in block_frames pages."""
    block = []
    frames = FrameCounter()
    for row in rows:
        length = size(row)
        if frames.pages_with(length) > block_frames:
            yield block
            block = []
            frames.reset()
        block.append(row)
        frames.add(length)
    if block:
        yield block


class Join(Operator):
    pipelined = False

    def __init__(self, ctx, path, left, right, condition):
        super().__init__(ctx, path, [left, right])
        self.condition = condition

    def derive_schema(self):
        left, right = self.children
        return left.schema + right.schema

    def key_matches(self, block, left_keys, inner_rows, right_keys):
        """Every block row joined with every inner row whose key equals its own,
        one comparison per pair, the joined tuple built only on a match."""
        keyed = [(tuple(row[i] for i in left_keys), row) for row in block]
        for inner_row in inner_rows:
            inner_key = tuple(inner_row[i] for i in right_keys)
            self.comparisons += len(keyed)
            for outer_key, outer_row in keyed:
                if outer_key == inner_key:
                    yield outer_row + inner_row


class NestedLoopJoin(Join):
    """Block nested loop. The outer is read in blocks of B - 2 frames and the whole
    inner streams past each block one page at a time. An input that is not a
    plain scan pipeline is first written to a temporary file: the inner because it
    is read once per block, the outer so that the operator below it releases its
    memory before the blocks are filled."""

    op = "nested_loop_join"

    def derive_schema(self):
        schema = super().derive_schema()
        self.check = None
        self.keys = None
        if self.condition is not None:
            self.check = compile_condition(self.condition, schema.columns, schema.types())
            left, right = self.children
            self.keys = equality_pairs(self.condition, left.schema, right.schema)
        return schema

    def min_open(self):
        return max([1] + [max(c.min_open(), c.min_stream() + 1)
                          for c in self.children if not c.is_pipeline()])

    def min_stream(self):
        return 3

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        for child in self.children:
            if child.is_pipeline():
                child.configure(1, 1)
            else:
                child.configure(open_frames, open_frames - 1)
        self.block_frames = stream_frames - 2

    def open(self):
        self.is_open = True
        self.blocks = 0
        left, right = self.children
        self.outer_run = None if left.is_pipeline() else self.materialize(left)
        self.inner_run = None if right.is_pipeline() else self.materialize(right)
        if self.outer_run is None:
            left.open()
        self.gen = self.produce()

    def inner_rows(self):
        right = self.children[1]
        if self.inner_run is not None:
            return read_run(self, self.inner_run, self.inner_decode)
        right.close()
        right.open()
        return drain(right)

    def produce(self):
        left, right = self.children
        if self.outer_run is not None:
            self.reserve(1)
            outer = read_run(self, self.outer_run, decoder(left.schema.defs))
        else:
            outer = drain(left)
        if self.inner_run is not None:
            self.reserve(1)
            self.inner_decode = decoder(right.schema.defs)
        check = self.check
        self.reserve(self.block_frames)
        for block in split_into_blocks(outer, sizer(left.schema.defs), self.block_frames):
            self.blocks += 1
            if self.keys is not None:
                left_keys, right_keys = self.keys
                yield from self.key_matches(block, left_keys, self.inner_rows(), right_keys)
                continue
            for inner_row in self.inner_rows():
                for outer_row in block:
                    joined = outer_row + inner_row
                    if check is None:
                        yield joined
                        continue
                    self.comparisons += 1
                    if check(joined):
                        yield joined


class HashJoin(Join):
    """Grace hash join; the left input is the build side.

    Both inputs are partitioned with h1 into P = (budget - input frame) files.
    For each pair, a partition of the left input of at most B - 1 pages is
    loaded into a table bucketed by h2 and the right partition probes it. A left
    partition larger than that (heavy skew) is joined by a block nested loop on
    that one pair of files instead."""

    op = "hash_join"

    def derive_schema(self):
        schema = super().derive_schema()
        left, right = self.children
        self.left_keys, self.right_keys = equi_join_keys(self.condition, left.schema, right.schema)
        return schema

    def min_open(self):
        return max(max(c.min_open(), c.min_stream() + 1) for c in self.children)

    def min_stream(self):
        return 3

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        left, right = self.children
        left_frames = share(left, open_frames)
        right_frames = share(right, open_frames)
        left.configure(open_frames, left_frames)
        right.configure(open_frames, right_frames)
        self.partitions = open_frames - max(left_frames, right_frames)
        self.partition_sizes = []
        self.overflowed = []

    def counters(self):
        out = super().counters()
        out["partitions"] = self.partitions
        out["overflow_partitions"] = len(self.overflowed)
        return out

    def open(self):
        self.is_open = True
        left, right = self.children
        self.left_runs = self.partition(left, self.left_keys)
        self.right_runs = self.partition(right, self.right_keys)
        self.partition_sizes = [(build.pages, probe.pages) for build, probe in zip(self.left_runs, self.right_runs)]
        self.gen = self.produce()

    def partition(self, child, keys):
        child.open()
        self.reserve(self.partitions)
        writers = [RunWriter(self, child.schema.defs) for _ in range(self.partitions)]
        for row in drain(child):
            writers[fnv_hash(tuple(row[i] for i in keys), 1) % self.partitions].add(row)
        runs = [writer.finish() for writer in writers]
        self.release(self.partitions)
        child.close()
        return runs

    def produce(self):
        left, right = self.children
        decode_left = decoder(left.schema.defs)
        decode_right = decoder(right.schema.defs)
        for i, (build, probe) in enumerate(zip(self.left_runs, self.right_runs)):
            if build.count and probe.count:
                if build.pages <= self.stream_frames - 1:
                    yield from self.probe(build, probe, decode_left, decode_right)
                else:
                    self.overflowed.append(i)
                    yield from self.block_join(build, probe, decode_left, decode_right)
            self.drop_temp(build.temp)
            self.drop_temp(probe.temp)

    def probe(self, build, probe, decode_left, decode_right):
        self.reserve(build.pages + 1)
        table = {}
        for row in read_run(self, build, decode_left):
            key = tuple(row[i] for i in self.left_keys)
            table.setdefault(fnv_hash(key, 2), []).append((key, row))
        for row in read_run(self, probe, decode_right):
            key = tuple(row[i] for i in self.right_keys)
            for build_key, build_row in table.get(fnv_hash(key, 2), ()):
                self.comparisons += 1
                if build_key == key:
                    yield build_row + row
        self.release(build.pages + 1)

    def block_join(self, build, probe, decode_left, decode_right):
        self.reserve(self.stream_frames)
        outer = read_run(self, build, decode_left)
        for block in split_into_blocks(outer, sizer(self.children[0].schema.defs), self.stream_frames - 2):
            probe_rows = read_run(self, probe, decode_right)
            yield from self.key_matches(block, self.left_keys, probe_rows, self.right_keys)
        self.release(self.stream_frames)


class SortMergeJoin(Join, SortedInputs):
    """Sort both inputs on the join key with the external sort, then merge.

    The right input's run of equal keys is kept in one frame and spilled to a
    temporary file when it is longer, then replayed for every left tuple with
    that key. Each sort's final merge streams into the join, so the budget is
    split between the two final merges plus the one frame for the run."""

    op = "sort_merge_join"
    merge_frames = 1

    def derive_schema(self):
        schema = super().derive_schema()
        left, right = self.children
        self.left_keys, self.right_keys = equi_join_keys(self.condition, left.schema, right.schema)
        self.sort_inputs(self.left_keys, self.right_keys)
        return schema

    def produce(self):
        left, right = self.left_sort, self.right_sort
        left_keys, right_keys = self.left_keys, self.right_keys
        size = sizer(self.children[1].schema.defs)
        decode = decoder(self.children[1].schema.defs)
        self.reserve(1)
        r = left.next()
        s = right.next()
        while r is not None and s is not None:
            r_key = tuple(r[i] for i in left_keys)
            s_key = tuple(s[i] for i in right_keys)
            self.comparisons += 1
            if r_key < s_key:
                r = left.next()
            elif r_key > s_key:
                s = right.next()
            else:
                run, spilled = [], None
                frames = FrameCounter()
                while s is not None:
                    if run or spilled is not None:
                        self.comparisons += 1
                        if tuple(s[i] for i in right_keys) != r_key:
                            break
                    if spilled is None and frames.pages_with(size(s)) > 1:
                        spilled = RunWriter(self, self.children[1].schema.defs)
                        for row in run:
                            spilled.add(row)
                        run = None
                    if spilled is None:
                        run.append(s)
                        frames.add(size(s))
                    else:
                        spilled.add(s)
                    s = right.next()
                spilled_run = spilled.finish() if spilled is not None else None
                while r is not None:
                    for s_row in (run if spilled_run is None else read_run(self, spilled_run, decode)):
                        yield r + s_row
                    r = left.next()
                    if r is None:
                        break
                    self.comparisons += 1
                    if tuple(r[i] for i in left_keys) != r_key:
                        break
                if spilled_run is not None:
                    self.drop_temp(spilled_run.temp)
