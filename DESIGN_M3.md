# Module 3 design: SQL front end

Code: `dbms/sql/` (`parser.py` scanner and parser, `ast.py` syntax tree and `SQLError`,
`analyzer.py` scopes, resolution, type rules and translation, `cli.py`). Command: `bin/sql`.
Grammar: `GRAMMAR_SQL.md`. The logical plan is the Bonus 1 tree (`dbms/ra/tree.py`), extended
with the nodes SQL needs and written as JSON by `to_json`.

## 1. Where duplicate elimination happens (bag semantics)

Every relation in this system is a bag. A table may hold identical rows, and every operator keeps
duplicates unless its definition removes them. Duplicate elimination happens in exactly these
places, and nowhere else:

| SQL | plan | why there |
|---|---|---|
| `SELECT DISTINCT` | one `distinct` node directly above the `project` | DISTINCT is defined on the projected row, so it must come after the projection. Below it, two rows differing only in a dropped column would survive as duplicates. |
| `UNION` | a `union` node | UNION is defined to return a set. `UNION ALL` becomes `union_all`, which keeps everything. The two plans differ in exactly one node (case 4). |
| `INTERSECT`, `EXCEPT` | `intersect`, `except` | set semantics as specified: one copy of each qualifying row, whatever the inputs held. |
| `GROUP BY` | `group_by` | produces one row per distinct group by construction. |

And these places deliberately do **not** remove duplicates:

- **`project` never does.** `SELECT name, age FROM Emp` keeps two employees with the same name
  and age (handout 5.1). This is the one place where Bonus 1's set-valued projection changed
  (case 23 in `DESIGN_LOG.md`).
- **No `distinct` above a GROUP BY when every grouping column is projected.**
  `SELECT DISTINCT did, COUNT(*) ... GROUP BY did` has one row per `did` already, so a distinct
  there would sort or hash the result for nothing. If the select list leaves out a grouping column
  (`SELECT DISTINCT COUNT(*) ... GROUP BY did`), two groups can produce the same row. There the
  `distinct` is kept, because dropping it would be wrong.
- **Scans and joins** return every matching combination, duplicates included.

**Why SELECT DISTINCT is not free.** `distinct` is blocking. It must see every row before it can
know a row is new, so it either sorts the whole projected result (passes over the data on disk
once it exceeds B) or builds a hash table that spills when it outgrows its frames. A `project`
costs nothing: it is pipelined and does no I/O. Writing DISTINCT turns a streaming plan into one
with a blocking step, which is why the translator emits `distinct` only when DISTINCT is written
and never "to be safe".

## 2. Scope and name resolution

The FROM clause builds a list of scope entries, in FROM order. A base table contributes an entry
named by its alias, or by its table name if it has none, holding the table's columns. A subquery
must have an alias. It is translated on its own first, and its output columns, named by its select
list, become the columns of the entry. Two entries with the same name is a `name` error
(`FROM Emp, Emp`; `FROM Emp e, Emp m` is fine).

A column reference is resolved against the scope by these rules, in order:

1. **Qualified `t.c`.** `t` must name an entry, else "unknown table or alias 't'". `c` must be a
   column of `t`, else "unknown column 'c' in table 't'". The message says which part failed.
2. **Unqualified `c`.** The entries that have a column `c` are collected. None is
   "unknown column 'c'". Two or more is an `ambiguity` error naming every candidate, as in
   `column 'city' is ambiguous: it could be Emp.city, Dept.city`.
3. **`SELECT *`** expands, before anything else, to every column of every entry: FROM order,
   then schema order, each one qualified (`e.did`, `d.did`). Shared names are no problem here
   because the expansion qualifies them (case 2).
4. **ORDER BY** first looks at the select list's output names: aliases, aggregate names such as
   `n`, and the names of plainly selected columns. Two output columns with that name is an
   `ambiguity` error, as in PostgreSQL. Otherwise it looks at the FROM scope. A column that is in
   scope but not in the select list is allowed (case 9, see 4).
5. **JOIN ... ON** sees the entries joined so far, including the one being joined.
6. **GROUP BY** resolves against the FROM scope. A bare name found nowhere there may name a
   select-list alias of a column (`SELECT did AS d ... GROUP BY d`), as PostgreSQL allows. A FROM
   column of the same name takes precedence, as it does in PostgreSQL.

After resolution every column is written fully qualified (`Emp.did`, `e.name`). The only bare
names left in a plan are the outputs that have no table: aggregate results (`n`, `count_star`)
and select-list aliases. They are bare in the plan's schema too, so they resolve exactly. A
subquery renamed `t` re-qualifies all of them (`t.did`, `t.n`). The compiled plan of every test
query is checked for this.

**Columns of one name in a subquery.** `SELECT t.pid, w.pid ...` is a fine query on its own, but
as a subquery aliased `z` both columns would become `z.pid`. The later ones are therefore named
`pid_2`, `pid_3`, ... by a `project` with `names` above the subquery. This happens only when names
collide. PostgreSQL accepts such a subquery and names both columns `pid`, but then refuses `z.pid`
as ambiguous; here `z.pid` is the first column and `z.pid_2` the second (GRAMMAR_SQL 4.9).

**Aliases on columns.** `SELECT did AS d` must be visible to an outer query as `t.d`. The `project`
node therefore carries an optional `names` array with the output name of each column, `null` where
the column keeps its own. The executor's `project` understands the same field. Without it, a column
alias inside a subquery could not be translated. The same field gives a column selected twice
without an alias (`SELECT a, a`) a second name, `a_2`, because one schema cannot hold two columns
with the same qualified name. PostgreSQL accepts that query, so it is accepted here too.

## 3. Aggregation and types

- A query is aggregating if it has GROUP BY or an aggregate in its select list.
- In an aggregating query, the select list, HAVING and ORDER BY may use only grouping columns and
  aggregates. Anything else is an `aggregate` error naming the column (case 6).
- An aggregate in WHERE or in ON is an `aggregate` error (case 7), because those clauses run
  before grouping.
- Every distinct aggregate in the select list, HAVING and ORDER BY becomes one entry of the
  `group_by` node. An aggregate that HAVING or ORDER BY needs but the select list does not show is
  computed anyway and dropped by the projection.

**Aggregate naming rule.** An aggregate gets its `AS` alias when it has one. Otherwise it is named
`<func>_<argument with dots replaced by underscores>`, or `<func>_star` for `COUNT(*)`:
`count_star`, `avg_Emp_salary`, `max_e_name`. A default name that is already taken, by an alias in
the select list or by another aggregate, gets `_2`, `_3`, ... (`SELECT MIN(age) AS max_Emp_age,
MAX(age)` names the second `max_Emp_age_2`). The same function on the same argument, written
twice without an alias, is computed once. HAVING and ORDER BY refer to it by that name, as in
handout 5.4, where `HAVING COUNT(*) > 5` becomes `select (n > 5)`. PostgreSQL's default names are
different (`count`, `avg`), so a query that refers to a default name, `ORDER BY count_star` or
`t.count_star` from outside a subquery, runs only here (GRAMMAR_SQL 4.9).

**Types.** INT and VARCHAR for columns; aggregate results are INT (COUNT), BIGINT (SUM), REAL (AVG)
or the argument's type (MIN, MAX). The type rules:

- A comparison needs both sides of the same class, numeric or string; INT against VARCHAR is a
  `type` error even for `=` (case 10).
- SUM and AVG need a number: an INT column, or a SUM or AVG result from a subquery. COUNT, MIN and
  MAX accept either class.
- Set operators need the same number of columns and the same class in each position, else a
  `schema` error (case 14). The result's type in a position is the wider of the two (INT, then
  BIGINT, then REAL; the longer VARCHAR), the same rule the executor applies
  (`common_definition`).
- INSERT is checked against the table: the value count, each value's type, INT range and VARCHAR
  length.

## 4. Translation

Bottom to top, in the handout's canonical order:

1. FROM: one `scan` per table, under a `rename` when it has an alias, and a `rename` over each
   subquery; joined left to right with `join` (JOIN ... ON, condition attached) or `cross`
   (commas).
2. WHERE: one `select` with the whole condition, unchanged and fully qualified. It is never split
   or moved: that is Module 4's work.
3. GROUP BY: `group_by` with keys and `[func, argument, name]` aggregates.
4. HAVING: a `select` above the `group_by`, with aggregates replaced by their names.
5. SELECT: `project`, then `distinct` if DISTINCT was written (see 1).
6. ORDER BY: `sort`.
7. LIMIT: `limit`.
8. Set operators between whole blocks, left associative. ORDER BY and LIMIT written after the last
   block apply to the whole result (GRAMMAR_SQL 4.3). There ORDER BY names a result column, by the
   left block's output name; a qualified name is a `name` error, as in PostgreSQL.

**ORDER BY on a column the select list drops** (case 9). The sort stays above the projection, as
the canonical order requires. The projection is widened with the missing sort columns, and a final
`project` above the sort removes them again:

```
SELECT name FROM Emp ORDER BY age DESC
project [Emp.name]
  sort [Emp.age desc]
    project [Emp.name, Emp.age]
      scan Emp
```

PostgreSQL does the same with "resjunk" target columns. With DISTINCT this is impossible,
because the distinct would compare the extra column, so `SELECT DISTINCT name ... ORDER BY age`
is a `schema` error, as in PostgreSQL.

The seven handout examples (5.1 to 5.7) reproduce node for node, field for field
(`tests/test_sql.py::TestHandoutExamples` compares the JSON). The fifteen required cases are in
`TestRequiredCases`. Case 15's two four-table plans differ only in where the predicates sit: three
`cross` nodes under one `select` against three `join` nodes carrying them.

## 5. The logical plan JSON

| op | fields |
|---|---|
| `scan` | `table` |
| `select` | `condition`, `child` |
| `project` | `columns` (qualified names or output names), `names`? (array of string or null), `child` |
| `rename` | `to`, `child` |
| `join` | `condition`, `left`, `right` |
| `cross` | `left`, `right` |
| `group_by` | `keys`, `aggregates`: `[[func, column or "*", name], ...]`, `child` |
| `distinct` | `child` |
| `sort` | `keys`: `[[column, "asc" or "desc"], ...]`, `child` |
| `limit` | `n`, `child` |
| `union`, `union_all`, `intersect`, `except` | `left`, `right` |

Conditions are strings in the Bonus 1 condition syntax (`Emp.age > 25 and not d.city = 'X'`),
written by `condition_text` so that parsing them back gives the same tree. No physical name
(`seqscan`, `hash_join`, ...) can appear: the translator has no way to produce one, and a test
checks compiled plans for them. `sql compile --json` prints one plan for a file with one query and
a JSON array for several statements. `CREATE TABLE`, `INSERT` and the shell statements are printed
as `{"statement": ...}` objects. A `CREATE TABLE` earlier in the same file is visible to the
queries after it, so a file can create a table and query it.

## 6. Sources and what they changed

- Ramakrishnan and Gehrke, ch. 4 and 5. The translation of a SELECT block into
  σ/π/⋈/γ in a fixed order is theirs. Their treatment of bags against sets is what fixed the rule
  in section 1: duplicate elimination only where a construct is defined to return a set.
- Nystrom, *Crafting Interpreters*, ch. 6 (parsing expressions). It gave the grammar its shape: one
  function per precedence level, with left-recursive rules rewritten as loops. That is why
  precedence and associativity live in the grammar and not in the parser.
- The PostgreSQL documentation on `SELECT` (output column names, ORDER BY resolution, UNION/ORDER BY
  scope). It decided every case the handout leaves open: an alias without `AS`, ORDER BY after a
  set operation, ORDER BY on a column the select list drops, a repeated column, and an ambiguous
  ORDER BY name. The hidden workload is checked against PostgreSQL, so where the sketch and
  PostgreSQL disagree, this front end follows PostgreSQL and says so in GRAMMAR_SQL.md.

## 7. Command line

```
sql parse   --query "SELECT ..." [--explain]   # syntax only, no catalog; --explain prints the syntax tree
sql compile --db DIR --file q.sql [--json]     # logical plan (JSON, or an indented tree)
sql check   --db DIR --file q.sql              # prints the first error, exit code 1; nothing and 0 if valid
```
