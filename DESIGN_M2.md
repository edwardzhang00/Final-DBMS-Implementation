# Module 2 design: execution engine

Code: `dbms/execution/` (`operators.py`, `sort.py`, `aggregate.py`, `joins.py`, `setops.py`,
`plan.py`, `runtime.py`, `cli.py`). Command: `bin/exec`. Sample plans: `workload/plans/`.

## Interface and counters

Every operator has `open()`, `next()` (a tuple or `None` for end of stream), `close()` and
`counters()`. Calling `next` on the root pulls one tuple through the tree. An operator touches
only its children and Module 1: the buffer pool for tables, and temp files for its own spills.
`close` releases every pin and reserved frame and deletes every temp file the operator made. The
tests check after every plan that nothing is pinned or reserved and that `tmp/` is gone.

Counters per operator:

- `tuples_out` counts tuples returned from `next`.
- `page_reads` and `page_writes` count disk-manager calls this operator caused: buffer pool
  misses during its own fetches, plus every page of its own temp files.
- `comparisons`:
  - filter: one per tuple examined;
  - nested loop join: one per pair, exactly |R|·|S| when a condition is given (a cross product
    evaluates nothing, so 0);
  - sort-merge join: key comparisons in the merge, plus every comparison made inside its two
    sorts;
  - hash join: one per (probe tuple, build tuple) pair in the probed bucket;
  - sort: every key comparison of the in-memory sort and the merge heaps;
  - hash group_by and distinct: one per table probe;
  - sort group_by: one per adjacent pair of keys;
  - set operators: tuple comparisons in the merge.

  Operators that run an external sort internally (sort-merge join, sort-mode group_by, the set
  operators) report that sort's work as their own, because the sort is not a node of the plan.

`exec run --json` prints `schema`, `rows` and `totals` exactly as the handout shows. `totals`
holds the root's `tuples_out` and the sum of comparisons, reads and writes over all nodes; the
extra fields `frames` and `peak_frames` are also printed. `exec trace` prints one line per node
and nothing else, in plan order, indented two spaces per level. Each line starts with the
handout's four counters, followed by any extra fields: `passes=` on a sort, `partitions=` and
`overflow_partitions=` on a hash join, `mode=` on group_by and distinct. The totals and the peak
frame count are printed by `exec run`.

```
$ python bin/exec trace --db DIR --plan workload/plans/02_hash_join.json --frames 4
hash_join tuples_out=385 comparisons=385 reads=9 writes=9 partitions=3 overflow_partitions=0
  filter tuples_out=385 comparisons=2000 reads=0 writes=0
    seqscan emp tuples_out=2000 comparisons=0 reads=25 writes=0
  seqscan dept tuples_out=100 comparisons=0 reads=2 writes=0
```

(on the workload data at scale 0.2: `python workload/generate.py data --scale 0.2`, then the
database built as in the README's quick start.)

## The plan JSON schema

This schema was not published with the handout, so this is the one the executor enforces
(`plan.py`). Every node is an object with `op` plus:

| op | fields |
|---|---|
| `seqscan` | `table`: string |
| `filter` | `condition`: string, `child` |
| `project` | `columns`: non-empty array of column names, `names`?: array of string or null (output names), `child` |
| `rename` | `to`: string, `child` |
| `distinct` | `mode`: `"sort"` or `"hash"`, `child` |
| `sort` | `keys`: non-empty array of `[column, "asc" or "desc"]`, `child` |
| `group_by` | `mode`, `keys`: array of columns (may be empty), `aggregates`: array of `[func, column or "*"]` or `[func, column or "*", output name]`, `child` |
| `nested_loop_join` | `condition`?: string (absent means cross product), `left`, `right` |
| `sort_merge_join`, `hash_join` | `condition`: `col = col [and col = col ...]`, `left`, `right` (hash join builds on `left`) |
| `union_all`, `union`, `intersect`, `except` | `left`, `right` |
| `limit` | `n`: integer >= 0, `child` |

`func` is `count`, `sum`, `avg`, `min` or `max`, and `"*"` is accepted only with `count`. A
group_by's output schema is its keys, then one column per aggregate, named by the third element
or else `count_star`, `avg_R_salary`, and so on. Extra fields are allowed and ignored: the
optimizer writes `est_rows` and `est_cost` on every node, and its plans run here unedited.
Conditions and column names are parsed by the Bonus 1 condition parser (`dbms/ra/parser.py`).

**Rejections.** Malformed JSON reports the decoder's line and column. A plan that does not
conform names the node by its operator and states the problem, in the handout's own words, then
gives the node's path from the root, since a plan can hold several nodes of one operator:

```
Node 'sort': missing required field 'keys'. [at root]
Node 'filter': field 'condition' must be a string. [at root]
Node 'merge_join': unknown operator 'merge_join'. [at root.right]
Node 'filter': field 'condition' is not a valid condition: Expected comparison operator at line 1, column 7 [at root.child]
Node 'hash_join': field 'condition' must be column = column equalities joined by 'and', got "R.a < S.a". [at root]
```

Column names resolve the same way in every module (`resolve_attr` in `dbms/ra/operators.py`). A
qualified name must match exactly. A bare name means the column of that name with no qualifier, an
alias or an aggregate result, when there is one, and otherwise a qualified column of that name. So
an alias `budget` and the column `d.budget` can sit in one schema without ambiguity.

Unknown tables and columns, type errors (INT against VARCHAR, equality included), duplicate
qualified names in one schema (`R` joined with `R` without a rename), and a join key that does
not compare a left column with a right column are all found by `bind()`. `bind()` runs over the
whole tree before any `open()`, so no page is read for a plan that will be rejected. Types are
checked once, from the schemas, before any tuple is read.

## The memory budget

**What a frame is.** The buffer pool has exactly B frames for the whole plan. A frame holds
either a page the pool cached or pinned, or tuples an operator buffered (a sort run, a nested-loop
block, a hash table, a temp file page being read or written). Operator buffers are taken out of
the pool with `reserve(n)` and returned with `release(n)`. A buffer of tuples occupies as many
frames as the pages those tuples would fill: they are counted with the slotted-page arithmetic
(`L + 4` bytes per record, 4088 per page). The pool keeps

```
pinned frames + reserved frames <= B
```

at every instant and raises an error instead of exceeding it. It records the largest value
reached (`peak_frames`), which is how the report knows how many frames a plan held: the number
is measured, not computed. Temp files are read and written straight from reserved frames through
the disk manager, the way PostgreSQL's BufFile bypasses shared buffers. Every temp page moved is
therefore exactly one counted read or write, and no dirty temp page lingers in the pool.

**How B is divided.** Every operator gets two numbers for itself and its subtree:

- `open_frames`: what it may hold during `open()`, where all blocking work happens (sort pass 0
  and its merges, hash partitioning, hash aggregation, nested-loop materialization). A parent
  opens a blocking input *before* it reserves anything itself, so the input's blocking work gets
  the parent's whole budget.
- `stream_frames`: what it may hold while it returns tuples, alongside whatever its parent is
  holding at that moment.

The rule a parent applies to an input it consumes while working:

- a **pipeline** input (seqscans under filter, project, rename, limit or union_all) streams with
  exactly 1 frame, the page its scan has pinned;
- a **blocking** input streams with `max(its minimum, budget // 2)`, the equal split between
  two blocking operators the handout suggests;
- the parent keeps the rest;
- when an input is closed its frames go back to the parent for the parent's next phase.

Every plan states its minimum B (`min_open`, `min_stream`). A plan that cannot run is refused
before execution with "This plan needs at least N frames", rather than exceeding B. With this
rule every plan in the tests runs at B = 4, including join chains of every algorithm, group_by
and distinct over joins, set operators over joins, and subqueries.

- **union_all's right input.** union_all reads its left input, then its right input, and has
  three ways to schedule the right one (`UnionAll.MODES`, in order of preference):
  - *lazy*: opened once the left is drained. Under a reader that holds frames of its own while
    it reads (any blocking operator above, through pipelined ones), it then opens beside that
    reader's buffer. A group_by without keys buffers nothing while its input streams, so it does
    not count as such a reader.
  - *right first*: opened in union_all's own `open()`, before the reader reserves anything. It
    then keeps its streaming frames while the left opens and streams.
  - *spill right*: copied to a temporary file in union_all's own `open()`, through one writer
    frame, and closed; after the left is drained the copy is read back one frame at a time. The
    right input never holds frames beside the left, at the price of writing and reading its
    output once.

  `prepare()` first marks the plan with lazy and right first allowed. Each union_all takes the
  way that needs fewer frames, its open minimum against its streaming minimum plus the reader's
  frame, and lazy on a tie; a right input that is itself a union_all is weighed with what it sits
  beside in each case (`UnionAll.mark_readers`). If the whole plan then needs more than B frames,
  it is marked again with spill right allowed as well, and each union_all again takes the way that
  needs the fewest frames, the earlier one on a tie. So a plan that fits without copying never
  copies. `SELECT ... UNION ALL SELECT DISTINCT ... ORDER BY` with a hash-mode distinct on the
  right opens it first and needs 4 frames (5 lazily); with a hash-mode distinct on the left and a
  sort-mode one on the right it stays lazy and needs 4
  (`test_right_input_opened_first_only_when_that_needs_fewer_frames`).
- **Every plan fits in 4 frames.** Under a reader with one buffer frame, spill right needs
  `max(R_open, R_stream + 1, L_open, L_stream + 1)`. Every other operator needs at most 4 frames
  beside such a reader when its inputs do: a sort or a hash-mode group_by opens with at most 4
  and streams with 1, a join streams with 3, a set operator with 2, and pipelined operators need
  what their input needs. So with spill right available no plan needs more than 4.
- **Which plans copy at B = 4.** Those that need 5 frames lazily and right first: a union_all
  under a blocking reader whose right input needs 4 frames to open (a hash-mode group_by or
  distinct, or a join that reads another join) while either that input streams with 3 (a join)
  or the left input needs 4 to open or 3 to stream (a hash-mode group_by or distinct, a join, or a
  union_all whose right input does blocking work), and union_alls nested in the right input of
  another with such an input inside. For example `SELECT ... FROM project ... UNION ALL SELECT ...
  FROM emp, works, dept ... ORDER BY` copies the three-way join's output at B = 4 and not at
  B = 5 (`test_union_alls_that_need_five_frames_otherwise_spill_their_right_input_at_four`). A join
  holds nothing between its open and its first tuple, so for a join the copy is more than the
  frames strictly require; counting that exactly would take a third frame figure for every
  operator, and the copy is one rule for every input. The optimizer does not see per-node frame
  shares, so its estimated cost does not include the copy.
- **At B = 3** a sort, a single join, and any of the four set operators over sorts, sort-mode
  group_bys or distincts and pipelines run, also under a sort, a sort-mode group_by or distinct, a
  join or a union (the handout's sort example is at B = 3). A hash-mode group_by or distinct needs
  4, and so does a join under a blocking operator or a join that reads another join. Every query
  that does not fit is refused before any work, as above. The handout asks for plans "as small
  as" 4 frames.

### What each operator holds

| operator | open phase | streaming phase |
|---|---|---|
| seqscan | - | 1 pinned page (a directory page is pinned only while its entries are copied) |
| filter, project, rename, limit | - | nothing beyond its input |
| union_all | a right input opened first, or copied to a temp file through 1 writer frame (see above) | one input at a time; a right input opened first keeps its streaming minimum while the left streams; a copied one is read with 1 frame |
| sort | input streams with s; run buffer B - s; then merges of B - 1 runs plus 1 output frame | <= min(stream, B - 1) runs, 1 frame each, or the whole input in memory if it fits |
| group_by / distinct, sort mode | its sort | its sort's streaming frames + one group |
| group_by / distinct, hash mode | input streams with s; table + spill buffers B - s; then 1 reader + table + spill + 1 result writer | the table in memory if one round sufficed and it fits, else 1 frame reading the result |
| nested_loop_join | each non-pipeline input copied to a temp file with 1 writer frame | B - 2 block, 1 frame for the outer's page, 1 for the inner's page |
| hash_join | each input in turn streams with s; P = B - max(s_left, s_right) partition buffers | build table <= B - 1 pages + 1 frame reading the probe partition |
| sort_merge_join | left sort, then right sort | left final merge + right final merge + 1 frame for a run of equal keys |
| union, intersect, except | left sort, then right sort | the two final merges |

Python objects are bigger than their encoded bytes, so "frames" here means the pages the
tuples occupy in the on-disk format, the unit every cost formula uses. Two kinds of small state
are not counted. One is the cursor state of a scan: the list of up to 681 page ids copied from
a directory page. The other is the single tuple in flight between two operators.

### Sort page accounting

Pass 0 fills the run buffer from the input. The input keeps the frame its page is pinned in, so
with B frames a run is **B - 1 pages**, one fewer than the handout's B. The handout's pass 0
sorts the input pages inside the B frames they were read into. An iterator *copies* tuples out
of its child's page, so that page and the run buffer hold memory at the same time, and a B-page
run would hold B + 1. Merge passes merge B - 1 runs at a time into one output frame. A run left
alone at the end of a pass is carried forward untouched, as in the handout's pass 1. The final
merge does not write: it streams through `next()` with one frame per run. If the input fits in
the run buffer, it stays in memory and the sort writes nothing.

The handout's example, N = 8, B = 3, in this variant (`test_sort_of_eight_pages_at_three_frames`):

| pass | what happens | reads | writes |
|---|---|---|---|
| 0 | the scan reads 8 pages (+1 directory page); runs of 2 pages, 4 runs | 8 (+1) | 8 |
| 1 | merge runs 1+2 and 3+4 | 8 | 8 |
| 2 | merge the last 2 runs, streaming | 8 | 0 |
| total | 3 passes | 24 (+1) | 16 |

The handout's own variant gives 22 and 14. The pass count is the same here and, as REPORT_M2
shows, equal to the handout's formula for every size measured. The difference is one extra
merge's worth of I/O when `ceil(N/(B-1))` runs need one more merge than `ceil(N/B)` would.

### Hash join page accounting

Partition phase: the left input is read (its own scan's reads) and every tuple is written to
partition `h1(key) mod P`, one buffer frame per partition. Then the right input is partitioned
the same way with the same P. That costs M + N reads by the scans and about M + N writes, plus
one partly filled last page per partition and side. Probe phase: for each partition pair, the
left partition (at most B - 1 pages) is read into a table bucketed by `h2` and the right
partition is read and probed, about M + N reads. Total about 3(M + N).

`h1` and `h2` are FNV-1a over the key's bytes with different seeds, not Python's `hash()`.
Python's string hash changes from process to process, and the counters must not. Equal INT and
BIGINT keys hash the same.

**Overflow.** A left partition larger than B - 1 pages cannot be built in memory. The hash join
then falls back to a **block nested loop join on that one pair of files**. The left partition is
read in blocks of B - 2 frames and the right partition streams past each block. Repartitioning
with another hash function would help only when the overflow comes from many keys; with one heavy
key (the skew test) every repartition would put it in one partition again. The fallback handles
both cases and never needs more than B frames. REPORT_M2 measures what it cost.

## Joins

- **Block nested loop.** Blocks of B - 2 frames of outer tuples. For each block the inner
  pipeline is closed and reopened, so a base table is rescanned (N reads per block), and the
  block is tested against every inner tuple. An input that is not a plain scan pipeline is first
  written to a temp file. The inner is read once per block and must not be recomputed. The outer
  is copied too, so the join below it releases its memory before the blocks are filled. This is
  also the M the cost model charges for an intermediate outer (Module 4). When the condition is
  made only of left = right column equalities, each pair is still evaluated and counted, but on
  precomputed keys, and the joined tuple is built only for a match.
- **Sort-merge.** Both inputs go through the external sort on their join keys, then the
  handout's merge with mark and rewind. Rewinding happens over the right input's run of equal
  keys, which is kept in one frame and spilled to a temp file when it is longer. It is then
  replayed for every left tuple with the same key, so a run longer than B is handled and never
  assumed to fit. `R.b = [1,2,2,3]`, `S.b = [2,2,4]` returns 4 rows
  (`test_sort_merge_join_duplicate_keys_give_four_rows`). The two final merges stream at the same
  time, so they share the streaming budget. The left sort gets `(B - 1) // 2` frames and the right
  sort the rest, less one frame for the run. The left sort keeps its result in memory only if that
  leaves the right sort enough frames to sort.
- **Hash join** as above.

All three return the same bag on every input in the tests, at B = 4, 7 and 32, and against a
naive reference evaluator on random plans.

## Aggregation, distinct, set operators

- **group_by**: COUNT(*), COUNT, SUM, AVG, MIN, MAX. SUM and AVG accept any number (a stored column
  is always INT; the sum of a sum is allowed, as in PostgreSQL). The result types are COUNT INT,
  SUM BIGINT (REAL over a REAL), AVG REAL, and MIN/MAX the argument's type. REAL and BIGINT exist
  only in intermediate results and temp files. AVG is kept at full precision. Only printing rounds
  it, to 4 decimal places with a half away from zero, as PostgreSQL's `round()` does, so an outer
  SUM or comparison sees the exact value (`format_value`).
  - With no grouping columns, an empty input gives exactly one row when only COUNT is asked for
    (COUNT 0). Any other aggregate over an empty input is an error, since there is no NULL.
  - With grouping columns, an empty input gives no rows.
- **Hash mode** (all in `open()`). A table of groups is kept in T frames, each group counted at
  its key's encoded size plus the size of its running aggregates (8 bytes for COUNT and SUM, 16 for
  AVG's sum and count, the column's full width for MIN or MAX of a VARCHAR). A tuple of a new group
  joins it while there is room and otherwise goes to one of P spill partitions by hash. A group in
  the table has seen all its tuples, and a spilled tuple belongs to a group not in the table. Each
  partition is processed the same way with a new seed, depth first, releasing its reader frame
  before the partitions it spilled into. Each round finishes at least a table's worth of groups,
  so it ends.
- **distinct** is group_by on every column with no aggregates, in both modes.
- **union, intersect, except** have set semantics and are sort-merge based: both inputs are sorted
  on every column with the external sort and merged. No side is ever read into a Python set.
  union_all is pipelined. The inputs must have the same arity and the same type class per
  position. Names come from the left. A position whose inputs are INT and REAL is REAL, and its
  integers are converted, so the column prints as one type at every B.
- **Hashing compares numbers by value.** A REAL with an integral value hashes like the INT, so an
  AVG of 40.0 meets an INT 40 in a hash join, a hash distinct or a hash group_by, as it does in
  the sort-based paths.
- **A tuple wider than a page** cannot be held or spilled, since an operator's buffers are pages.
  Joining two wide rows can produce one (8 VARCHAR(255) columns joined to themselves are about 4120
  bytes). Every operator that buffers it then stops with "A tuple of N bytes does not fit on a
  page", rather than writing a page with no slot for it. Streaming operators pass such tuples
  through, so a plain join of wide rows still returns them.
- **limit** stops pulling after n tuples and closes its input at once, so the pages are released
  early. The trace shows the early stop: a sort under `limit 5` reports `tuples_out=5`.

## Determinism

Same data, same plan, same B gives the same counters: every command starts from an empty pool,
hashing is FNV, dicts iterate in insertion order, and Python's sort is deterministic for equal
input. `test_counters_are_identical_across_runs` runs a plan twice and compares every counter of
every node.
