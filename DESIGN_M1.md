# Module 1 design: storage engine and buffer pool

Code: `dbms/storage/` (`disk.py`, `record.py`, `page.py`, `heapfile.py`, `bufferpool.py`,
`catalog.py`, `loader.py`, `database.py`, `tempfile.py`, `cli.py`). Command: `bin/storage`.

## Files

A database is a directory holding `catalog.db`, one `<table>.db` per table, and, only while a
query runs, `tmp/` for the executor's spill files. Every file is a sequence of 4096-byte pages with
nothing outside them. A page id is the zero-based index of the page in its file, and a rid is
`(page id, slot)`. All integers are little-endian.

`DiskManager` (`disk.py`) is the only code that opens a table file. `read_page` and
`write_page` count one read and one write each. `allocate_page` extends the file by a zero page
and is not counted, because it is not a `write_page` call. A new page is written (and counted)
when the buffer pool evicts or flushes it.

## Record format

Fields in schema order, no header: `INT` is 4 bytes signed, `VARCHAR(n)` is a uint16 byte
length `L` (`L <= n <= 255`) followed by `L` bytes of UTF-8. `(42, 'bob', 7)` under
`(a INT, b VARCHAR(5), c INT)` is `2a 00 00 00 | 03 00 62 6f 62 | 07 00 00 00`, 13 bytes
(`tests/test_storage.py::test_handout_record_example`). Code never assumes a fixed length.

## Data page, byte by byte (a real page)

Below is page 1 of `R.db` after loading `42,bob,7`, `1,abcdefghij,2` and `43,amy,9` into
`R(a INT, b VARCHAR(20), c INT)` and then running `storage delete --rid 1:1`. The bytes are
read from the file itself.

```
offset  bytes                       field
0       01 00 00 00                 page_id = 1
4       03 00                       slot_count = 3 (the dead slot still counts)
6       d2 0f                       free_end = 0x0fd2 = 4050
8       f3 0f 0d 00                 slot 0: offset 4083, length 13
12      00 00 00 00                 slot 1: offset 0, length 0  -> dead
16      d2 0f 0d 00                 slot 2: offset 4050, length 13
20                                  free space starts here
...     (zeros)                     free = free_end - (8 + 4 * slot_count) = 4050 - 20 = 4030
4050    2b 00 00 00 03 00 61 6d 79 09 00 00 00          record of slot 2: (43, 'amy', 9)
4063    01 00 00 00 0a 00 61 62 63 64 65 66 67 68 69 6a 02 00 00 00
                                    the deleted 20-byte record: its bytes stay, nothing points at them
4083    2a 00 00 00 03 00 62 6f 62 07 00 00 00          record of slot 0: (42, 'bob', 7)
4095                                end of page
```

`storage layout --db DIR --table R --page 1` prints the same page:

```
table R, file R.db, page 1: data page
  offset 0   page_id     1
  offset 4   slot_count  3
  offset 6   free_end    4050
  free space 4030 bytes (offset 20 to 4049)
  slot directory 0:(4083,13) 1:(0,0) 2:(4050,13)
  slot 0 at offset 8: record bytes 4083..4095 (13 bytes) (42, 'bob', 7)
  slot 1 at offset 12: dead
  slot 2 at offset 16: record bytes 4050..4062 (13 bytes) (43, 'amy', 9)
```

These are exactly the numbers of the handout's worked example (section 3.2): 4083/4071,
4063/4047, 4050/4030, then the delete changes nothing but slot 1. Insert, get and delete follow
the handout's pseudocode (`page.py`). A slot is never reused or renumbered, so a rid stays
valid. Reading a dead slot raises an error naming the rid.

## Heap file and free space strategy

Page 0 of every table file is the first directory page:

```
offset 0   uint32 page_id
offset 4   uint32 next_dir_page   (0xFFFFFFFF: none)
offset 8   uint16 entry_count
offset 10  entries of 6 bytes: uint32 data_page_id, uint16 free_bytes   (681 per page)
```

Page 0 of the `R.db` above is `00 00 00 00 | ff ff ff ff | 01 00 | 01 00 00 00 be 0f`. That is
page 0, no next directory page, one entry: data page 1 with `0x0fbe` = 4030 free bytes.

**Free space strategy: first fit through the directory.** An insert of an `L`-byte record walks
the directory pages and takes the first data page whose `free_bytes >= L + 4`. Only that data
page is fetched, so the walk reads directory entries and not data pages. If no page has room, a
new data page is allocated and its entry appended to the last directory page; a full directory
page gets a successor, linked through `next_dir_page`. At most two pages are pinned during an
insert. With fixed-length records loaded in order, first fit fills pages to capacity: every page except
the last is full, which is what makes the capacity measurement in REPORT_M1 exact. With
variable-length records, a short record can still land in an earlier page that a long one did
not fit, which is first fit doing its job. A free-space bitmap
or a "last page with room" hint would make inserts O(1); the handout asks for the directory, so
the walk stays.

`delete(rid)` first finds the page's entry in the directory, so a rid on a page that is not one of
the table's data pages (a directory page, or past the end of the file) is an error and nothing is
touched. It then marks the slot dead and refreshes the entry's `free_bytes`. Because space is not reclaimed, the value does not change, so the directory page is
unpinned clean and nothing extra is written.

**Compaction decision: none.** Deleted bytes stay where they are (the 20 bytes at 4063 above).
Compaction would have to move live records and rewrite slot offsets while rids stay valid. It
pays only for workloads that delete and then insert into the same table, and this project's
workload never deletes. The cost is visible: a page whose records were all deleted still costs a
read in every scan, and `scan` of such a table returns nothing without error (tested).

`scan()` holds at most one page pinned. It pins a directory page only long enough to copy that
page's list of data page ids (at most 681 four-byte ids), unpins it, then pins each data page in
turn while its live records are returned, and unpins it before moving on. Holding the directory
page and a data page together would also be within the handout's rule (one of each). Releasing
the directory first means a scan costs exactly one frame, which Module 2's memory budget counts
on. A record that cannot fit on an empty page (more than 4084 bytes plus its 4-byte slot) is
rejected with the table name and the row.

## Buffer pool

`BufferPool(frames, policy)` (`bufferpool.py`) has a fixed array of B frames. Each frame holds
the page bytes, the disk it came from, a pin count and a dirty bit. A page table maps
`(file, page id)` to a frame, so one pool serves every file of the database. `fetch_page`,
`unpin_page`, `new_page`, `flush_all` and `stats` follow the handout's pseudocode and counting
rules:

- a hit is a fetch of a resident page;
- a miss reads the page;
- an eviction is a miss that replaced a resident page (the first B misses are not evictions);
- every read and every write is one disk-manager call.

A fetch takes an empty frame (lowest index first) before it asks the policy for a victim. If no
frame is unpinned, the fetch fails with "no unpinned frame available". A pinned page is never
evicted (tested for both policies). Unpinning a page at pin count 0 is an error.

- **LRU** keeps frames in order of last access. Every fetch, hit or miss, moves the frame to the
  most-recent end. The victim is the least recent unpinned frame.
- **CLOCK** (the default of every tool, as REPORT_M1 recommends; `--policy lru` selects LRU) keeps a
  reference bit per frame and a hand. Every fetch sets the bit. Loading a page
  sets its bit and moves the hand past it. `choose_victim` is the handout's loop, giving up after
  sweeping 2B frames.

The handout's trace `1 2 3 1 4 2` at B = 3 gives LRU 1 hit, 5 misses, 2 evictions (pool ends
as 1, 4, 2) and CLOCK 2 hits, 4 misses, 1 eviction (4, 2, 3), from `storage trace` and in
`test_handout_trace_lru` / `test_handout_trace_clock`.

**The pool also enforces the executor's memory budget.** `reserve(n)` takes n frames out of the
pool for an operator's own tuple buffers, evicting unpinned pages if needed, and `release(n)`
gives them back. The pool keeps `pinned + reserved <= B` at every moment, refuses anything that
would break it, and records the largest value reached (`peak_held`). Module 2 explains how
operators use this. `clear()` empties every unpinned frame, so the integrated shell can start
each query from a cold pool.

## Catalog page format

`catalog.db` is read and written through the buffer pool like any other file. Each catalog page
is a chunk header followed by payload:

```
offset 0  uint32 page_id
offset 4  uint16 bytes of payload on this page
offset 6  up to 4090 bytes of payload
```

The payload, concatenated over the pages in order, is a sequence of fields (`str` = uint16
length + UTF-8):

```
uint16 table_count
per table:  str name, str file, uint32 num_pages, uint32 data_pages, uint32 num_tuples,
            uint16 column_count,
            per column: str name, uint8 type (0 INT, 1 VARCHAR), uint16 length
            uint8 has_stats, and when it is 1 (written by ANALYZE, Module 4):
              uint32 n_tuples, uint32 n_pages, float64 width
              per column: uint32 ndv, float64 avg_width, uint8 has_range, [min, max],
                          uint16 bucket_count, per bucket: lo, hi, uint32 count, uint32 ndv
            (min, max, lo, hi are stored in the column's own encoding)
```

A table's first directory page is always page 0 of its file, so the catalog does not store it.

The catalog of the `R` database above is one page with 44 payload bytes:

```
00 00 00 00 2c 00                    page 0, 44 payload bytes
01 00                                1 table
01 00 52                             name "R"
04 00 52 2e 64 62                    file "R.db"
02 00 00 00 01 00 00 00              num_pages 2, data_pages 1
02 00 00 00                          num_tuples 2 (one of three was deleted)
03 00                                3 columns
01 00 61 00 00 00                    "a" INT
01 00 62 01 14 00                    "b" VARCHAR(20)
01 00 63 00 00 00                    "c" INT
00                                   no statistics yet
```

The whole catalog is rewritten on each change (create, load, insert, ANALYZE). The same content
therefore always produces the same bytes, which is why running ANALYZE twice leaves
`catalog.db` byte-identical (tested in Module 4). Trailing pages left over when the catalog
shrinks are written with 0 payload bytes.

## Loader

`storage load` reads CSV with no header line, one record per line. A field containing a comma is
wrapped in double quotes, and inside quotes `""` is one quote. The parser is written by hand.
The whole file is checked before the first insert. A bad INT (or one outside 4 bytes), a VARCHAR
longer than its column, a wrong field count, or a record too long for a page stops the load with
the line number (and the column where one is at fault), and leaves the table unchanged.

Table names follow the SQL identifier rule (a letter or underscore, then letters, digits and
underscores), because a name becomes a file name. `catalog` is refused in any case, since
`catalog.db` is the catalog's own file. Names are case-sensitive, as SQL identifiers are here, but
two names that differ only in case (`Dept`, `dept`) are refused: on a file system that ignores
case, such as Windows', they would share one file. A CSV line that is not UTF-8 text is a load
error naming the line.

## Command line

```
storage create-table --db DIR --name R --schema "a:INT,b:VARCHAR(20),c:INT"
storage load   --db DIR --table R --file r.csv [--frames 64]
storage scan   --db DIR --table R --frames 8 [--limit N] [--json]
storage layout --db DIR --table R --page 3
storage trace  --db DIR --file trace.txt --frames 16 --policy lru|clock [--json]
storage delete --db DIR --table R --rid PAGE:SLOT
```

Every command also accepts `--frames`. `trace` replays the page ids of `--file` against a scratch
data file, `DIR/trace.tmp`, extended to the largest page id and deleted afterwards, so a trace can
never touch a table's file. `scan --json` and `trace --json` print exactly
the handout's field names. The scan's `stats` are
the pool's counters for the scan alone: the catalog page read while opening the database is
counted before the counters are reset. `delete` is an addition: without it, the handout's page
example could not be reproduced from the command line. Running the same command twice gives
identical counters, because every command starts with a fresh pool.
