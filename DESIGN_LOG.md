In summary, 3 working sessions of many hours each. There were many more issues with the AI generated code than written here.
### 9/16 — First draft of EBNF and tuple-list handling

Trying to write the EBNF before writing the parser, covering relation definitions, tuple lists, newlines, and comments.

Tried: started from the Wikipedia EBNF article and had AI draft a grammar. The first version used `TupleList ::= Tuple { Newline Tuple } [ Newline ]` and `Tuple ::= TupleValue { "," TupleValue }`. The parser algorithm also parsed the first tuple immediately without skipping leading newlines.

What broke: relation definitions with a blank line after `{` or before `}` failed, and trailing newlines were not handled cleanly. The grammar required the first token of a tuple list to be a `Tuple`, but the tokenizer emits `NEWLINE` tokens. I found this by manually tracing `R (x) = {\n 1\n 2\n}` and by testing an empty tuple list and a tuple list with leading blank lines.

AI issue: AI produced a grammar that looked reasonable but did not match its own parser algorithm. Fix: changed `TupleList` to allow optional leading `Newline+` and trailing `Newline+`, and made the parser skip newlines before parsing the first tuple. Also documented the bounded-backtracking rule for newlines before commas.

### 9/18 — Tokenizer, parser cases, and instrumentation

Trying to get the hand-written tokenizer and recursive-descent parser through the required tokenizer and grammar cases, then add the Section 8.2 counters.

Tried: AI suggested checking parentheses by splitting on them before tokenizing. It also suggested incrementing `STATS["select_tuples"] += 1` directly inside the row loop in `op_select`. I implemented maximal munch for `>=`, `<=`, `!=`, and `-30`, and kept string literals entirely inside the tokenizer.

What broke: pre-tokenizing parenthesis matching fails case 5, `select[Name='Bob)'](R)`, because the `)` is inside a string. The per-row stats increment added overhead and made the counter code noisy. Also, the first `experiment.py` rebuilt and reparsed the entire relation program for every query and every `n`, so the early timings were dominated by setup rather than query execution.

AI issue: AI’s first tokenizer/parser suggestion was incomplete for quoted strings and keyword-as-identifier cases. I found it by running the Section 7 cases 1–9 and 10–17. Fix: strings are consumed by the tokenizer, `not`, `select`, `project`, and `rename` use one-token lookahead, and stats are accumulated in local counters and added once per operator. Later, `experiment.py` was changed to build `R` and `S` directly as `Relation` objects once per `n` and to parse query strings once at startup.

### 9/28 — Performance study and join hot loop

Trying to run the Section 8.3 experiment at 1k–64k and answer the report questions.

Tried: first used an AI-assisted `experiment.py` that rebuilt and reparsed the database for every query and size. After fixing that, I ran the nested-loop join. AI also suggested adding a per-pair type check inside the fast equi-join loop to catch `TypeMismatchError`.

What broke: the original experiment produced wall times dominated by parsing and database setup. After fixing that, the join times showed the expected quadratic trend. The per-pair type check made the hot loop slower and cluttered the innermost loop. For the equality fast path, the check is unnecessary for the generated integer/integer data; the generic condition evaluator still performs type checks for non-fast-path joins and selects. I found this by comparing before/after timings and by checking that the comparison counter still matched `n*m` while wall time rose.

AI issue: AI’s first performance script was slow and incomplete because it re-did parsing and database construction inside the timed function. It also put type validation in the innermost join loop. Fix: build the database once per `n`, reuse parsed query ASTs, and keep only `jp += 1` plus the equality test in the hot loop.

The two join timing tables below show the effect of removing the repeated parsing/database construction before the later hot-loop cleanup.

**(Join)**
**Before:**

| n     | m     | comparisons | wall time (s) | output tuples |
|-------|-------|-------------|---------------|---------------|
| 1000  | 1000  | 1,000,000   | 1.166         | 10,169        |
| 2000  | 2000  | 4,000,000   | 4.783         | 19,756        |
| 4000  | 4000  | 16,000,000  | 18.590        | 39,977        |
| 8000  | 8000  | 64,000,000  | 76.844        | 79,669        |
| 16000 | 16000 | 256,000,000 | 324.790       | 159,823       |
| 32000 | 32000 | 1,024,000,000 | 1346.656    | 319,454       |
| 64000 | 64000 | 4,096,000,000 | 5356.260    | 640,388       |

**After**

| n     | m     | comparisons   | wall time (s) | output tuples |
|-------|-------|---------------|---------------|---------------|
| 1000  | 1000  | 1,000,000     | 0.034         | 10,169        |
| 2000  | 2000  | 4,000,000     | 0.120         | 19,756        |
| 4000  | 4000  | 16,000,000    | 0.502         | 39,977        |
| 8000  | 8000  | 64,000,000    | 1.972         | 79,669        |
| 16000 | 16000 | 256,000,000   | 10.554        | 159,823       |
| 32000 | 32000 | 1,024,000,000 | 45.751        | 319,454       |
| 64000 | 64000 | 4,096,000,000 | 213.158       | 640,388       |
## Project 2

### 10/11 — Week one: the engine as a library

Trying to turn `ra.py` into the logical layer the four modules import, per handout section 1.3.

Tried: split it into `dbms/ra/tokenizer.py`, `parser.py`, `tree.py` and `operators.py`, with `ra.py` left as a thin command line over them. Added `parse_condition`, `parse_column`, `to_json`/`from_json`, `condition_text`, a `distinct(...)` operator in the RA syntax, and qualified names in projection lists (`project[Emp.name]`). The operators now take any re-iterable source of rows instead of in-memory sets.

What broke and changed: moving from sets to bags changed the meaning of **case 23**. `project[DID](Employees)` now returns three tuples (D1, D1, D2) instead of two, because projection keeps duplicates; `distinct(project[DID](Employees))` returns the old two (`test_case_23b`). No other case changed meaning. Some tests changed only in form: results are now lists compared as bags rather than Python sets, and `Project.attrs` holds `AttrRef` objects so that qualified names are possible (case 14 compares `[AttrRef(None, 'Name')]`). Relation literals now keep duplicate lines, since a relation is a bag. A test parses every module of `dbms/ra/` and checks that none calls `print`, `open` or `input`.

### 10/18 — Module 1: storage

Trying to reproduce the handout's page example (3.2) and buffer trace (5.3) exactly, and to make "the same command twice gives the same counters" hold.

What broke: my first determinism test scanned the same table twice in one process and got different counters (9 misses, then 10), because the second scan found pages cached by the first. The handout means two runs of the *command*, each with a cold pool. Fixed the test to reopen the database. The storage CLI already started every command with a fresh pool. Added `storage delete`, because without it the handout's page example (which deletes slot 1) could not be reproduced from the command line.

AI issues:
1. **A report that contradicted the code.** AI wrote REPORT_M1's recommendation to ship CLOCK, while every tool it had written defaulted to LRU. I found it when the audit (below) checked each report's claims against the command-line defaults. Fix: CLOCK is the default everywhere, and every REPORT_M2 number came out the same under it.
2. **A delete that trusted its rid.** AI's `HeapFile.delete` went straight to the page named in the rid. So `storage delete --rid 0:0` marked a slot dead on the *directory* page, and the table lost its data pages. Found by the audit's adversarial inputs. Fix: the page is looked up in the directory first, and anything else is an error.
3. **A name check that was quietly incomplete.** AI used table names as file names unchecked:
   - `catalog` overwrote `catalog.db`, and `../x` escaped the database directory;
   - `storage trace` deleted a table named `trace`.

   Its first fix compared names case-sensitively. So `Catalog` still landed on `catalog.db`, and on Windows `Dept` and `dept` shared one file, where a later scan crashed with a `struct.error`. The second audit found that. Fix: names must be identifiers, `catalog` is refused in any case, and names differing only in case are refused.
4. **A guarantee the code did not keep.** The loader's docstring said the whole file is checked before the first insert. But a record too long for a page was only caught by the insert itself, after the earlier rows were already in the table. Found by the audit. Fix: the pre-check computes each record's size.

### 10/25 — Module 2: the memory budget, and a leak that corrupted results

Trying to run every plan at B = 4 under a budget that is enforced, not just intended.

Tried first: one static frame budget per subtree. That broke in two ways.

1. A random plan test (`TestRandomPlans`) produced a hash join result that differed from the reference evaluator, but only when run after other tests. The cause was a chain. A plan that failed inside `open()` left `close()` crashing on a generator that was never created, so its temp files were never deleted. Temp file names restarted at 1 for every `Database`, so a later query reopened a leaked file and read its stale pages as its own partition. Fixed in three places: `close()` tolerates a failed open, temp names come from one process-wide counter, and temp files are always created empty (`DiskManager(fresh=True)`).
2. Group_by over a join over a distinct needed 5 frames, and the handout requires every plan at B = 4. Static budgets made each blocking operator keep its whole share for its whole life. Replaced them with two numbers per operator: `open_frames`, what it may hold while doing its blocking work, before its parent holds anything, and `stream_frames`, what it may hold while returning tuples. Every operator was reworked.

The buffer pool's `reserve()` refuses to exceed B, and that is what caught the next bug. Hash aggregation's recursion kept the parent partition's reader frame reserved while it processed the child partitions ("Memory budget exceeded: 0 pinned and 5 reserved out of 5"). Fixed by processing partitions from an explicit stack and releasing each reader frame before the partitions it spilled into. A sort-merge join with a blocking right input needed 5 frames, because the left sort held its in-memory result while the right sort opened. The left sort now writes its result out when the right one needs the frames. Union became a sort-merge of two sorted inputs instead of a sort over a concatenation, for the same reason.

Speed: q08 took 66 s because the block nested loop built a joined tuple and evaluated the compiled condition for every pair in Python. For conditions made only of column equalities, it now compares precomputed key tuples and builds the joined tuple on a match only. The comparison count is unchanged, still |R|·|S|. q08 went from 66 s to 8 s, and to 0.6 s after the optimizer fix below.

AI issues:
1. **Rows lost without an error.** AI's `PageBuilder.add` ignored the `None` that `insert_record` returns when a record does not fit, and counted the record anyway. A joined tuple wider than a page (a table of eight VARCHAR(255) columns joined to itself) therefore vanished from every sort, spill and set operation. `SELECT * FROM w x, w y ORDER BY x.k` returned 0 rows where PostgreSQL returned 36. Found by the second audit's hand-written queries against PostgreSQL. Fix: holding or spilling such a tuple is an error.
2. **Equal numbers in different buckets.** AI's hash key encoding tagged 40 as an integer and 40.0 as a float. So a hash join on `AVG(hours) = eid` returned 5109 rows at B = 4, where the nested loop chosen at B = 32 and PostgreSQL both return 18000. Found the same way, by running each query at several B. Fix: an integral float hashes as the integer.
3. **A fix that broke other plans.** To run `... UNION ALL SELECT DISTINCT ... ORDER BY` at B = 4, AI first made union_all always open a blocking right input early. A union_all over joins then needed up to 7 frames, and a top-level UNION ALL of two DISTINCTs needed 5. I only saw the second because a later fuzz round (seed 505) failed 3 queries at B = 4. Fix: open early only under a reader that holds frames, and only when the right input needs more frames to open than both inputs need to stream. That test did not check that the total went down, and sometimes it raised it (the third audit, below).
4. **Numbers written without being measured.** AI's REPORT_M2 gave the skewed join's heavy pairs as 575 × 576 and 576 × 552, without having counted them. Counting the key-7 rows in the measurement tables gave 564 and 563, so 317,532 pairs. It also explained a +122 I/O gap as "+120, one write and one read of each input", although the read-back is already in the formula. Fix: both corrected from the data.

### 10/28 — Module 3: SQL front end

Tried: the handout's grammar sketch as written, then a differential test against PostgreSQL 17 (`scripts/fuzz_sql.py`: random queries over the whole subset, run at B = 4, 7 and 32).

What broke: the first fuzz round, 200 queries, found 6 queries rejected here and accepted by PostgreSQL, all `SELECT t0.budget, t0.budget ...`. I had made a repeated unaliased column an error, because the executor refuses two columns with one qualified name. PostgreSQL accepts it, and the hidden workload is checked against PostgreSQL, so the second copy is now named `budget_2`. The 582 results the round did compare were all identical to PostgreSQL's. Reading the sketch against PostgreSQL also showed that `A UNION B ORDER BY x` would sort only B. ORDER BY and LIMIT after the last block now apply to the whole chain (GRAMMAR_SQL.md, 4.3). After both changes: 500 new random queries at three buffer sizes, 1500 results, all identical to PostgreSQL's.

AI issues:
1. **A query that ran and printed the wrong column.** For `SELECT d.did AS budget FROM dept d ORDER BY d.budget DESC`, AI's name resolution let the alias `budget` match `d.budget` as well. Rule 6 then pruned the alias column, so the query returned the budgets themselves instead of the `did` values. Nothing failed. Found by the first audit's adversarial queries; PostgreSQL returns the `did` values. Fix: a bare name means the unqualified column when there is one (`resolve_attr`), and ORDER BY looks at output names first.
2. **A name generated without looking.** AI's default aggregate names (`max_emp_age`) were never checked against the aliases. So `SELECT MIN(age) AS max_emp_age, MAX(age) FROM emp` failed, and `sql compile` emitted a plan with two columns of one name. Found by the second audit. Fix: a taken default name gets `_2`.
3. **An error path outside the `try`.** AI's shell tokenized its input before entering its `try` block. So a stray `#` or an unterminated string ended an interactive session with a traceback. Found by the second audit, which drove the shell through a terminal. Fix: the whole script runs inside the error handling.
4. **A grammar document the parser did not follow.** GRAMMAR_SQL.md gave `query ::= block { setop block } [order_by] [limit]`. But the parser only lifted ORDER BY from an unparenthesized last block, so `(SELECT ...) UNION (SELECT ...) ORDER BY did` was a syntax error, which PostgreSQL accepts. Found by the second audit. Fix: the parser now follows the rule.

### 11/01 — Module 4: optimizer

Tried: rules 1-7, then Selinger DP, checked against the handout's two worked examples and the twenty-query workload.

What broke:
1. Rule 4 pushed a select below a rename even when the rename sat directly on a scan, so the handout's rewrite example came out with `select` under `rename e` instead of above it. Rename, project, distinct and sort now only pass a select through when it can move further down.
2. For `SELECT *` over a reordered join, the columns came out in the wrong order. Rule 7 had removed the `SELECT *` projection as a no-op, and then the join order changed. The planner now restores the written column order after reordering.
3. The rule ablation showed q08 cheaper *without* rule 6 (385 against 666 page I/O). The projections rule 6 inserts between joins were acting as walls, so the four-way join was planned as three separate two-way joins in written order. Join-region collection now looks through those projections. q08's DP table now has all 15 subsets and the plan costs 293.
4. The ablation itself never finished. Without rule 2 or 3, comma joins stay cross products: q07 becomes 3·10^11 tuples. Plans with any node estimated above 2,000,000 rows are now reported with their estimate instead of being run.
5. EXPLAIN ANALYZE showed q-errors of 7 on the inner input of nested loop joins, because rows were summed over every rescan. It now reports rows per execution with `loops=N`, as PostgreSQL does. It also showed a q-error of 8.9 on a hash join whose non-equality predicate runs in a filter above it, because the join had been given the post-filter estimate. Fixed to 1.12.
6. A test planned with B = 64 on a database opened with a 16-frame pool, and the pool refused the reservation. `prepare()` now takes B from the pool itself, so the two cannot disagree.

AI issues (the breaks listed above were also in AI's code; three more):
1. **A rewrite that changed the answer.** AI's rule 5 only checked that every column a predicate uses is a grouping column. `WHERE 1 = 2` uses no columns, so it passed vacuously and was pushed below a GROUP BY without keys. Such a GROUP BY still returns its one row on empty input, so the query returned a row where PostgreSQL returns none. Found by the second audit's planner differential: each query planned with every rule set left out in turn, and the results compared. Fix: rule 5 needs grouping columns.
2. **A cost model that did not match the executor.** AI copied the handout's sort formula, with `ceil(M/B)` runs, while the executor it wrote makes runs of B − 1 pages. Found by the first audit. Fix: the cost model counts `ceil(M/(B−1))` runs (DESIGN_M4 section 4). Only q20's estimated cost at B = 4 moved, from 329 to 351.
3. **An explanation that did not fit its own numbers.** AI wrote that q15's filtered outer was 11 pages in 6 blocks, and that the bad estimate stopped mattering at B = 8. The measured nested-loop I/O (951, 594, 475, 356 and 237 at B = 4 to 16) only fits 14 pages, and EXPLAIN ANALYZE shows `loops=7`. It also described the unoptimized baseline as grouping by sort, which the code never did. Found by re-running each report's source script and reading the plans. Fix: the text was corrected; the measured numbers were right.

### 11/06 — Integration: the shell, the workload and PostgreSQL

Tried: `bin/dbsh` over all four modules, and `scripts/run_workload.py`. The script builds the database, runs the twenty queries at a given B and the same SQL on PostgreSQL 17, and compares the results, as bags or in order where the query sorts. All twenty match at B = 32 and B = 4 (REPORT_INTEGRATION.md).

AI issues:
1. **Report numbers written before they were measured.** AI's REPORT_INTEGRATION said q05's nested loop compares 10000 × 2004 pairs, and blamed q03's underestimate on correlation. The counters say 2009 × 10000 = 20,090,000 comparisons. Counting employees by region shows the East has 2683, where a uniform join predicts 2000: skew in `did`, not correlation. Found by checking each number against a run before keeping it. Fix: both corrected.
2. **A README that did not work from scratch.** Following it on a clean copy showed two problems:
   - the module examples needed tables the quick start never created;
   - the instruction (from Bonus 1) to pipe a file through `ra.py` never worked, since `ra.py` does not read standard input.

   Fix: a one-line build of the whole workload database, and a description of what `ra.py` actually reads.

### 11/07 — An independent audit against the handouts

A second reviewer, given only the specifications and the code, ran adversarial inputs through every tool. What it found, and the fixes:

1. **Wrong results:**
   - The bug: `SELECT d.did AS budget FROM dept d ORDER BY d.budget` returned budgets instead of `did` values. A bare reference to the alias `budget` also matched the qualified column `d.budget`. Rule 6 then treated the ambiguity as "not in this input" and pruned the alias column away.
   - The fix: a bare name now means the column with no qualifier when there is one (`resolve_attr`, shared by every module), and rule 6 keeps every column a reference could mean.
   - Also, ORDER BY now resolves against output column names first, so two output columns with the asked-for name are an ambiguity error, as in PostgreSQL.
2. `storage delete --rid 0:0` "deleted" a slot of the directory page and lost the table. A delete now first finds the page in the table's directory.
3. Table names became file names unchecked:
   - a table named `catalog` overwrote `catalog.db`, and `../x` was accepted;
   - `storage trace` used `trace.db`, so it deleted a table named `trace`.
   Names must now be identifiers and `catalog` is refused; the trace scratch file is `trace.tmp`.
4. Tracebacks instead of clean errors:
   - the shell accepted `SET frames = 2`, which the optimizer divides by `B - 2`;
   - `opt` failed on an unknown column in a hand-written logical plan and on `--buckets 0`.
   All three are now one-line errors.
5. The optimizer costed sorts with the handout's `ceil(M/B)` runs, but the executor makes runs of B − 1 pages. The cost model was fixed to match the executor (DESIGN_M4 section 4).
6. REPORT_M1 recommended CLOCK while every tool defaulted to LRU. CLOCK is now the default. Every REPORT_M2 number came out identical under it.
7. Smaller fixes:
   - a LOAD with a record too long for a page now fails in the pre-check, so it never loads part of the file;
   - hash aggregation counts the true size of AVG, MIN and MAX states against its frames;
   - `exec trace` prints only node lines;
   - every tool accepts `--frames`;
   - read commands no longer create a database directory by accident;
   - one failing query no longer stops `run_workload.py`.

Every one of these has a regression test. Seven of them (the trace scratch file, CLOCK as the default, `--buckets 0`, `exec trace`, `--frames`, read commands and `run_workload.py`), and the sort cost and the aggregate state sizes, got theirs only in the third audit, below; until then this sentence was not true.

### 11/08 — A second audit, aimed at what the fuzzer cannot generate

A second adversarial round wrote about 430 queries by hand and ran each at B = 3, 4 and 32 against PostgreSQL. It found the following.

**Wrong results:**
1. **Tuples wider than a page were dropped.** Joining a table of eight VARCHAR(255) columns to itself makes tuples of about 4120 bytes. `PageBuilder.add` ignored the failed insert, so every sort, hash spill and set operation returned 0 rows. Holding or spilling such a tuple is now an error ("does not fit on a page"); streaming plans still return them.
2. **Hashing separated equal numbers.** An AVG of 40.0 and an INT 40 hashed differently, so a hash join on `AVG = INT` lost rows at B = 4, and a hash DISTINCT over a UNION ALL of the two counted 2330 values instead of 2000 at B = 12. Integral REALs now hash as integers. The UNION ALL column was also typed REAL with its INT values left as integers, so it printed as "39" or "39.0000" depending on B; set operations now convert them.
3. **Rule 5 pushed a constant predicate below a GROUP BY without keys.** `SELECT t.c FROM (SELECT COUNT(*) AS c FROM emp) t WHERE 1 = 2` returned one row. Rule 5 now needs grouping columns.
4. **A default aggregate name could collide with an alias.** In `SELECT MIN(age) AS max_emp_age, MAX(age)` the second column took the same name, and the query failed. It is now `max_emp_age_2`. Subquery columns of one name (`t.pid`, `w.pid`) are likewise told apart as `pid`, `pid_2`.

**A plan refused at B = 4:** `... UNION ALL SELECT DISTINCT ... ORDER BY` needed 5 frames. union_all opened its right input only after the left was drained, while the sort above already held its buffer. Under a reader that holds frames, a right input whose open needs more than both inputs' streaming frames is now opened first. The first version did this everywhere. It made joins need up to 7 frames, and a top-level UNION ALL of two DISTINCTs need 5, which a later fuzz round caught (DESIGN_M2).

**Crashes, now one-line errors:**
- a lexical error in the shell (the tokenizer ran outside the `try`);
- non-UTF-8 input to any tool;
- conditions of about 1000 OR terms;
- table names that differ only in case, which share one file on Windows.

**Rejected but valid:**
- ORDER BY after a parenthesized last block;
- SUM of a SUM.

**Closer to PostgreSQL:**
- AVG kept 4 rounded decimals in its value, so an outer SUM drifted in the 4th decimal. It is now exact, and only printing rounds it, half away from zero, as `round()` does.
- The plan validation messages now start with the handout's exact wording, and the node's path comes after it.

Every fix has a regression test; non-UTF-8 input to the tools other than the loader, the REAL column of a set operation and the exact AVG got theirs only in the third audit, below. The 430 queries were rerun: everything matches PostgreSQL. The only refusals are the B = 3 plans no executor could fit and the one 5-frame shape left (DESIGN_M2).

### 11/09 — A third audit: the union_all rule, and checks weaker than their claims

A third review read the code against the handouts and the earlier fixes, ran about 110 hand-written queries at B = 3, 4, 5, 7 and 32 against PostgreSQL (AVG compared at four decimals), and re-ran every report's script. It found no wrong result and no report number that did not reproduce. What it found, how, and the fixes:

1. **union_all opened its right input first even when that needed more frames.** Found by reading `opens_right_first`: it tested `L_stream + R_stream < R_open` and never compared the totals. With a hash-mode distinct on the left and a sort-mode one on the right, `SELECT DISTINCT eid, did, name, salary FROM emp UNION ALL SELECT DISTINCT * FROM project ORDER BY eid` was refused at B = 4 on the scale 0.2 data, though opened lazily it fits in 4. A group_by without keys also counted as a reader that holds frames, so `SELECT COUNT(*) FROM (SELECT DISTINCT e.did FROM emp e UNION ALL SELECT DISTINCT d.did FROM dept d) t` was refused at B = 3. Fix: `UnionAll.mark_readers` works out the node's need both ways and opens the right input first only when that is smaller, and a group_by without keys passes its reader's frames on. Both queries now run in 4 and 3 frames, as does the review's hand-written plan.
   - The first version of this fix marked the right input as if it were always opened last. That made `A UNION ALL (B UNION ALL SELECT DISTINCT ...) ORDER BY` need 5 where the old rule fitted 4. Working that shape through by hand caught it before anything else ran. The right input is now weighed with what it sits beside in each mode (`test_a_union_all_nested_on_the_right_opens_its_hash_input_first`).
   - Checked by a sweep of 14,400 plans: the four set operations over every pair of 15 inputs (scans, sorts, both modes of distinct and group_by, a group_by without keys, the three joins, a limit, nested set operations) under 16 readers. No plan needs more frames than before, 1,856 need fewer, and the most any plan needs fell from 7 to 5. Every plan was run at every B from its minimum to 6, 45,110 runs: each gave the same bag as at B = 32, never held more than B frames, and left nothing pinned or reserved.
2. **A failed `reserve(n)` kept the frames it had already taken**, for the rest of a shell session. Found on a 4-frame pool, where `reserve(5)` failed and left 4 frames reserved. The pool now gives them back before raising.
3. **`storage trace` printed an empty line for an empty trace**: the conditional expression covered the whole print. Found by running it.
4. **Tracebacks on digits such as `²`**, which `isdigit()` accepts and `int()` refuses: `--rid`, `VARCHAR(²)` and a trace line. Found by adversarial input. Only ASCII digits are accepted now, with the usual one-line errors.
5. **Differences from PostgreSQL**, found by running the same queries on both:
   - `(SELECT ... LIMIT 5) ORDER BY did` was refused. It now sorts and then keeps 5 rows, as in PostgreSQL; only a second ORDER BY or a second LIMIT is refused.
   - A qualified name in ORDER BY after a set operation was accepted. It is now a `name` error, as in PostgreSQL.
   - What is still accepted here and refused there is listed in GRAMMAR_SQL 4.9: ORDER BY and LIMIT in a non-final block, which the handout's sketch allows, and the `did_2` and `count_star` names.
6. **`sql check` passed `CREATE TABLE catalog` and `CREATE TABLE EMP` beside `emp`**, which the catalog then refused. Found by comparing the analyzer's checks with the catalog's. The analyzer now calls the catalog's rule (`check_table_name`).
7. **A lexical error stopped a script before its first statement**, while a syntax error stopped it after the earlier statements had run, because the shell tokenized the whole script first. Found by reading `run_script`. Tokens are now produced as the parser asks for them.
8. **The oracle checks were weaker than the docs.** `run_workload.py` compared AVG at two decimals; results are now compared as they print, AVG at four. The fuzzer had never generated ORDER BY, LIMIT, HAVING beyond COUNT, or a set operation under a sort or in a subquery. It now does, and compares in order when ORDER BY covers every output column. Four new seeds (11, 23, 47, 89) at B = 3, 4, 5, 7 and 32 gave 5,155 results, all identical to PostgreSQL. They also gave 22 queries refused at B = 4 because they needed 5 frames, such as a one-table SELECT UNION ALL a three-way join, ORDER BY. The code from before this round refuses the same 22. See item 9.
9. **Plans that needed 5 frames at B = 4**, against the handout's "every plan at B = 4": item 8's 22 queries, and the shapes the review's sweep turned up (a join beside a hash-mode input, a join that reads a join on the right). The cause: a right input opened first is counted at its whole streaming minimum while the left runs, and a right input opened last opens beside the reader's buffer. union_all now has a third way to schedule its right input, spill right: it copies the input to a temporary file in its own open, through one writer frame, and reads the copy back one frame at a time after the left. `prepare()` offers it only when the plan does not fit in B frames lazily or right first, so a plan that fits without copying never copies. With it no plan needs more than 4 frames (DESIGN_M2 gives the argument). The other option, counting what each operator holds between its open and its first tuple, would have meant a third frame figure on every operator.
   - Checked by a larger sweep of 35,112 plans: the inputs of item 1 plus a join of joins (hash and nested loop), a sort-merge join over a nested loop, a hash-mode distinct over a join, a sort-mode group_by over a join and a union_all of a join and a hash-mode distinct, under 20 readers (item 1's, plus group_bys of both modes, a union and a nested loop join reading it). Without the copy 7,110 of them need 5 frames; with it none needs more than 4. Every plan ran at B = 4 and 5, and at 3 where it fits: 74,402 runs, each the same bag as at B = 32, never above B frames, nothing left pinned or reserved.
   - The same four fuzz seeds then gave 5,177 results identical to PostgreSQL, no mismatch and no refusal at B = 4. The one-table SELECT UNION ALL three-way join copies the join's output at B = 4 (919 page I/Os, against 659 at B = 5, where it is not copied).
   - The workload has no UNION ALL, so no report number moved.

The docs were corrected from re-runs: REPORT_INTEGRATION (times exclude optimization; four decimals; the fuzz runs), DESIGN_M2 (the three ways to schedule union_all's right input, why every plan fits in 4, which plans copy, what fits at B = 3), REPORT_M2 and ARCHITECTURE (P = B − 1 only over two pipelines), REPORT_M4 (9 of 20 queries at ratio 1.0; q08's estimates against its actual 76 and 2285 rows), DESIGN_M4 (the DP pseudocode, `a <> b`), README (the module examples run as written on a clean copy), and GRAMMAR.md and REPORT.md (project keeps duplicates, `distinct`, qualified names).

Every bug fix above has a test that fails with the fix reverted. The command-line behaviours the earlier entries claimed were tested now have tests. 191 tests pass under Python 3.9, 3.12 and 3.14. The workload at B = 32 and B = 4 matches PostgreSQL at four decimals; its I/O, rows, estimates and peak frames equal the reported run. `measure_m1.py`, `measure_m2.py` and `measure_m4.py` reproduce every table in the reports.

AI issues:
1. **A rule that did not do what its log entry said.** AI added the right-first rule in the second audit, and the log said it opened early "only when it lowers the requirement". The code tested something else. Nothing caught it, because the fuzzer never reached the rule. Re-planning the 14 earlier seeds gave 21,000 plans with 720 union_all nodes, and none opened its right input first: the generator never put a set operation under ORDER BY or in a subquery. Found by the third review reading the code.
2. **"Every one of these has a regression test", without the tests.** Two entries said every fix was tested. Nine fixes of the first audit had no test that would fail without them: seven command-line behaviours, the sort cost and the aggregate state sizes. Neither did three of the second audit's: non-UTF-8 input outside the loader, the REAL column of a set operation and exact AVG. Found by the third review searching the tests for the command lines, and by checking every listed fix against the tests.
3. **"The one shape that still needs 5", written from the formulas.** DESIGN_M2 named a single 5-frame shape, and treated it as acceptable although the handout asks for every plan at B = 4. The review's hash and sort plan, the sweep and the extended fuzzer found the others: a join beside a hash-mode input, and a join that reads a join on the right. Item 9 now runs all of them at 4.
4. **A README fix that fixed half.** The integration entry said the module examples worked. They still named `r.csv` and `q.sql`, which existed nowhere, and `layout --page 1` met a table with one page. Found by running them on a clean copy.
5. **Prose not checked against output.** Found by re-running each report's command:
   - REPORT_INTEGRATION said its times included optimization, but the clock starts after it;
   - REPORT_M2 and ARCHITECTURE said every hash join used P = B − 1, but over a sort at B = 16 it uses 8;
   - REPORT_M4 said "half the queries" (it is 9 of 20) and gave estimates as counts;
   - ARCHITECTURE called the worst estimates "all correlation", but the two worst are set operations;
   - DESIGN_M4's DP pseudocode and its `a <> b` rule did not match the code;
   - GRAMMAR.md and REPORT.md still described Bonus 1's set-valued project.
6. **This round's first draft of the union_all fix** made a right-nested union_all need 5 frames where the old rule fitted 4 (item 1 above).
