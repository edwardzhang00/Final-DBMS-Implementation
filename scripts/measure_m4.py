#!/usr/bin/env python3
"""Measurements for REPORT_M4.md beyond the workload run: rule ablation, how B
changes the chosen algorithms, the correlated-columns plan, and a four-relation
DP table.

    python scripts/measure_m4.py --db DB_DIR --queries workload/queries > results_m4.md

DB_DIR is the analyzed workload database built by scripts/run_workload.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from run_workload import compile_query, algorithms, load_queries  # noqa: E402
from dbms.execution.plan import DEFAULT_FRAMES, walk  # noqa: E402
from dbms.optimizer.planner import JOIN_ALGORITHMS, Planner  # noqa: E402
from dbms.optimizer.rewrite import ALL_RULES, Rule  # noqa: E402
from dbms.shell import Shell, q_error  # noqa: E402

RUNNABLE_ROWS = 2_000_000
RULE_NAMES = {Rule.SPLIT_CONJUNCTIONS: "split conjunctions",
              Rule.PUSH_SELECT_BELOW_JOIN: "push select below join/cross",
              Rule.CROSS_TO_JOIN: "cross + select to join",
              Rule.PUSH_SELECT_BELOW_RENAME_UNION: "push select below rename/union",
              Rule.PUSH_SELECT_BELOW_GROUP_BY: "push select below group_by",
              Rule.PUSH_PROJECTIONS_DOWN: "push projections down",
              Rule.REMOVE_NO_OP_PROJECTIONS_MERGE_SELECTS: "remove no-op projections, merge selects"}


def ablation(shell, queries, frames):
    print(f"## Rule ablation at B = {frames}\n")
    print(f"A plan is executed only if no node is estimated above {RUNNABLE_ROWS:,} rows; otherwise its "
          "estimated cost is given.\n")
    print("| rules | total I/O of the queries that ran | change | queries not run (estimated I/O) |")
    print("|---|---|---|---|")
    baseline = {}
    for rule in [None] + sorted(ALL_RULES):
        rules = ALL_RULES if rule is None else ALL_RULES - {rule}
        measured, skipped = {}, []
        for name, sql in queries.items():
            planner = Planner(shell.db.catalog, frames, rules=rules)
            _, physical = planner.optimize(compile_query(shell, sql))
            biggest = max(node.est.rows for node, _ in walk(physical))
            if biggest > RUNNABLE_ROWS:
                skipped.append(f"{name} ({physical.cost:,}; a node of {biggest:,} rows)")
                continue
            measured[name] = shell.run_query(compile_query(shell, sql), planner).io
        if rule is None:
            baseline = measured
        comparable = [query for query in measured if query in baseline]
        total = sum(measured[query] for query in comparable)
        base_total = sum(baseline[query] for query in comparable)
        changed = [f"{query} {baseline[query]}→{measured[query]}" for query in comparable
                   if measured[query] != baseline[query]]
        label = "all seven" if rule is None else f"without {rule.value} ({RULE_NAMES[rule]})"
        delta = "" if rule is None else f"{total - base_total:+d} ({'; '.join(changed) or 'no query changed'})"
        print(f"| {label} | {total} | {delta} | {'; '.join(skipped) or 'none'} |", flush=True)
    print()


def frame_sensitivity(shell, queries):
    print("## How B changes the chosen algorithms\n")
    print("| query | " + " | ".join(f"B = {b}" for b in (4, 8, 16, 32, 128)) + " |")
    print("|---|---|---|---|---|---|")
    for name, sql in queries.items():
        choices = []
        for frames in (4, 8, 16, 32, 128):
            _, physical = Planner(shell.db.catalog, frames).optimize(compile_query(shell, sql))
            choices.append(", ".join(algorithms(physical)) or "-")
        if len(set(choices)) > 1:
            print(f"| {name} | " + " | ".join(choices) + " |")
    print()


def correlation(directory, query, filter_text):
    print(f"## Correlated columns: {query}\n")
    print("| B | chosen plan | est. rows of the emp filter | actual | q-error | est. cost | actual I/O | "
          "nested loop I/O | sort-merge I/O | hash I/O |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for frames in (4, 6, 8, 12, 16, 32):
        shell = Shell(directory, frames)
        logical = compile_query(shell, query)
        run = shell.run_query(logical)
        est = actual = None
        for p, op, _, loops, _ in run.node_pairs():
            if p.op == "filter" and filter_text in p.label:
                est, actual = p.est.rows, op.counters()["tuples_out"] // loops
        forced = [shell.run_query(logical, Planner(shell.db.catalog, frames, fixed_join=algorithm)).io
                  for algorithm in JOIN_ALGORITHMS]
        shell.close()
        print(f"| {frames} | {', '.join(algorithms(run.physical))} | {est} | {actual} | "
              f"{q_error(est, actual):.2f} | {run.physical.cost} | {run.io} | {forced[0]} | {forced[1]} | "
              f"{forced[2]} |", flush=True)
    print()


def dp_table(shell, sql, frames):
    print(f"## DP table, four relations, B = {frames}\n")
    print(f"`{sql}`\n")
    planner = Planner(shell.db.catalog, frames)
    _, physical = planner.optimize(compile_query(shell, sql))
    print("```")
    print(planner.dp_table_text())
    print("```\n")
    print("Chosen plan:\n")
    print("```")
    print("\n".join(physical.explain()))
    print("```\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    args = parser.parse_args()
    queries = load_queries(args.queries)
    shell = Shell(args.db, args.frames)
    frame_sensitivity(shell, queries)
    dp_table(shell, queries["q08"], args.frames)
    ablation(shell, queries, args.frames)
    shell.close()
    correlation(args.db, queries["q15"], "e.level = 5")
    correlation(args.db, "SELECT e.name, e.city, e.age, e.salary, w.pid, w.hours FROM emp e JOIN works w "
                         "ON e.eid = w.eid WHERE e.level = 5 AND e.salary >= 105000;", "e.level = 5")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    main()
