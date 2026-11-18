# Video Demo
https://www.youtube.com/watch?v=bqyST6Z7yN4

# A database system, one module at a time

COMP 3005 Project 2: a working DBMS in Python, built on the Bonus Project 1 relational algebra engine.
SQL goes in, a plan comes out, and tuples are read from pages on disk through a buffer pool. Python
3.9 or later, standard library only (PostgreSQL is used only by the optional correctness scripts).

| module | what it is | tool | design | measurements |
|---|---|---|---|---|
| Bonus 1, refactored | relational algebra library: tokenizer, parser, tree, bag operators | `python ra.py` | `GRAMMAR.md` | `REPORT.md` |
| M1 | pages, heap files, LRU/CLOCK buffer pool, catalog, CSV loader | `python bin/storage` | `DESIGN_M1.md` | `REPORT_M1.md` |
| M2 | iterators, external sort, three joins, aggregation, set ops, under B frames | `python bin/exec` | `DESIGN_M2.md` | `REPORT_M2.md` |
| M3 | SQL scanner and parser, name resolution, types, translation to a logical plan | `python bin/sql` | `GRAMMAR_SQL.md`, `DESIGN_M3.md` | tests |
| M4 | ANALYZE, selectivity, seven rewrite rules, cost model, DP join ordering | `python bin/opt` | `DESIGN_M4.md` | `REPORT_M4.md` |
| integration | one shell over all four | `python bin/dbsh` | `ARCHITECTURE.md` | `REPORT_INTEGRATION.md` |

`DESIGN_LOG.md` records what broke along the way and which Bonus 1 test changed meaning.

## Quick start

```
python workload/generate.py data          # CSVs for the 20-query workload
python bin/dbsh --db mydb --frames 32     # then type SQL; statements end with ';'
```
```
db> CREATE TABLE emp (eid INT, name VARCHAR(20), age INT, did INT, salary INT, level INT, city VARCHAR(20), mgr INT);
db> LOAD emp FROM 'data/emp.csv';
db> ANALYZE emp;
db> SELECT level, COUNT(*) AS n, AVG(salary) FROM emp GROUP BY level ORDER BY level;
db> EXPLAIN ANALYZE SELECT name FROM emp WHERE age > 60;
db> SET frames = 4;
```

To build a database holding all four workload tables at once (`wdb` below), analyzed:

```bash
(cat workload/schema.sql; for t in dept emp project works; do echo "LOAD $t FROM 'data/$t.csv'; ANALYZE $t;"; done) | python bin/dbsh --db wdb
```

In PowerShell, write the statements to a file and run it with `-f`. Piping them straight into the
shell can prepend a byte-order mark, which the SQL scanner rejects:

```powershell
(Get-Content workload/schema.sql) + ('dept','emp','project','works' | ForEach-Object { "LOAD $_ FROM 'data/$_.csv'; ANALYZE $_;" }) | Set-Content -Encoding ascii build.sql
python bin/dbsh --db wdb -f build.sql
```

Each module also runs on its own. After `generate.py` and the `wdb` build above, these commands
run as written, in this order (any database directory can stand in for `wdb`):

```bash
printf '42,bob,7\n43,"x,y",9\n' > r.csv
python bin/storage create-table --db wdb --name R --schema "a:INT,b:VARCHAR(20),c:INT"
python bin/storage load   --db wdb --table R --file r.csv
python bin/storage scan   --db wdb --table R --frames 8 --limit 1 --json
python bin/storage layout --db wdb --table R --page 1
python bin/storage trace  --db wdb --file workload/traces/trace3.txt --frames 64 --policy lru --json
python bin/exec run   --db wdb --plan workload/plans/02_hash_join.json --frames 4 --json
python bin/exec trace --db wdb --plan workload/plans/02_hash_join.json --frames 4
python bin/sql parse   --query "SELECT a FROM R WHERE c > 1" --explain
python bin/sql compile --db wdb --file workload/queries/q08.sql --json > q.json
python bin/sql check   --db wdb --file workload/queries/q08.sql
python bin/opt analyze  --db wdb --table R --buckets 20
python bin/opt plan     --db wdb --logical q.json --frames 32 --json > plan.json
python bin/opt explain  --db wdb --logical q.json --frames 32 --rewrites
python bin/opt dp-table --db wdb --logical q.json --frames 32
```

The block uses bash syntax, so it runs as written in Git Bash. In PowerShell, three lines change.
`>` there writes UTF-16, which the JSON readers reject:

```powershell
Set-Content -Encoding ascii r.csv '42,bob,7','43,"x,y",9'
python bin/sql compile --db wdb --file workload/queries/q08.sql --json | Set-Content -Encoding ascii q.json
python bin/opt plan --db wdb --logical q.json --frames 32 --json | Set-Content -Encoding ascii plan.json
```

## Tests and correctness checks

```
python tests/run_tests.py          # every test: Bonus 1 cases, the library, M1, M2, M3, M4, shell
python scripts/run_workload.py --schema workload/schema.sql --data data --queries workload/queries --db wdb --frames 32 --pg 127.0.0.1:5432/postgres --measure     # results checked against PostgreSQL
python scripts/fuzz_sql.py --db wdb --pg 127.0.0.1:5432/postgres --count 300   # random SQL against PostgreSQL
```

The measurements in the reports come from `scripts/measure_m1.py`, `measure_m2.py`, `measure_m4.py`
and `run_workload.py`. Worked examples: `scripts/dp_example.py` builds the catalog of the Module 4
DP example.

---

## Relational Algebra Engine

A relational algebra engine implementing a tokenizer, recursive descent parser, parse tree printer, and evaluator for the six core relational algebra operators (select, project, rename, union, minus, times), plus intersect, join and distinct. Since Project 2 it is a library (`dbms/ra/`) and `ra.py` is its command line; it works on bags (see below).

## Running a Query
```
# Evaluate a query
python ra.py "project[Name](select[Age>30](Employees))"

# Print the parse tree without executing
python ra.py --tree "project[Name](select[Age>30](Employees))"

# Run all semantic test cases (18-25)
python ra.py
```

### Loading Your Own Data

`ra.py` evaluates a query against the relations defined in `DEFAULT_PROGRAM` in `ra.py`; to query your own relations, edit those definitions. For data loaded from files, use the DBMS shell (`python bin/dbsh`, with `CREATE TABLE` and `LOAD`).

### Running the Performance Study

```
# Generate data and run the full experiment
python experiment.py --match-rate 10 --out results.csv

# Run a subset of sizes
python experiment.py --sizes 1000 2000 4000

# Run only the join query
python experiment.py --queries join
```

### Generating Test Data

```
python datagen.py --n 10000 --m 10000 --match-rate 10 --out data/prog_10000.txt
```

### Relation Definitions

```
RelationName (Attr1, Attr2, ...) = {
  value1, value2, ...
  value1, value2, ...
}
```

- Values are numbers or strings.
- Strings with commas, spaces, parentheses, or quotes must be single-quoted.
- Two single quotes inside a quoted string represent one literal quote.
- Duplicate tuples are kept: relations are bags since Project 2 (before, they collapsed to one).
- Lines starting with `//` are comments.

### Operators

|Operator|Syntax|Description|
|---|---|---|
|Select|`select[condition](expr)`|Filters tuples by condition|
|Project|`project[attr1, R.attr2](expr)`|Selects attributes (qualified names allowed); keeps duplicates since Project 2|
|Distinct|`distinct(expr)`|Removes duplicate tuples (added in Project 2)|
|Rename|`rename[newName](expr)`|Renames the relation|
|Union|`expr union expr`|Set union (requires union compatibility)|
|Intersect|`expr intersect expr`|Set intersection|
|Minus|`expr minus expr`|Set difference|
|Times|`expr times expr`|Cartesian product|
|Join|`expr join[condition] expr`|Theta join (times + select)|

### Conditions

Conditions support comparisons (`=`, `!=`, `<`, `<=`, `>`, `>=`) combined with `and`, `or`, `not`, and parentheses. Operands can be numbers, strings, or attribute names (optionally qualified as `Relation.Attribute`).

### Precedence (highest to lowest)

1. Unary operators (`select`, `project`, `rename`, parentheses)
2. `times`, `join`
3. `intersect`
4. `union`, `minus`

All binary operators are left-associative.

### Condition Precedence (highest to lowest)

1. Comparisons (non-associative)
2. `not` (right-associative)
3. `and` (left-associative)
4. `or` (left-associative)

## Error Handling

The engine reports five categories of errors with position information where applicable:

- **Lexical errors**: Unterminated strings, invalid characters, malformed numbers
- **Syntax errors**: Missing parentheses, empty projection lists, missing operands
- **Name errors**: Unknown relations or attributes, ambiguous attribute references
- **Schema errors**: Union incompatibility, qualified name collisions
- **Type errors**: Comparing numbers to strings

All errors produce a clear message; no stack traces are shown. When the query given on the command
line fails, `ra.py` prints `Error: ...` on standard error and exits with status 1; `python ra.py`
with no query prints each case's error in place of its result.

## Known Limitations and Documented Decisions

### Self-Joins and Why `rename` Is Necessary

Consider the self-join:`rename[E2](Emp) join[Emp.MgrID=E2.EID] Emp`. Without `rename`, the only way to write this is: `Emp join[Emp.MgrID=Emp.EID] Emp`

This query is unanswerable in our algebra for two reasons.

1. **Qualified-name collision in the schema.**  
`join` is defined as `times` followed by `select`. The `times` operator concatenates the attributes of both inputs, qualified by relation name. If both inputs are named `Emp`, the combined schema contains duplicate qualified columns such as `Emp.EID` and `Emp.EID`, `Emp.MgrID` and `Emp.MgrID`. Our `op_join` raises a schema error on any qualified-name collision. Even if duplicates were allowed, `Emp.EID` would not identify which copy of `Emp` is meant.

2. **Ambiguous attribute references in the condition.**  
The join condition `Emp.MgrID = Emp.EID` is ambiguous. The resolver finds multiple columns matching `Emp.EID` and raises `Ambiguous attribute Emp.EID`. It cannot know whether `Emp.MgrID` should compare to the right-hand `Emp.EID` or to its own `Emp.EID`.

`rename[E2](Emp)` solves both problems by giving one copy of `Emp` a distinct relation name. The left side then has columns like `E2.EID` and `E2.MgrID`, while the right side still has `Emp.EID` and `Emp.MgrID`. The condition `Emp.MgrID = E2.EID` is now unambiguous: the employee’s manager ID equals the manager copy’s employee ID. So `rename` is not syntactic sugar here. It is the only way to make the two copies of `Emp` distinguishable.

### Duplicate Attributes in Projection

`project[Name, Name](R)` is rejected with a clear error. The assignment left this as a design decision; we chose to reject it rather than silently deduplicate.

### Keyword as Identifier

Keywords (`select`, `project`, etc.) may be used as attribute or relation names when the grammar position expects an identifier. For example, `select[union=3](R)` treats `union` as an attribute name. This is resolved by one token of lookahead.

### Qualified Name Collisions

After `times` or `join`, if both inputs contain an attribute with the same qualified name (relation name + attribute name), that is a schema error. This is why `rename` is required for self-joins.

### Union Compatibility

Two relations are union-compatible if they have the same number of attributes, the same attribute names in the same order, and compatible types position by position. Otherwise, `union`, `intersect`, and `minus` raise a schema error.

### Type Comparisons

Comparing a number to a string is a type error, not a silent `false`.

### Integer Literals

Integer literals are signed 64-bit values. Leading zeros are allowed (`007` is 7). There is no unary minus; the minus sign is part of the literal.

### String Literals

Strings are single-quoted. Two consecutive single quotes inside a string represent one literal quote. There are no backslash escapes. Strings may span multiple lines.

### Bare Values in Tuple Definitions

Inside a relation definition tuple, a bare token that is not a valid integer is treated as a string. Bare strings may contain any character except commas, whitespace, parentheses, braces, single quotes, or `//`.

## File Structure

```
.
├── ra.py                     Bonus 1 command line over dbms/ra
├── datagen.py, experiment.py Bonus 1 performance study
├── dbms/
│   ├── ra/                   tokenizer, parser, tree (+ JSON), bag operators
│   ├── storage/              Module 1 (+ temp files for Module 2)
│   ├── execution/            Module 2
│   ├── sql/                  Module 3
│   ├── optimizer/            Module 4
│   └── shell.py              integration
├── bin/                      storage, exec, sql, opt, dbsh
├── workload/                 schema, data generator, 20 queries, sample plans, buffer traces
├── scripts/                  measurements, the PostgreSQL workload check, the SQL fuzzer, the DP example
├── tests/                    one file per module, plus the Bonus 1 cases and a reference evaluator
├── images/                   report charts
└── *.md                      GRAMMAR, GRAMMAR_SQL, DESIGN_M1-4, REPORT, REPORT_M1, REPORT_M2, REPORT_M4, REPORT_INTEGRATION,
                              ARCHITECTURE, DESIGN_LOG
```

## Running the Tests

`python tests/run_tests.py` runs everything. Or one file at a time:

```
python tests/test_tokenizer.py   # Bonus 1 cases 1-9
python tests/test_parser.py      # Bonus 1 cases 10-17
python tests/test_semantics.py   # Bonus 1 cases 18-25 (case 23 now keeps duplicates; 23b is distinct)
python tests/test_library.py     # week one: layers, JSON round trip of every node, condition parser
python tests/test_storage.py     # Module 1
python tests/test_execution.py   # Module 2 (a few minutes: random plans against a reference evaluator)
python tests/test_sql.py         # Module 3: the 15 required cases and the 7 handout examples
python tests/test_optimizer.py   # Module 4 and the shell
```

#### Notes on Tests
The `test_extra_` tests in the three Bonus 1 files (`test_tokenizer.py`, `test_parser.py`,
`test_semantics.py`) catch regressions the 25 required cases miss.

`test_case_20b_self_join_without_rename_fails` is the assertion that backs the self-join explanation above: without `rename`, the qualified name `Emp.EID` collides with itself and the engine raises a `SchemaError` rather than producing a wrong answer.
