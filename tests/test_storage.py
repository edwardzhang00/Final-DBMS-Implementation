"""Storage engine and buffer pool."""
import gc
import os
import shutil
import tempfile
import unittest

from test_helpers import _HERE, run_command  # noqa: F401

from dbms.storage.bufferpool import BufferPool, BufferPoolError, ClockPolicy, replay_trace
from dbms.storage.cli import main as storage_main
from dbms.storage.database import Database
from dbms.storage.disk import DiskManager, StorageError
from dbms.storage.loader import load_csv
from dbms.storage.page import (
    init_data_page, insert_record, delete_record, get_record, page_header, free_space,
    slot_entries, records_per_page, DIR_CAPACITY,
)
from dbms.storage.record import encode, decoder, parse_schema


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Registered first, so it runs last: after every db.close() cleanup.
        # Windows cannot delete a file that is still open, so the directory
        # must be removed only once the databases using it are closed.
        self.addCleanup(self.remove_dir)

    def remove_dir(self):
        gc.collect()  # close files of objects a failing command left open
        shutil.rmtree(self.dir)

    def write_csv(self, name, lines):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path


class TestRecordFormat(unittest.TestCase):
    def test_handout_record_example(self):
        schema = parse_schema("a:INT,b:VARCHAR(5),c:INT")
        record = encode(schema, (42, "bob", 7))
        self.assertEqual(record.hex(" "), "2a 00 00 00 03 00 62 6f 62 07 00 00 00")
        self.assertEqual(decoder(schema)(record), (42, "bob", 7))

    def test_varchar_longer_than_declared_is_rejected(self):
        schema = parse_schema("b:VARCHAR(2)")
        with self.assertRaises(StorageError):
            encode(schema, ("abc",))


class TestSlottedPage(unittest.TestCase):
    def test_handout_page_example(self):
        buf = bytearray(4096)
        init_data_page(buf, 3)
        trace = [(page_header(buf)[1:], free_space(buf))]
        for length in (13, 20, 13):
            insert_record(buf, bytes(length))
            trace.append((page_header(buf)[1:], free_space(buf)))
        delete_record(buf, 1)
        trace.append((page_header(buf)[1:], free_space(buf)))
        self.assertEqual(trace, [((0, 4096), 4088), ((1, 4083), 4071), ((2, 4063), 4047),
                                 ((3, 4050), 4030), ((3, 4050), 4030)])
        self.assertEqual(slot_entries(buf), [(0, 4083, 13), (1, 0, 0), (2, 4050, 13)])

    def test_reading_a_dead_slot_names_the_rid(self):
        buf = bytearray(4096)
        init_data_page(buf, 5)
        insert_record(buf, b"x" * 13)
        delete_record(buf, 0)
        with self.assertRaisesRegex(StorageError, r"\(5, 0\)"):
            get_record(buf, 0)

    def test_capacity_formula(self):
        self.assertEqual(records_per_page(13), 240)
        buf = bytearray(4096)
        init_data_page(buf, 1)
        count = 0
        while insert_record(buf, bytes(13)) is not None:
            count += 1
        self.assertEqual(count, 240)


class TestBufferPool(TempDirTest):
    def disk(self, name, pages):
        disk = DiskManager(os.path.join(self.dir, name))
        self.addCleanup(disk.close)
        for _ in range(pages):
            disk.allocate_page()
        return disk

    def test_handout_trace_lru(self):
        pool = replay_trace(os.path.join(self.dir, "t.db"), [1, 2, 3, 1, 4, 2], 3, "lru")
        self.assertEqual(pool.stats(), {"hits": 1, "misses": 5, "evictions": 2, "reads": 5, "writes": 0})
        self.assertEqual(sorted(f.page_id for f in pool.frames), [1, 2, 4])

    def test_handout_trace_clock(self):
        pool = replay_trace(os.path.join(self.dir, "t.db"), [1, 2, 3, 1, 4, 2], 3, "clock")
        self.assertEqual(pool.stats(), {"hits": 2, "misses": 4, "evictions": 1, "reads": 4, "writes": 0})
        self.assertEqual([f.page_id for f in pool.frames], [4, 2, 3])

    def test_evicting_a_pinned_page_fails(self):
        disk = self.disk("p.db", 3)
        for policy in ("lru", "clock"):
            pool = BufferPool(2, policy)
            pool.fetch_page(disk, 0)
            pool.fetch_page(disk, 1)
            with self.assertRaisesRegex(BufferPoolError, "no unpinned frame"):
                pool.fetch_page(disk, 2)
            self.assertEqual(sorted(f.page_id for f in pool.frames), [0, 1])

    def test_unpin_at_pin_count_zero_fails(self):
        disk = self.disk("u.db", 1)
        pool = BufferPool(2)
        pool.fetch_page(disk, 0)
        pool.unpin_page(disk, 0, False)
        with self.assertRaises(BufferPoolError):
            pool.unpin_page(disk, 0, False)

    def test_dirty_pages_are_written_on_eviction_and_flush(self):
        disk = self.disk("d.db", 3)
        pool = BufferPool(1)
        buf = pool.fetch_page(disk, 0)
        buf[0] = 7
        pool.unpin_page(disk, 0, True)
        pool.fetch_page(disk, 1)
        pool.unpin_page(disk, 1, True)
        pool.flush_all()
        self.assertEqual(pool.stats()["writes"], 2)
        self.assertEqual(disk.read_page(0)[0], 7)

    def test_reserved_frames_shrink_the_pool(self):
        disk = self.disk("r.db", 4)
        pool = BufferPool(3)
        pool.reserve(2)
        pool.fetch_page(disk, 0)
        with self.assertRaises(BufferPoolError):
            pool.fetch_page(disk, 1)
        self.assertEqual(pool.peak_held, 3)
        pool.release(2)
        pool.fetch_page(disk, 1)

    def test_a_failed_reserve_takes_no_frames(self):
        pool = BufferPool(4)
        with self.assertRaisesRegex(BufferPoolError, "0 pinned and 4 reserved out of 4"):
            pool.reserve(5)
        self.assertEqual(pool.reserved, 0)
        pool.reserve(4)


class TestHeapFileAndCatalog(TempDirTest):
    def open(self, frames):
        db = Database(self.dir, frames)
        self.addCleanup(db.close)
        return db

    def make_table(self, frames=8, rows=0, schema="a:INT,b:VARCHAR(5),c:INT"):
        db = self.open(frames)
        db.create_table("R", parse_schema(schema))
        heap = db.heap_file("R")
        for i in range(rows):
            heap.insert((i, "bob", i * 2))
        db.save()
        return db, heap

    def test_insert_get_and_rids(self):
        db, heap = self.make_table()
        rid = heap.insert((42, "bob", 7))
        self.assertEqual(rid, (1, 0))
        self.assertEqual(heap.get(rid), (42, "bob", 7))

    def test_scan_of_table_twenty_times_b_pins_one_page(self):
        self.make_table(frames=4, rows=240 * 80)
        db = self.open(4)
        heap = db.heap_file("R")
        self.assertEqual(heap.info.data_pages, 80)
        db.pool.reset_stats()
        max_pinned = 0
        count = 0
        for _ in heap.scan():
            count += 1
            max_pinned = max(max_pinned, db.pool.pinned)
        self.assertEqual(count, 240 * 80)
        self.assertEqual(max_pinned, 1)
        self.assertEqual(db.pool.stats()["reads"], 81)

    def test_same_command_twice_gives_identical_counters(self):
        self.make_table(frames=3, rows=2000)
        results = []
        for _ in range(2):
            db = self.open(3)
            heap = db.heap_file("R")
            db.pool.reset_stats()
            rows = [row for _, row in heap.scan()]
            results.append((db.pool.stats(), rows))
        self.assertEqual(results[0], results[1])

    def test_delete_everything_then_scan_returns_nothing(self):
        db, heap = self.make_table(rows=500)
        for rid, _ in list(heap.scan()):
            heap.delete(rid)
        self.assertEqual(list(heap.scan()), [])
        self.assertEqual(heap.info.num_tuples, 0)
        with self.assertRaises(StorageError):
            heap.delete((1, 0))

    def test_record_too_long_names_table_and_row(self):
        db, heap = self.make_table(schema=",".join(f"c{i}:VARCHAR(255)" for i in range(17)))
        with self.assertRaisesRegex(StorageError, r"table R.*row"):
            heap.insert(tuple("x" * 255 for _ in range(17)))

    def test_directory_chains_past_681_entries(self):
        db, heap = self.make_table(frames=8, rows=0, schema="a:INT,b:VARCHAR(200)")
        per_page = records_per_page(4 + 2 + 200)
        for i in range(per_page * (DIR_CAPACITY + 5)):
            heap.insert((i, "y" * 200))
        self.assertEqual(heap.info.data_pages, DIR_CAPACITY + 5)
        self.assertEqual(sum(1 for _ in heap.scan()), per_page * (DIR_CAPACITY + 5))

    def test_delete_refuses_a_directory_page(self):
        db, heap = self.make_table(rows=3)
        with self.assertRaisesRegex(StorageError, "not one of its data pages"):
            heap.delete((0, 0))
        self.assertEqual(sum(1 for _ in heap.scan()), 3)
        self.assertEqual(heap.info.num_tuples, 3)

    def test_table_names_cannot_reach_other_files(self):
        db = self.open(4)
        for name in ("catalog", "Catalog", "../x", "a b", ""):
            with self.assertRaises(StorageError):
                db.create_table(name, parse_schema("a:INT"))

    def test_names_differing_only_in_case_would_share_a_file(self):
        db = self.open(4)
        db.create_table("dept", parse_schema("a:INT"))
        with self.assertRaisesRegex(StorageError, "would share a file with table dept"):
            db.create_table("Dept", parse_schema("a:INT"))

    def test_catalog_survives_reopening(self):
        self.make_table(rows=10)
        again = self.open(8)
        info = again.catalog.get_table("R")
        self.assertEqual((info.num_tuples, info.data_pages, info.num_pages), (10, 1, 2))
        self.assertEqual([c.type_text() for c in info.schema], ["INT", "VARCHAR(5)", "INT"])
        self.assertEqual(sum(1 for _ in again.heap_file("R").scan()), 10)


class TestLoader(TempDirTest):
    def setUp(self):
        super().setUp()
        self.db = Database(self.dir, 8)
        self.addCleanup(self.db.close)
        self.db.create_table("R", parse_schema("a:INT,b:VARCHAR(5)"))

    def test_quoted_field_with_comma(self):
        path = self.write_csv("ok.csv", ['1,"a,b"', '2,"x""y"', "3,"])
        self.assertEqual(load_csv(self.db, "R", path), 3)
        rows = [row for _, row in self.db.heap_file("R").scan()]
        self.assertEqual(rows, [(1, "a,b"), (2, 'x"y'), (3, "")])

    def test_bad_int_names_line_and_column_and_loads_nothing(self):
        path = self.write_csv("bad.csv", ["1,a", "x2,b"])
        with self.assertRaisesRegex(StorageError, "Line 2, column a"):
            load_csv(self.db, "R", path)
        self.assertEqual(list(self.db.heap_file("R").scan()), [])

    def test_text_that_is_not_utf8_names_its_line_and_loads_nothing(self):
        path = os.path.join(self.dir, "latin1.csv")
        with open(path, "wb") as f:
            f.write(b"1,a\n2,caf\xe9\n")
        with self.assertRaisesRegex(StorageError, "Line 2: not valid UTF-8"):
            load_csv(self.db, "R", path)
        self.assertEqual(list(self.db.heap_file("R").scan()), [])

    def test_record_too_long_for_a_page_loads_nothing(self):
        db = Database(os.path.join(self.dir, "wide"), 8)
        self.addCleanup(db.close)
        db.create_table("W", parse_schema(",".join(f"c{i}:VARCHAR(255)" for i in range(17))))
        path = self.write_csv("wide.csv", [",".join(["x"] * 17), ",".join(["y" * 255] * 17)])
        with self.assertRaisesRegex(StorageError, "Line 2: the record is"):
            load_csv(db, "W", path)
        self.assertEqual(list(db.heap_file("W").scan()), [])

    def test_varchar_too_long_names_line(self):
        path = self.write_csv("long.csv", ["1,abcdef"])
        with self.assertRaisesRegex(StorageError, "Line 1, column b"):
            load_csv(self.db, "R", path)


class TestCommandLine(TempDirTest):
    def storage(self, *argv):
        return run_command(storage_main, list(argv))

    def write_text(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_trace_defaults_to_clock_and_leaves_a_table_named_trace_alone(self):
        db = os.path.join(self.dir, "db")
        self.storage("create-table", "--db", db, "--name", "trace", "--schema", "a:INT")
        trace = self.write_text("trace.txt", b"1\n2\n3\n1\n4\n2\n")
        code, out, _ = self.storage("trace", "--db", db, "--file", trace, "--frames", "3")
        self.assertEqual((code, out), (0, "policy=clock frames=3 hits=2 misses=4 evictions=1 reads=4 writes=0 "
                                          "hit_rate=0.3333\n"))
        self.assertEqual(sorted(os.listdir(db)), ["catalog.db", "trace.db"])
        self.assertEqual(self.storage("scan", "--db", db, "--table", "trace")[0], 0)
        database = Database(db, 4)
        self.addCleanup(database.close)
        self.assertIsInstance(database.pool.policy, ClockPolicy)

    def test_an_empty_trace_prints_its_counters(self):
        trace = self.write_text("empty.txt", b"")
        code, out, _ = self.storage("trace", "--db", self.dir, "--file", trace, "--frames", "4")
        self.assertEqual((code, out), (0, "policy=clock frames=4 hits=0 misses=0 evictions=0 reads=0 writes=0\n"))

    def test_digits_other_than_ascii_are_refused_with_an_error(self):
        db = os.path.join(self.dir, "db")
        self.storage("create-table", "--db", db, "--name", "R", "--schema", "a:INT")
        self.assertEqual(self.storage("delete", "--db", db, "--table", "R", "--rid", "\u00b2:0"),
                         (1, "", "error: --rid must be page:slot, got '\u00b2:0'\n"))
        code, _, err = self.storage("create-table", "--db", db, "--name", "S", "--schema", "a:VARCHAR(\u00b2)")
        self.assertEqual((code, err), (1, "error: Unknown type 'VARCHAR(\u00b2)' for column a\n"))
        trace = self.write_text("digits.txt", "1\n\u00b2\n".encode())
        code, _, err = self.storage("trace", "--db", db, "--file", trace, "--frames", "4")
        self.assertEqual((code, err), (1, f"error: {trace} line 2: '\u00b2' is not a page id\n"))

    def test_a_trace_that_is_not_utf8_is_a_one_line_error(self):
        trace = self.write_text("latin1.txt", b"1\n\xe9\n")
        code, _, err = self.storage("trace", "--db", self.dir, "--file", trace, "--frames", "4")
        self.assertEqual(code, 1)
        self.assertRegex(err, "^error: .*can't decode byte 0xe9.*\n$")


if __name__ == "__main__":
    unittest.main()
