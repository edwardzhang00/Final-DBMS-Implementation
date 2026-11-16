"""The integrated system: SQL in, rows out.

    sql: parse, resolve, translate -> optimizer: rewrite, estimate, choose
    -> execution: iterators under B frames -> storage: buffer pool, heap files, catalog

Besides the SQL subset the shell understands EXPLAIN, EXPLAIN ANALYZE,
ANALYZE table, SET frames = n and LOAD table FROM 'file'. Every query starts
from an empty buffer pool, so its counters do not depend on what ran before.
"""

import argparse
import sys
import time

from .execution.plan import DEFAULT_FRAMES, MIN_FRAMES, PlanError, prepare, execute, walk, totals
from .execution.runtime import ExecError, format_value
from .optimizer.planner import Planner
from .optimizer.stats import DEFAULT_BUCKETS, analyze_table
from .ra.operators import EvalError
from .sql.analyzer import Analyzer
from .sql.ast import SQLError
from .sql.parser import Parser
from .storage.database import Database
from .storage.disk import StorageError
from .storage.loader import load_csv


def format_table(header, rows):
    cells = [[format_value(v) for v in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(header)]
    lines = [" | ".join(h.ljust(widths[i]) for i, h in enumerate(header)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in cells]
    return "\n".join(lines)


def q_error(estimate, actual):
    estimate, actual = max(1, estimate), max(1, actual)
    return max(estimate, actual) / min(estimate, actual)


class QueryRun:
    """One optimized and executed query with everything EXPLAIN ANALYZE and the
    workload reports need."""

    def __init__(self, physical, root, rows, seconds):
        self.physical = physical
        self.root = root
        self.rows = rows
        self.seconds = seconds
        self.totals = totals(root, rows)

    @property
    def io(self):
        return self.totals["page_reads"] + self.totals["page_writes"]

    def node_pairs(self):
        """(physical plan node, executor operator, depth, loops, under_limit) in plan order.

        loops is how many times the node's subtree ran: the inner input of a
        nested loop join is scanned again for every block of the outer, so its
        rows per execution are tuples_out / loops. Nodes under a limit stop
        early by design, so their actual rows say nothing about estimation."""
        def pair(p, op, depth, loops, under_limit):
            yield p, op, depth, loops, under_limit
            for i, (pc, oc) in enumerate(zip(p.children, op.children)):
                rescanned = op.op == "nested_loop_join" and i == 1 and oc.is_pipeline()
                yield from pair(pc, oc, depth + 1, loops * max(1, op.blocks) if rescanned else loops,
                                under_limit or op.op == "limit")
        return list(pair(self.physical, self.root, 0, 1, False))


class Shell:
    def __init__(self, directory, frames, out=sys.stdout):
        self.directory = directory
        self.frames = frames
        self.out = out
        self.db = Database(directory, frames)

    def print(self, text):
        print(text, file=self.out)

    def run_script(self, text):
        """Run statements one at a time; stop at the first error. Returns success."""
        try:
            parser = Parser(text)
            while True:
                while parser.at_symbol(";"):
                    parser.advance()
                if parser.at("EOF"):
                    return True
                stmt = parser.parse_statement()
                if not parser.at("EOF"):
                    parser.symbol(";")
                self.run_statement(stmt)
        except SQLError as e:
            self.print(str(e))
        except (PlanError, ExecError, EvalError, StorageError, OSError) as e:
            self.print(f"error: {e}")
        except RecursionError:
            self.print("error: the statement is nested too deeply to process")
        return False

    def run_statement(self, stmt):
        result = Analyzer(self.db.catalog.schemas()).statement(stmt)
        kind = result.kind
        if kind == "create":
            self.db.create_table(result.table, result.schema)
            self.print(f"created table {result.table}")
        elif kind == "insert":
            self.db.heap_file(result.table).insert(result.values)
            self.db.save()
            self.print("inserted 1 row")
        elif kind == "load":
            count = load_csv(self.db, result.table, result.path)
            self.print(f"loaded {count} rows into {result.table}")
        elif kind == "analyze":
            stats = analyze_table(self.db, result.table, DEFAULT_BUCKETS, self.frames)
            self.print(f"analyzed {result.table}: {stats.n_tuples} tuples, {stats.n_pages} pages")
        elif kind == "set":
            self.db.close()
            self.frames = result.value
            self.db = Database(self.directory, self.frames)
            self.print(f"frames = {self.frames}")
        elif kind == "explain" and not result.analyze:
            _, physical = Planner(self.db.catalog, self.frames).optimize(result.plan)
            self.print("\n".join(physical.explain()))
        elif kind == "explain":
            self.explain_analyze(self.run_query(result.plan))
        else:
            run = self.run_query(result.plan)
            header = [run.root.schema.name(i) for i in range(len(run.root.schema))]
            self.print(format_table(header, run.rows))
            self.print(f"({len(run.rows)} rows)  page I/O {run.io} ({run.totals['page_reads']} reads, "
                       f"{run.totals['page_writes']} writes)  time {run.seconds:.3f}s  "
                       f"root rows estimated {run.physical.est.rows}, actual {len(run.rows)}")

    def run_query(self, logical, planner=None):
        planner = planner or Planner(self.db.catalog, self.frames)
        _, physical = planner.optimize(logical)
        self.db.pool.clear()
        start = time.perf_counter()
        root = prepare(self.db, physical.to_json())
        rows = execute(self.db, root)
        return QueryRun(physical, root, rows, time.perf_counter() - start)

    def explain_analyze(self, run):
        for p, op, depth, loops, _ in run.node_pairs():
            actual = op.counters()["tuples_out"] // loops
            repeated = f" loops={loops}" if loops > 1 else ""
            subtree_io = sum(o.counters()["page_reads"] + o.counters()["page_writes"] for o, _ in walk(op))
            self.print("  " * depth + f"{p.label} est_rows={p.est.rows} actual_rows={actual}{repeated} "
                       f"q_error={q_error(p.est.rows, actual):.2f} est_cost={p.cost} "
                       f"actual_io={subtree_io}")
        self.print(f"total: {len(run.rows)} rows, page I/O {run.io}, time {run.seconds:.3f}s, "
                   f"peak frames {self.db.pool.peak_held}/{self.frames}")

    def close(self):
        self.db.close()


def interactive(shell):
    buffer = []
    while True:
        try:
            line = input("db> " if not buffer else "..> ")
        except EOFError:
            print()
            return
        buffer.append(line)
        text = "\n".join(buffer)
        if text.strip().endswith(";"):
            buffer = []
            shell.run_script(text)


def main(argv):
    parser = argparse.ArgumentParser(prog="dbsh", description="SQL shell over the whole system")
    parser.add_argument("--db", required=True)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("-f", "--file", help="run the statements in this file and exit")
    args = parser.parse_args(argv)
    if args.frames < MIN_FRAMES:
        print(f"error: --frames must be at least {MIN_FRAMES}", file=sys.stderr)
        return 1
    shell = Shell(args.db, args.frames)
    try:
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                return 0 if shell.run_script(f.read()) else 1
        if not sys.stdin.isatty():
            return 0 if shell.run_script(sys.stdin.read()) else 1
        interactive(shell)
        return 0
    except (OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        shell.close()
