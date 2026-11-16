"""sql: command line tool for the SQL front end."""

import argparse
import json
import sys

from ..ra.tree import to_json, explain_tree
from ..storage.database import open_database
from ..storage.disk import StorageError
from .analyzer import Analyzer
from .ast import (
    SQLError, CreateTable, Insert, SelectBlock, SetOp, SubqueryRef, BoolOp, NotOp, Explain, Analyze,
    SetFrames,
)
from .parser import parse_script


def condition_source(cond):
    if isinstance(cond, BoolOp):
        return f"({condition_source(cond.left)} {cond.op.upper()} {condition_source(cond.right)})"
    if isinstance(cond, NotOp):
        return f"NOT {condition_source(cond.cond)}"
    op = "<>" if cond.op == "!=" else cond.op
    return f"{cond.left.text()} {op} {cond.right.text()}"


def ordering_lines(query, pad):
    lines = []
    if query.order_by:
        lines.append(pad + "order by " + ", ".join(f"{o.expr.text()} {o.direction}" for o in query.order_by))
    if query.limit is not None:
        lines.append(pad + f"limit {query.limit}")
    return lines


def syntax_tree(stmt, depth=0):
    pad = "  " * depth
    if isinstance(stmt, SetOp):
        return ([pad + stmt.op.replace("_", " ")] + syntax_tree(stmt.left, depth + 1)
                + syntax_tree(stmt.right, depth + 1) + ordering_lines(stmt, "  " * (depth + 1)))
    if isinstance(stmt, SelectBlock):
        lines = [pad + ("select distinct" if stmt.distinct else "select")]
        inner = "  " * (depth + 1)
        items = ["*"] if stmt.star else [
            (i.expr.text() + (f" AS {i.alias}" if i.alias else "")) for i in stmt.items]
        lines.append(inner + "columns " + ", ".join(items))
        lines.append(inner + ("from" if stmt.join_conditions is None else "from (join chain)"))
        for k, ref in enumerate(stmt.from_refs):
            alias = f" AS {ref.alias}" if ref.alias else ""
            on = (f" ON {condition_source(stmt.join_conditions[k - 1])}"
                  if stmt.join_conditions is not None and k > 0 else "")
            if isinstance(ref, SubqueryRef):
                lines.append(inner + "  subquery" + alias + on)
                lines += syntax_tree(ref.query, depth + 3)
            else:
                lines.append(inner + f"  table {ref.name}{alias}{on}")
        if stmt.where is not None:
            lines.append(inner + "where " + condition_source(stmt.where))
        if stmt.group_by is not None:
            lines.append(inner + "group by " + ", ".join(c.text() for c in stmt.group_by))
        if stmt.having is not None:
            lines.append(inner + "having " + condition_source(stmt.having))
        return lines + ordering_lines(stmt, inner)
    if isinstance(stmt, CreateTable):
        return [pad + f"create table {stmt.name}"] + [
            pad + f"  {c.name} {c.type}" + (f"({c.length})" if c.length else "") for c in stmt.columns]
    if isinstance(stmt, Insert):
        return [pad + f"insert into {stmt.table} values ({', '.join(v.text() for v in stmt.values)})"]
    if isinstance(stmt, Explain):
        return [pad + ("explain analyze" if stmt.analyze else "explain")] + syntax_tree(stmt.query, depth + 1)
    if isinstance(stmt, Analyze):
        return [pad + f"analyze {stmt.table}"]
    if isinstance(stmt, SetFrames):
        return [pad + f"set {stmt.name} = {stmt.value}"]
    return [pad + f"load {stmt.table} from '{stmt.path}'"]


def statement_json(result):
    if result.kind == "query":
        return to_json(result.plan)
    if result.kind == "explain":
        return {"statement": "explain", "analyze": result.analyze, "plan": to_json(result.plan)}
    if result.kind == "create":
        return {"statement": "create_table", "table": result.table,
                "schema": [c.to_json() for c in result.schema]}
    if result.kind == "insert":
        return {"statement": "insert", "table": result.table, "values": result.values}
    if result.kind == "set":
        return {"statement": "set", "frames": result.value}
    if result.kind == "load":
        return {"statement": "load", "table": result.table, "file": result.path}
    return {"statement": "analyze", "table": result.table}


def statement_text(result):
    if result.kind in ("query", "explain"):
        return explain_tree(result.plan)
    return json.dumps(statement_json(result))


def analyze_file(db_dir, path, frames):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    db = open_database(db_dir, frames)
    try:
        analyzer = Analyzer(db.catalog.schemas())
        return [analyzer.statement(s) for s in parse_script(text)]
    finally:
        db.close()


def cmd_parse(args):
    statements = parse_script(args.query)
    if args.explain:
        for stmt in statements:
            print("\n".join(syntax_tree(stmt)))
    else:
        print(f"syntax ok: {len(statements)} statement{'s' if len(statements) != 1 else ''}")


def cmd_compile(args):
    results = analyze_file(args.db, args.file, args.frames)
    if args.json:
        objects = [statement_json(r) for r in results]
        print(json.dumps(objects[0] if len(objects) == 1 else objects, indent=2))
    else:
        print("\n\n".join(statement_text(r) for r in results))


def cmd_check(args):
    analyze_file(args.db, args.file, args.frames)


def build_parser():
    parser = argparse.ArgumentParser(prog="sql", description="Module 3 SQL front end")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--query", required=True)
    p.add_argument("--explain", action="store_true")
    p.add_argument("--frames", type=int, default=8, help="accepted like every tool's; parsing uses no pages")
    p.set_defaults(run=cmd_parse)
    p = sub.add_parser("compile")
    p.add_argument("--db", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--frames", type=int, default=8)
    p.set_defaults(run=cmd_compile)
    p = sub.add_parser("check")
    p.add_argument("--db", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--frames", type=int, default=8)
    p.set_defaults(run=cmd_check)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except SQLError as e:
        print(str(e), file=sys.stdout if args.command == "check" else sys.stderr)
        return 1
    except (StorageError, OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RecursionError:
        print("error: the statement is nested too deeply to process", file=sys.stderr)
        return 1
    return 0
