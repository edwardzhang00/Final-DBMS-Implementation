"""Query optimizer, and the integrated shell."""
import contextlib
import gc
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from test_helpers import _HERE, run_command

from dbms.execution.cli import main as exec_main
from dbms.execution.plan import prepare, execute
from dbms.optimizer.cli import main as opt_main
from dbms.optimizer.estimate import ColumnEstimate, selectivity
from dbms.optimizer.planner import Planner, unoptimized_planner, sort_passes
from dbms.optimizer.rewrite import Rewriter
from dbms.optimizer.stats import analyze_table
from dbms.ra.parser import parse_condition
from dbms.ra.tree import explain_tree
from dbms.shell import Shell, main as shell_main
from dbms.sql.analyzer import Analyzer
from dbms.sql.cli import main as sql_main
from dbms.sql.parser import parse_script
from dbms.storage.cli import main as storage_main
from dbms.storage.database import Database
from dbms.storage.record import ColumnDef, parse_schema

sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))
import dp_example  # noqa: E402
import fuzz_sql  # noqa: E402
import run_workload  # noqa: E402

EMP = "eid:INT,name:VARCHAR(20),age:INT,did:INT,salary:INT,mgr:INT,city:VARCHAR(20)"
DEPT = "did:INT,title:VARCHAR(20),budget:INT,city:VARCHAR(20)"


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Registered first, so it runs last: after every db.close() cleanup.
        # Windows cannot delete a file that is still open, so the directory
        # must be removed only once the databases using it are closed.
        self.addCleanup(self.remove_dir)

    def remove_dir(self):
        gc.collect()  # close files of objects a failing command left open
        shutil.rmtree(self.dir)

    def open(self, frames=16):
        db = Database(self.dir, frames)
        self.addCleanup(db.close)
        return db

    def compile(self, db, sql):
        return Analyzer(db.catalog.schemas()).statement(parse_script(sql)[0]).plan


class TestRewrites(DatabaseTest):
    def test_handout_rewrite_example(self):
        db = self.open()
        db.create_table("Emp", parse_schema(EMP))
        db.create_table("Dept", parse_schema(DEPT))
        plan = self.compile(db, "SELECT e.name FROM Emp e, Dept d "
                                "WHERE e.did = d.did AND e.age > 60 AND d.budget > 1000;")
        after = Rewriter(lambda n: db.catalog.get_table(n).schema).rewrite(plan)
        self.assertEqual(explain_tree(after), "\n".join([
            "project [e.name]",
            "  join (e.did = d.did)",
            "    project [e.name, e.did]",
            "      select (e.age > 60)",
            "        rename e",
            "          scan Emp",
            "    project [d.did]",
            "      select (d.budget > 1000)",
            "        rename d",
            "          scan Dept"]))

    def test_having_predicate_never_moves_but_grouping_predicate_does(self):
        db = self.open()
        db.create_table("Emp", parse_schema(EMP))
        plan = self.compile(db, "SELECT t.did FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) t "
                                "WHERE t.n > 3 AND t.did = 4")
        text = explain_tree(Rewriter(lambda n: db.catalog.get_table(n).schema).rewrite(plan))
        self.assertLess(text.index("select (t.n > 3)"), text.index("group_by"))
        self.assertGreater(text.index("select (Emp.did = 4)"), text.index("group_by"))


class TestDynamicProgramming(DatabaseTest):
    def test_handout_dp_table_given_its_statistics(self):
        dp_example.main(self.dir)
        db = self.open(32)
        with open(os.path.join(self.dir, "q.sql")) as f:
            plan = self.compile(db, f.read())
        planner = Planner(db.catalog, 32)
        _, physical = planner.optimize(plan)
        rows = {line.split()[0]: line.split()[1:4] for line in planner.dp_table_text().splitlines()
                if line.startswith("{")}
        self.assertEqual(rows["{A}"], ["5000", "100", "100"])
        self.assertEqual(rows["{Bp}"], ["500", "10", "10"])
        self.assertEqual(rows["{C}"], ["20000", "400", "400"])
        self.assertEqual(rows["{A,Bp}"], ["1000", "20", "110"])
        self.assertEqual(rows["{A,C}"], ["-", "-", "-"])
        self.assertEqual(rows["{Bp,C}"], ["2000", "40", "410"])
        self.assertEqual(rows["{A,Bp,C}"][2], "530")
        self.assertIn("{A,Bp} outer (20 pages), nested_loop_join, C inner: 110 + 20 + 1x400 = 530",
                      planner.dp_table_text())
        self.assertEqual(physical.op, "project")
        join = physical.children[0]
        self.assertEqual((join.op, join.children[1].op), ("nested_loop_join", "seqscan"))
        self.assertEqual(join.children[0].children[0].fields, {"table": "Bp"})


class TestSelectivity(unittest.TestCase):
    def test_handout_worked_example(self):
        emp = {"Emp.did": ColumnEstimate("INT", 4, 50), "Emp.age": ColumnEstimate("INT", 4, 51, 20, 70)}
        lookup = lambda ref: emp[ref.text]  # noqa: E731
        rows = lambda cond: max(1, -(-round(10000 * selectivity(parse_condition(cond), lookup) * 1e9) // 10 ** 9))  # noqa: E731
        self.assertEqual(rows("Emp.did = 7"), 200)
        self.assertEqual(rows("Emp.age > 60"), 2000)
        self.assertEqual(rows("Emp.did = 7 and Emp.age > 60"), 40)

    def test_join_estimate_uses_max_ndv(self):
        from dbms.optimizer.estimate import join_selectivity
        stats = {"Emp.did": ColumnEstimate("INT", 4, 50), "Dept.did": ColumnEstimate("INT", 4, 50)}
        sel = join_selectivity(parse_condition("Emp.did = Dept.did"), lambda r: stats[r.text], lambda r: None)
        self.assertEqual(round(10000 * 50 * sel), 10000)

    def test_histogram_equality_and_range(self):
        from dbms.optimizer.stats import column_statistics
        values = sorted([1] * 50 + list(range(2, 52)))
        stat = column_statistics(iter(values), len(values), 10, ColumnDef("x", "INT"))
        col = ColumnEstimate("INT", 4, stat.ndv, stat.min, stat.max, stat.buckets, len(values))
        lookup = lambda ref: col  # noqa: E731
        self.assertEqual(stat.ndv, 51)
        self.assertEqual(sum(b.count for b in stat.buckets), 100)
        self.assertAlmostEqual(selectivity(parse_condition("x = 1"), lookup), 0.5, places=6)
        self.assertAlmostEqual(selectivity(parse_condition("x < 1"), lookup), 0.0, places=6)
        self.assertEqual(selectivity(parse_condition("x = 99"), lookup), 0.0)


class TestCostModel(DatabaseTest):
    def test_sort_passes_count_the_executors_runs_of_b_minus_1_pages(self):
        db = self.open(3)
        db.create_table("W", parse_schema("a:INT,b:VARCHAR(100)"))
        heap = db.heap_file("W")
        for i in range(37 * 9):
            heap.insert((i, "x" * 100))
        db.save()
        root = prepare(db, {"op": "sort", "keys": [["W.a", "desc"]], "child": {"op": "seqscan", "table": "W"}})
        execute(db, root)
        self.assertEqual(heap.info.data_pages, 9)
        self.assertEqual(root.counters()["passes"], 4)
        self.assertEqual(sort_passes(9, 3), 4)


class TestWorkloadProperties(DatabaseTest):
    def setUp(self):
        super().setUp()
        rng = random.Random(9)
        shell = Shell(self.dir, 32, out=io.StringIO())
        cities = ["Ottawa", "Toronto", "Montreal"]
        script = ["CREATE TABLE Emp (eid INT, name VARCHAR(20), age INT, did INT, salary INT, mgr INT, "
                  "city VARCHAR(20));",
                  "CREATE TABLE Dept (did INT, title VARCHAR(20), budget INT, city VARCHAR(20));"]
        for d in range(40):
            script.append(f"INSERT INTO Dept VALUES ({d}, 'D{d}', {rng.randrange(100, 5000)}, '{cities[d % 3]}');")
        emp_csv = os.path.join(self.dir, "emp.csv")
        with open(emp_csv, "w") as f:
            for i in range(3000):
                f.write(f"{i},n{i},{rng.randrange(20, 70)},{rng.randrange(40)},{rng.randrange(1000)},"
                        f"{rng.randrange(3000)},{cities[i % 3]}\n")
        script.append(f"LOAD Emp FROM '{emp_csv}';")
        self.assertTrue(shell.run_script("\n".join(script)))
        shell.close()

    def physical(self, db, sql, frames, **options):
        return Planner(db.catalog, frames, **options).optimize(self.compile(db, sql))[1]

    def test_commas_and_join_on_give_the_same_physical_plan(self):
        db = self.open()
        commas = self.physical(db, "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND d.budget > 900", 16)
        joins = self.physical(db, "SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 900", 16)
        self.assertEqual(commas.to_json(), joins.to_json())

    def test_changing_b_changes_the_join_algorithm(self):
        db = self.open()
        sql = "SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid"
        chosen = {self.physical(db, sql, frames).children[0].op for frames in (4, 64)}
        self.assertEqual(len(chosen), 2, chosen)

    def test_no_statistics_still_gives_a_valid_plan(self):
        db = self.open()
        self.assertIsNone(db.catalog.get_table("Emp").stats)
        plan = self.physical(db, "SELECT d.title, COUNT(*) FROM Emp e JOIN Dept d ON e.did = d.did "
                                 "WHERE e.age > 30 GROUP BY d.title", db.pool.size)
        rows = execute(db, prepare(db, plan.to_json()))
        self.assertGreater(len(rows), 0)

    def test_analyze_twice_changes_nothing(self):
        db = self.open()
        analyze_table(db, "Emp", 20, 8)
        with open(os.path.join(self.dir, "catalog.db"), "rb") as f:
            first = f.read()
        analyze_table(db, "Emp", 20, 8)
        with open(os.path.join(self.dir, "catalog.db"), "rb") as f:
            self.assertEqual(first, f.read())
        stats = db.catalog.get_table("Emp").stats
        self.assertEqual((stats.n_tuples, len(stats.columns[2].buckets)), (3000, 20))
        self.assertEqual(stats.columns[3].ndv, 40)

    def test_every_physical_plan_runs_unedited_and_matches_the_baseline(self):
        db = self.open()
        analyze_table(db, "Emp", 20, 8)
        analyze_table(db, "Dept", 20, 8)
        queries = [
            "SELECT e.name, d.title FROM Emp e, Dept d WHERE e.did = d.did AND e.age > 60",
            "SELECT city, COUNT(*) AS n, MAX(salary) FROM Emp GROUP BY city HAVING COUNT(*) > 5 ORDER BY n DESC",
            "SELECT e.name FROM Emp e JOIN Emp m ON e.mgr = m.eid JOIN Dept d ON d.did = m.did WHERE d.budget < 900",
            "SELECT DISTINCT d.city FROM Dept d, Emp e WHERE d.did = e.did AND e.salary < 10 ORDER BY d.city",
            "SELECT city FROM Emp WHERE age < 25 UNION SELECT city FROM Dept INTERSECT SELECT city FROM Emp",
            "SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) t, Dept d "
            "WHERE t.did = d.did AND t.n > 70",
        ]
        databases = {frames: self.open(frames) for frames in (4, 9, 64)}
        for sql in queries:
            logical = self.compile(db, sql)
            expected = None
            for frames, fdb in databases.items():
                for planner in (Planner(fdb.catalog, frames), unoptimized_planner(fdb.catalog, frames)):
                    rows = execute(fdb, prepare(fdb, planner.optimize(logical)[1].to_json()))
                    self.assertLessEqual(fdb.pool.peak_held, frames)
                    if expected is None:
                        expected = sorted(rows)
                    self.assertEqual(sorted(rows), expected, (sql, frames))

    def test_an_alias_named_like_another_column_keeps_its_own_values(self):
        db = self.open()
        for frames in (4, 32):
            logical = self.compile(db, "SELECT d.did AS budget FROM Dept d WHERE d.did < 4 ORDER BY d.budget DESC")
            fdb = self.open(frames)
            rows = execute(fdb, prepare(fdb, Planner(fdb.catalog, frames).optimize(logical)[1].to_json()))
            budgets = {did: budget for did, _, budget, _ in
                       (row for _, row in fdb.heap_file("Dept").scan())}
            self.assertEqual(sorted(r[0] for r in rows), [0, 1, 2, 3])
            self.assertEqual([r[0] for r in rows], sorted(range(4), key=lambda d: -budgets[d]))

    def rows(self, sql, frames, **options):
        fdb = self.open(frames)
        physical = Planner(fdb.catalog, frames, **options).optimize(self.compile(fdb, sql))[1]
        rows = execute(fdb, prepare(fdb, physical.to_json()))
        self.assertLessEqual(fdb.pool.peak_held, frames)
        return rows

    def test_equal_int_and_real_keys_meet_in_hashing(self):
        ages = [row[2] for _, row in self.open().heap_file("Emp").scan()]
        sql = ("SELECT COUNT(*) FROM (SELECT DISTINCT t.a FROM (SELECT AVG(age) AS a FROM Emp GROUP BY eid "
               "UNION ALL SELECT age FROM Emp) t) u")
        for frames in (4, 12, 32):
            self.assertEqual(self.rows(sql, frames), [(len(set(ages)),)], frames)
        joined = "SELECT COUNT(*) FROM (SELECT eid, AVG(age) AS a FROM Emp GROUP BY eid) t JOIN Emp e ON t.a = e.age"
        self.assertEqual(self.rows(joined, 8, fixed_join="hash_join"),
                         self.rows(joined, 8, fixed_join="nested_loop_join"))

    def test_union_all_with_a_blocking_right_input_runs_at_four_frames(self):
        dept_cities = [row[3] for _, row in self.open().heap_file("Dept").scan()]
        emp_cities = {row[6] for _, row in self.open().heap_file("Emp").scan()}
        rows = self.rows("SELECT city FROM Dept UNION ALL SELECT DISTINCT city FROM Emp ORDER BY city", 4)
        self.assertEqual([r[0] for r in rows], sorted(dept_cities + list(emp_cities)))
        rows = self.rows("SELECT DISTINCT city FROM Emp UNION ALL SELECT DISTINCT city FROM Dept", 4)
        self.assertEqual(sorted(r[0] for r in rows), sorted(list(emp_cities) + list(set(dept_cities))))

    def test_a_union_all_of_a_table_and_a_three_way_join_runs_at_four_frames(self):
        sql = ("SELECT d.city FROM Dept d UNION ALL SELECT e.city FROM Emp e JOIN Emp m ON e.mgr = m.eid "
               "JOIN Dept x ON m.did = x.did ORDER BY city")
        self.assertEqual(self.rows(sql, 4), self.rows(sql, 32))

    def test_a_constant_predicate_stays_above_a_group_by_without_keys(self):
        self.assertEqual(self.rows("SELECT t.c FROM (SELECT COUNT(*) AS c FROM Emp) t WHERE 1 = 2", 8), [])

    def test_a_forced_join_algorithm_still_plans_a_cross_product(self):
        sql = "SELECT d.did, e.eid FROM Dept d, Emp e WHERE d.did = 5 AND e.eid = 7"
        for join in ("hash_join", "sort_merge_join"):
            self.assertEqual(self.rows(sql, 8, fixed_join=join), [(5, 7)])

    def test_a_tuple_wider_than_a_page_is_refused_not_dropped(self):
        out = io.StringIO()
        shell = Shell(self.dir, 8, out=out)
        wide = ", ".join(f"c{i} VARCHAR(255)" for i in range(8))
        values = ", ".join(f"'{'x' * 255}'" for _ in range(8))
        self.assertTrue(shell.run_script(f"CREATE TABLE W (k INT, {wide}); INSERT INTO W VALUES (1, {values}); "
                                         f"INSERT INTO W VALUES (2, {values});"))
        self.assertTrue(shell.run_script("SELECT x.k, y.k FROM W x, W y;"))
        self.assertFalse(shell.run_script("SELECT * FROM W x, W y ORDER BY x.k;"))
        shell.close()
        self.assertIn("(4 rows)", out.getvalue())
        self.assertIn("does not fit on a page", out.getvalue())

    def test_unknown_column_in_a_logical_plan_is_a_clean_error(self):
        from dbms.ra.operators import EvalError
        from dbms.ra.tree import from_json
        db = self.open()
        logical = from_json({"op": "select", "condition": "Emp.nope > 3", "child": {"op": "scan", "table": "Emp"}})
        with self.assertRaisesRegex(EvalError, "Unknown attribute Emp.nope"):
            Planner(db.catalog, 8).optimize(logical)

    def test_shell_statements(self):
        out = io.StringIO()
        shell = Shell(self.dir, 8, out=out)
        self.assertTrue(shell.run_script(
            "ANALYZE Emp; SET frames = 5; EXPLAIN SELECT name FROM Emp WHERE age > 65;"
            "EXPLAIN ANALYZE SELECT COUNT(*) FROM Emp e JOIN Dept d ON e.did = d.did;"
            "SELECT COUNT(*) AS n FROM Dept;"))
        self.assertFalse(shell.run_script("SELECT nope FROM Emp;"))
        self.assertFalse(shell.run_script("SET frames = 2;"))
        self.assertFalse(shell.run_script("SELECT # FROM Emp;"))
        self.assertFalse(shell.run_script(
            "SELECT eid FROM Emp WHERE " + " OR ".join(f"eid = {i}" for i in range(3000)) + ";"))
        shell.close()
        text = out.getvalue()
        self.assertIn("frames = 5", text)
        self.assertIn("est_rows=", text)
        self.assertIn("actual_rows=", text)
        self.assertIn("40", text)
        self.assertIn("error: name: unknown column 'nope'", text)
        self.assertIn("frames must be at least 3", text)
        self.assertIn("error: lexical: unexpected character '#'", text)
        self.assertIn("nested too deeply", text)

    def test_a_script_stops_at_its_first_failing_statement(self):
        out = io.StringIO()
        shell = Shell(self.dir, 8, out=out)
        self.assertFalse(shell.run_script("CREATE TABLE L (a INT); INSERT INTO L VALUES (1); SELECT 'x"))
        self.assertFalse(shell.run_script("INSERT INTO L VALUES (2); SELECT nope FROM L; INSERT INTO L VALUES (3);"))
        self.assertTrue(shell.run_script("SELECT a FROM L;"))
        shell.close()
        self.assertIn("error: lexical: unterminated string literal at line 1, column 58", out.getvalue())
        self.assertIn("(2 rows)", out.getvalue())


class TestCommandLines(DatabaseTest):
    def setUp(self):
        super().setUp()
        db = Database(self.dir, 8)
        db.create_table("R", parse_schema("a:INT"))
        db.heap_file("R").insert((1,))
        db.save()
        db.close()
        self.plan = self.write("plan.json", b'{"op": "seqscan", "table": "R"}')
        self.logical = self.write("logical.json", b'{"op": "scan", "table": "R"}')
        self.sql = self.write("q.sql", b"SELECT a FROM R;")

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_every_tool_takes_frames(self):
        d = self.dir
        csv = self.write("t.csv", b"7\n")
        trace = self.write("trace.txt", b"0\n")
        quiet = self.write("quiet.sql", b"-- prints nothing\n")
        commands = [
            (storage_main, ["create-table", "--db", d, "--name", "T", "--schema", "a:INT"]),
            (storage_main, ["load", "--db", d, "--table", "T", "--file", csv]),
            (storage_main, ["scan", "--db", d, "--table", "T"]),
            (storage_main, ["layout", "--db", d, "--table", "T", "--page", "1"]),
            (storage_main, ["delete", "--db", d, "--table", "T", "--rid", "1:0"]),
            (storage_main, ["trace", "--db", d, "--file", trace]),
            (exec_main, ["run", "--db", d, "--plan", self.plan]),
            (exec_main, ["trace", "--db", d, "--plan", self.plan]),
            (sql_main, ["parse", "--query", "SELECT a FROM R"]),
            (sql_main, ["compile", "--db", d, "--file", self.sql]),
            (sql_main, ["check", "--db", d, "--file", self.sql]),
            (opt_main, ["analyze", "--db", d, "--table", "R"]),
            (opt_main, ["plan", "--db", d, "--logical", self.logical]),
            (opt_main, ["explain", "--db", d, "--logical", self.logical]),
            (opt_main, ["dp-table", "--db", d, "--logical", self.logical]),
            (shell_main, ["--db", d, "-f", quiet]),
        ]
        for main, argv in commands:
            code, _, err = run_command(main, argv + ["--frames", "5"])
            self.assertEqual((code, err), (0, ""), argv)

    def test_commands_that_read_a_database_do_not_create_one(self):
        missing = os.path.join(self.dir, "missing")
        commands = [
            (storage_main, ["load", "--db", missing, "--table", "R", "--file", self.sql]),
            (storage_main, ["scan", "--db", missing, "--table", "R"]),
            (storage_main, ["layout", "--db", missing, "--table", "R", "--page", "0"]),
            (storage_main, ["delete", "--db", missing, "--table", "R", "--rid", "1:0"]),
            (exec_main, ["run", "--db", missing, "--plan", self.plan]),
            (exec_main, ["trace", "--db", missing, "--plan", self.plan]),
            (sql_main, ["compile", "--db", missing, "--file", self.sql]),
            (sql_main, ["check", "--db", missing, "--file", self.sql]),
            (opt_main, ["analyze", "--db", missing, "--table", "R"]),
            (opt_main, ["plan", "--db", missing, "--logical", self.logical]),
            (opt_main, ["explain", "--db", missing, "--logical", self.logical]),
            (opt_main, ["dp-table", "--db", missing, "--logical", self.logical]),
        ]
        for main, argv in commands:
            self.assertEqual(run_command(main, argv),
                             (1, "", f"error: No database in {missing} (catalog.db not found)\n"), argv)
        self.assertFalse(os.path.exists(missing))

    def test_analyze_refuses_zero_buckets(self):
        self.assertEqual(run_command(opt_main, ["analyze", "--db", self.dir, "--table", "R", "--buckets", "0"]),
                         (1, "", "error: The bucket count must be between 1 and 65535, got 0\n"))

    def test_input_that_is_not_utf8_is_a_one_line_error(self):
        latin1 = self.write("latin1.txt", b"SELECT '\xe9';")
        for main, argv in ((shell_main, ["--db", self.dir, "-f", latin1]),
                           (opt_main, ["plan", "--db", self.dir, "--logical", latin1])):
            code, _, err = run_command(main, argv)
            self.assertEqual(code, 1)
            self.assertRegex(err, "^error: .*can't decode byte 0xe9.*\n$")

    def test_the_workload_script_runs_every_query_when_one_fails(self):
        queries = os.path.join(self.dir, "queries")
        os.mkdir(queries)
        self.write("queries/q1.sql", b"SELECT nope FROM R;")
        self.write("queries/q2.sql", b"SELECT a FROM R;")
        schema = self.write("schema.sql", b"CREATE TABLE R (a INT);")
        argv = ["run_workload.py", "--schema", schema, "--data", self.dir, "--queries", queries, "--db", self.dir]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                run_workload.main()
        self.assertEqual(cm.exception.code, "queries that failed: q1")
        results = [json.loads(line) for line in out.getvalue().splitlines()][:2]
        self.assertEqual(results[0], {"query": "q1", "error": "error: name: unknown column 'nope' at line 1, column 8"})
        self.assertEqual((results[1]["query"], results[1]["rows"]), ("q2", 1))


class TestCorrectnessScripts(unittest.TestCase):
    def test_results_are_compared_as_printed_with_avg_to_four_places(self):
        postgres = run_workload.Postgres("127.0.0.1:5432/postgres")
        with mock.patch.object(postgres, "run", return_value="20.53125000000000000000\t7\n1.00004999\t8\n"):
            theirs = postgres.rows("SELECT ...", ["REAL", "INT"])
        self.assertEqual(theirs, [("20.5313", "7"), ("1.0000", "8")])
        ours = run_workload.printed([(20.53125, 7), (1.00004999, 8)])
        self.assertTrue(run_workload.same_result(ours, theirs, True))
        self.assertFalse(run_workload.same_result(run_workload.printed([(20.53125, 7), (1.0001, 8)]), theirs, False))

    def test_the_fuzzer_orders_limits_and_nests_set_operations(self):
        generator = fuzz_sql.Generator(random.Random(1))
        queries = [generator.query() for _ in range(400)]
        texts = [sql for sql, _ in queries]
        self.assertTrue(any(ordered and " LIMIT " in sql for sql, ordered in queries))
        self.assertTrue(any(f"HAVING {func}(" in sql for sql in texts for func in ("SUM", "AVG", "MIN", "MAX")))
        self.assertTrue(any(") AS q" in sql and " UNION ALL " in sql for sql in texts))
        self.assertTrue(any(" UNION " in sql and sql.endswith(("ORDER BY c0", "ORDER BY c0 DESC", "ORDER BY c0 ASC"))
                            for sql in texts))


if __name__ == "__main__":
    unittest.main()
