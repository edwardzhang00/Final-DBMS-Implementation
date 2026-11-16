#!/usr/bin/env python3
"""Relational algebra engine command line: evaluate a query or print its tree."""

import sys

from dbms.ra.tokenizer import LexError
from dbms.ra.parser import ParseError, parse_program
from dbms.ra.tree import render_tree
from dbms.ra.operators import (
    EvalError, STATS, reset_stats, build_db, dict_lookup, eval_expr,
)


def format_relation(rel):
    labels = rel.labels()
    rows = sorted(rel.rows)
    widths = [len(label) for label in labels]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(str(v)))
    sep = "  "
    lines = [sep.join(label.ljust(widths[i]) for i, label in enumerate(labels)),
             sep.join("-" * w for w in widths)]
    for row in rows:
        lines.append(sep.join(str(v).ljust(widths[i]) for i, v in enumerate(row)))
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


DEFAULT_PROGRAM = """
Employees (EID, Name, Age, DID) = {
  E1, John, 32, D1
  E2, Alice, 28, D2
  E3, Bob, 29, D1
}

Dept (DID, DName) = {
  D1, Sales
  D2, Eng
}

R (A, B) = {
  1, 1
  2, 2
  3, 1
}

S (B, C) = {
  1, 10
  2, 20
}

A (x) = {
  1
  2
}

B (x) = {
  2
}

C (x) = {
  1
}
"""


def run_program(text, mode="eval"):
    reset_stats()
    program = parse_program(text)
    db = build_db(program)
    if mode == "tree":
        return render_tree(program.query)
    result = eval_expr(program.query, dict_lookup(db))
    return (format_relation(result) + "\n"
            + f"join_pairs={STATS['join_pairs']} select_tuples={STATS['select_tuples']}")


def run_and_print(query, mode, error_stream):
    """Print the query's result, or its error to error_stream. Returns whether it ran."""
    try:
        print(run_program(DEFAULT_PROGRAM + "\n" + query, mode))
    except (LexError, ParseError, EvalError) as e:
        print(f"Error: {e}", file=error_stream)
        return False
    return True


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--tree":
        if len(sys.argv) < 3:
            print("Usage: python ra.py --tree \"<query>\"", file=sys.stderr)
            return 1
        return 0 if run_and_print(" ".join(sys.argv[2:]), "tree", sys.stderr) else 1

    if len(sys.argv) >= 2:
        return 0 if run_and_print(" ".join(sys.argv[1:]), "eval", sys.stderr) else 1

    cases = [
        ("18", "select[A=B](R)"),
        ("19", "Employees join[Employees.DID=Dept.DID] Dept"),
        ("20", "rename[E2](Employees) join[Employees.DID=E2.DID] Employees"),
        ("21", "Employees union R"),
        ("22", "select[Age>'30'](Employees)"),
        ("23", "project[DID](Employees)"),
        ("23b", "distinct(project[DID](Employees))"),
        ("24", "project[Name, Name](Employees)"),
        ("25", "select[Age>100](Employees)"),
    ]
    for number, query in cases:
        print(f"\n--- Case {number}: {query}")
        run_and_print(query, "eval", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
