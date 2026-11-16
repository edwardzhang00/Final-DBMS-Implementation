#!/usr/bin/env python3
"""Run a SQL workload through the system, check it against PostgreSQL, measure it.

    python scripts/run_workload.py --schema workload/schema.sql --data DATA_DIR \\
        --queries workload/queries --db DB_DIR [--frames 32] [--pg 127.0.0.1:55433/oracle] \\
        [--measure] [--json results.json]

DATA_DIR holds one <table>.csv per table. The database is built (CREATE TABLE,
LOAD, ANALYZE) when DB_DIR does not exist yet. With --pg the same schema and
data are loaded into PostgreSQL and every result is compared with its result
(as a list when the query ends in ORDER BY, as a bag otherwise). It prints, per
query, the rows, page I/O, wall time and estimated against actual rows at the
root, and the q-error over every plan node. --measure also runs the
unoptimized plan (no rewrites, joins as written, hash join everywhere).
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter, namedtuple
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dbms.execution.plan import DEFAULT_FRAMES, PlanError  # noqa: E402
from dbms.execution.runtime import ExecError, FOUR_PLACES, format_value  # noqa: E402
from dbms.optimizer.planner import unoptimized_planner  # noqa: E402
from dbms.ra.operators import EvalError  # noqa: E402
from dbms.shell import Shell, q_error  # noqa: E402
from dbms.sql.analyzer import Analyzer  # noqa: E402
from dbms.sql.ast import SQLError  # noqa: E402
from dbms.sql.parser import parse_script  # noqa: E402
from dbms.storage.disk import StorageError  # noqa: E402

NodeError = namedtuple("NodeError", "q_error query label estimated actual under_limit")


def table_names(schema_text):
    return [s.name for s in parse_script(schema_text)]


def build(db_dir, schema_text, data_dir):
    shell = Shell(db_dir, 64)
    script = [schema_text]
    for table in table_names(schema_text):
        script.append(f"LOAD {table} FROM '{os.path.join(data_dir, table + '.csv')}';")
        script.append(f"ANALYZE {table};")
    ok = shell.run_script("\n".join(script))
    shell.close()
    if not ok:
        sys.exit("building the database failed")


class Postgres:
    def __init__(self, spec):
        hostport, _, self.database = spec.partition("/")
        self.host, _, self.port = hostport.partition(":")

    def run(self, sql, *extra):
        cmd = ["psql", "-h", self.host, "-p", self.port, "-U", "postgres", "-d", self.database,
               "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-F", "\t", *extra, "-c", sql]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    def load(self, schema_text, data_dir):
        tables = table_names(schema_text)
        self.run("DROP TABLE IF EXISTS " + ", ".join(tables) + ";")
        for stmt in schema_text.split(";"):
            body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--"))
            if body.strip():
                self.run(body + ";")
        for table in tables:
            # Forward slashes: psql reads a backslash in a quoted \\copy path as an escape (Windows).
            path = os.path.abspath(os.path.join(data_dir, table + ".csv")).replace("\\", "/")
            self.run(f"\\copy {table} FROM '{path}' WITH (FORMAT csv)")
        self.run("ANALYZE;")

    def rows(self, sql, types):
        """The result as this system prints it: a REAL (PostgreSQL's numeric AVG) to
        four places, a half rounded away from zero."""
        out = self.run(sql)
        return [tuple(str(Decimal(field).quantize(FOUR_PLACES, ROUND_HALF_UP)) if type_name == "REAL" else field
                      for field, type_name in zip(line.split("\t"), types))
                for line in out.splitlines()]


def printed(rows):
    return [tuple(format_value(v) for v in row) for row in rows]


def same_result(ours, theirs, ordered):
    """Compares two printed results, in order or as bags."""
    return ours == theirs if ordered else Counter(ours) == Counter(theirs)


def ends_with_order_by(sql):
    text = sql.upper()
    return "ORDER BY" in text and "UNION" not in text and "INTERSECT" not in text and "EXCEPT" not in text


def load_queries(directory):
    """{name: SQL text} for each <name>.sql in directory, in name order."""
    queries = {}
    for file_name in sorted(os.listdir(directory)):
        if file_name.endswith(".sql"):
            with open(os.path.join(directory, file_name), encoding="utf-8") as f:
                queries[file_name[:-len(".sql")]] = f.read().strip()
    return queries


def compile_query(shell, sql):
    [stmt] = parse_script(sql)
    return Analyzer(shell.db.catalog.schemas()).statement(stmt).plan


def algorithms(node):
    found = []
    if node.op.endswith("_join") or node.op in ("group_by", "distinct"):
        found.append(node.op + (f"({node.fields['mode']})" if "mode" in node.fields else ""))
    for c in node.children:
        found += algorithms(c)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--pg")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--json")
    args = parser.parse_args()

    with open(args.schema, encoding="utf-8") as f:
        schema_text = f.read()
    if not os.path.exists(os.path.join(args.db, "catalog.db")):
        build(args.db, schema_text, args.data)
    pg = None
    if args.pg:
        pg = Postgres(args.pg)
        pg.load(schema_text, args.data)

    shell = Shell(args.db, args.frames)
    results = []
    node_errors = []
    for name, sql in load_queries(args.queries).items():
        try:
            logical = compile_query(shell, sql)
            run = shell.run_query(logical)
        except (SQLError, PlanError, ExecError, EvalError, StorageError) as e:
            results.append({"query": name, "error": str(e)})
            print(json.dumps(results[-1]), flush=True)
            continue
        row = {"query": name, "rows": len(run.rows), "io": run.io, "reads": run.totals["page_reads"],
               "writes": run.totals["page_writes"], "seconds": round(run.seconds, 3),
               "est_root": run.physical.est.rows, "est_cost": run.physical.cost,
               "peak_frames": shell.db.pool.peak_held, "plan": ", ".join(algorithms(run.physical))}
        for p, op, _, loops, under_limit in run.node_pairs():
            actual = op.counters()["tuples_out"] // loops
            node_errors.append(NodeError(q_error(p.est.rows, actual), name, p.label, p.est.rows, actual, under_limit))
        if pg:
            theirs = pg.rows(sql, [d.type for d in run.root.schema.defs])
            row["postgres_rows"] = len(theirs)
            row["matches_postgres"] = same_result(printed(run.rows), theirs, ends_with_order_by(sql))
        if args.measure:
            base = shell.run_query(logical, unoptimized_planner(shell.db.catalog, args.frames))
            row["unoptimized_io"] = base.io
            row["unoptimized_seconds"] = round(base.seconds, 3)
            row["io_ratio"] = round(base.io / max(1, run.io), 2)
            if not same_result(printed(run.rows), printed(base.rows), ends_with_order_by(sql)):
                row["unoptimized_differs"] = True
        results.append(row)
        print(json.dumps(row), flush=True)

    summary = {"frames": args.frames, "queries": results}
    failed = [r["query"] for r in results if "error" in r]
    summary["failed"] = failed
    errors = [e.q_error for e in node_errors]
    estimated = [e.q_error for e in node_errors if not e.under_limit]
    summary["q_error"] = {
        "nodes": len(errors), "median": statistics.median(errors), "worst": max(errors),
        "nodes_not_under_limit": len(estimated), "median_not_under_limit": statistics.median(estimated),
        "worst_not_under_limit": max(estimated),
        "worst_nodes": [list(e) for e in sorted(node_errors, reverse=True)[:8]],
        "all_nodes": [list(e) for e in node_errors]}
    print(json.dumps({k: v for k, v in summary["q_error"].items() if k != "all_nodes"}))
    shell.close()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    if failed:
        sys.exit(f"queries that failed: {', '.join(failed)}")
    if pg and not all(r["matches_postgres"] for r in results):
        sys.exit("some results differ from PostgreSQL")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    main()
