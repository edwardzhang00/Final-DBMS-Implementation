"""ANALYZE: exact statistics stored in the catalog.

One scan of the table counts tuples and record bytes and writes each column's
values to its own temporary file (one output frame per column, so a table
with more than B - 1 columns is scanned once per batch of B - 1 columns).
Each column file is then sorted by the executor's external sort, and one pass
over the sorted values gives ndv, min, max and the equi-depth histogram: the
k buckets are cut at every n/k-th value, so a value repeated across a cut
appears in both buckets.
"""

from ..execution.operators import Operator
from ..execution.plan import ExecContext
from ..execution.runtime import RunWriter, Schema, read_run
from ..execution.sort import Sort, ascending
from ..ra.operators import Column
from ..storage.catalog import Bucket, ColumnStats, TableStats
from ..storage.disk import StorageError
from ..storage.record import decoder, sizer

DEFAULT_BUCKETS = 20


class RunScan(Operator):
    """Reads a temporary file back through one reserved frame."""

    op = "runscan"

    def __init__(self, ctx, path, run, schema):
        super().__init__(ctx, path, [])
        self.run = run
        self.run_schema = schema

    def derive_schema(self):
        return self.run_schema

    def min_open(self):
        return 1

    def min_stream(self):
        return 1

    def configure(self, open_frames, stream_frames):
        pass

    def produce(self):
        self.reserve(1)
        yield from read_run(self, self.run, decoder(self.run_schema.defs))


def column_statistics(values, count, buckets_requested, column):
    """One pass over the count sorted values of a column."""
    bucket_count = min(buckets_requested, count)
    cuts = [i * count // bucket_count for i in range(bucket_count + 1)] if bucket_count else [0]
    value_size = sizer([column])
    buckets = []
    ndv = 0
    width_total = 0
    previous = None
    bucket = None
    next_bucket = 0
    for i, v in enumerate(values):
        if i == cuts[next_bucket]:
            bucket = Bucket(v, v, 0, 0)
            buckets.append(bucket)
            next_bucket += 1
            bucket_previous = None
        if v != previous or i == 0:
            ndv += 1
        if v != bucket_previous:
            bucket.ndv += 1
        bucket.hi = v
        bucket.count += 1
        width_total += value_size((v,))
        previous = v
        bucket_previous = v
    if count == 0:
        return ColumnStats(0, 0.0, None, None, [])
    return ColumnStats(ndv, width_total / count, buckets[0].lo, buckets[-1].hi, buckets)


def analyze_table(db, table, buckets, frames):
    if not 1 <= buckets <= 65535:
        raise StorageError(f"The bucket count must be between 1 and 65535, got {buckets}")
    heap = db.heap_file(table)
    info = heap.info
    schema = info.schema
    ctx = ExecContext(db)
    record_size = sizer(schema)
    batch = max(1, frames - 1)
    columns = [None] * len(schema)
    n_tuples = 0
    total_bytes = 0
    for start in range(0, len(schema), batch):
        positions = list(range(start, min(len(schema), start + batch)))
        holder = Operator(ctx, f"analyze {table}", [])
        holder.reserve(len(positions))
        writers = {c: RunWriter(holder, [schema[c]]) for c in positions}
        for _, row in heap.scan():
            if start == 0:
                n_tuples += 1
                total_bytes += record_size(row)
            for c in positions:
                writers[c].add((row[c],))
        runs = {c: writers[c].finish() for c in positions}
        holder.release(len(positions))
        for c in positions:
            column_schema = Schema([Column(table, schema[c].name)], [schema[c]])
            sort = Sort(ctx, f"analyze {table}.{schema[c].name}",
                        RunScan(ctx, "analyze", runs[c], column_schema), ascending(column_schema, [0]))
            sort.bind()
            sort.configure(frames, frames)
            sort.open()
            sorted_values = (row[0] for row in iter(sort.next, None))
            columns[c] = column_statistics(sorted_values, n_tuples, buckets, schema[c])
            sort.close()
            holder.drop_temp(runs[c].temp)
    info.stats = TableStats(n_tuples, info.data_pages, total_bytes / n_tuples if n_tuples else 0.0, columns)
    db.save()
    db.remove_temp_dir()
    return info.stats
