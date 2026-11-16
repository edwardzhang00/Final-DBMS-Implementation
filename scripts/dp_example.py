#!/usr/bin/env python3
"""Build the database of the three-relation DP join-ordering example for `opt dp-table`.

The example gives statistics, not data: A has 100 pages and 5000 tuples, B'
(here Bp) 10 pages and 500 tuples, C 400 pages and 20000 tuples; A.x = B'.x
estimates 1000 rows on 20 pages and B'.y = C.y 2000 rows on 40 pages. The
tables are created empty and these statistics are written to the catalog:
widths of 40 bytes give 20 and 40 pages for the two joins, and
max(ndv) of 2500 and 5000 give 1000 and 2000 rows.

    python scripts/dp_example.py DIR
    bin/sql compile --db DIR --file DIR/q.sql --json > DIR/q.json
    bin/opt dp-table --db DIR --logical DIR/q.json --frames 32
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dbms.storage.catalog import ColumnStats, TableStats  # noqa: E402
from dbms.storage.database import Database  # noqa: E402
from dbms.storage.record import parse_schema  # noqa: E402

TABLES = {
    "A": ("x:INT,a:VARCHAR(40)", 5000, 100, [(2500, 4), (None, 36)]),
    "Bp": ("x:INT,y:INT,b:VARCHAR(40)", 500, 10, [(500, 4), (500, 4), (None, 32)]),
    "C": ("y:INT,c:VARCHAR(40)", 20000, 400, [(5000, 4), (None, 36)]),
}


def main(directory):
    db = Database(directory, 8)
    for name, (schema, tuples, pages, columns) in TABLES.items():
        info = db.create_table(name, parse_schema(schema))
        stats = [ColumnStats(ndv or tuples, width, None, None, []) for ndv, width in columns]
        info.stats = TableStats(tuples, pages, sum(w for _, w in columns), stats)
    db.save()
    db.close()
    with open(os.path.join(directory, "q.sql"), "w", encoding="utf-8") as f:
        f.write("SELECT * FROM A, Bp, C WHERE A.x = Bp.x AND Bp.y = C.y;\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    main(sys.argv[1])
