#!/usr/bin/env python3
"""Measurements for REPORT_M1.md: record capacity, replacement policies, scan cost.

    python scripts/measure_m1.py WORK_DIR > results_m1.md

Writes the five traces to workload/traces/ if they are missing. The tables it
prints are the ones in REPORT_M1.md.
"""
import os
import random
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from dbms.storage.bufferpool import replay_trace  # noqa: E402
from dbms.storage.database import Database  # noqa: E402
from dbms.storage.page import SLOT_SIZE, USABLE_SPACE, page_header, records_per_page  # noqa: E402
from dbms.storage.record import parse_schema, sizer  # noqa: E402

TRACES = os.path.join(ROOT, "workload", "traces")


def uniform(rng, pages, accesses):
    return [rng.randrange(pages) for _ in range(accesses)]


def hot_set(rng, hot_pages, cold_pages, hot_share, accesses):
    return [rng.randrange(hot_pages) if rng.random() < hot_share else hot_pages + rng.randrange(cold_pages)
            for _ in range(accesses)]


def repeated_scan(pages, times):
    return [page for _ in range(times) for page in range(pages)]


def scan_with_hot_lookups(rng, first_page, pages, hot_pages, times):
    """times scans of the pages from first_page on, each page followed by a lookup on one of
    the first hot_pages pages."""
    return [access for _ in range(times) for page in range(pages)
            for access in (first_page + page, rng.randrange(hot_pages))]


def zipf_like(rng, pages, alpha, accesses):
    return [min(pages - 1, int(rng.paretovariate(alpha)) - 1) for _ in range(accesses)]


def make_traces():
    rng = random.Random(3005)
    traces = {
        "trace1": uniform(rng, pages=200, accesses=5000),
        "trace2": hot_set(rng, hot_pages=50, cold_pages=200, hot_share=0.8, accesses=5000),
        "trace3": repeated_scan(pages=80, times=10),
        "trace4": scan_with_hot_lookups(rng, first_page=100, pages=300, hot_pages=10, times=2),
        "trace5": zipf_like(rng, pages=1000, alpha=1.1, accesses=5000),
    }
    os.makedirs(TRACES, exist_ok=True)
    for name, pages in traces.items():
        path = os.path.join(TRACES, name + ".txt")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(map(str, pages)) + "\n")


def read_trace(path):
    with open(path) as f:
        return [int(line) for line in f if line.strip()]


def remove_database(db_dir):
    if os.path.exists(db_dir):
        shutil.rmtree(db_dir)


def capacity(work):
    print("## Capacity\n")
    print(f"| schema | record bytes | computed floor({USABLE_SPACE}/(L+{SLOT_SIZE})) | measured on a full page | gap |")
    print("|---|---|---|---|---|")
    rng = random.Random(1)
    cases = [
        ("a INT, b INT, c INT", "a:INT,b:INT,c:INT", lambda i: (i, i, i), "12"),
        ("a INT, b VARCHAR(12), random length 1..12", "a:INT,b:VARCHAR(12)",
         lambda i: (i, "x" * rng.randrange(1, 13)), "4+2+L, L avg 6.5"),
        ("a INT, b VARCHAR(200), always 200", "a:INT,b:VARCHAR(200)", lambda i: (i, "y" * 200), "206"),
        ("a INT, b VARCHAR(200), random length 1..200", "a:INT,b:VARCHAR(200)",
         lambda i: (i, "z" * rng.randrange(1, 201)), "4+2+L, L avg 100.5"),
    ]
    for k, (label, schema, row, size_text) in enumerate(cases):
        db_dir = os.path.join(work, f"cap{k}")
        remove_database(db_dir)
        db = Database(db_dir, 16)
        info = db.create_table("T", parse_schema(schema))
        heap = db.heap_file("T")
        record_size = sizer(info.schema)
        lengths = []
        for i in range(3000):
            values = row(i)
            heap.insert(values)
            lengths.append(record_size(values))
        db.save()
        full_pages = []
        for page_id in range(1, info.num_pages - 1):
            page = db.pool.fetch_page(heap.disk, page_id)
            full_pages.append(page_header(page).slot_count)
            db.pool.unpin_page(heap.disk, page_id, False)
        db.close()
        worst = max(lengths)
        mean = sum(lengths) / len(lengths)
        measured = sum(full_pages) / len(full_pages)
        computed = (f"{records_per_page(worst)} at L={worst} (declared max); "
                    f"{USABLE_SPACE / (mean + SLOT_SIZE):.1f} at the mean L={mean:.1f}" if worst != min(lengths)
                    else f"{records_per_page(worst)}")
        print(f"| {label} | {size_text} | {computed} | {measured:.1f} | "
              f"{'none' if worst == min(lengths) and round(measured) == records_per_page(worst) else 'see text'} |")
    print()


def policies(work):
    print("## Replacement policies: hit rate\n")
    print("| trace | accesses | distinct pages | B | LRU hit rate | CLOCK hit rate |")
    print("|---|---|---|---|---|---|")
    for name in sorted(os.listdir(TRACES)):
        pages = read_trace(os.path.join(TRACES, name))
        for frames in (4, 16, 64, 256):
            rates = []
            for policy in ("lru", "clock"):
                stats = replay_trace(os.path.join(work, "trace.db"), pages, frames, policy).stats()
                rates.append(f"{stats['hits'] / len(pages):.3f} ({stats['hits']} hits, {stats['evictions']} evictions)")
            print(f"| {name[:-4]} | {len(pages)} | {len(set(pages))} | {frames} | {rates[0]} | {rates[1]} |")
    print()


def scan_cost(work):
    print("## Scan cost of a 500 page table\n")
    db_dir = os.path.join(work, "scan500")
    remove_database(db_dir)
    db = Database(db_dir, 16)
    db.create_table("T", parse_schema("a:INT,b:VARCHAR(200)"))
    heap = db.heap_file("T")
    per_page = records_per_page(4 + 2 + 200)
    for i in range(per_page * 500):
        heap.insert((i, "p" * 200))
    db.save()
    info = db.catalog.get_table("T")
    db.close()
    print(f"T: {info.num_tuples} tuples, {info.data_pages} data pages + "
          f"{info.num_pages - info.data_pages} directory page\n")
    print("| B | page reads | hits | misses | evictions |")
    print("|---|---|---|---|---|")
    for frames in (2, 8, 64):
        db = Database(db_dir, frames)
        heap = db.heap_file("T")
        db.pool.reset_stats()
        for _ in heap.scan():
            pass
        stats = db.pool.stats()
        db.close()
        print(f"| {frames} | {stats['reads']} | {stats['hits']} | {stats['misses']} | {stats['evictions']} |")
    print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    make_traces()
    capacity(work)
    policies(work)
    scan_cost(work)
