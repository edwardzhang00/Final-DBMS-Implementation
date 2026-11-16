"""storage: command line tool for the storage engine."""

import argparse
import json
import os
import sys

from .bufferpool import replay_trace
from .database import Database, open_database
from .disk import StorageError
from .loader import load_csv
from .page import (
    page_header, slot_entries, free_space, directory_header, directory_entries, NO_PAGE,
    HEADER_SIZE, SLOT_SIZE, DIR_HEADER_SIZE, DIR_ENTRY_SIZE,
)
from .record import parse_schema, is_ascii_digits

DEFAULT_FRAMES = 64


def cmd_create_table(args):
    schema = parse_schema(args.schema)
    db = Database(args.db, args.frames)
    try:
        info = db.create_table(args.name, schema)
    finally:
        db.close()
    print(f"created table {info.name} ({', '.join(c.name + ' ' + c.type_text() for c in info.schema)})")


def cmd_load(args):
    db = open_database(args.db, args.frames)
    count = load_csv(db, args.table, args.file)
    info = db.catalog.get_table(args.table)
    db.close()
    print(f"loaded {count} rows into {info.name}: {info.num_tuples} tuples, "
          f"{info.data_pages} data pages, {info.num_pages} pages in {info.file}")


def cmd_scan(args):
    if args.limit is not None and args.limit < 0:
        raise StorageError("--limit must be 0 or more")
    db = open_database(args.db, args.frames)
    heap = db.heap_file(args.table)
    db.pool.reset_stats()
    rows = []
    scan = heap.scan()
    if args.limit != 0:
        for _, row in scan:
            rows.append(row)
            if len(rows) == args.limit:
                break
    scan.close()
    stats = db.pool.stats()
    db.close()
    if args.json:
        print(json.dumps({"table": args.table, "schema": [c.to_json() for c in heap.info.schema],
                          "rows": rows, "stats": stats}))
        return
    print("\t".join(c.name for c in heap.info.schema))
    for row in rows:
        print("\t".join(str(v) for v in row))
    print(f"({len(rows)} rows) " + " ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_layout(args):
    db = open_database(args.db, args.frames)
    heap = db.heap_file(args.table)
    disk = heap.disk
    if not 0 <= args.page < disk.num_pages():
        raise StorageError(f"Table {args.table} has {disk.num_pages()} pages; page {args.page} does not exist")
    is_directory = args.page in [dir_id for dir_id, _ in heap.directory_pages()]
    page = db.pool.fetch_page(disk, args.page)
    lines = [f"table {args.table}, file {heap.info.file}, page {args.page}: "
             + ("directory page" if is_directory else "data page")]
    if is_directory:
        page_id, next_page, count = directory_header(page)
        lines += [f"  offset 0   page_id        {page_id}",
                  f"  offset 4   next_dir_page  {'none (0xFFFFFFFF)' if next_page == NO_PAGE else next_page}",
                  f"  offset 8   entry_count    {count}"]
        for i, (data_id, free_bytes) in enumerate(directory_entries(page)):
            lines.append(f"  offset {DIR_HEADER_SIZE + DIR_ENTRY_SIZE * i:<4} entry {i}: "
                         f"data_page_id={data_id} free_bytes={free_bytes}")
    else:
        page_id, slot_count, free_end = page_header(page)
        slots = slot_entries(page)
        lines += [f"  offset 0   page_id     {page_id}",
                  f"  offset 4   slot_count  {slot_count}",
                  f"  offset 6   free_end    {free_end}",
                  f"  free space {free_space(page)} bytes "
                  f"(offset {HEADER_SIZE + SLOT_SIZE * slot_count} to {free_end - 1})",
                  "  slot directory " + " ".join(f"{slot}:({offset},{length})" for slot, offset, length in slots)]
        for slot, offset, length in slots:
            where = f"slot {slot} at offset {HEADER_SIZE + SLOT_SIZE * slot}"
            if offset == 0:
                lines.append(f"  {where}: dead")
            else:
                values = heap.decode(page[offset:offset + length])
                lines.append(f"  {where}: record bytes {offset}..{offset + length - 1} "
                             f"({length} bytes) {values}")
    db.pool.unpin_page(disk, args.page, False)
    db.close()
    print("\n".join(lines))


def cmd_delete(args):
    page_text, _, slot_text = args.rid.partition(":")
    if not (is_ascii_digits(page_text) and is_ascii_digits(slot_text)):
        raise StorageError(f"--rid must be page:slot, got {args.rid!r}")
    db = open_database(args.db, args.frames)
    try:
        db.heap_file(args.table).delete((int(page_text), int(slot_text)))
        db.save()
    finally:
        db.close()
    print(f"deleted ({page_text}, {slot_text}) from {args.table}")


def cmd_trace(args):
    page_ids = []
    with open(args.file, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            if not is_ascii_digits(text):
                raise StorageError(f"{args.file} line {line_no}: {text!r} is not a page id")
            page_ids.append(int(text))
    os.makedirs(args.db, exist_ok=True)
    pool = replay_trace(os.path.join(args.db, "trace.tmp"), page_ids, args.frames, args.policy)
    result = {"policy": args.policy, "frames": args.frames}
    result.update(pool.stats())
    if args.json:
        print(json.dumps(result))
    else:
        accesses = len(page_ids)
        hit_rate = f" hit_rate={result['hits'] / accesses:.4f}" if accesses else ""
        print(" ".join(f"{k}={v}" for k, v in result.items()) + hit_rate)


def build_parser():
    parser = argparse.ArgumentParser(prog="storage", description="Module 1 storage engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-table")
    p.add_argument("--db", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.set_defaults(run=cmd_create_table)

    p = sub.add_parser("load")
    p.add_argument("--db", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.set_defaults(run=cmd_load)

    p = sub.add_parser("scan")
    p.add_argument("--db", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_scan)

    p = sub.add_parser("layout")
    p.add_argument("--db", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.set_defaults(run=cmd_layout)

    p = sub.add_parser("delete")
    p.add_argument("--db", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--rid", required=True, help="page:slot")
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.set_defaults(run=cmd_delete)

    p = sub.add_parser("trace")
    p.add_argument("--db", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--frames", type=int, required=True)
    p.add_argument("--policy", choices=["lru", "clock"], default="clock")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_trace)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except (StorageError, OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
