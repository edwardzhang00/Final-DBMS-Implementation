# Architecture

One Python package, `dbms/`, with no dependencies outside the standard library. Five command line
tools in `bin/`, one per module plus the shell, each a thin wrapper over the same code:

```
bin/storage   Module 1   dbms/storage/     pages, heap files, buffer pool, catalog, loader
bin/exec      Module 2   dbms/execution/   iterators, external sort, joins, aggregation, set ops
bin/sql       Module 3   dbms/sql/         scanner, parser, name resolution, translation
bin/opt       Module 4   dbms/optimizer/   statistics, estimates, rewrites, cost, join order
bin/dbsh      all four   dbms/shell.py     SQL in, rows out
              Bonus 1    dbms/ra/          tokenizer, parser, tree, operators (the logical layer); ra.py is its CLI
```

## How a query flows

```
SQL text
  |  dbms/sql/parser.py         hand-written scanner + recursive descent  ->  syntax tree
  |  dbms/sql/analyzer.py       scopes, resolution, types, bag rules      ->  logical plan
  v                                                                           (dbms/ra/tree.py nodes)
logical plan          (JSON: scan / select / project / join / cross / rename / group_by / distinct /
  |                          sort / limit / union / union_all / intersect / except)
  |  dbms/optimizer/rewrite.py  rules 1-7
  |  dbms/optimizer/planner.py  estimates from catalog statistics, Selinger DP over left-deep
  v                             orders, algorithm choice, cost
physical plan         (JSON: seqscan / filter / project / rename / nested_loop_join / sort_merge_join /
  |                          hash_join / sort / group_by / distinct / set ops / limit, + est_rows, est_cost)
  |  dbms/execution/plan.py     validate, build the operator tree, bind (names, types), divide B
  v  open / next / close
operator tree  --- page requests, frame reservations, temp files --->  dbms/storage/
                                                                   buffer pool -> heap files -> pages on disk
                                     catalog and statistics  <---  read by the analyzer and the optimizer
```

## The interfaces between modules

Every boundary is a contract that can be crossed from the command line, so each module can be run
and graded alone.

**Bonus 1 engine → Modules 2, 3, 4.** The relational algebra library (`dbms/ra/`) supplies three
things the others reuse:

- *the condition language*: `parse_condition` turns `"R.a > 5 and S.b = 'x'"` into a tree,
  `condition_text` writes it back, and `compile_condition` resolves it against a schema once and
  returns a row predicate, type-checked before the first row;
- *the plan tree*: Module 3 builds it, Module 4 rewrites it;
- *the JSON form*: `to_json` and `from_json` round-trip every node type.

The library prints nothing and opens no file. Its operators pull rows from whatever iterable they
are given.

**Module 1 ↔ Module 2.** The executor touches storage only through:

- `HeapFile.scan()`, a generator over `(rid, tuple)` with one page pinned;
- `BufferPool.reserve(n)` / `release(n)` for operator memory, and the pool's counters for I/O
  attribution;
- `TempFile` for spills (append a page, read a page, delete).

The buffer pool is where the memory budget is enforced: pinned plus reserved frames never exceed
B.

**Module 1 ↔ Modules 3 and 4.** The catalog. `get_table(name)` gives the schema, page and tuple
counts, and, after ANALYZE, `TableStats` (row and page counts, widths, ndv, min/max, equi-depth
histograms). Module 4 writes the statistics back through the same catalog, which rewrites
`catalog.db` through the buffer pool.

**Module 3 → Module 4: the logical plan JSON** (DESIGN_M3, section 5). Fully qualified column
names, conditions in the Bonus 1 syntax, no physical operator names. `sql compile --json` writes
it, `opt plan --logical` reads it.

**Module 4 → Module 2: the physical plan JSON** (DESIGN_M2, plan schema). It is the same format a
person can write by hand, which is how Module 2 was tested before the optimizer existed.
`opt plan --json` writes it. `exec run --plan` reads it unedited and ignores the `est_rows` and
`est_cost` the optimizer adds to every node.

**The shell** calls these same functions in process: `Parser` → `Analyzer` → `Planner.optimize` →
`prepare`/`execute`. It adds the statements of the integration handout:

- `EXPLAIN q` prints the physical plan with estimates;
- `EXPLAIN ANALYZE q` runs it and prints estimated against actual rows (per execution, with
  `loops=` for rescanned inputs), the q-error and the I/O of every node;
- `ANALYZE t`;
- `SET frames = n`, which reopens the database with an n-frame pool;
- `LOAD t FROM 'file.csv'`.

Each query starts from an empty buffer pool, so its counters are the same however the session
began. The module tools still work on their own, and the shell is wiring only.

## Running the hidden workload

```
python scripts/run_workload.py --schema schema.sql --data DATA_DIR --queries QUERY_DIR --db DB_DIR --frames 32 --pg HOST:PORT/DBNAME --json results.json
```

- It builds the database (CREATE TABLE from the schema, `LOAD` of `DATA_DIR/<table>.csv`, ANALYZE)
  if `DB_DIR` does not exist yet.
- It runs every `*.sql` in `QUERY_DIR` and prints, per query: the result row count, page I/O
  (reads and writes), wall time, estimated against actual rows at the root, and the algorithms
  chosen.
- With `--pg` it loads the same schema and data into PostgreSQL and compares every result: as a
  list when the query ends in ORDER BY, as a bag otherwise.

REPORT_INTEGRATION.md is this run on the twenty-query workload in `workload/`: every result is
identical to PostgreSQL 17's at B = 32 and at B = 4. `scripts/fuzz_sql.py` repeats the comparison
on random queries over the whole SQL subset, each at three buffer sizes.

## Sources

- Hellerstein, Stonebraker and Hamilton, *Architecture of a Database System*, 2007. Its picture of
  the query processor (parser and rewriter, optimizer, executor over an access-methods and
  buffer-manager layer) is this system's module boundary. It also argues for strict layering, which
  is why only `dbms/storage` touches a file and why the executor reaches storage only through the
  buffer pool and the temp-file interface.
- The CMU 15-445 lectures (Pavlo) on query processing models and query planning. They shaped the
  shell's EXPLAIN ANALYZE, which reports actual rows per execution with a loop count, as PostgreSQL
  does, rather than totals that hide rescans.

## If I started again

**Pass pages, not tuples, between operators.** The iterator interface moves one tuple per `next()`.
Two costs come from that.

- *A lost frame in every sort.* An operator that copies tuples out of its child's page holds that
  page and its own buffer at once. So every external sort run is B − 1 pages instead of B (the
  only place the executor departs from the handout's sort example).
- *Python speed.* Python pays per call, so the block nested loop spends its time in interpreter
  overhead.

A `next_page()` that hands over a pinned page of tuples would let pass 0 sort the input pages in
place, and would cut the per-tuple overhead by two orders of magnitude.

**Decide the memory protocol before writing any operator.** I first gave each subtree one static
budget. It was correct, but group_by over a join over a distinct could not run at B = 4, and the
handout requires every plan to run there. The fix was to split every budget into what an operator
may hold while it opens (when its parent holds nothing yet) and while it streams (next to its
parent). That meant reworking the frame logic of every operator. Designing the open/stream split,
and the rule that a parent opens its inputs before reserving its own frames, before writing the
first operator would have avoided the rework.

**Size hash partitions from the estimate.** The handout fixes P = B − 1, which the executor uses
whenever both inputs are pipelines, and REPORT_M2 shows it backfiring at both ends: overflow when B < √M, and hundreds of nearly empty partitions when B is
large. The optimizer already knows M when it emits the plan. The plan should carry the partition
count, as it carries the algorithm.

**Keep one statistics object per column group from the start.** Apart from the two set operations
(q12's EXCEPT and q10's UNION, which no statistic of a single relation can estimate), the worst
estimates in REPORT_M4 are correlation: `level` with `salary`, and `did` with `city` across a
join. Independence is
wired into `selectivity()` as a product. A column-group statistic (joint ndv, or a dependency
degree as in PostgreSQL's `CREATE STATISTICS ... (dependencies)`) would have to touch the
estimator, the catalog format and ANALYZE together. It is far easier to leave room for it at the
start than to add it later.

**One tree, two phases.** The logical plan uses the Bonus 1 node classes, and the physical plan
is a separate small class in the optimizer that is turned into JSON. That is the boundary the
handout asks for, but it duplicates schema derivation: output columns are derived in the analyzer,
the rewriter, the planner and the executor's `bind`. A single node type with a `physical` slot,
and one schema function, would remove three copies of the same logic and the chance of them
drifting apart.
