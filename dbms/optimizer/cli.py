"""opt: command line tool for the query optimizer."""

import argparse
import json
import sys

from ..execution.plan import DEFAULT_FRAMES, MIN_FRAMES, PlanError
from ..ra.operators import EvalError
from ..ra.parser import ParseError
from ..ra.tokenizer import LexError
from ..ra.tree import from_json, explain_tree
from ..storage.database import open_database
from ..storage.disk import StorageError
from .planner import Planner
from .stats import DEFAULT_BUCKETS, analyze_table


def load_logical(path):
    with open(path, encoding="utf-8") as f:
        try:
            plan = json.load(f)
        except json.JSONDecodeError as e:
            raise PlanError(f"{path}: malformed JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None
    if not isinstance(plan, dict) or "op" not in plan:
        raise PlanError(f"{path} does not hold one logical plan (compile a single query with sql compile --json)")
    try:
        return from_json(plan)
    except (KeyError, TypeError, ValueError, LexError, ParseError) as e:
        raise PlanError(f"{path} is not a valid logical plan: {e}") from None


def plan_for(args):
    logical = load_logical(args.logical)
    db = open_database(args.db, args.frames)
    try:
        planner = Planner(db.catalog, args.frames)
        rewritten, physical = planner.optimize(logical)
    finally:
        db.close()
    return planner, logical, rewritten, physical


def cmd_analyze(args):
    db = open_database(args.db, args.frames)
    try:
        stats = analyze_table(db, args.table, args.buckets, args.frames)
        info = db.catalog.get_table(args.table)
    finally:
        db.close()
    print(f"analyzed {args.table}: n_tuples={stats.n_tuples} n_pages={stats.n_pages} width={stats.width:.2f}")
    for column, column_stats in zip(info.schema, stats.columns):
        print(f"  {column.name}: ndv={column_stats.ndv} min={column_stats.min!r} max={column_stats.max!r} "
              f"avg_width={column_stats.avg_width:.2f} buckets={len(column_stats.buckets)}")


def cmd_plan(args):
    _, _, _, physical = plan_for(args)
    print(json.dumps(physical.to_json(), indent=2 if not args.json else None))


def cmd_explain(args):
    _, logical, rewritten, physical = plan_for(args)
    if args.rewrites:
        print("-- logical plan as compiled")
        print(explain_tree(logical))
        print("-- after rewrite rules")
        print(explain_tree(rewritten))
        print("-- physical plan")
    print("\n".join(physical.explain()))


def cmd_dp_table(args):
    planner, _, _, _ = plan_for(args)
    text = planner.dp_table_text()
    print(text if text else "no joins in this query")


def build_parser():
    parser = argparse.ArgumentParser(prog="opt", description="Module 4 query optimizer")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--db", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.set_defaults(run=cmd_analyze)
    for name, handler in (("plan", cmd_plan), ("explain", cmd_explain), ("dp-table", cmd_dp_table)):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--logical", required=True)
        p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
        if name == "plan":
            p.add_argument("--json", action="store_true", help="compact single-line JSON")
        if name == "explain":
            p.add_argument("--rewrites", action="store_true", help="also show the logical plan before and after")
        p.set_defaults(run=handler)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    if args.frames < MIN_FRAMES:
        print(f"error: --frames must be at least {MIN_FRAMES}", file=sys.stderr)
        return 1
    try:
        args.run(args)
    except (PlanError, StorageError, EvalError, OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RecursionError:
        print("error: the plan is nested too deeply to process", file=sys.stderr)
        return 1
    return 0
