#!/usr/bin/env python3
"""Differential test: random queries from the SQL subset run through the whole
system at several buffer sizes and through PostgreSQL, results compared as
printed (AVG to four places), in order when ORDER BY covers every output column
and as bags otherwise.

    python scripts/fuzz_sql.py --db DB_DIR --pg 127.0.0.1:55433/fuzz [--count 300] [--seed 1]

DB_DIR and the PostgreSQL database must hold the workload tables with the same
data (scripts/run_workload.py --pg builds both). A query the front end
rejects must also be rejected by PostgreSQL. The one intended difference is an
aggregate other than COUNT over an empty input with no GROUP BY: PostgreSQL
returns NULL, this system has no NULL and reports an error. A plan refused
because it needs more than B frames is a failure from B = 4 up and is only
counted below that, since every plan must run in as few as 4 frames.
"""
import argparse
import os
import random
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from run_workload import Postgres, same_result, compile_query, printed  # noqa: E402
from dbms.execution.aggregate import UndefinedAggregateError  # noqa: E402
from dbms.execution.plan import TooFewFramesError  # noqa: E402
from dbms.execution.runtime import ExecError  # noqa: E402
from dbms.shell import Shell  # noqa: E402
from dbms.sql.ast import SQLError  # noqa: E402

TABLES = {
    "dept": {"did": "i", "title": "s", "budget": "i", "city": "s", "region": "s"},
    "emp": {"eid": "i", "name": "s", "age": "i", "did": "i", "salary": "i", "level": "i", "city": "s", "mgr": "i"},
    "project": {"pid": "i", "did": "i", "title": "s", "budget": "i"},
    "works": {"eid": "i", "pid": "i", "hours": "i"},
}
LINKS = [("emp", "did", "dept", "did"), ("project", "did", "dept", "did"), ("works", "eid", "emp", "eid"),
         ("works", "pid", "project", "pid"), ("emp", "mgr", "emp", "eid"), ("emp", "city", "dept", "city")]
LITERALS = {
    "did": (-5, 105), "budget": (0, 1000001), "age": (18, 68), "salary": (40000, 120000), "level": (0, 7),
    "hours": (0, 42), "eid": (0, 2100), "mgr": (0, 2100), "pid": (0, 210),
    "city": ["Ottawa", "Toronto", "Inuvik", "Calgary", "M", "Zz"], "region": ["East", "West", "North", "Q"],
    "title": ["Dept-010", "Proj-0050", "D", "Proj-1"], "name": ["emp00010", "emp01000", "e", "f"],
}


class Generator:
    def __init__(self, rng):
        self.rng = rng

    def literal(self, column):
        choices = LITERALS[column]
        if isinstance(choices, tuple):
            return str(self.rng.randrange(*choices))
        return "'" + self.rng.choice(choices) + "'"

    def from_items(self):
        """FROM items (table, alias) and the equalities that link each to an earlier one."""
        rng = self.rng
        items = [(rng.choice(list(TABLES)), "t0")]
        links = []
        for k in range(1, rng.choice([1, 1, 2, 2, 3, 3, 4])):
            used = {t for t, _ in items}
            options = [(table_a, column_a, table_b, column_b) for table_a, column_a, table_b, column_b in LINKS
                       if table_a in used or table_b in used]
            table_a, column_a, table_b, column_b = rng.choice(options)
            if any(t == table_a for t, _ in items):
                old, old_col, new, new_col = table_a, column_a, table_b, column_b
            else:
                old, old_col, new, new_col = table_b, column_b, table_a, column_a
            old_alias = rng.choice([alias for t, alias in items if t == old])
            items.append((new, f"t{k}"))
            links.append(f"{old_alias}.{old_col} = t{k}.{new_col}")
        return items, links

    def predicate(self, items):
        rng = self.rng
        table, alias = rng.choice(items)
        column = rng.choice(list(TABLES[table]))
        op = rng.choice(["=", "<>", "<", "<=", ">", ">="])
        if rng.random() < 0.15:
            kind = TABLES[table][column]
            _, other_alias, other_column = rng.choice(
                [(t, a, c) for t, a in items for c, k in TABLES[t].items() if k == kind])
            return f"{alias}.{column} {op} {other_alias}.{other_column}"
        return f"{alias}.{column} {op} {self.literal(column)}"

    def condition(self, items, depth=0):
        roll = self.rng.random()
        if depth < 2 and roll < 0.25:
            word = self.rng.choice(["AND", "OR"])
            return f"({self.condition(items, depth + 1)} {word} {self.condition(items, depth + 1)})"
        if depth < 2 and roll < 0.32:
            return f"NOT {self.condition(items, depth + 1)}"
        return self.predicate(items)

    def block(self, int_columns=None):
        """A SELECT block, its output column count and the name ORDER BY gives each
        output column. With int_columns, an ungrouped block of that many INT columns."""
        rng = self.rng
        items, links = self.from_items()
        use_join = bool(links) and rng.random() < 0.5
        where = [] if use_join else list(links)
        where += [self.condition(items) for _ in range(rng.choice([0, 1, 1, 2]))]
        columns = [(f"{a}.{c}", k) for t, a in items for c, k in TABLES[t].items()]
        hidden = None
        if int_columns is None and rng.random() < 0.35:
            keys = rng.sample(columns, rng.choice([1, 1, 2]))
            aggregates = ["COUNT(*)"]
            for _ in range(rng.choice([0, 1, 2])):
                func, column = self.aggregate(columns)
                aggregates.append(f"{func}({column})")
            select = [k for k, _ in keys] + [f"{a} AS a{i}" for i, a in enumerate(aggregates)]
            names = [k for k, _ in keys] + [f"a{i}" for i in range(len(aggregates))]
            tail = " GROUP BY " + ", ".join(k for k, _ in keys)
            if rng.random() < 0.4:
                tail += " HAVING " + self.having(columns, keys)
        else:
            if int_columns is None:
                select = [c for c, _ in rng.sample(columns, min(rng.choice([1, 2, 3]), len(columns)))]
            else:
                select = [rng.choice([c for c, k in columns if k == "i"]) for _ in range(int_columns)]
            names = list(select)
            tail = ""
            if rng.random() < 0.25:
                select[0] = "DISTINCT " + select[0]
            else:
                hidden = rng.choice(columns)[0]
        if use_join:
            source = f"{items[0][0]} {items[0][1]}" + "".join(
                f" JOIN {t} {a} ON {cond}" for (t, a), cond in zip(items[1:], links))
        else:
            source = ", ".join(f"{t} {a}" for t, a in items)
        sql = f"SELECT {', '.join(select)} FROM {source}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql + tail, len(select), names, hidden

    def aggregate(self, columns):
        """An aggregate function and the column it reads: SUM and AVG read an INT column."""
        func = self.rng.choice(["SUM", "AVG", "MIN", "MAX", "COUNT"])
        pool = [c for c in columns if c[1] == "i"] if func in ("SUM", "AVG") else columns
        return func, self.rng.choice(pool)[0]

    def having(self, columns, keys, depth=0):
        """A HAVING condition over aggregates of any kind and the grouping columns."""
        rng = self.rng
        roll = rng.random()
        if depth < 1 and roll < 0.25:
            word = rng.choice(["AND", "OR"])
            return f"({self.having(columns, keys, depth + 1)} {word} {self.having(columns, keys, depth + 1)})"
        if depth < 1 and roll < 0.32:
            return f"NOT {self.having(columns, keys, depth + 1)}"
        op = rng.choice(["=", "<>", "<", "<=", ">", ">="])
        kind = rng.random()
        if kind < 0.25:
            return f"COUNT(*) {op} {rng.randrange(1, 30)}"
        if kind < 0.75:
            func, column = self.aggregate(columns)
            if func == "COUNT":
                value = str(rng.randrange(1, 30))
            elif func == "SUM":
                value = str(int(self.literal(column.split(".")[1])) * rng.choice([1, 2, 5, 20]))
            else:
                value = self.literal(column.split(".")[1])
            return f"{func}({column}) {op} {value}"
        key = rng.choice(keys)[0]
        return f"{key} {op} {self.literal(key.split('.')[1])}"

    def order_by(self, names, hidden=None):
        """An ORDER BY clause and whether it orders by every output column, which
        makes the order of the rows defined; then a LIMIT may follow."""
        rng = self.rng
        if rng.random() < 0.6:
            keys = rng.sample(names, len(names))
            total = True
        else:
            keys = rng.sample(names, rng.randrange(1, len(names) + 1))
            if hidden is not None and rng.random() < 0.5:
                keys.append(hidden)
            total = set(names) <= set(keys)
        clause = " ORDER BY " + ", ".join(k + rng.choice(["", " ASC", " DESC"]) for k in keys)
        if total and rng.random() < 0.5:
            clause += f" LIMIT {rng.randrange(0, 20)}"
        return clause, total

    def aliased(self, block_sql):
        """The block with its select items named c0, c1, ..."""
        head, _, rest = block_sql.partition(" FROM ")
        exprs = head[len("SELECT "):].split(", ")
        return "SELECT " + ", ".join(f"{e} AS c{i}" for i, e in enumerate(exprs)) + " FROM " + rest

    def set_operation(self, n):
        """Two blocks of n INT columns under a set operator, the left one naming its
        columns c0, c1, ...; sometimes ordered by every column, perhaps limited."""
        rng = self.rng
        op = rng.choice(["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
        sql = f"{self.aliased(self.block(n)[0])} {op} {self.block(n)[0]}"
        total = False
        if rng.random() < 0.4:
            clause, total = self.order_by([f"c{i}" for i in range(n)])
            sql += clause
        return sql, total

    def over_set_operation(self):
        """A query reading a set operation in a FROM subquery."""
        rng = self.rng
        n = rng.choice([1, 2])
        inner, _ = self.set_operation(n)
        columns = [f"q.c{i}" for i in range(n)]
        roll = rng.random()
        if roll < 0.2:
            return f"SELECT COUNT(*) FROM ({inner}) AS q", True
        if roll < 0.4:
            sql = f"SELECT q.c0, COUNT(*) AS n FROM ({inner}) AS q GROUP BY q.c0"
            if rng.random() < 0.3:
                sql += f" HAVING COUNT(*) > {rng.randrange(1, 4)}"
            names = ["q.c0", "n"]
        elif roll < 0.5:
            sql = f"SELECT q.c0, d.title FROM ({inner}) AS q JOIN dept d ON q.c0 = d.did"
            names = ["q.c0", "d.title"]
        else:
            distinct = "DISTINCT " if rng.random() < 0.5 else ""
            sql = f"SELECT {distinct}{', '.join(columns)} FROM ({inner}) AS q"
            if rng.random() < 0.4:
                sql += f" WHERE q.c0 > {rng.randrange(0, 200)}"
            names = columns
        total = False
        if rng.random() < 0.5:
            clause, total = self.order_by(names)
            sql += clause
        return sql, total

    def query(self):
        """A random query and whether its rows come in a defined order."""
        rng = self.rng
        roll = rng.random()
        if roll < 0.15:
            return self.set_operation(rng.choice([1, 2]))
        if roll < 0.27:
            return self.over_set_operation()
        if roll < 0.4:
            inner, n, _, _ = self.block(rng.choice([1, 2, 3]))
            inner = self.aliased(inner)
            outer_where = f" WHERE q.c0 > {rng.randrange(0, 200)}" if rng.random() < 0.5 else ""
            if rng.random() < 0.5:
                return f"SELECT q.c0, d.title FROM ({inner}) AS q JOIN dept d ON q.c0 = d.did{outer_where}", False
            return f"SELECT {', '.join(f'q.c{i}' for i in range(n))} FROM ({inner}) AS q{outer_where}", False
        sql, _, names, hidden = self.block()
        total = False
        if rng.random() < 0.4:
            clause, total = self.order_by(names, hidden)
            sql += clause
        return sql, total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--pg", required=True)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--frames", type=int, nargs="*", default=[4, 7, 32])
    args = parser.parse_args()
    pg = Postgres(args.pg)
    gen = Generator(random.Random(args.seed))
    shells = {f: Shell(args.db, f) for f in args.frames}
    first = shells[args.frames[0]]
    failures = checked = rejected = undefined = refused = 0
    for n in range(args.count):
        sql, ordered = gen.query()
        try:
            logical = compile_query(first, sql)
        except SQLError as e:
            try:
                pg.run(sql)
            except subprocess.CalledProcessError:
                rejected += 1
                continue
            print(f"[{n}] rejected here only: {sql}\n    {e}")
            failures += 1
            continue
        try:
            raw = pg.run(sql)
        except subprocess.CalledProcessError as e:
            print(f"[{n}] rejected by PostgreSQL only: {sql}\n    {e.stderr.strip()}")
            failures += 1
            continue
        postgres_has_null = "\t\t" in raw or raw.startswith("\t") or "\t\n" in raw
        for frames, shell in shells.items():
            try:
                run = shell.run_query(logical)
            except TooFewFramesError as e:
                if frames < 4:
                    refused += 1
                    continue
                error = e
            except UndefinedAggregateError as e:
                if postgres_has_null:
                    undefined += 1
                    continue
                error = e
            except ExecError as e:
                error = e
            else:
                theirs = pg.rows(sql, [d.type for d in run.root.schema.defs])
                if same_result(printed(run.rows), theirs, ordered):
                    checked += 1
                else:
                    print(f"[{n}] MISMATCH at B={frames}: {sql}\n    {len(run.rows)} rows here, "
                          f"{len(theirs)} in PostgreSQL")
                    failures += 1
                continue
            print(f"[{n}] error at B={frames}: {sql}\n    {error}")
            failures += 1
    for shell in shells.values():
        shell.close()
    print(f"{checked} results identical to PostgreSQL, {rejected} queries rejected by both, "
          f"{undefined} empty aggregates (error here, NULL there), {refused} plans refused below 4 frames, "
          f"{failures} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    main()
