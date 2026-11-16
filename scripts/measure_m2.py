#!/usr/bin/env python3
"""Measurements for REPORT_M2.md: join I/O against the formulas, sort passes,
crossover, skew, and the memory budget.

    python scripts/measure_m2.py WORK_DIR > results_m2.md

Writes images/m2_crossover_frames.svg and images/m2_crossover_size.svg.
"""
import math
import os
import random
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbms.execution.plan import prepare, execute, totals  # noqa: E402
from dbms.optimizer.planner import JOIN_ALGORITHMS, sort_passes  # noqa: E402
from dbms.storage.database import Database  # noqa: E402
from dbms.storage.page import records_per_page  # noqa: E402
from dbms.storage.record import parse_schema  # noqa: E402
from svgplot import line_chart  # noqa: E402

PER_PAGE = records_per_page(4 + 4 + 2 + 150)


def build(work):
    """R<n>(a, b, pad) and S<n>(b, c, pad), n pages each; b gives each R tuple about two S matches."""
    db_dir = os.path.join(work, "m2db")
    if os.path.exists(db_dir):
        shutil.rmtree(db_dir)
    db = Database(db_dir, 64)
    rng = random.Random(2)
    for pages in (5, 10, 20, 40, 60, 80, 160, 180):
        n = pages * PER_PAGE
        for name, schema in ((f"R{pages}", "a:INT,b:INT,pad:VARCHAR(150)"), (f"S{pages}", "b:INT,c:INT,pad:VARCHAR(150)")):
            db.create_table(name, parse_schema(schema))
            heap = db.heap_file(name)
            for i in range(n):
                key = rng.randrange(max(1, n // 2))
                heap.insert((i, key, "x" * 150) if name[0] == "R" else (key, i, "x" * 150))
    for pages in (8, 60, 100, 320, 500):
        db.create_table(f"T{pages}", parse_schema("a:INT,b:INT,pad:VARCHAR(150)"))
        heap = db.heap_file(f"T{pages}")
        for i in range(pages * PER_PAGE):
            heap.insert((i, rng.randrange(1 << 30), "y" * 150))
    n = 60 * PER_PAGE
    for name, heavy in (("K1", True), ("K2", True), ("U1", False), ("U2", False)):
        db.create_table(name, parse_schema("k:INT,v:INT,pad:VARCHAR(150)"))
        heap = db.heap_file(name)
        for i in range(n):
            key = 7 if heavy and rng.random() < 0.4 else rng.randrange(n)
            heap.insert((key, i, "z" * 150))
    db.save()
    db.close()
    return db_dir


def run(db_dir, plan, frames):
    db = Database(db_dir, frames)
    root = prepare(db, plan)
    rows = execute(db, root)
    peak = db.pool.peak_held
    db.close()
    counts = totals(root, rows)
    return root, rows, counts["page_reads"], counts["page_writes"], peak


def scan(t):
    return {"op": "seqscan", "table": t}


def handout_passes(pages, frames):
    if pages <= frames:
        return 1
    return 1 + math.ceil(math.log(math.ceil(pages / frames)) / math.log(frames - 1) - 1e-12)


def formula(algorithm, m, n, frames):
    if algorithm == "nested_loop_join":
        return m + math.ceil(m / (frames - 2)) * n
    if algorithm == "hash_join":
        return 3 * (m + n)
    sort = lambda pages: 2 * pages * (handout_passes(pages, frames) - 1) + pages  # noqa: E731
    return sort(m) + sort(n) + m + n


def join_plan(algorithm, m, n):
    return {"op": algorithm, "condition": f"R{m}.b = S{n}.b", "left": scan(f"R{m}"), "right": scan(f"S{n}")}


def joins_against_formula(db_dir):
    print("## Joins: measured page I/O against the formula\n")
    print(f"R and S have {PER_PAGE} tuples per page; M = N = data pages of each input; the scans also read "
          "one directory page per pass.\n")
    print("| join | M = N | B | reads | writes | measured I/O | formula | measured - formula | comparisons | output |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for algorithm in JOIN_ALGORITHMS:
        for size in (20, 60, 180):
            for frames in (4, 16, 64, 256):
                root, rows, reads, writes, _ = run(db_dir, join_plan(algorithm, size, size), frames)
                io = reads + writes
                expected = formula(algorithm, size, size, frames)
                print(f"| {algorithm} | {size} | {frames} | {reads} | {writes} | {io} | {expected} | "
                      f"{io - expected:+d} | {root.counters()['comparisons']} | {len(rows)} |")
    print()


def sorts_against_formula(db_dir):
    print("## External sort: passes against the formula\n")
    print("| N pages | B | measured passes | handout 1+ceil(log(ceil(N/B))/log(B-1)) | "
          "ours 1+ceil(log(ceil(N/(B-1)))/log(B-1)) | reads | writes | peak frames |")
    print("|---|---|---|---|---|---|---|---|")
    for pages, frames in ((8, 3), (60, 4), (80, 4), (100, 5), (180, 8), (320, 16), (500, 10), (500, 64)):
        table = f"T{pages}" if pages in (8, 60, 100, 320, 500) else f"R{pages}"
        root, _, reads, writes, peak = run(db_dir, {"op": "sort", "keys": [[f"{table}.b", "asc"]],
                                                    "child": scan(table)}, frames)
        print(f"| {pages} | {frames} | {root.counters()['passes']} | {handout_passes(pages, frames)} | "
              f"{sort_passes(pages, frames)} | {reads} | {writes} | {peak} |")
    print()


def crossover(db_dir):
    print("## Crossover\n")
    print("Fixed relations R60 (60 pages) and S180 (180 pages), B varies:\n")
    print("| B | " + " | ".join(JOIN_ALGORITHMS) + " | cheapest |")
    print("|---|---|---|---|---|")
    series = {a: [] for a in JOIN_ALGORITHMS}
    for frames in (4, 6, 8, 12, 16, 22, 32, 48, 64):
        costs = {}
        for algorithm in JOIN_ALGORITHMS:
            _, _, reads, writes, _ = run(db_dir, join_plan(algorithm, 60, 180), frames)
            costs[algorithm] = reads + writes
            series[algorithm].append((frames, reads + writes))
        print(f"| {frames} | " + " | ".join(str(costs[a]) for a in JOIN_ALGORITHMS) + f" | {min(costs, key=costs.get)} |")
    line_chart(os.path.join(ROOT, "images", "m2_crossover_frames.svg"),
               "Join page I/O, R = 60 pages, S = 180 pages", "buffer frames B", "page reads + writes (log scale)",
               [(a, series[a]) for a in JOIN_ALGORITHMS], log_x=True, log_y=True)
    print("\nFixed B = 16, both inputs of the same size:\n")
    print("| M = N | " + " | ".join(JOIN_ALGORITHMS) + " | cheapest |")
    print("|---|---|---|---|---|")
    series = {a: [] for a in JOIN_ALGORITHMS}
    for size in (5, 10, 20, 40, 80, 160):
        costs = {}
        for algorithm in JOIN_ALGORITHMS:
            _, _, reads, writes, _ = run(db_dir, join_plan(algorithm, size, size), 16)
            costs[algorithm] = reads + writes
            series[algorithm].append((size, reads + writes))
        print(f"| {size} | " + " | ".join(str(costs[a]) for a in JOIN_ALGORITHMS) + f" | {min(costs, key=costs.get)} |")
    line_chart(os.path.join(ROOT, "images", "m2_crossover_size.svg"),
               "Join page I/O at B = 16, M = N", "pages per input", "page reads + writes (log scale)",
               [(a, series[a]) for a in JOIN_ALGORITHMS], log_x=True, log_y=True)
    print()


def skew(db_dir):
    print("## Skew: hash join with one key in 40 % of rows on both sides (B = 16)\n")
    print("| data | output rows | partitions (left pages, right pages) | overflowing partitions | reads | writes | "
          "I/O | comparisons |")
    print("|---|---|---|---|---|---|---|---|")
    for label, left, right in (("skewed", "K1", "K2"), ("uniform", "U1", "U2")):
        plan = {"op": "hash_join", "condition": f"{left}.k = {right}.k", "left": scan(left), "right": scan(right)}
        root, rows, reads, writes, _ = run(db_dir, plan, 16)
        sizes = " ".join(f"({left_pages},{right_pages})" for left_pages, right_pages in root.partition_sizes)
        print(f"| {label} | {len(rows)} | {sizes} | {root.counters()['overflow_partitions']} | {reads} | {writes} | "
              f"{reads + writes} | {root.counters()['comparisons']} |")
    print()


def budget(db_dir):
    print("## The budget: sort of 20 x B pages at B = 4\n")
    root, rows, reads, writes, peak = run(db_dir, {"op": "sort", "keys": [["R80.b", "desc"]], "child": scan("R80")}, 4)
    ok = all(rows[i][1] >= rows[i + 1][1] for i in range(len(rows) - 1))
    print(f"R80: 80 data pages, {len(rows)} tuples, sorted correctly: {ok}; passes {root.counters()['passes']}, "
          f"reads {reads}, writes {writes}; largest number of frames held at once: {peak} of 4.\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    db_dir = build(work)
    joins_against_formula(db_dir)
    sorts_against_formula(db_dir)
    crossover(db_dir)
    skew(db_dir)
    budget(db_dir)
