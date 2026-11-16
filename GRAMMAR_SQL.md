# SQL grammar (Module 3)

The SQL subset the front end accepts, in the same EBNF conventions as `GRAMMAR.md`:
`::=` defines, `|` separates alternatives, `{ }` is zero or more, `[ ]` is optional, `"x"` is a
terminal symbol, and upper-case words are keywords. The scanner and parser are hand written
(`dbms/sql/parser.py`), with no regular expressions and no parser generator.

## 1. Tokens

```
keyword     ::= SELECT | DISTINCT | FROM | WHERE | GROUP | BY | HAVING | ORDER | ASC | DESC | LIMIT
              | UNION | ALL | INTERSECT | EXCEPT | JOIN | ON | AS | AND | OR | NOT
              | CREATE | TABLE | INSERT | INTO | VALUES | INT | VARCHAR
              | COUNT | SUM | AVG | MIN | MAX | EXPLAIN | ANALYZE | SET | LOAD
ident       ::= letter { letter | digit }        letter ::= "a".."z" | "A".."Z" | "_"
int         ::= [ "+" | "-" ] digit { digit }
string      ::= "'" { any character except "'" | "''" } "'"
symbol      ::= "(" | ")" | "," | ";" | "." | "*" | "=" | "<>" | "!=" | "<" | "<=" | ">" | ">="
comment     ::= "--" { any character except newline }
```

- Keywords are case-insensitive: the scanner compares the upper-cased word with the list.
  Identifiers are case-sensitive and kept as written, so `Emp` and `emp` are different tables.
- A word that spells a keyword in any case is always a keyword. This is why `count` cannot be a
  column name.
- Maximal munch: on `<` the scanner looks at the next character before choosing between `<`,
  `<=` and `<>`, and likewise for `>`, `>=`, `!=`.
- A sign is part of a number only when a digit follows immediately. There is no arithmetic, so
  `-` never means subtraction.
- `--` starts a comment, so `--5` is a comment, not minus minus five.
- Inside a string, `''` is one quote. An unterminated string is a lexical error at its opening
  quote.

## 2. Grammar

```
script        ::= [ statement ] { ";" [ statement ] }
statement     ::= create | insert | query
                | EXPLAIN [ ANALYZE ] query | ANALYZE ident | SET ident "=" int | LOAD ident FROM string

create        ::= CREATE TABLE ident "(" coldef { "," coldef } ")"
coldef        ::= ident ( INT | VARCHAR "(" int ")" )
insert        ::= INSERT INTO ident VALUES "(" literal { "," literal } ")"
literal       ::= int | string

query         ::= block { setop block } [ order_by ] [ limit ]        -- see 4.3
setop         ::= UNION [ ALL ] | INTERSECT | EXCEPT
block         ::= select_block | "(" query ")"
select_block  ::= SELECT [ DISTINCT ] select_list
                  FROM from_list
                  [ WHERE condition ]
                  [ GROUP BY column { "," column } [ HAVING condition ] ]
                  [ order_by ]
                  [ limit ]
order_by      ::= ORDER BY sort_item { "," sort_item }
sort_item     ::= ( column | aggregate ) [ ASC | DESC ]
limit         ::= LIMIT int                                         -- int >= 0

select_list   ::= "*" | select_item { "," select_item }
select_item   ::= ( column | aggregate ) [ [ AS ] ident ]
aggregate     ::= COUNT "(" ( column | "*" ) ")" | ( SUM | AVG | MIN | MAX ) "(" column ")"

from_list     ::= table_ref { "," table_ref }
                | table_ref join_item { join_item }
join_item     ::= JOIN table_ref ON condition
table_ref     ::= ident [ [ AS ] ident ] | "(" query ")" [ AS ] ident

condition     ::= and_cond { OR and_cond }
and_cond      ::= not_cond { AND not_cond }
not_cond      ::= NOT not_cond | primary
primary       ::= "(" condition ")" | comparison
comparison    ::= operand compare_op operand
compare_op    ::= "=" | "<>" | "!=" | "<" | "<=" | ">" | ">="
operand       ::= column | aggregate | int | string
column        ::= ident [ "." ident ]
```

The grammar is LL(1) except in one place. In a `query`, a trailing `order_by`/`limit` belongs to
the last select block and is then lifted onto the chain (4.3). Everywhere else one token of
lookahead decides:

- `(` in a condition always opens a parenthesized condition, because no operand starts with `(`;
- `(` in FROM is always a subquery;
- an identifier right after a table name or a select item is its alias, because every word that
  could follow there (`JOIN`, `WHERE`, `FROM`, `ON`, ...) is a keyword.

## 3. Precedence and associativity

| level (loosest first) | operators | associativity | enforced by |
|---|---|---|---|
| 1 | `UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT` | left | `query ::= block { setop block }` builds `((b1 op b2) op b3)` |
| 2 | `OR` | left | `condition ::= and_cond { OR and_cond }` |
| 3 | `AND` | left | `and_cond ::= not_cond { AND not_cond }` |
| 4 | `NOT` | right (prefix) | `not_cond ::= NOT not_cond \| primary` |
| 5 | `=` `<>` `!=` `<` `<=` `>` `>=` | none | `comparison ::= operand compare_op operand`: exactly one operator, so `a < b < c` is a syntax error |

The four set operators share one level, as the handout asks. `A UNION B EXCEPT C` is therefore
`(A UNION B) EXCEPT C`, and `A EXCEPT B UNION C` is `(A EXCEPT B) UNION C`. Parentheses
override this through `block ::= "(" query ")"`. Inside a condition the levels give NOT over AND
over OR: `NOT a = 1 AND b = 2 OR c = 3` is `((NOT a = 1) AND b = 2) OR c = 3`
(`test_not_and_or_precedence`). All of this lives in the grammar's layering. The parser has one
function per rule and no precedence table.

**Parsing strategy.** This is recursive descent, one function per nonterminal, as in Bonus
Project 1. Left associativity would naturally be written left-recursively
(`query ::= query setop block`), and a recursive descent parser would loop forever on that,
calling `parse_query` before consuming anything. Each left-recursive rule is therefore written
as iteration (`block { setop block }`), and the loop builds the tree leaning left. The same
happens for `OR` and `AND`.

## 4. Where this grammar differs from the handout's sketch, and why

1. **`AS` is optional before an alias**, for tables, subqueries and select items. The handout's
   own examples write `FROM Emp e JOIN Dept d` and `FROM Emp e, Emp m` without `AS`. The lecture
   slides write `FROM Student s` and `) avgTable` for a derived table. PostgreSQL accepts both.
2. **`!=` is accepted as well as `<>`.** Lecture 05 lists both ("`<>` or `!=`"). In the logical
   plan both become `!=`, the spelling of the Bonus 1 condition language the executor parses.
3. **ORDER BY and LIMIT after the last block of a set-operation chain apply to the whole
   result.** In the sketch they belong to `select_block`, which would make
   `SELECT ... UNION SELECT ... ORDER BY x` sort only the second block. Standard SQL and PostgreSQL
   sort the union, and the hidden workload is checked against PostgreSQL, so the parser lifts a
   trailing ORDER BY/LIMIT from the last block onto the chain. To order or limit one block, put it
   in parentheses: `(SELECT ... ORDER BY x LIMIT 3) UNION ...`. When the last block is itself in
   parentheses, an ORDER BY/LIMIT after its `)` also applies to the whole chain:
   `(SELECT ...) UNION (SELECT ...) ORDER BY x`. After the `)` of a single parenthesized query, a
   clause it does not have yet joins its own, as in PostgreSQL: `(SELECT ... LIMIT 5) ORDER BY x`
   sorts and then keeps 5 rows, like `SELECT ... ORDER BY x LIMIT 5`. A second ORDER BY or a second
   LIMIT is a syntax error, as in PostgreSQL; written as a subquery in FROM, the query can be
   ordered or limited again. ORDER BY after a set operation names columns of its result, so
   `... UNION SELECT e.did FROM Emp e ORDER BY e.did` is a `name` error, as in PostgreSQL, and
   `ORDER BY did` is the way to write it.
4. **An aggregate may appear as a comparison operand and as a sort item.** The handout's example
   `HAVING COUNT(*) > 5` needs it. The grammar accepts it anywhere a column may stand. Semantic
   analysis rejects it in WHERE and in JOIN ... ON with an `aggregate` error, because those run
   before grouping.
5. **`COUNT(*)` only.** `SUM(*)` and the others are syntax errors.
6. **A FROM list is either all commas or all JOINs**, as in the sketch. `FROM A, B JOIN C ON ...`
   is a syntax error.
7. **Integers may carry a `+` sign** ("integers only, optionally signed").
8. **Shell statements.** `EXPLAIN`, `EXPLAIN ANALYZE`, `ANALYZE t`, `SET frames = n` and
   `LOAD t FROM 'file'` are part of the statement grammar so that the integrated shell needs no
   second parser. `sql compile` reports them as JSON statements, and only queries become plans.
9. **What is accepted here and refused by PostgreSQL.** These follow the handout or are design
   choices of this front end:
   - ORDER BY and LIMIT in a block before a set operator, without parentheses:
     `SELECT ... ORDER BY x LIMIT 3 UNION ALL SELECT ...`. The sketch's `select_block` includes
     both clauses, so the parser keeps them, and they order and limit that block alone.
     PostgreSQL wants the block in parentheses.
   - A subquery whose columns share a name, `(SELECT d.did, p.did ...) t`. PostgreSQL then refuses
     `t.did` as ambiguous. Here the later columns are renamed `did_2`, ... (DESIGN_M3 section 2), so
     `t.did` is the first one.
   - Default aggregate names: `count_star`, `avg_Emp_salary` (DESIGN_M3 section 3). PostgreSQL names
     these columns `count` and `avg`, so `ORDER BY count_star` or `t.count_star` works only here.
     A query that names its aggregates with `AS` means the same in both.

## 5. Errors

Every error is one line, `error: <category>: <message> at line L, column C`. The position is the
token where the problem is noticed (syntax) or the start of the offending construct (lexical,
semantic). Input must be UTF-8 text; anything else is one `error:` line too. A statement nested more deeply
than Python's recursion limit allows (a condition of about 1000 terms joined by OR, or about 300
levels of parentheses) is refused with "nested too deeply". The first error stops the
parse and nothing after it is checked: there is no recovery to report
further errors. The handout asks for exactly this ("panic mode is enough: report the first error
and stop"). The shell runs a script one statement at a time, so the statements before the first
error, lexical or not, have run when it stops. Categories: `lexical` (bad character,
unterminated string), `syntax`, `name`, `ambiguity`, `type`, `aggregate`, `schema`. Examples
from the required cases:

```
error: syntax: expected a column, a number or a string, found ';' at line 1, column 34
error: ambiguity: column 'city' is ambiguous: it could be Emp.city, Dept.city at line 1, column 8
error: aggregate: aggregate COUNT(*) is not allowed in WHERE, which runs before grouping at line 1, column 28
error: type: cannot compare age (INT) with '30' (VARCHAR) at line 1, column 28
error: name: unknown table 'Empl' at line 1, column 18
error: name: unknown column 'nam' at line 1, column 8
error: name: duplicate table name or alias 'Emp' in FROM; give each one a distinct alias at line 1, column 23
error: schema: UNION column 1 is VARCHAR(20) on the left and INT on the right at line 1, column 22
```
