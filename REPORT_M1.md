# Module 1 report: storage engine and buffer pool

**Machine:** 12th Gen Intel(R) Core(TM) i5-12400F - 2.50 GHz, 16 GB RAM, NVIDIA GeForce RTX 3060 Ti (8Gb) - Windows 10 Pro 22H2
**Language:** Python 3.11.5

Every number below comes from `python scripts/measure_m1.py WORK_DIR`. Counters are
deterministic, so rerunning it prints the same tables.

## 1. Records per page: computed against measured

The computation follows section 3.3 of the handout: a page holds `floor(4088 / (L + 4))` records
of `L` bytes, because the 8-byte header leaves 4088 bytes and each record also costs a 4-byte
slot. Each table was loaded with 3000 rows. "Measured" is the average slot count over its full
pages, read from the page headers (the last, partly filled page is left out).

| schema | record bytes L | computed | measured |
|---|---|---|---|
| `a INT, b INT, c INT` | 12 | 255 | 255.0 |
| `a INT, b VARCHAR(12)`, strings of 1 to 12 characters | 7 to 18, mean 12.5 | 185 using the declared maximum L = 18; 247.1 using the mean L | 246.9 |
| `a INT, b VARCHAR(200)`, always 200 characters | 206 | 19 | 19.0 |
| `a INT, b VARCHAR(200)`, 1 to 200 characters | 7 to 206, mean 106.2 | 19 using L = 206; 37.1 using the mean L | 37.1 |

For fixed-length records the two numbers agree exactly: `floor(4088/16) = 255` and
`floor(4088/210) = 19`, measured on every full page.

For VARCHAR the gap depends on which L you compute with. The declared maximum gives a lower bound:
185 and 19. The pages actually hold 246.9 and 37.1, because a `VARCHAR(n)` record stores only the
bytes it has, so the planner would be wrong by 25 % and 49 % if it took the declared length. The
mean record length predicts the measurement almost exactly, 247.1 against 246.9. The small
remainder is the space at the end of each page: a page is closed when the *next* record does not
fit, so on average about half a record's worth of bytes per page is left over. This is why Module
4 takes the average width from ANALYZE and never the declared length.

## 2. Replacement policies

No traces were published with the handout, so five were made (`workload/traces/`, written by
`measure_m1.py`, seed 3005). Trace 3 is the one the handout describes:

| trace | what it is |
|---|---|
| trace1 | 5000 accesses, uniform over 200 pages |
| trace2 | 5000 accesses, 80 % to a hot set of 50 pages, 20 % to 200 cold pages |
| trace3 | sequential scan of an 80 page file, repeated 10 times (800 accesses) |
| trace4 | a 300 page scan done twice, each scan access followed by a lookup in 10 hot pages |
| trace5 | 5000 accesses with a Zipf-like (Pareto 1.1) skew over 100 distinct pages |

Each page id is fetched and immediately unpinned as clean, as the handout specifies for
`storage trace`.

| trace | B | LRU hit rate | CLOCK hit rate |
|---|---|---|---|
| trace1 | 4 | 0.020 | 0.020 |
| trace1 | 16 | 0.080 | 0.080 |
| trace1 | 64 | 0.316 | 0.319 |
| trace1 | 256 | 0.960 | 0.960 |
| trace2 | 4 | 0.048 | 0.048 |
| trace2 | 16 | 0.198 | 0.195 |
| trace2 | 64 | 0.678 | 0.642 |
| trace2 | 256 | 0.950 | 0.950 |
| trace3 | 4 | 0.000 | 0.000 |
| trace3 | 16 | 0.000 | 0.000 |
| trace3 | 64 | **0.000** | **0.000** |
| trace3 | 256 | 0.900 | 0.900 |
| trace4 | 4 | 0.096 | 0.086 |
| trace4 | 16 | 0.317 | 0.287 |
| trace4 | 64 | 0.489 | 0.468 |
| trace4 | 256 | 0.492 | 0.485 |
| trace5 | 4 | 0.723 | 0.687 |
| trace5 | 16 | 0.931 | 0.924 |
| trace5 | 64 | 0.977 | 0.975 |
| trace5 | 256 | 0.980 | 0.980 |

(`measure_m1.py` also prints hit, miss and eviction counts for every row.) Once every distinct
page fits, the hit rate reaches its ceiling of `1 - distinct / accesses`, for example
`1 - 80/800 = 0.900` on trace 3 at B = 256. Trace 4 is the exception there: its 310 distinct pages
do not fit in 256 frames, so it stays at 0.49 against a ceiling of 0.742.

**What LRU does on trace 3 at B = 64: it never hits, 800 misses out of 800 accesses.** When page
p is requested again, 79 other pages have been used since its last access, and LRU keeps only the
64 most recent ones, so p is always among the evicted. In a cyclic scan longer than the pool, LRU
evicts exactly the page that will be needed soonest. This is sequential flooding (O'Neil et al.,
section 1). CLOCK does no better here, because with nothing pinned and every bit set by the same
sweep it behaves like FIFO.

**Which policy to ship: CLOCK, and it is the default of every tool** (`--policy lru` remains
available). On the traces with a hot set (2, 4, 5), LRU is ahead by up to 0.036 wherever the
pool is too small for every page, with one exception: trace 2 at B = 4, where CLOCK has 2 more hits
out of 5000. On the uniform trace 1 the two are within 0.003 of each other (CLOCK ahead at B = 64),
and both fail identically on the scan.

- CLOCK's bookkeeping on a hit is setting one bit. LRU has to move the frame in a list on every
  access, which in a concurrent server means taking a latch on the list for every page hit.
- In this system the pool serves sequential scans almost exclusively. There are no indexes, and
  each query starts from a cold pool. Where LRU's advantage is measured it is small.

The thing that would actually matter is not either policy but a scan-resistant one: LRU-K (LRU-2
would keep trace 4's ten hot pages through the scans) or a small ring of frames reserved for large
sequential scans. PostgreSQL ships both clock-sweep and such a ring (`BAS_BULKREAD`).

## 3. Scan cost of a 500 page table

`T(a INT, b VARCHAR(200))` with 19 records of 206 bytes per page: 9500 tuples on 500 data pages
plus 1 directory page.

| B | page reads | hits | misses | evictions |
|---|---|---|---|---|
| 2 | 501 | 0 | 501 | 500 |
| 8 | 501 | 0 | 501 | 494 |
| 64 | 501 | 0 | 501 | 438 |

The reads are 501 at every B: the 500 data pages plus the directory page. A scan visits each page
exactly once, so there is nothing for a bigger pool to cache, and the count neither falls nor
rises with B. Only the eviction count changes, because with more frames the first B misses land
in empty frames. At most one page is ever pinned: the test
`test_scan_of_table_twenty_times_b_pins_one_page` scans 80 pages at B = 4 and checks that
`pinned` never exceeds 1.

## Checklist

- The page example of section 3.2 reproduces exactly (`storage layout`, DESIGN_M1, and
  `test_handout_page_example`).
- The buffer example of section 5.3 reproduces exactly for both policies (`storage trace`,
  `test_handout_trace_lru`, `test_handout_trace_clock`).
- A scan of a table twenty times larger than B completes and never pins more than one page
  (the handout allows two).
- Running the same command twice gives identical counters
  (`test_same_command_twice_gives_identical_counters`).
- Deleting every record and scanning returns nothing, without error
  (`test_delete_everything_then_scan_returns_nothing`).
- Evicting a pinned page fails (`test_evicting_a_pinned_page_fails`), and unpinning at pin
  count 0 fails.

## Sources and what they changed

- Ramakrishnan and Gehrke, *Database Management Systems*, 3rd ed., ch. 9 (9.4 buffer manager,
  9.6 page formats, 9.7 record formats). The heap file is their "directory of pages", with a free
  byte count per data page, as the handout specifies. Their discussion of which component may hold
  pins is why `scan()` copies a directory page's entries and drops the pin at once: the scan then
  costs one frame, which Module 2's budget relies on.
- O'Neil, O'Neil and Weikum, *The LRU-K Page Replacement Algorithm for Database Disk
  Buffering*, SIGMOD 1993. It explains sequential flooding and why plain LRU cannot tell a page
  used once by a scan from a page used again and again. It changed the recommendation above from
  "ship whichever hits more" to "ship the cheaper one and fix scans differently". It is also why
  trace 4 mixes a scan with a hot set.
- PostgreSQL documentation and source, `src/backend/storage/buffer/README` (clock sweep, buffer
  access strategies). It supports shipping CLOCK, and its ring-buffer strategy is the fix
  proposed for flooding.
