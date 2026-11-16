# Module 4 design: query optimizer

Code: `dbms/optimizer/` (`stats.py` ANALYZE, `estimate.py` selectivity and cardinality,
`rewrite.py` the seven rules, `planner.py` cost model, join enumeration and physical plans,
`cli.py`). Command: `bin/opt`.

## 1. Statistics and where they live

`opt analyze --db DIR --table T [--buckets 20]` (or `ANALYZE T` in the shell) computes, exactly
and without sampling:

| statistic | how |
|---|---|
| `n_tuples` | counted during the scan |
| `n_pages` | the heap file's data pages (the directory pages are not counted) |
| `width` | average encoded record length in bytes |
| per column `ndv`, `min`, `max` | one pass over the column's values in sorted order |
| per column `avg_width` | average encoded bytes of the column (an addition: projections need per-column widths) |
| per column equi-depth histogram | k buckets cut at every n/k-th sorted value; each keeps `lo`, `hi`, `count`, `ndv` |

The table is scanned once. Each column's values are written to their own temp file, and each file
is sorted by **the Module 2 external sort**: a `sort` operator over a reader of that file,
configured with the analyze command's `--frames`. So a large column is never held in memory. A
table with more columns than B − 1 is scanned once per batch of B − 1 columns, since each column
needs a frame to write into. Equal values that straddle a cut appear in both buckets, which is
what equi-depth means when cuts fall at fixed ranks. The statistics are stored in the catalog
record of the table (the layout is in DESIGN_M1). The catalog is rewritten in full, and the same
statistics always give the same bytes, so **running ANALYZE twice leaves `catalog.db`
byte-identical** (tested).

**Without ANALYZE.** The catalog still knows every table's tuple and data page counts from Module 1.
Columns then have no ndv, range or histogram, and these documented defaults apply:

| without statistics | default |
|---|---|
| `c = v` | 0.1 (System R) |
| `c < v`, `c > v` | 1/3 (System R) |
| `R.a = S.b` | `1 / max(rows(R), rows(S))`: assume a key joined to a foreign key |
| column width | INT 4 bytes, `VARCHAR(n)` 2 + n/2 bytes |

Planning without statistics always produces a valid plan (tested).

## 2. Selectivity

| predicate | implemented |
|---|---|
| `c = v` | the buckets whose `[lo, hi]` contains v: sum of `count(b) / (n_tuples × ndv(b))`; 0 if v is outside [min, max]; `1/ndv(c)` without a histogram |
| `c < v`, `c <= v` | fraction below v: whole buckets with `hi < v`, plus `count(b) × (v − lo)/(hi − lo)` of the bucket containing v; without a histogram `(v − min)/(max − min)` |
| `c > v`, `c >= v` | 1 − the fraction below v |
| `c <> v` | 1 − sel(c = v) |
| `p AND q` | sel(p) × sel(q) (independence) |
| `p OR q` | sel(p) + sel(q) − sel(p) × sel(q) |
| `NOT p` | 1 − sel(p) |
| `R.a = S.b` between two inputs | output = `n(R) × n(S) / max(ndv(R.a), ndv(S.b))` |
| any other predicate between two inputs | 0.1 |
| `a = b` within one input | `1 / max(ndv(a), ndv(b))`; `a <> b` is 1 − that; other comparisons 1/3 |

A node's rows are `ceil(input rows × selectivity)`, never below 1. Its pages are
`ceil(rows × width / 4096)`. A projection reduces width, and so the pages of everything above it.
After a filter, a column's ndv is capped at the filter's output rows.

**Deviations from the handout's defaults, and why:**

- **Strictness is ignored for ranges.** The interpolation is continuous, so `c < v` and `c <= v`
  get the same estimate. The handout's worked example needs exactly this: `age > 60` on ages
  20..70 is `10/50 = 0.2`, and 2000 rows, only if the point v itself carries no extra mass.
  `test_handout_worked_example` reproduces 200, 2000 and 40, and `test_join_estimate_uses_max_ndv`
  the join's 10000.
- **A value spanning several buckets sums them.** The handout says "find the bucket holding v". A
  heavy value cut across buckets would then be estimated from one bucket only. Summing every
  bucket that contains v fixes that (`test_histogram_equality_and_range`: a value making up half
  the column is estimated at exactly 0.5).
- **Strings are interpolated** by mapping their first 8 bytes to an integer (big-endian), which
  keeps byte order, as PostgreSQL's `convert_string_to_scalar` does.

## 3. Rewrite rules

Applied in this order to the logical plan before any costing: rule 1, then rules 2 to 5 as one
bottom-up pushdown pass, then 6, then 7. Each rule can be switched off separately for the
ablation in REPORT_M4.

| # | rule | precondition | what breaks if it is applied without it |
|---|---|---|---|
| 1 | `select[p and q]` becomes `select[p](select[q])` | none | nothing; it only makes rule 2 possible |
| 2 | push a select into the side of a join or cross | every column it uses is on that side | a predicate needing both sides would be evaluated where one side is missing |
| 3 | `select[a = b]` over `cross` becomes `join[a = b]` | the predicate is an equality between a column of each side | a non-equality would then be offered to hash and sort-merge, which cannot evaluate it |
| 4 | push a select below `rename` (rewriting `t.c` to the inner name), `union` and `union_all` (into both branches, with the right branch's names mapped by position), and through `project`, `distinct` and `sort` | the select can then move past something below them; a select over a plain renamed scan stays where it is | none for correctness; moving past a `limit` would be wrong, so no rule does |
| 5 | push a select below `group_by` | it uses grouping columns only, and the group_by has grouping columns | a predicate on an aggregate (`t.n > 3`) moved below would filter tuples instead of groups: that is HAVING against WHERE. Without grouping columns a group_by returns one row even for an empty input, so even `WHERE 1 = 2` must stay above it |
| 6 | insert a `project` below each `join`, `cross`, `group_by` and `sort`, keeping only the columns needed above it plus the ones the operator itself needs | never drop a column a predicate or key above still uses | dropping a column still needed would make an upper predicate unresolvable |
| 7 | remove a `project` that keeps every column in order, and merge adjacent `select`s | the projection renames nothing (`names` absent) | none |

Rule 6 inserts projections only at the inputs of operators that move pages: joins, sorts and
grouping. A projection under a pipelined select or rename would save nothing.

The worked example reproduces exactly (`test_handout_rewrite_example`; `opt explain --rewrites`
prints both trees):

```
before                                  after
project [e.name]                        project [e.name]
  select (e.did = d.did and e.age > 60    join (e.did = d.did)
          and d.budget > 1000)              project [e.name, e.did]
    cross                                     select (e.age > 60)
      rename e                                  rename e
        scan Emp                                  scan Emp
      rename d                              project [d.did]
        scan Dept                             select (d.budget > 1000)
                                                rename d
                                                  scan Dept
```

Rule 1 split the conjunction, rule 2 sent the two single-table predicates to their sides, rule 3
made the join, and rule 6 added the two projections. The select stays above `rename e`, as in the
handout, because rule 4 moves a select through a rename only when it can then go further.

## 4. Cost model

Cost is page I/O, reads plus writes, with CPU ignored. B is the frames given with `--frames`. For
an input X of a join or a sort:

- `size(X)` = estimated output pages of X.
- `read(X)` = pages read to obtain X's tuples once. For a **base access path** (a scan under
  filters, projections and renames) that is the table's data pages, because the scan reads all
  of them whatever its filters keep. For anything else it is `size(X)`, read back as if
  materialized.
- `produce(X)` = 0 for a base access path, since `read(X)` already covers it; otherwise X's own
  cost.

| operator | cost |
|---|---|
| seqscan | M (data pages) |
| filter, project, rename, limit | 0 of its own |
| sort | `2 × size × (passes − 1)` on top of its input, with `passes = 1 + ceil(log(ceil(M/(B−1))) / log(B−1))` (1 when M ≤ B − 1) |
| distinct, group_by | sort mode: as sort; hash mode: 0 if the groups fit in (B−1)/2 frames, else `2 × size(input)`; the cheaper mode is chosen, hash on ties; sort when there are no grouping keys or B < 4 |
| nested_loop_join | `produce(L) + produce(R) + read(L) + ceil(size(L)/(B−2)) × read(R)` |
| hash_join | `produce(L) + produce(R) + read(L) + read(R) + 2(size(L) + size(R))` |
| sort_merge_join | `produce(L) + produce(R) + Σ over L, R of [read(X) + 2 size(X)(passes(X) − 1)] + size(L) + size(R)` |
| union, intersect, except | both inputs sorted; union_all costs nothing of its own |

**These are the handout's formulas, with one deliberate change.** With bare scans as inputs,
`read = size = M`, and they reduce exactly to `M + ceil(M/(B−2))N`, `3(M + N)` and
`sort(M) + sort(N) + M + N`. The change is the sort's run count. The executor's pass 0 makes runs
of B − 1 pages (DESIGN_M2), so the cost model counts `ceil(M/(B−1))` runs and treats an input as
fitting in memory only up to B − 1 pages. The handout asks that the two modules agree, and says
that when they do not, one should be fixed. The executor's B − 1 is forced by the frame budget, so
the formula is the one that changed. The handout's DP example is unaffected: none of its sorts
crosses the boundary. The handout's DP
table in fact uses this convention: an intermediate outer is charged its own cost plus M again
(`110 + 20 + 1×400`). The generalization only matters for a filtered base input. There the table is
still read in full (`read`), but the number of blocks or sorted pages comes from the filtered size.
Every node of `opt explain` and of the physical plan JSON carries `est_rows` and `est_cost`, the
cumulative cost of producing that node's output, so the root's `est_cost` is the plan's cost.
REPORT_M2 measured where the executor departs from these formulas (directory pages, the streaming
final merge, partition overflow, tiny partitions), and REPORT_M4 compares predictions with
measured I/O.

## 5. Join enumeration

After rewriting, every maximal tree of `join` and `cross` nodes is a join region. Selects resting
on it, whose predicates rules 2 and 3 have already reduced to multi-table ones, belong to the
region too. Its leaves are base access paths or whole subqueries, planned recursively. Its
predicates are all the conjuncts of its join conditions and of those selects. The region is
enumerated in Selinger style:

```
best[{r}] = the leaf's plan (its scan, filters and projections)
for k = 2 .. n, for every subset S of size k (in a fixed order):
    for every r in S with best[S - {r}] known:
        preds(r) = predicates using r and something in S - {r} (and nothing outside S)
    candidates = the r with preds(r) not empty
    if there are none: skip S ("no join predicate, skipped"), or, in the second pass, take every r
    for each candidate r and algorithm (nested loop always; sort-merge and hash only with an
    equality in preds(r)):
        cost(best[S - {r}] as outer, r as inner)
    best[S] = the cheapest; ties go to the first found
if best[all leaves] is unknown: run the whole enumeration again as the second pass
answer = best[all leaves]
```

If the query is connected, subsets with no predicate are skipped ("no join predicate, skipped" in
`dp-table`). If it is not, `best[all leaves]` is never reached, and the enumeration is repeated
with cross products allowed where a subset has no predicate. The estimate
of a subset does not depend on the order it was built in: the product of its leaves' rows and of
the selectivities of the predicates inside it. A hash join or sort-merge join takes the equality
predicates as its condition. Any non-equality predicate between the two sides goes into a filter
above it; a nested loop takes every predicate. Predicates and leaves are kept in a canonical order,
which is why **a comma join and the same query written with JOIN ON give the same physical plan**
(tested). After reordering, the output columns are put back in the order the query wrote them.

**Why left-deep.**

- *What it buys:* the inner of every join is a base table. A nested loop can therefore rescan it
  directly, and a hash join probes with a stream instead of a materialized result. The outer side
  is a pipeline: each join's output streams into the next join without being written. The search
  space is n! orders, or `n·2^(n−1)` subsets with this DP: 192 entries for six relations, instead
  of the far larger space of bushy trees (`(2n−2)!/(n−1)!` shapes, 30240 for n = 6).
- *What it gives up:* bushy plans, which join two independent pairs separately (A⋈B and C⋈D) and
  then join the two small results. With two selective but unrelated join pairs, a bushy plan can
  keep every intermediate small, while a left-deep plan must carry one growing result through
  every step.

**The worked example.** `scripts/dp_example.py` writes the handout's statistics into a catalog.
`opt dp-table` then prints:

```
subset        rows   pages     cost  best plan
{A}           5000     100      100  seqscan A
{Bp}           500      10       10  seqscan Bp
{C}          20000     400      400  seqscan C
{A,Bp}        1000      20      110  {Bp} outer (10 pages), nested_loop_join, A inner: 10 + 1x100 = 110
{A,C}            -       -        -  no join predicate, skipped
{Bp,C}        2000      40      410  {Bp} outer (10 pages), nested_loop_join, C inner: 10 + 1x400 = 410
{A,Bp,C}      4000     118      530  {A,Bp} outer (20 pages), nested_loop_join, C inner: 110 + 20 + 1x400 = 530
```

(with every alternative listed under each subset, `test_handout_dp_table_given_its_statistics`).

The handout's alternative for the full set, "{B′,C} outer (40 pages), BNL with A inner,
410 + 40 + 100 = 550", contains an arithmetic slip. At B = 32 a block is 30 pages, so a 40-page
outer needs `ceil(40/30) = 2` passes over A: `410 + 40 + 2×100 = 650`, which is what `dp-table`
prints. It changes nothing about the winner.

## 6. Output

```
opt analyze  --db DIR --table R [--buckets 20] [--frames 32]
opt plan     --db DIR --logical q.json --frames 32 [--json]   # physical plan, runs unedited in exec
opt explain  --db DIR --logical q.json --frames 32 [--rewrites]
opt dp-table --db DIR --logical q.json --frames 32
```

`explain` prints one line per node: `hash_join (R.b = S.b) est_rows=2000 est_cost=330`. With
`--rewrites` it first prints the logical plan as compiled and after the rules. Every physical plan
the optimizer emits runs unedited in the executor, at B = 4, 9 and 64, on queries covering joins,
grouping, distinct, set operators and subqueries. The results match the unoptimized baseline's
(`test_every_physical_plan_runs_unedited_and_matches_the_baseline`) and PostgreSQL's (REPORT_M4).

**The unoptimized baseline** (`unoptimized_planner`) is what "no optimizer at all" means in
REPORT_M4: no rewrite rules, FROM items joined in written order, and every equi-join a hash join.
Grouping and DISTINCT pick their mode by the same cost rule as the optimizer's, since the handout
fixes only the join algorithm. A WHERE equality that links a newly joined FROM item to an earlier one becomes
that join's condition. Every other predicate stays in one filter above all the joins. Without that
one concession, a comma join of three tables would be a literal cross product (3·10¹¹ tuples for q07) and
could not be run at all.
