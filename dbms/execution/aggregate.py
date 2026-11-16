"""group_by and distinct, each sort based or hash based.

distinct is group_by on every column with no aggregates.

Sort mode sorts on the grouping columns with the external sort and folds each
run of equal keys into one output row.

Hash mode does all of its work in open(). It keeps a table of groups in T
frames: a tuple whose group is in the table updates it, a tuple of a new group
joins the table while there is room and is otherwise written to one of P spill
partitions (chosen by a hash of the key). A group that made it into the table
saw all of its tuples, and a spilled tuple belongs to a group that is not in
the table. Each partition is then processed the same way with a fresh hash
seed. Every round finishes at least a table's worth of groups, so it ends. If
one round was enough and its table fits the streaming budget, the table is
streamed from memory; otherwise finished groups go to a result file that is
streamed one frame at a time.
"""

from ..ra.operators import Column
from ..ra.tree import default_aggregate_name
from ..storage.record import FIXED_WIDTH, LENGTH, aggregate_result_definition, decoder, sizer
from .operators import Operator, share, drain
from .runtime import ExecError, Schema, FrameCounter, RunWriter, read_run, fnv_hash
from .sort import Sort, ascending

NUMBER_STATE_WIDTH = FIXED_WIDTH["BIGINT"]
SUM_AND_COUNT_STATE_WIDTH = 2 * NUMBER_STATE_WIDTH


class UndefinedAggregateError(ExecError):
    pass


def state_width(func, argument):
    """Bytes a group's running state for one aggregate is counted as."""
    if func == "avg":
        return SUM_AND_COUNT_STATE_WIDTH
    if func in ("min", "max") and argument.type == "VARCHAR":
        return LENGTH.size + argument.length
    return NUMBER_STATE_WIDTH


class GroupBy(Operator):
    op = "group_by"
    pipelined = False

    def __init__(self, ctx, path, child, mode, keys, aggregates):
        super().__init__(ctx, path, [child])
        self.mode = mode
        self.keys = keys
        self.aggregates = aggregates

    def counters(self):
        out = super().counters()
        out["mode"] = self.mode
        return out

    def key_positions(self, child):
        return [child.resolve(k) for k in self.keys]

    def derive_schema(self):
        child = self.children[0].schema
        self.key_indices = self.key_positions(child)
        columns = [child.columns[i] for i in self.key_indices]
        defs = [child.defs[i] for i in self.key_indices]
        self.agg_specs = []
        self.state_width = 0
        for func, arg, name in self.aggregates:
            idx = None if arg is None else child.resolve(arg)
            name = name or default_aggregate_name(func, arg)
            argument = None if idx is None else child.defs[idx]
            if func in ("sum", "avg") and argument.type == "VARCHAR":
                raise ExecError(f"{func.upper()} requires a numeric column, {arg.text} is VARCHAR")
            defs.append(aggregate_result_definition(name, func, argument))
            columns.append(Column(None, name))
            self.agg_specs.append((func, idx))
            self.state_width += state_width(func, argument)
        self.key_defs = [child.defs[i] for i in self.key_indices]
        self.output_schema = Schema(columns, defs)
        if self.mode == "sort" and self.key_indices:
            self.sorter = Sort(self.ctx, self.path, self.children[0], ascending(child, self.key_indices))
            self.sorter.bind_self()
            self.internals = [self.sorter]
        return self.output_schema

    def mark_readers(self, reader_frames, may_spill):
        """Without grouping keys nothing is buffered while the input streams."""
        self.children[0].mark_readers(1 if self.key_indices else reader_frames, may_spill)

    def min_open(self):
        child = self.children[0]
        if not self.key_indices:
            return child.min_open()
        if self.mode == "sort":
            return self.sorter.min_open()
        return max(4, child.min_open(), child.min_stream() + 1)

    def min_stream(self):
        return self.children[0].min_stream() if not self.key_indices else 1

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        child = self.children[0]
        if not self.key_indices:
            child.configure(open_frames, stream_frames)
        elif self.mode == "sort":
            self.sorter.configure(open_frames, stream_frames)
        else:
            self.input_frames = share(child, open_frames)
            # The first round needs a table frame and a spill frame beside the
            # streaming input; if that does not fit, copy the input to a file first.
            self.copies_input = open_frames - self.input_frames < 2
            child.configure(open_frames, open_frames - 1 if self.copies_input else self.input_frames)

    def new_state(self):
        return [[0, 0] if func == "avg" else (None if func in ("min", "max") else 0)
                for func, _ in self.agg_specs]

    def update(self, state, row):
        for j, (func, idx) in enumerate(self.agg_specs):
            if func == "count":
                state[j] += 1
            elif func == "sum":
                state[j] += row[idx]
            elif func == "avg":
                state[j][0] += row[idx]
                state[j][1] += 1
            else:
                v = row[idx]
                if state[j] is None or (v < state[j] if func == "min" else v > state[j]):
                    state[j] = v

    def finish(self, key, state):
        values = list(key)
        for j, (func, _) in enumerate(self.agg_specs):
            values.append(state[j][0] / state[j][1] if func == "avg" else state[j])
        return tuple(values)

    def open(self):
        self.is_open = True
        if not self.key_indices:
            self.children[0].open()
            self.gen = self.aggregate_everything()
        elif self.mode == "sort":
            self.sorter.open()
            self.gen = self.sorted_groups()
        else:
            self.gen = self.hash_aggregate()

    def stream_table(self, table):
        for key, state in table.items():
            yield self.finish(key, state)

    def stream_result(self, output):
        self.reserve(1)
        yield from read_run(self, output, decoder(self.output_schema.defs))

    def aggregate_everything(self):
        state = self.new_state()
        count = 0
        for row in drain(self.children[0]):
            count += 1
            self.update(state, row)
        if count == 0:
            undefined = [func for func, _ in self.agg_specs if func != "count"]
            if undefined:
                raise UndefinedAggregateError(f"{undefined[0].upper()} over an empty input with no grouping "
                                              f"columns is undefined (there are no NULLs)")
        yield self.finish((), state)

    def sorted_groups(self):
        indices = self.key_indices
        current = None
        state = None
        for row in drain(self.sorter):
            key = tuple(row[i] for i in indices)
            if state is not None:
                self.comparisons += 1
            if state is None or key != current:
                if state is not None:
                    yield self.finish(current, state)
                current = key
                state = self.new_state()
            self.update(state, row)
        if state is not None:
            yield self.finish(current, state)

    def hash_aggregate(self):
        """All rounds run here, inside open(); returns the generator that streams the result."""
        child = self.children[0]
        child.open()
        input_defs = child.schema.defs
        decode = decoder(input_defs)
        if self.copies_input:
            self.reserve(1)
            source = self.write_run(drain(child), input_defs)
            child.close()
            rows, input_frames = read_run(self, source, decode), 1
        else:
            source, rows, input_frames = None, drain(child), self.input_frames

        table, frames, spilled = self.build_table(rows, 0, self.open_frames - input_frames, input_defs)
        child.close()
        if source is not None:
            self.release(1)
            self.drop_temp(source.temp)

        if not spilled and frames <= self.stream_frames:
            return self.stream_table(table)

        self.reserve(1)
        result = RunWriter(self, self.output_schema.defs)
        for key, state in table.items():
            result.add(self.finish(key, state))
        table.clear()
        self.release(self.reserved - 1)

        # Depth-first over spill partitions: a partition's reader frame is
        # released before the partitions it spilled into are processed.
        pending = [(run, 1) for run in reversed(spilled)]
        while pending:
            run, depth = pending.pop()
            self.reserve(1)
            table, _, more = self.build_table(read_run(self, run, decode), depth, self.open_frames - 2, input_defs)
            for key, state in table.items():
                result.add(self.finish(key, state))
            table.clear()
            self.release(self.reserved - 1)
            self.drop_temp(run.temp)
            pending.extend((r, depth + 1) for r in reversed(more))
        output = result.finish()
        self.release(1)
        return self.stream_result(output)

    def build_table(self, rows, depth, available, input_defs):
        """One round with `available` frames for the table and the spill writers.
        Leaves the table's frames reserved; spill writer frames are released."""
        partitions = max(1, (available - 1) // 2)
        table_frames = available - partitions
        key_size = sizer(self.key_defs)
        entry_extra = self.state_width
        indices = self.key_indices

        self.reserve(table_frames)
        table = {}
        frames = FrameCounter()
        writers = [None] * partitions
        for row in rows:
            key = tuple(row[i] for i in indices)
            self.comparisons += 1
            state = table.get(key)
            if state is None:
                length = key_size(key) + entry_extra
                if frames.pages_with(length) > table_frames:
                    partition = fnv_hash(key, depth) % partitions
                    if writers[partition] is None:
                        self.reserve(1)
                        writers[partition] = RunWriter(self, input_defs)
                    writers[partition].add(row)
                    continue
                frames.add(length)
                state = table[key] = self.new_state()
            self.update(state, row)
        spilled = []
        for writer in writers:
            if writer is not None:
                spilled.append(writer.finish())
                self.release(1)
        self.release(table_frames - frames.pages)
        return table, frames.pages, spilled


class Distinct(GroupBy):
    op = "distinct"

    def __init__(self, ctx, path, child, mode):
        super().__init__(ctx, path, child, mode, None, [])

    def key_positions(self, child):
        return list(range(len(child)))
