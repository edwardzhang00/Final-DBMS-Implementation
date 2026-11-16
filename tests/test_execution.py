"""Execution engine."""
import json
import os
import random
import shutil
import tempfile
import unittest

from test_helpers import _HERE, run_command  # noqa: F401

from dbms.execution.cli import main as exec_main
from dbms.execution.plan import PlanError, load_plan, prepare, execute, walk, totals
from dbms.execution.runtime import ExecError, format_value
from dbms.storage.database import Database
from dbms.storage.record import parse_schema
from reference import evaluate, bag


def scan(t):
    return {"op": "seqscan", "table": t}


class ExecTest(unittest.TestCase):
    """Builds a database once per class from TABLES."""

    TABLES = {}

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        db = Database(cls.dir, 64)
        cls.data = {}
        for name, (schema_text, rows) in cls.TABLES.items():
            schema = parse_schema(schema_text)
            db.create_table(name, schema)
            heap = db.heap_file(name)
            for row in rows:
                heap.insert(row)
            cls.data[name] = ([c.name for c in schema], [c.type for c in schema], rows)
        db.save()
        db.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def run_plan(self, plan, frames):
        db = Database(self.dir, frames)
        try:
            root = prepare(db, plan)
            rows = execute(db, root)
            self.assertEqual(db.pool.pinned, 0, "pinned frames left after close")
            self.assertEqual(db.pool.reserved, 0, "reserved frames left after close")
            self.assertLessEqual(db.pool.peak_held, frames)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "tmp")), "temp files left behind")
            return root, rows
        finally:
            db.close()

    def assert_matches_reference(self, plan, frames):
        root, rows = self.run_plan(plan, frames)
        self.assertEqual(bag(rows), bag(evaluate(plan, self.data).rows), json.dumps(plan))
        return root, rows


def random_rows(rng, n, keys):
    words = ["ann", "bob", "cy", "dee", "ed", "flo", "gus"]
    return [(i, rng.randrange(keys), rng.choice(words)) for i in range(n)]


class TestWorkedExamples(ExecTest):
    TABLES = {
        "N8": ("a:INT", [(i * 7919 % 4088,) for i in range(8 * 511)]),
        "RB": ("b:INT,x:INT", [(1, 0), (2, 1), (2, 2), (3, 3)]),
        "SB": ("b:INT,y:INT", [(2, 10), (2, 11), (4, 12)]),
        "E": ("a:INT,b:INT", []),
        "R": ("a:INT,b:INT,w:VARCHAR(8)", random_rows(random.Random(3), 3000, 40)),
        "S": ("b:INT,c:INT,w:VARCHAR(8)", [(b, i, w) for i, b, w in random_rows(random.Random(4), 2000, 40)]),
    }

    def test_sort_of_eight_pages_at_three_frames(self):
        root, rows = self.run_plan({"op": "sort", "keys": [["N8.a", "asc"]], "child": scan("N8")}, 3)
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in self.data["N8"][2]))
        c = root.counters()
        # Documented variant: one of the three frames holds the input page, so
        # pass 0 makes four runs of two pages; pass 1 merges them pairwise; pass 2 streams.
        self.assertEqual((c["passes"], c["page_reads"], c["page_writes"]), (3, 16, 16))
        self.assertEqual(root.children[0].counters()["page_reads"], 9)

    def test_sort_merge_join_duplicate_keys_give_four_rows(self):
        plan = {"op": "sort_merge_join", "condition": "RB.b = SB.b", "left": scan("RB"), "right": scan("SB")}
        root, rows = self.assert_matches_reference(plan, 4)
        self.assertEqual(sorted(rows), [(2, 1, 2, 10), (2, 1, 2, 11), (2, 2, 2, 10), (2, 2, 2, 11)])

    def test_all_three_joins_return_the_same_bag(self):
        results = []
        for algorithm in ("nested_loop_join", "sort_merge_join", "hash_join"):
            for frames in (4, 7, 32):
                plan = {"op": algorithm, "condition": "R.b = S.b", "left": scan("R"), "right": scan("S")}
                results.append(bag(self.assert_matches_reference(plan, frames)[1]))
        self.assertTrue(all(r == results[0] for r in results))

    def test_block_nested_loop_compares_every_pair_and_matches_its_formula(self):
        for frames in (4, 6, 16):
            plan = {"op": "nested_loop_join", "condition": "R.b = S.b and R.a < S.c",
                    "left": scan("R"), "right": scan("S")}
            root, _ = self.assert_matches_reference(plan, frames)
            self.assertEqual(root.counters()["comparisons"], 3000 * 2000)
            r_pages = root.children[0].heap.info.data_pages
            s_reads = root.children[1].counters()["page_reads"]
            blocks = -(-r_pages // (frames - 2))
            self.assertEqual(s_reads, blocks * (root.children[1].heap.info.data_pages + 1))

    def test_join_output_larger_than_both_inputs(self):
        plan = {"op": "hash_join", "condition": "R.b = S.b", "left": scan("R"), "right": scan("S")}
        _, rows = self.assert_matches_reference(plan, 4)
        self.assertGreater(len(rows), 3000 + 2000)

    def test_empty_input_through_every_operator(self):
        e = scan("E")
        plans = [
            {"op": "filter", "condition": "E.a > 1", "child": e},
            {"op": "project", "columns": ["E.b"], "child": e},
            {"op": "rename", "to": "X", "child": e},
            {"op": "distinct", "mode": "sort", "child": e},
            {"op": "distinct", "mode": "hash", "child": e},
            {"op": "sort", "keys": [["E.a", "desc"]], "child": e},
            {"op": "group_by", "mode": "sort", "keys": ["E.a"], "aggregates": [["count", "*"]], "child": e},
            {"op": "group_by", "mode": "hash", "keys": ["E.a"], "aggregates": [["sum", "E.b"]], "child": e},
            {"op": "limit", "n": 5, "child": e},
        ]
        for algorithm in ("nested_loop_join", "sort_merge_join", "hash_join"):
            plans.append({"op": algorithm, "condition": "E.a = R.a", "left": e, "right": scan("R")})
            plans.append({"op": algorithm, "condition": "R.a = E.a", "left": scan("R"), "right": e})
        for op in ("union_all", "union", "intersect", "except"):
            plans.append({"op": op, "left": e, "right": {"op": "rename", "to": "E", "child": e}})
        for plan in plans:
            self.assertEqual(self.run_plan(plan, 4)[1], [], plan["op"])

    def test_group_by_without_keys_on_empty_input(self):
        plan = {"op": "group_by", "mode": "hash", "keys": [], "aggregates": [["count", "*"]], "child": scan("E")}
        root, rows = self.run_plan(plan, 4)
        self.assertEqual(rows, [(0,)])
        self.assertEqual(root.schema.to_json(), [["count_star", "INT"]])
        plan["aggregates"].append(["sum", "E.a"])
        with self.assertRaisesRegex(ExecError, "SUM over an empty input"):
            self.run_plan(plan, 4)

    def test_aggregate_output_schema_names(self):
        plan = {"op": "group_by", "mode": "sort", "keys": ["R.b"],
                "aggregates": [["count", "*"], ["avg", "R.a"], ["max", "R.w"]], "child": scan("R")}
        root, _ = self.assert_matches_reference(plan, 4)
        self.assertEqual([c[0] for c in root.schema.to_json()], ["R.b", "count_star", "avg_R_a", "max_R_w"])

    def test_avg_keeps_full_precision_and_set_operations_make_its_column_real(self):
        def average(table):
            return {"op": "group_by", "mode": "sort", "keys": [], "aggregates": [["avg", f"{table}.b"]],
                    "child": scan(table)}
        self.assertEqual(self.run_plan(average("SB"), 4)[1], [(8 / 3,)])
        plain = {"op": "project", "columns": ["RB.b"], "child": scan("RB")}
        for op in ("union_all", "union", "intersect", "except"):
            values = [row[0] for left, right in ((average("RB"), plain), (plain, average("RB")))
                      for row in self.run_plan({"op": op, "left": left, "right": right}, 4)[1]]
            self.assertTrue(values, op)
            self.assertTrue(all(isinstance(v, float) for v in values), op)

    def test_hash_aggregation_counts_each_running_aggregate_at_its_size(self):
        plan = {"op": "group_by", "mode": "hash", "keys": ["R.b"],
                "aggregates": [["count", "*"], ["avg", "R.a"], ["max", "R.w"], ["min", "R.a"]], "child": scan("R")}
        root, _ = self.assert_matches_reference(plan, 4)
        self.assertEqual(root.state_width, 8 + 16 + (2 + 8) + 8)


class TestMemoryBudget(ExecTest):
    TABLES = {
        "Big": ("a:INT,b:INT,w:VARCHAR(20)",
                [(i, (i * 37) % 1000, "w" * (i % 20)) for i in range(20000)]),
        "Skew": ("k:INT,v:INT,pad:VARCHAR(40)", [(7 if i % 10 < 4 else i, i, "s" * 40) for i in range(2400)]),
        "Skew2": ("k:INT,u:INT,pad:VARCHAR(40)", [(7 if i % 10 < 4 else i + 3, i, "t" * 40) for i in range(1200)]),
    }

    def test_sort_of_twenty_times_b_at_four_frames(self):
        plan = {"op": "sort", "keys": [["Big.b", "asc"], ["Big.a", "desc"]], "child": scan("Big")}
        root, rows = self.run_plan(plan, 4)
        self.assertGreaterEqual(root.children[0].heap.info.data_pages, 20 * 4)
        expected = sorted(self.data["Big"][2], key=lambda r: (r[1], -r[0]))
        self.assertEqual(rows, expected)

    def test_hash_join_with_a_heavy_key_overflows_and_stays_in_budget(self):
        plan = {"op": "hash_join", "condition": "Skew.k = Skew2.k", "left": scan("Skew"), "right": scan("Skew2")}
        root, rows = self.assert_matches_reference(plan, 6)
        self.assertGreater(root.counters()["overflow_partitions"], 0)

    def test_hash_distinct_and_group_by_spill(self):
        for mode in ("hash", "sort"):
            self.assert_matches_reference({"op": "distinct", "mode": mode,
                                           "child": {"op": "project", "columns": ["Big.b", "Big.w"],
                                                     "child": scan("Big")}}, 4)
            self.assert_matches_reference({"op": "group_by", "mode": mode, "keys": ["Big.b"],
                                           "aggregates": [["count", "*"], ["min", "Big.w"], ["sum", "Big.a"]],
                                           "child": scan("Big")}, 4)

    def test_counters_are_identical_across_runs(self):
        plan = {"op": "hash_join", "condition": "Skew.k = Skew2.k",
                "left": {"op": "distinct", "mode": "hash", "child": scan("Skew")}, "right": scan("Skew2")}
        first = self.run_plan(plan, 5)
        second = self.run_plan(plan, 5)
        self.assertEqual([op.counters() for op, _ in walk(first[0])], [op.counters() for op, _ in walk(second[0])])
        self.assertEqual(first[1], second[1])


class TestUnionAllFrames(ExecTest):
    TABLES = {
        "E": ("k:INT,v:INT", [(i % 300, i) for i in range(1500)]),
        "D": ("k:INT,w:INT", [(i, i % 7) for i in range(300)]),
    }

    def distinct(self, mode, table, column="k"):
        return {"op": "distinct", "mode": mode, "child": {"op": "project", "columns": [f"{table}.{column}"],
                                                          "child": scan(table)}}

    def sorted_union_all(self, left, right):
        return {"op": "sort", "keys": [["E.k", "asc"]],
                "child": {"op": "union_all", "left": left, "right": right}}

    def test_right_input_opened_first_only_when_that_needs_fewer_frames(self):
        hash_group = {"op": "group_by", "mode": "hash", "keys": ["E.k"], "aggregates": [], "child": scan("E")}
        self.assert_matches_reference(self.sorted_union_all(hash_group, self.distinct("sort", "D")), 4)
        self.assert_matches_reference(self.sorted_union_all(self.distinct("sort", "E"), self.distinct("hash", "D")), 4)

    def test_a_group_by_without_keys_holds_no_frames_while_its_input_streams(self):
        counted = {"op": "group_by", "mode": "hash", "keys": [], "aggregates": [["count", "*"]],
                   "child": {"op": "union_all", "left": self.distinct("sort", "E"), "right": self.distinct("sort", "D")}}
        _, rows = self.assert_matches_reference(counted, 3)
        self.assertEqual(rows, [(600,)])
        hashed = dict(counted, child={"op": "union_all", "left": {"op": "project", "columns": ["D.k"], "child": scan("D")},
                                      "right": self.distinct("hash", "E", "v")})
        root, rows = self.assert_matches_reference(hashed, 32)
        self.assertEqual(totals(root, rows)["page_writes"], 0)

    def test_union_alls_that_need_five_frames_otherwise_spill_their_right_input_at_four(self):
        join = {"op": "project", "columns": ["E.k"], "child": {"op": "hash_join", "condition": "E.k = D.k",
                                                               "left": scan("E"), "right": scan("D")}}
        join_of_join = {"op": "project", "columns": ["E.k"], "child": {
            "op": "nested_loop_join", "condition": "E.v = Z.k", "left": join["child"],
            "right": {"op": "rename", "to": "Z", "child": scan("D")}}}
        shapes = [(self.distinct("hash", "E"), self.distinct("hash", "D")),
                  (join, self.distinct("hash", "D")),
                  ({"op": "project", "columns": ["E.k"], "child": scan("E")}, join_of_join)]
        for left, right in shapes:
            plan = self.sorted_union_all(left, right)
            root, _ = self.assert_matches_reference(plan, 4)
            self.assertGreater(root.children[0].page_writes, 0)
            root, _ = self.assert_matches_reference(plan, 5)
            self.assertEqual(root.children[0].page_writes, 0)

    def test_a_union_all_nested_on_the_right_opens_its_hash_input_first(self):
        nested = {"op": "union_all", "left": {"op": "project", "columns": ["D.k"], "child": scan("D")},
                  "right": self.distinct("hash", "E")}
        plan = self.sorted_union_all({"op": "project", "columns": ["E.k"], "child": scan("E")}, nested)
        self.assert_matches_reference(plan, 4)


class TestRandomPlans(ExecTest):
    rng = random.Random(11)
    TABLES = {
        "A": ("a:INT,b:INT,s:VARCHAR(6)", random_rows(random.Random(21), 900, 25)),
        "B": ("b:INT,c:INT,s:VARCHAR(6)", random_rows(random.Random(22), 700, 25)),
        "C": ("c:INT,d:INT,s:VARCHAR(6)", random_rows(random.Random(23), 500, 60)),
    }

    def random_input(self, depth):
        rng = self.rng
        table = rng.choice(["A", "B", "C"])
        node = scan(table)
        if rng.random() < 0.5:
            col = {"A": "A.b", "B": "B.c", "C": "C.d"}[table]
            node = {"op": "filter", "condition": f"{col} {rng.choice(['<', '>=', '!='])} {rng.randrange(30)}",
                    "child": node}
        if depth > 0 and rng.random() < 0.4:
            node = {"op": "distinct", "mode": rng.choice(["sort", "hash"]), "child": node}
        return table, node

    def test_random_join_trees_match_reference_at_small_budgets(self):
        joins = ("nested_loop_join", "sort_merge_join", "hash_join")
        for trial in range(36):
            ta, left = self.random_input(1)
            tb, right = self.random_input(1)
            if ta == tb:
                right = {"op": "rename", "to": "Z", "child": right}
                tb = "Z"
            key_a = {"A": "b", "B": "b", "C": "c"}[ta]
            key_b = {"A": "b", "B": "b", "C": "c", "Z": "s"}[tb]
            cond = f"{ta}.s = {tb}.s" if tb == "Z" else f"{ta}.{key_a} = {tb}.{key_b}"
            if ta == "C" and tb != "Z" and key_b != "c":
                cond = f"{ta}.s = {tb}.s"
            plan = {"op": joins[trial % 3], "condition": cond, "left": left, "right": right}
            if trial % 4 == 0:
                plan = {"op": "group_by", "mode": self.rng.choice(["sort", "hash"]), "keys": [f"{ta}.s"],
                        "aggregates": [["count", "*"], ["max", f"{ta}.s"]], "child": plan}
            elif trial % 4 == 1:
                plan = {"op": joins[(trial + 1) % 3], "condition": f"{ta}.s = A3.s", "left": plan,
                        "right": {"op": "rename", "to": "A3", "child": scan("A")}}
            for frames in (4, 5, 9):
                self.assert_matches_reference(plan, frames)

    def test_set_operators_match_reference(self):
        a = {"op": "project", "columns": ["A.b", "A.s"], "child": scan("A")}
        b = {"op": "project", "columns": ["B.c", "B.s"], "child": scan("B")}
        for op in ("union_all", "union", "intersect", "except"):
            for frames in (4, 6):
                self.assert_matches_reference({"op": op, "left": a, "right": b}, frames)
                self.assert_matches_reference({"op": op, "left": b, "right": a}, frames)


class TestPlanValidation(ExecTest):
    TABLES = {"R": ("a:INT,b:VARCHAR(5)", [(1, "x")])}

    def error_for(self, plan, frames=8):
        db = Database(self.dir, frames)
        try:
            with self.assertRaises((PlanError, ExecError)) as cm:
                prepare(db, plan)
            return str(cm.exception)
        finally:
            db.close()

    def test_messages_name_the_node_and_the_problem(self):
        self.assertEqual(self.error_for({"op": "sort", "child": scan("R")}),
                         "Node 'sort': missing required field 'keys'. [at root]")
        self.assertEqual(self.error_for({"op": "filter", "condition": 5, "child": scan("R")}),
                         "Node 'filter': field 'condition' must be a string. [at root]")
        self.assertEqual(self.error_for({"op": "hash_join", "condition": "R.a = R.a", "left": scan("R"),
                                         "right": {"op": "merge_join", "left": scan("R"), "right": scan("R")}}),
                         "Node 'merge_join': unknown operator 'merge_join'. [at root.right]")
        self.assertIn("field 'condition' is not a valid condition",
                      self.error_for({"op": "filter", "condition": "R.a >", "child": scan("R")}))
        self.assertIn("column = column", self.error_for(
            {"op": "hash_join", "condition": "R.a < Q.a", "left": scan("R"),
             "right": {"op": "rename", "to": "Q", "child": scan("R")}}))
        self.assertIn("field 'mode'", self.error_for({"op": "distinct", "mode": "fast", "child": scan("R")}))

    def test_real_values_print_rounded_half_away_from_zero(self):
        self.assertEqual([format_value(v) for v in (20.53125, 1045.28125, -0.00005, 41.0, 7)],
                         ["20.5313", "1045.2813", "-0.0001", "41.0000", "7"])

    def test_unknown_table_and_column_rejected_at_open(self):
        self.assertIn("Unknown table Nope", self.error_for(scan("Nope")))
        self.assertEqual(self.error_for({"op": "project", "columns": ["R.zz"], "child": scan("R")}),
                         "Node 'project': Unknown attribute R.zz [at root]")

    def test_type_errors_and_duplicate_columns(self):
        self.assertIn("Cannot compare", self.error_for({"op": "filter", "condition": "R.a = 'x'",
                                                        "child": scan("R")}))
        self.assertIn("Two columns named R.a", self.error_for(
            {"op": "nested_loop_join", "left": scan("R"), "right": scan("R")}))

    def test_malformed_json_reports_location(self):
        with self.assertRaisesRegex(PlanError, "line 2, column"):
            load_plan('{"op": "seqscan",\n "table": }')

    def test_totals_shape(self):
        db = Database(self.dir, 4)
        root = prepare(db, scan("R"))
        rows = execute(db, root)
        db.close()
        self.assertEqual(set(totals(root, rows)), {"tuples_out", "comparisons", "page_reads", "page_writes"})


class TestCommandLine(ExecTest):
    TABLES = {"R": ("a:INT,b:INT", [(i, i % 5) for i in range(300)]),
              "S": ("b:INT,c:VARCHAR(4)", [(i, "s") for i in range(5)])}

    def write_plan(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        self.addCleanup(os.remove, path)
        return path

    def test_trace_prints_one_line_per_node_and_nothing_else(self):
        plan = {"op": "hash_join", "condition": "R.b = S.b",
                "left": {"op": "filter", "condition": "R.a < 100", "child": scan("R")}, "right": scan("S")}
        path = self.write_plan("join.json", json.dumps(plan).encode())
        code, out, _ = run_command(exec_main, ["trace", "--db", self.dir, "--plan", path, "--frames", "4"])
        self.assertEqual(code, 0)
        self.assertEqual([line.split(" tuples_out=")[0] for line in out.splitlines()],
                         ["hash_join", "  filter", "    seqscan R", "  seqscan S"])

    def test_a_plan_file_that_is_not_utf8_is_a_one_line_error(self):
        path = self.write_plan("latin1.json", b'{"op": "seqscan", "table": "\xe9"}')
        code, _, err = run_command(exec_main, ["run", "--db", self.dir, "--plan", path, "--frames", "4"])
        self.assertEqual(code, 1)
        self.assertRegex(err, "^error: .*can't decode byte 0xe9.*\n$")


if __name__ == "__main__":
    unittest.main()
