#!/usr/bin/env python3
"""Performance study: R join[R.b=S.b] S at n = 1000, 2000, ..., 64000 tuples
per relation, and a select and a project at the same sizes, written as a CSV
table of tuple counts and wall time.

Usage:
    python experiment.py --match-rate 10 --out results.csv
"""
import argparse
import csv
import sys
import time

import datagen
from dbms.ra.parser import parse_query
from dbms.ra.operators import eval_expr, dict_lookup, STATS, reset_stats, Relation, Column

SIZES = [1000, 2000, 4000, 8000, 16000, 32000, 64000]
JOIN_QUERY    = "R join[R.b=S.b] S"
SELECT_QUERY  = "select[b>0](R)"
PROJECT_QUERY = "project[b](R)"


def make_db(n, m, match_rate, seed):
    """Generate R(a,b) and S(b,c) and return a database dict of Relations."""
    R, S, _ = datagen.generate(n, m, match_rate, seed)
    db = {}
    db["R"] = Relation("R", [Column("R", "a"), Column("R", "b")], R)
    db["S"] = Relation("S", [Column("S", "b"), Column("S", "c")], S)
    return db


def run_one(db, query_tree, query_text, n, m):
    reset_stats()
    start = time.perf_counter()
    output = sum(1 for _ in eval_expr(query_tree, dict_lookup(db)).rows)
    end = time.perf_counter()

    return {
        "n": n,
        "m": m,
        "query": query_text,
        "join_pairs":     STATS["join_pairs"],
        "select_tuples":  STATS["select_tuples"],
        "project_tuples": STATS["project_tuples"],
        "wall_time": end - start,
        "output": output,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-rate", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--sizes", type=int, nargs="*", default=SIZES)
    parser.add_argument("--queries", nargs="*", default=["join", "select", "project"])
    parser.add_argument("--out", default="results.csv")
    args = parser.parse_args()

    query_map = {
        "join":    JOIN_QUERY,
        "select":  SELECT_QUERY,
        "project": PROJECT_QUERY,
    }
    parsed_queries = {
        name: parse_query(query_text)
        for name, query_text in query_map.items()
    }

    rows = []
    print(f"{'query':>8} {'n':>7} {'m':>7} {'pairs':>14} "
          f"{'sel':>10} {'proj':>10} {'wall (s)':>10} {'out':>9}")

    for n in args.sizes:
        db = make_db(n, n, args.match_rate, args.seed)

        for query_name in args.queries:
            result = run_one(db, parsed_queries[query_name], query_map[query_name], n, n)
            rows.append(result)

            print(f"{query_name:>8} {result['n']:>7} {result['m']:>7} "
                  f"{result['join_pairs']:>14} {result['select_tuples']:>10} "
                  f"{result['project_tuples']:>10} {result['wall_time']:>10.3f} "
                  f"{result['output']:>9}")
            sys.stdout.flush()

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()