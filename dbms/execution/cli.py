"""exec: command line tool for the execution engine."""

import argparse
import json
import sys

from ..storage.database import open_database
from ..storage.disk import StorageError
from .plan import DEFAULT_FRAMES, PlanError, load_plan, prepare, execute, walk, totals
from .runtime import ExecError, format_value


def counter_line(op):
    c = op.counters()
    text = (f"{op.label()} tuples_out={c['tuples_out']} comparisons={c['comparisons']} "
            f"reads={c['page_reads']} writes={c['page_writes']}")
    extras = " ".join(f"{k}={v}" for k, v in c.items()
                      if k not in ("tuples_out", "comparisons", "page_reads", "page_writes"))
    return text + (" " + extras if extras else "")


def run(args):
    with open(args.plan, encoding="utf-8") as f:
        plan = load_plan(f.read())
    db = open_database(args.db, args.frames, args.policy)
    try:
        root = prepare(db, plan)
        rows = execute(db, root)
    finally:
        db.close()
    return root, rows, db.pool.peak_held


def cmd_run(args):
    root, rows, peak = run(args)
    if args.json:
        rows_json = [[float(format_value(v)) if isinstance(v, float) else v for v in row] for row in rows]
        print(json.dumps({"schema": root.schema.to_json(), "rows": rows_json,
                          "totals": totals(root, rows), "frames": args.frames, "peak_frames": peak}))
        return
    print("\t".join(root.schema.name(i) for i in range(len(root.schema))))
    for row in rows:
        print("\t".join(format_value(v) for v in row))
    t = totals(root, rows)
    print(f"({len(rows)} rows) comparisons={t['comparisons']} reads={t['page_reads']} "
          f"writes={t['page_writes']} peak_frames={peak}/{args.frames}")


def cmd_trace(args):
    root, _, _ = run(args)
    for op, depth in walk(root):
        print("  " * depth + counter_line(op))


def build_parser():
    parser = argparse.ArgumentParser(prog="exec", description="Module 2 execution engine")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("run", cmd_run), ("trace", cmd_trace)):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--plan", required=True)
        p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
        p.add_argument("--policy", choices=["lru", "clock"], default="clock")
        if name == "run":
            p.add_argument("--json", action="store_true")
        p.set_defaults(run=handler)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except (PlanError, ExecError, StorageError, OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RecursionError:
        print("error: the plan is nested too deeply to process", file=sys.stderr)
        return 1
    return 0
