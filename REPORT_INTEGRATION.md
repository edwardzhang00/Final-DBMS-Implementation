# Integration report

The whole system, SQL in and rows out, on the twenty-query workload in `workload/`. The data is
`python workload/generate.py DATA` (seed 2026): dept 100 rows, emp 10000, project 1000,
works 30000. This is the procedure the hidden workload will go through when it is published:

```
python scripts/run_workload.py --schema workload/schema.sql --data DATA --queries workload/queries --db DB --frames 32 --pg 127.0.0.1:55433/oracle --measure --json results.json
```

- **The database.** It is built by the shell itself: CREATE TABLE, LOAD, then ANALYZE with 20
  buckets.
- **Each query** goes through parse, resolve, rewrite, estimate, plan, and execute on a cold
  buffer pool of B frames.
- **The check.** The same schema and CSV files are loaded into **PostgreSQL 17** (C collation, so
  that string order is byte order as here). Every result is compared as it prints: as a list when
  the query ends in ORDER BY, as a bag otherwise, with AVG to four decimals (PostgreSQL's exact
  value rounded half away from zero, as `format_value` rounds here).

Machine: 12th Gen Intel(R) Core(TM) i5-12400F - 2.50 GHz, 16 GB RAM, Windows 10 Pro 22H2, Python 3.11.5. Times are wall-clock seconds of execution (preparing
the plan's operators and running them); optimization is done before the clock starts.

| query | rows | B = 32: page I/O | time (s) | root rows est. / actual | peak frames | B = 4: page I/O | time (s) | root rows est. / actual | peak frames | same as PostgreSQL (B = 32 / 4) |
|---|---|---|---|---|---|---|---|---|---|---|
| q01 | 1088 | 118 (118 r, 0 w) | 0.030 | 1250 / 1088 | 1 | 118 (118 r, 0 w) | 0.026 | 1250 / 1088 | 1 | yes / yes |
| q02 | 844 | 120 (120 r, 0 w) | 0.042 | 1300 / 844 | 31 | 120 (120 r, 0 w) | 0.041 | 1300 / 844 | 3 | yes / yes |
| q03 | 560 | 120 (120 r, 0 w) | 0.032 | 350 / 560 | 31 | 120 (120 r, 0 w) | 0.031 | 350 / 560 | 3 | yes / yes |
| q04 | 10 | 120 (120 r, 0 w) | 0.078 | 10 / 10 | 24 | 210 (165 r, 45 w) | 0.106 | 10 / 10 | 3 | yes / yes |
| q05 | 194 | 236 (236 r, 0 w) | 4.099 | 225 / 194 | 31 | 476 (408 r, 68 w) | 0.326 | 225 / 194 | 4 | yes / yes |
| q06 | 5 | 127 (127 r, 0 w) | 0.835 | 5 / 5 | 24 | 1334 (967 r, 367 w) | 1.409 | 5 / 5 | 4 | yes / yes |
| q07 | 622 | 265 (255 r, 10 w) | 0.391 | 744 / 622 | 32 | 402 (389 r, 13 w) | 0.441 | 744 / 622 | 4 | yes / yes |
| q08 | 5 | 293 (270 r, 23 w) | 0.748 | 5 / 5 | 32 | 633 (573 r, 60 w) | 0.826 | 5 / 5 | 4 | yes / yes |
| q09 | 20 | 118 (118 r, 0 w) | 0.030 | 20 / 20 | 17 | 118 (118 r, 0 w) | 0.028 | 20 / 20 | 3 | yes / yes |
| q10 | 20 | 120 (120 r, 0 w) | 0.027 | 267 / 20 | 32 | 122 (121 r, 1 w) | 0.027 | 267 / 20 | 4 | yes / yes |
| q11 | 4 | 120 (120 r, 0 w) | 0.034 | 20 / 4 | 32 | 147 (133 r, 14 w) | 0.049 | 20 / 4 | 4 | yes / yes |
| q12 | 0 | 120 (120 r, 0 w) | 0.032 | 100 / 0 | 32 | 122 (121 r, 1 w) | 0.030 | 100 / 0 | 4 | yes / yes |
| q13 | 10 | 118 (118 r, 0 w) | 0.036 | 34 / 10 | 17 | 118 (118 r, 0 w) | 0.036 | 34 / 10 | 3 | yes / yes |
| q14 | 63 | 122 (121 r, 1 w) | 0.030 | 64 / 63 | 32 | 122 (121 r, 1 w) | 0.031 | 64 / 63 | 4 | yes / yes |
| q15 | 5850 | 237 (237 r, 0 w) | 1.488 | 1026 / 5850 | 31 | 951 (951 r, 0 w) | 1.850 | 1026 / 5850 | 4 | yes / yes |
| q16 | 1 | 2 (2 r, 0 w) | 0.000 | 1 / 1 | 1 | 2 (2 r, 0 w) | 0.000 | 1 / 1 | 1 | yes / yes |
| q17 | 5 | 118 (118 r, 0 w) | 0.026 | 9 / 5 | 1 | 118 (118 r, 0 w) | 0.026 | 9 / 5 | 1 | yes / yes |
| q18 | 5 | 118 (118 r, 0 w) | 0.037 | 5 / 5 | 17 | 118 (118 r, 0 w) | 0.038 | 5 / 5 | 3 | yes / yes |
| q19 | 20 | 120 (120 r, 0 w) | 0.080 | 20 / 20 | 24 | 174 (147 r, 27 w) | 0.099 | 20 / 20 | 3 | yes / yes |
| q20 | 20 | 251 (248 r, 3 w) | 0.157 | 20 / 20 | 32 | 325 (298 r, 27 w) | 0.138 | 20 / 20 | 4 | yes / yes |

**Every result equals PostgreSQL's, at both buffer sizes, and no plan ever held more than B frames.**
The peak column is the buffer pool's own count of pinned plus reserved frames.

**The numbers explain themselves.**

- **Root estimates.** Most are within a few percent. The large misses are the cases analyzed in
  REPORT_M4: correlation (q15), the union upper bound (q10) and an empty EXCEPT (q12). q03
  (350 estimated, 560 actual) is skew meeting correlation. A third of the employees work in
  departments 0 to 9, and four of those are in the East, so the East has 2683 employees where a
  uniform join estimate expects 2000.
- **I/O.** A table scan reads its data pages plus one directory page: emp is 117 + 1 = 118, the
  floor of every emp query. Writes appear only where an operator spilled: hash partitions, sort
  runs, a temp copy of a nested loop's intermediate outer.
- **Time is not I/O.** q05 does 236 page reads at B = 32 but takes 4.1 s, because the optimizer,
  counting I/O only as the handout asks, chose a nested loop for its self join. A nested loop
  compares every pair in Python: 2009 × 10000 = 20,090,000 comparisons here. At B = 4 the same query uses a hash join, does
  twice the I/O, and runs 13 times faster. The time column is where a cost model that ignores CPU
  shows its blind spot.

## Random queries against PostgreSQL

`scripts/fuzz_sql.py` generates random queries over the whole SQL subset:

- 1 to 5 tables joined with commas or JOIN ON, including self joins and joins on city;
- nested AND/OR/NOT conditions, including column-to-column comparisons;
- GROUP BY with all five aggregates, and HAVING over any aggregate and the grouping columns;
- DISTINCT;
- ORDER BY, also on a column the select list drops, and LIMIT after an ORDER BY that covers
  every output column;
- the four set operators, ordered and limited, and inside subqueries in FROM under DISTINCT,
  GROUP BY, COUNT(*), a join and ORDER BY;
- subqueries in FROM, alone or joined to a base table.

It runs each one at several B (4, 7 and 32 unless `--frames` says otherwise) and compares the
result with PostgreSQL's as it prints: in order when ORDER BY covers every output column, as a bag
otherwise. A plan refused for needing more frames than B counts as a failure from B = 4 up. The
workload data was at scale 0.2 (`--scale 0.2`).

| run | queries | results compared | identical to PostgreSQL | failures |
|---|---|---|---|---|
| seed 1 (first generator: no ORDER BY, LIMIT or nested set operations) | 200 | 582 | 582 | 6 queries rejected here but accepted by PostgreSQL (`SELECT t.a, t.a ...`), since fixed (DESIGN_LOG) |
| seed 7, after the fix (first generator) | 500 | 1500 | **1500** | **0** |
| seeds 11, 23, 47 and 89, the generator above, B = 3, 4, 5, 7, 32, first run | 1200 | 5155 | 5155 | 22 queries refused at B = 4: they needed 5 frames (DESIGN_LOG, 10/31) |
| the same seeds, after union_all could copy its right input (DESIGN_M2) | 1200 | 5177 | **5177** | **0**; 823 refusals at B = 3 not counted |

## Sources

- PostgreSQL 17 served as the reference for correctness, as the handout specifies. Its documented
  semantics also settled the open questions: ORDER BY scope after set operations, output column
  names, and C collation for byte-order string comparison.
- Leis et al., *How Good Are Query Optimizers, Really?*, VLDB 2015, for reporting estimated against
  actual rows at the root and treating the large misses as correlation, not as noise.

## The shell

```
$ python bin/dbsh --db DB --frames 32
db> SET frames = 4;
frames = 4
db> EXPLAIN ANALYZE SELECT e.name, e.city, w.hours FROM emp e JOIN works w ON e.eid = w.eid WHERE e.level = 5 AND e.salary >= 105000;
project [e.name, e.city, w.hours] est_rows=1026 actual_rows=5850 q_error=5.70 est_cost=235 actual_io=951
  nested_loop_join (e.eid = w.eid) est_rows=1026 actual_rows=5850 q_error=5.70 est_cost=235 actual_io=951
    project [e.eid, e.name, e.city] est_rows=342 actual_rows=1950 q_error=5.70 est_cost=117 actual_io=118
      filter (e.level = 5 and e.salary >= 105000) est_rows=342 actual_rows=1950 q_error=5.70 est_cost=117 actual_io=118
        rename e est_rows=10000 actual_rows=10000 q_error=1.00 est_cost=117 actual_io=118
          seqscan emp est_rows=10000 actual_rows=10000 q_error=1.00 est_cost=117 actual_io=118
    project [w.eid, w.hours] est_rows=30000 actual_rows=30000 loops=7 q_error=1.00 est_cost=118 actual_io=833
      rename w est_rows=30000 actual_rows=30000 loops=7 q_error=1.00 est_cost=118 actual_io=833
        seqscan works est_rows=30000 actual_rows=30000 loops=7 q_error=1.00 est_cost=118 actual_io=833
total: 5850 rows, page I/O 951, time 1.830s, peak frames 4/4
```

That is the correlated-columns plan from REPORT_M4, read straight off the shell. The filter's
estimate is 5.7 times too low, so works is scanned `loops=7` times by a nested loop that the
estimate said needed only one pass.
