"""External merge sort, and the base of operators that merge two sorted inputs.

Pass 0 fills the sort's run buffer with input tuples, sorts it in memory and
writes it as a run. The input keeps the frame it reads into (a scan's pinned
page) while this happens, so with B frames a run is B - 1 pages. Merge passes
merge B - 1 runs at a time into one output frame, carrying a lone leftover run
forward untouched, until few enough runs remain for the final merge, which
does not write: it streams to the parent through next(), one frame per run.
When the whole input fits in memory it is kept there and nothing is written.
"""

import heapq

from ..storage.record import decoder, sizer
from .operators import Operator, share, drain
from .runtime import FrameCounter, read_run, sort_key_class


class HeapEntry:
    __slots__ = ("key", "source", "row")

    def __init__(self, key, source, row):
        self.key = key
        self.source = source
        self.row = row

    def __lt__(self, other):
        if self.key < other.key:
            return True
        if other.key < self.key:
            return False
        return self.source < other.source


def ascending(schema, positions):
    """Sort keys on these positions of schema, each ascending."""
    return [(schema.ref(i), "asc") for i in positions]


class Sort(Operator):
    op = "sort"
    pipelined = False

    def __init__(self, ctx, path, child, keys):
        """keys: [(AttrRef, "asc"|"desc")], resolved against the input's schema."""
        self.counter = [0]
        super().__init__(ctx, path, [child])
        self.keys = keys
        self.memory_ok = True
        self.passes = 0

    @property
    def comparisons(self):
        return self.counter[0]

    @comparisons.setter
    def comparisons(self, value):
        self.counter[0] = value

    def derive_schema(self):
        schema = self.children[0].schema
        self.key_indices = [schema.resolve(attr) for attr, _ in self.keys]
        self.descending = [direction == "desc" for _, direction in self.keys]
        return schema

    def min_open(self):
        child = self.children[0]
        return max(3, child.min_open(), child.min_stream() + 1)

    def min_stream(self):
        return 1

    def configure(self, open_frames, stream_frames):
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        child = self.children[0]
        child_frames = share(child, open_frames)
        child.configure(open_frames, child_frames)
        self.run_frames = open_frames - child_frames
        self.fan_in = open_frames - 1
        self.final_fan_in = min(stream_frames, open_frames - 1)

    def counters(self):
        out = super().counters()
        out["passes"] = self.passes
        return out

    def open(self):
        self.is_open = True
        child = self.children[0]
        child.open()
        Key = sort_key_class(self.descending, self.counter)
        indices = self.key_indices
        self.key_of = lambda row: Key(tuple(row[i] for i in indices))
        size = sizer(self.schema.defs)

        self.reserve(self.run_frames)
        runs = []
        buffer = []
        frames = FrameCounter()
        for row in drain(child):
            length = size(row)
            if frames.pages_with(length) > self.run_frames:
                buffer.sort(key=self.key_of)
                runs.append(self.write_run(buffer, self.schema.defs))
                buffer = []
                frames.reset()
            buffer.append(row)
            frames.add(length)
        child.close()
        self.passes = 1

        memory_run = None
        if buffer:
            buffer.sort(key=self.key_of)
            if not runs and self.memory_ok and frames.pages <= self.final_fan_in:
                memory_run = buffer
            else:
                runs.append(self.write_run(buffer, self.schema.defs))
        self.release(self.run_frames - (frames.pages if memory_run is not None else 0))

        while len(runs) > self.final_fan_in:
            runs = self.merge_pass(runs)
            self.passes += 1
        if runs:
            self.passes += 1
        self.gen = self.produce(runs, memory_run)

    def merge(self, runs):
        decode = decoder(self.schema.defs)
        readers = [read_run(self, run, decode) for run in runs]
        heap = []
        for source, reader in enumerate(readers):
            row = next(reader, None)
            if row is not None:
                heap.append(HeapEntry(self.key_of(row), source, row))
        heapq.heapify(heap)
        while heap:
            top = heap[0]
            yield top.row
            row = next(readers[top.source], None)
            if row is None:
                heapq.heappop(heap)
            else:
                heapq.heapreplace(heap, HeapEntry(self.key_of(row), top.source, row))

    def merge_pass(self, runs):
        merged = []
        for start in range(0, len(runs), self.fan_in):
            group = runs[start:start + self.fan_in]
            if len(group) == 1:
                merged.append(group[0])
                continue
            self.reserve(len(group) + 1)
            merged.append(self.write_run(self.merge(group), self.schema.defs))
            self.release(len(group) + 1)
            for run in group:
                self.drop_temp(run.temp)
        return merged

    def produce(self, runs, memory_run):
        if memory_run is not None:
            yield from memory_run
            return
        if runs:
            self.reserve(len(runs))
            yield from self.merge(runs)


class SortedInputs(Operator):
    """Sorts both inputs with the external sort and merges the two sorted streams
    side by side, holding merge_frames frames of its own while they stream."""

    pipelined = False
    merge_frames = 0

    def sort_inputs(self, left_keys, right_keys):
        """Set up the two internal sorts, on these key positions of each input."""
        left, right = self.children
        self.left_sort = Sort(self.ctx, self.path, left, ascending(left.schema, left_keys))
        self.right_sort = Sort(self.ctx, self.path, right, ascending(right.schema, right_keys))
        self.left_sort.bind_self()
        self.right_sort.bind_self()
        self.internals = [self.left_sort, self.right_sort]

    def min_open(self):
        return max(self.left_sort.min_open(), self.right_sort.min_open())

    def min_stream(self):
        return 2 + self.merge_frames

    def configure(self, open_frames, stream_frames):
        """The left input is sorted first. If its result fits its share of the final
        merge it may stay in memory while the right input is sorted, unless the
        right one needs those frames to open, in which case it is written out."""
        self.open_frames = open_frames
        self.stream_frames = stream_frames
        left_final = max(1, (stream_frames - self.merge_frames) // 2)
        right_final = stream_frames - self.merge_frames - left_final
        self.left_sort.memory_ok = open_frames - left_final >= self.right_sort.min_open()
        self.left_sort.configure(open_frames, left_final)
        self.right_sort.configure(open_frames - left_final if self.left_sort.memory_ok else open_frames,
                                  right_final)

    def open(self):
        self.is_open = True
        self.left_sort.open()
        self.right_sort.open()
        self.gen = self.produce()
