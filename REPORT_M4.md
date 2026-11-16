# Module 4 report: query optimizer

**Machine:** 12th Gen Intel(R) Core(TM) i5-12400F - 2.50 GHz, 16 GB RAM, NVIDIA GeForce RTX 3060 Ti (8Gb) - Windows 10 Pro 22H2

**Language:** Python 3.11.5

No workload or dataset was published, so
both are in `workload/`.

- **Data:** `generate.py` with seed 2026 writes dept (100 rows, 1 page), emp (10000 rows,
  117 pages), project (1000 rows, 7 pages) and works (30000 rows, 118 pages).
- **Queries:** twenty of them in `queries/`, covering selections, 2- to 4-way joins written with
  commas and with JOIN ON, a self join, GROUP BY/HAVING, DISTINCT, ORDER BY/LIMIT, UNION, INTERSECT,
  EXCEPT and subqueries in FROM.
- **Correlated columns:** two pairs are correlated on purpose. `emp.salary` lies in a band
  determined by `emp.level`, and `dept.region` is determined by `dept.city`. Besides those,
  `emp.city` usually equals the city of the employee's department.

All tables were analyzed with 20 buckets. The numbers come from:

- `scripts/run_workload.py ... --pg ... --measure` (estimates, I/O, the unoptimized plan, and
  every result compared with PostgreSQL 17);
- `scripts/measure_m4.py` (ablation, B sensitivity, the correlated plan, the DP table).

**All twenty results are identical to PostgreSQL's, at B = 32 and at B = 4.**

## 1. Estimation quality

Estimated against actual rows at every node of the twenty chosen plans at B = 32. The q-error is
`max(est, act) / min(est, act)`, with 0 counted as 1. Actual rows are per execution: the inner
input of a nested loop is rescanned once per block of the outer, and its count is divided by those
loops.

| nodes | median q-error | 75th percentile | 90th percentile | worst | nodes above 2 |
|---|---|---|---|---|---|
| 137 (all nodes not under a LIMIT) | **1.00** | 1.11 | 1.60 | **100** | 11 |
| 173 (every node) | 1.00 | | | 200 | |

The 36 nodes under a LIMIT are reported separately. A `limit 5` stops pulling after five rows, so
the sort under it really does emit only 5 of the 1000 rows it was estimated to sort (q06, q-error
200; q20, 100). That is the limit working, not an estimate failing.

The worst real estimates, and why they are wrong:

| query | node | est. | actual | q-error | what went wrong |
|---|---|---|---|---|---|
| q12 | `except` | 100 | 0 | 100 | dept minus emp-departments is empty, but `except` is estimated as its left input; there is no statistic about the overlap of two relations |
| q19 | `join (e.city = d.city and e.did = d.did)` | 500 | 8106 | 16.2 | two join predicates multiplied as if independent, while an employee's city mostly *is* their department's city (section 4) |
| q10 | `union` | 267 | 20 | 13.4 | union is estimated as the sum of its inputs (an upper bound); 267 rows hold only 20 distinct cities |
| q15 | `filter (e.level = 5 and e.salary >= 105000)` | 342 | 1950 | 5.7 | independence between level and salary (section 4) |
| q16 | `filter (dept.city = 'Ottawa' and dept.region = 'East')` | 1 | 5 | 5.0 | city determines region, so the second predicate removes nothing, yet it was multiplied in |

Everything else is close: median 1.00, three quarters of the nodes within 11 %. Single-column
predicates on uniform columns are almost exact with 20 buckets. The skewed `emp.did` is captured
by the histogram: q17's `age = 30 and did = 7` is estimated at 9 rows against 5 actual. Every one
of the large errors is a correlation or a missing cross-relation statistic, the failure mode Leis
et al. measure in real systems.

## 2. Does optimization pay?

The unoptimized plan is described in DESIGN_M4 section 6: no rewrite rules, FROM items joined in
written order, a hash join for every equi-join (a WHERE equality linking a new FROM item becomes
that join's condition, so that comma joins are not literal cross products). Grouping picks its
mode by the optimizer's cost rule in both plans, so the ratio measures join order, join algorithm
and the rewrites. Both plans were executed and their page I/O measured.

| query | B = 32: est. cost | actual I/O | unoptimized I/O | ratio | B = 4: est. cost | actual I/O | unoptimized I/O | ratio |
|---|---|---|---|---|---|---|---|---|
| q01 | 117 | 118 | 118 | 1.00 | 117 | 118 | 118 | 1.00 |
| q02 | 118 | 120 | 454 | 3.78 | 118 | 120 | 418 | 3.48 |
| q03 | 118 | 120 | 454 | 3.78 | 118 | 120 | 418 | 3.48 |
| q04 | 118 | 120 | 454 | 3.78 | 118 | 210 | 885 | 4.21 |
| q05 | 234 | 236 | 738 | 3.13 | 342 | 476 | 2973 | 6.25 |
| q06 | 125 | 127 | 455 | 3.58 | 473 | 1334 | 2102 | 1.58 |
| q07 | 252 | 265 | 1667 | 6.29 | 276 | 402 | 7759 | 19.30 |
| q08 | 264 | 293 | 2745 | 9.37 | 326 | 633 | 31643 | 49.99 |
| q09 | 117 | 118 | 118 | 1.00 | 117 | 118 | 118 | 1.00 |
| q10 | 118 | 120 | 120 | 1.00 | 118 | 122 | 122 | 1.00 |
| q11 | 118 | 120 | 120 | 1.00 | 128 | 147 | 147 | 1.00 |
| q12 | 118 | 120 | 120 | 1.00 | 118 | 122 | 122 | 1.00 |
| q13 | 117 | 118 | 118 | 1.00 | 117 | 118 | 118 | 1.00 |
| q14 | 119 | 122 | 184 | 1.51 | 119 | 122 | 128 | 1.05 |
| q15 | 235 | 237 | 733 | 3.09 | 235 | 951 | 2974 | 3.13 |
| q16 | 1 | 2 | 2 | 1.00 | 1 | 2 | 2 | 1.00 |
| q17 | 117 | 118 | 118 | 1.00 | 117 | 118 | 118 | 1.00 |
| q18 | 117 | 118 | 118 | 1.00 | 117 | 118 | 118 | 1.00 |
| q19 | 118 | 120 | 443 | 3.69 | 118 | 174 | 807 | 4.64 |
| q20 | 253 | 251 | 1565 | 6.24 | 351 | 325 | 35251 | 108.46 |
| total | | 2963 | 10844 | 3.66 | | 5850 | 86341 | 14.76 |

**The range across the workload is 1.0 to 9.4 at B = 32, and 1.0 to 108 at B = 4.**

- **Ratio 1.0 (9 of the 20: q01, q09 to q13, q16 to q18).** These are single-table queries and
  set operations over single-table blocks, which have one plan up to pipelined filters.
- **1.5 to 9.4 at B = 32.** The joins. Pushdown shrinks the join inputs, rule 6 shrinks their
  width, and DP joins the small or selective inputs first: q08 starts with the projects estimated
  at 82 rows ⋈ works instead of emp ⋈ dept. q14, a grouped subquery joined to dept, gains least
  (1.51, and 1.05 at B = 4).
- **Large ratios at small B.** With 4 frames the unoptimized plan's hash joins on unfiltered,
  full-width inputs overflow their 3 partitions and fall back to nested loops (REPORT_M2), while the
  optimized plan joins filtered, narrow inputs. q20's unoptimized plan does 35251 page I/Os; the
  optimized one does 325.

**Predicted against measured.** At B = 32 the chosen plan's estimated cost is within 3 % of its
measured I/O on 17 queries. The gap is the directory page of each scan (+1 per table) plus a few
partial temp pages. The same directory page is the whole of q16's difference: 1 page predicted,
2 read. The exceptions are informative:

- **q07 and q08** (+5 % and +11 %): the outer of an upper nested-loop join is an intermediate
  result. The executor writes it to a temp file before reading it in blocks, and the cost model
  charges only the read.
- **At B = 4 several predictions are low** (q06 473 against 1334, q08 326 against 633): the cost
  model trusts the formulas' assumptions (partitions that fit, sorted inputs streamed), and at
  B = 4 those break, exactly as measured in REPORT_M2.
- **q15**: the estimate itself is wrong, which is section 4.

## 3. Which rule mattered

Each rule switched off in turn, everything else on, the whole workload re-planned and executed at
B = 32. A plan with any node estimated above 2,000,000 rows was not executed; its estimate is given
instead.

| rules | total I/O of the queries that ran | change | not run (estimated I/O) |
|---|---|---|---|
| all seven | 2963 | | |
| without 1 (split conjunctions) | 2965 | +2 (q07 265→267) | |
| without 2 (push select below join/cross) | 4621 | **+1923** (q05 236→353, q08 293→1114, q15 237→473, q20 251→1000) | **q07: est. 271,477, a 300,000,000-row cross product** |
| without 3 (cross + select to join) | 2698 | +0 on the 19 that ran | **q07: est. 249 for a plan with a 15,000,000-row cross product** |
| without 4 (select below rename/union) | 2963 | +0 | |
| without 5 (select below group_by) | 2963 | +0 | |
| without 6 (push projections down) | 3215 | **+252** (q07 265→283, q08 293→385, q20 251→393) | |
| without 7 (tidy) | 2963 | +0 | |

- **Rule 2 is worth almost everything.** Without it, filters run after the joins, so every join
  sees whole tables. The four join queries that ran cost up to 4× more, and q07, a three-way comma
  join with two selective filters, cannot run at all.
- **Rule 3 decides whether comma joins are joins.** It changed the I/O of none of the 19 queries
  that ran. With JOIN ON the join already exists. The two-table comma join q03 does become a cross
  product with the equality in a filter above it, but its filtered dept is a 1-page block, so the
  nested loop reads each input once either way: 120 page I/Os and 49,100 comparisons in both
  plans. But the one comma join with three tables (q07)
  becomes a cross product of 15 million rows without it. The optimizer even prices that plan at
  only 249, because each cross product is a nested loop with a 1-page block. The estimate of the
  *I/O* is not crazy; the CPU and the 15 million intermediate rows are what the I/O model does not
  see.
- **Rule 6 is worth 8 %.** Narrower inputs mean fewer pages per block and per partition. It took a
  bug fix to get there: at first rule 6 *cost* 252, because its projections between joins hid the
  join region from the DP (DESIGN_LOG, Module 4).
- **Rules 1, 4, 5 and 7 are worth nothing on this workload, for different reasons.**
  - *Rule 1:* the join planner splits conjunctions itself when it collects a region's predicates,
    and a single-table conjunct found there is applied at its leaf. Only the conjunct that rule 1
    alone frees before planning differs (q07, +2).
  - *Rules 4 and 5:* need a predicate *on the output* of a subquery or a union. The only one in
    the workload is q13's `t.n > 120`, which tests an aggregate and so may not move. q14 filters
    the joined dept, not the subquery. A query filtering a subquery on its grouping column
    (`WHERE t.did = 4`, tested in `test_having_predicate_never_moves_but_grouping_predicate_does`)
    is where rules 4 and 5 pay.
  - *Rule 7:* cosmetic, as the handout says.

## 4. Where it breaks: correlated columns

The query is q15:

```sql
SELECT e.name, e.city, w.hours FROM emp e JOIN works w ON e.eid = w.eid
WHERE e.level = 5 AND e.salary >= 105000;
```

In this data `salary = 30000 + 15000·level + [0, 15000)`, so `salary >= 105000` holds exactly when
`level = 5`. The two predicates select the same 1950 rows.

| B | chosen | est. rows of the filter | actual | q-error | est. cost | actual I/O | nested loop | sort-merge | hash join |
|---|---|---|---|---|---|---|---|---|---|
| 4 | nested loop | 342 | 1950 | 5.70 | 235 | **951** | 951 | 1175 | **657** |
| 6 | nested loop | 342 | 1950 | 5.70 | 235 | 594 | 594 | 827 | 509 |
| 8 | nested loop | 342 | 1950 | 5.70 | 235 | 475 | 475 | 621 | 475 |
| 12 | nested loop | 342 | 1950 | 5.70 | 235 | 356 | 356 | 621 | 473 |
| 16 | nested loop | 342 | 1950 | 5.70 | 235 | 237 | 237 | 443 | 451 |
| 32 | nested loop | 342 | 1950 | 5.70 | 235 | 237 | 237 | 415 | 485 |

(the last three columns are the measured I/O of the same query with the join algorithm forced.)

- **The estimate.** `sel(level = 5) × sel(salary >= 105000) = 0.175 × 0.195 ≈ 0.034`, so 342 rows,
  about 2 pages after projection. The cost model therefore believes the filtered emp fits in one
  block of B − 2 frames even at B = 4, making the nested loop cost one scan of each table (235).
- **The actual.** 1950 rows, 14 pages after projection: 7 blocks of 2 frames at B = 4, so works
  is scanned 7 times (EXPLAIN ANALYZE shows `actual_rows=30000 loops=7` on it, and 7 × 119 = 833
  of its I/O). The plan costs **951 page I/Os where a hash join costs 657: 45 % more.** At B = 6
  (4 blocks) it is 17 % more, and at B = 8 (3 blocks) the two tie at 475. The error stops mattering
  at B = 16, where 14 pages fit in one block.
- **The proof that the estimate is to blame.** With the redundant predicate removed
  (`WHERE e.level = 5`, the same 1950 rows), the filter is estimated at 1750 rows (q-error 1.11).
  At B = 4 the optimizer then chooses the **hash join**, estimated at 375 and measured at 657 page
  I/O.

**The assumption that failed** is independence: `sel(p AND q) = sel(p) × sel(q)` (DESIGN_M4,
section 2). It holds only when knowing one column tells you nothing about the other. Here knowing
the salary tells you the level exactly. q16 is the same failure with a functional dependency
(city → region, estimated 1, actual 5). q19 is the same failure across a join: the two join
predicates `e.did = d.did` and `e.city = d.city` are each about 1/ndv selective, but given the first,
the second is true 80 % of the time, hence 500 estimated against 8106 actual.

**The statistic that would have prevented it** is a multi-column statistic on the pair, gathered by
ANALYZE.

- **Functional-dependency degree** (PostgreSQL's `CREATE STATISTICS ... (dependencies)`): "salary
  band → level holds for 100 % of rows". With it, `sel(level = 5 AND salary >= 105000)` becomes
  `sel(salary >= 105000)`, which is 0.195, or 1950 rows.
- **A two-dimensional histogram, or the joint ndv of (level, salary-band).** These handle partial
  correlation as well.
- **For q19:** the joint ndv of `(emp.did, emp.city)`, used as the ndv of the composite join key in
  `n(R)·n(S)/max(ndv)` instead of multiplying two single-column selectivities.

## 5. The DP table of a four-relation query

q08 at B = 32, every subset and every alternative considered (`opt dp-table`):

```
SELECT d.region, COUNT(*) AS n FROM emp e JOIN dept d ON e.did = d.did JOIN works w ON w.eid = e.eid
JOIN project p ON p.pid = w.pid WHERE p.budget < 100000 GROUP BY d.region ORDER BY d.region;

subset         rows   pages     cost  best plan
{e}           10000      20      117  project [e.eid, e.did]
{d}             100       1        1  project [d.did, d.region]
{w}           30000      59      118  project [w.eid, w.pid]
{p}              82       1        7  project [p.pid]
{e,d}         10000      48      118  {d} outer (1 pages), nested_loop_join, e inner: 1 + 1x117 = 118
                                      alternative: {d} outer (1 pages), sort_merge_join, e inner: sort 1 + 1 + sort 117 + 20 = 139
                                      alternative: {d} outer (1 pages), hash_join, e inner: 1 + 117 + 2x21 = 160
                                      alternative: {e} outer (20 pages), nested_loop_join, d inner: 117 + 1x1 = 118
                                      alternative: {e} outer (20 pages), sort_merge_join, d inner: sort 117 + 20 + sort 1 + 1 = 139
                                      alternative: {e} outer (20 pages), hash_join, d inner: 117 + 1 + 2x21 = 160
{e,w}         30000     118      235  {e} outer (20 pages), nested_loop_join, w inner: 117 + 1x118 = 235
                                      alternative: {w} outer (59 pages), nested_loop_join, e inner: 118 + 2x117 = 352
                                      alternative: {w} outer (59 pages), sort_merge_join, e inner: sort 236 + 59 + sort 117 + 20 = 432
                                      alternative: {w} outer (59 pages), hash_join, e inner: 118 + 117 + 2x79 = 393
                                      alternative: {e} outer (20 pages), sort_merge_join, w inner: sort 117 + 20 + sort 236 + 59 = 432
                                      alternative: {e} outer (20 pages), hash_join, w inner: 117 + 118 + 2x79 = 393
{e,p}             -       -        -  no join predicate, skipped
{d,w}             -       -        -  no join predicate, skipped
{d,p}             -       -        -  no join predicate, skipped
{w,p}          2460       8      125  {p} outer (1 pages), nested_loop_join, w inner: 7 + 1x118 = 125
                                      alternative: {p} outer (1 pages), sort_merge_join, w inner: sort 7 + 1 + sort 236 + 59 = 303
                                      alternative: {p} outer (1 pages), hash_join, w inner: 7 + 118 + 2x60 = 245
                                      alternative: {w} outer (59 pages), nested_loop_join, p inner: 118 + 2x7 = 132
                                      alternative: {w} outer (59 pages), sort_merge_join, p inner: sort 236 + 59 + sort 7 + 1 = 303
                                      alternative: {w} outer (59 pages), hash_join, p inner: 118 + 7 + 2x60 = 245
{e,d,w}       30000     201      357  {e,w} outer (118 pages), nested_loop_join, d inner: 235 + 118 + 4x1 = 357
                                      alternative: {e,w} outer (118 pages), sort_merge_join, d inner: 235 + sort 354 + 118 + sort 1 + 1 = 709
                                      alternative: {e,w} outer (118 pages), hash_join, d inner: 235 + 118 + 1 + 2x119 = 592
                                      alternative: {e,d} outer (48 pages), nested_loop_join, w inner: 118 + 48 + 2x118 = 402
                                      alternative: {e,d} outer (48 pages), sort_merge_join, w inner: 118 + sort 144 + 48 + sort 236 + 59 = 605
                                      alternative: {e,d} outer (48 pages), hash_join, w inner: 118 + 48 + 118 + 2x107 = 498
{e,d,p}           -       -        -  no join predicate, skipped
{e,w,p}        2460      13      250  {w,p} outer (8 pages), nested_loop_join, e inner: 125 + 8 + 1x117 = 250
                                      alternative: {w,p} outer (8 pages), sort_merge_join, e inner: 125 + sort 8 + 8 + sort 117 + 20 = 278
                                      alternative: {w,p} outer (8 pages), hash_join, e inner: 125 + 8 + 117 + 2x28 = 306
                                      alternative: {e,w} outer (118 pages), nested_loop_join, p inner: 235 + 118 + 4x7 = 381
                                      alternative: {e,w} outer (118 pages), sort_merge_join, p inner: 235 + sort 354 + 118 + sort 7 + 1 = 715
                                      alternative: {e,w} outer (118 pages), hash_join, p inner: 235 + 118 + 7 + 2x119 = 598
{d,w,p}           -       -        -  no join predicate, skipped
{e,d,w,p}      2460      19      264  {e,w,p} outer (13 pages), nested_loop_join, d inner: 250 + 13 + 1x1 = 264
                                      alternative: {e,w,p} outer (13 pages), sort_merge_join, d inner: 250 + sort 13 + 13 + sort 1 + 1 = 278
                                      alternative: {e,w,p} outer (13 pages), hash_join, d inner: 250 + 13 + 1 + 2x14 = 292
                                      alternative: {e,d,w} outer (201 pages), nested_loop_join, p inner: 357 + 201 + 7x7 = 607
                                      alternative: {e,d,w} outer (201 pages), sort_merge_join, p inner: 357 + sort 603 + 201 + sort 7 + 1 = 1169
                                      alternative: {e,d,w} outer (201 pages), hash_join, p inner: 357 + 201 + 7 + 2x202 = 969
```

The winner starts from the one selective table, project (82 of 1000 rows estimated, 76 in
EXPLAIN ANALYZE), joins it to works first, which cuts 30000 rows to an estimated 2460 (2285
actual), and only then brings in emp and dept. Joining in written order costs
357 by the time it has reached three tables, more than the whole chosen plan (264). Measured, the
chosen plan does 293 page I/Os. The difference is the temp file the executor writes for each
intermediate outer (DESIGN_M2) plus the directory pages.

## 6. Changing B changes the algorithm

Queries whose chosen algorithms differ between buffer sizes (`measure_m4.py`):

| query | B = 4 | B = 8 | B = 16 | B = 32 | B = 128 |
|---|---|---|---|---|---|
| q05 | hash_join | hash_join | nested_loop_join | nested_loop_join | nested_loop_join |
| q07 | hash_join, nested_loop_join | nested_loop_join ×2 | nested_loop_join ×2 | nested_loop_join ×2 | nested_loop_join ×2 |
| q08 | …, hash_join, … | …, hash_join, … | nested_loop_join ×3 | nested_loop_join ×3 | nested_loop_join ×3 |
| q20 | hash_join ×2 | nested_loop_join ×2 | nested_loop_join ×2 | nested_loop_join ×2 | nested_loop_join ×2 |

The nested loop's cost falls as `ceil(M/(B−2))` falls, while the hash join's stays at 3(M + N), so
the same joins change algorithm as B grows, just as in the crossover measured in REPORT_M2.

## Checklist

- The rewrite example of section 3 reproduces exactly (`test_handout_rewrite_example`, DESIGN_M4).
- The DP table of section 5.1 reproduces given those statistics: 100, 10, 400, 110, 410, skipped,
  530 (`test_handout_dp_table_given_its_statistics`). The handout's 550 alternative is an
  arithmetic slip for 650 (DESIGN_M4 section 5).
- Every physical plan runs unedited in Module 2: all 20 workload plans at B = 4 and 32, matched
  against PostgreSQL. The test suite also covers B = 4, 9 and 64 against the unoptimized plan.
- Commas and JOIN ON produce the same physical plan
  (`test_commas_and_join_on_give_the_same_physical_plan`).
- Changing B changes the join algorithm: q05, q07, q08 and q20 above.
- Running ANALYZE twice changes nothing: `catalog.db` is byte-identical
  (`test_analyze_twice_changes_nothing`).
- With no statistics, planning still produces a valid plan using the documented defaults
  (`test_no_statistics_still_gives_a_valid_plan`).

## Sources and what they changed

- Selinger et al., *Access Path Selection in a Relational Database Management System*, SIGMOD 1979.
  The DP over left-deep orders and the "no cross product unless necessary" heuristic are theirs, as
  is the idea that the cost of a subplan is independent of how it is used later. The skip rule and
  the per-subset table in `dp-table` follow the paper's presentation.
- Leis et al., *How Good Are Query Optimizers, Really?*, VLDB 2015. Their finding is that
  cardinality errors, not cost models, cause bad plans, and that they grow with correlation and
  with the number of joins. That shaped this report: the workload's correlations were built in on
  purpose, the q-error is measured at *every* node, and the bad plan is traced to its estimate
  rather than to a cost formula.
- Ramakrishnan and Gehrke, ch. 15. The equivalence rules behind rules 1 to 6, including when a
  selection may pass a grouping, and the textbook join cost formulas this cost model generalizes.
- PostgreSQL documentation, *Extended Statistics* (`CREATE STATISTICS`, functional dependencies,
  n-distinct). It is the concrete form of the statistic proposed in section 4.
