"""SQL front end. The fifteen required cases and the seven translation examples."""
import json
import os
import shutil
import tempfile
import unittest

from test_helpers import _HERE, run_command  # noqa: F401

from dbms.ra.tree import to_json
from dbms.sql.analyzer import Analyzer
from dbms.sql.ast import SQLError
from dbms.sql.cli import main as sql_main
from dbms.sql.parser import parse_script, tokenize
from dbms.storage.database import Database
from dbms.storage.record import parse_schema

SCHEMAS = {
    "Emp": parse_schema("eid:INT,name:VARCHAR(20),age:INT,did:INT,salary:INT,mgr:INT,city:VARCHAR(20)"),
    "Dept": parse_schema("did:INT,title:VARCHAR(20),budget:INT,city:VARCHAR(20)"),
    "Proj": parse_schema("pid:INT,did:INT,title:VARCHAR(20)"),
}


def compile_sql(text):
    analyzer = Analyzer(SCHEMAS)
    results = [analyzer.statement(s) for s in parse_script(text)]
    return [to_json(r.plan) for r in results if r.kind == "query"]


def plan(text):
    return compile_sql(text)[0]


def scan(t):
    return {"op": "scan", "table": t}


def rename(alias, child):
    return {"op": "rename", "to": alias, "child": child}


def ops(node):
    """Every op name in a plan, preorder."""
    out = [node["op"]]
    for key in ("child", "left", "right"):
        if key in node:
            out += ops(node[key])
    return out


def columns_in(node):
    """Every column string mentioned anywhere in a plan."""
    found = list(node.get("columns", [])) + list(node.get("keys", []))
    for agg in node.get("aggregates", []):
        found.append(agg[1])
    for key in ("child", "left", "right"):
        if key in node:
            found += columns_in(node[key])
    return [c[0] if isinstance(c, list) else c for c in found]


class SQLTest(unittest.TestCase):
    def assert_error(self, text, category, fragment=None):
        with self.assertRaises(SQLError) as cm:
            compile_sql(text)
        self.assertEqual(cm.exception.category, category, str(cm.exception))
        self.assertRegex(str(cm.exception), r"^error: \w+: .* at line \d+, column \d+$")
        if fragment:
            self.assertIn(fragment, str(cm.exception))
        return cm.exception


class TestLexer(SQLTest):
    def test_keywords_case_insensitive_identifiers_case_sensitive(self):
        a = [(t.kind, t.value) for t in tokenize("SeLeCt Emp FROM emp")]
        self.assertEqual(a[:4], [("KEYWORD", "SELECT"), ("IDENT", "Emp"), ("KEYWORD", "FROM"), ("IDENT", "emp")])

    def test_maximal_munch_strings_numbers_comments(self):
        tokens = [(t.kind, t.value) for t in tokenize("a<=-5 and b<>'O''Brien' -- note\n;c>=+7")]
        self.assertEqual(tokens, [("IDENT", "a"), ("SYMBOL", "<="), ("INT", -5), ("KEYWORD", "AND"),
                                  ("IDENT", "b"), ("SYMBOL", "<>"), ("STRING", "O'Brien"), ("SYMBOL", ";"),
                                  ("IDENT", "c"), ("SYMBOL", ">="), ("INT", 7), ("EOF", None)])

    def test_several_statements_per_file(self):
        self.assertEqual(len(compile_sql("SELECT name FROM Emp; SELECT title FROM Dept;")), 2)

    def test_lexical_error_has_position(self):
        e = self.assert_error("SELECT name\nFROM Emp WHERE name = 'x", "lexical")
        self.assertEqual((e.line, e.col), (2, 23))

    def test_set_operators_left_associative_same_precedence(self):
        p = plan("SELECT city FROM Emp UNION SELECT city FROM Dept EXCEPT SELECT title FROM Proj")
        self.assertEqual(p["op"], "except")
        self.assertEqual(p["left"]["op"], "union")

    def test_order_by_after_a_set_operation_applies_to_the_whole_result(self):
        p = plan("SELECT city FROM Emp UNION SELECT city FROM Dept ORDER BY city DESC LIMIT 2")
        self.assertEqual(ops(p)[:3], ["limit", "sort", "union"])
        self.assertEqual(p["child"]["keys"], [["Emp.city", "desc"]])
        q = plan("(SELECT name FROM Emp ORDER BY age LIMIT 1) UNION ALL SELECT title FROM Dept")
        self.assertEqual(ops(q)[:2], ["union_all", "limit"])

    def test_order_by_and_limit_after_a_parenthesized_last_block(self):
        q = plan("(SELECT did FROM Dept) UNION (SELECT did FROM Emp) ORDER BY did DESC LIMIT 3")
        self.assertEqual(ops(q)[:3], ["limit", "sort", "union"])
        self.assertEqual(ops(plan("(SELECT did FROM Dept) ORDER BY did"))[:2], ["sort", "project"])

    def test_a_parenthesized_query_takes_the_clause_it_lacks(self):
        self.assertEqual(ops(plan("(SELECT did FROM Dept LIMIT 2) ORDER BY did DESC"))[:3], ["limit", "sort", "project"])
        self.assertEqual(ops(plan("(SELECT did FROM Dept ORDER BY did) LIMIT 2"))[:3], ["limit", "sort", "project"])
        q = plan("(SELECT did FROM Dept UNION SELECT did FROM Emp LIMIT 3) ORDER BY did")
        self.assertEqual(ops(q)[:3], ["limit", "sort", "union"])
        self.assert_error("(SELECT did FROM Dept LIMIT 2) LIMIT 1", "syntax", "already has its own LIMIT")
        self.assert_error("(SELECT did FROM Dept ORDER BY did) ORDER BY did", "syntax", "already has its own ORDER BY")
        self.assert_error("SELECT did FROM Dept LIMIT 2 ORDER BY did", "syntax", "expected ';'")

    def test_order_by_after_a_set_operation_names_result_columns_only(self):
        self.assertEqual(plan("SELECT d.did FROM Dept d UNION SELECT e.did FROM Emp e ORDER BY did")["keys"],
                         [["d.did", "asc"]])
        self.assert_error("SELECT d.did FROM Dept d UNION SELECT e.did FROM Emp e ORDER BY d.did", "name",
                          "has no table 'd'")

    def test_a_block_before_a_set_operator_may_order_and_limit_itself(self):
        q = plan("SELECT did FROM Dept ORDER BY did DESC LIMIT 3 UNION ALL SELECT did FROM Emp")
        self.assertEqual(ops(q)[:4], ["union_all", "limit", "sort", "project"])

    def test_a_default_aggregate_name_never_takes_an_alias(self):
        q = plan("SELECT MIN(age) AS max_Emp_age, MAX(age) FROM Emp")
        self.assertEqual(q["child"]["aggregates"],
                         [["min", "Emp.age", "max_Emp_age"], ["max", "Emp.age", "max_Emp_age_2"]])
        q = plan("SELECT MAX(age), MIN(age) AS max_Emp_age FROM Emp")
        self.assertEqual([a[2] for a in q["child"]["aggregates"]], ["max_Emp_age_2", "max_Emp_age"])

    def test_columns_of_one_name_in_a_subquery_are_told_apart(self):
        q = plan("SELECT * FROM (SELECT * FROM Emp e JOIN Dept d ON e.did = d.did) t")
        self.assertIn("t.did_2", q["columns"])
        self.assertIn("t.city_2", q["columns"])

    def test_group_by_may_name_a_select_list_alias(self):
        q = plan("SELECT did AS d, COUNT(*) FROM Emp GROUP BY d")
        self.assertEqual(q["child"]["keys"], ["Emp.did"])
        # A FROM column of that name wins, so this groups by Emp.age and did is not grouped.
        self.assert_error("SELECT did AS age FROM Emp GROUP BY age", "aggregate")

    def test_sum_and_avg_take_any_number(self):
        q = plan("SELECT SUM(t.s), AVG(t.s) FROM (SELECT did, SUM(salary) AS s FROM Emp GROUP BY did) t")
        self.assertEqual(q["child"]["aggregates"][0], ["sum", "t.s", "sum_t_s"])
        self.assert_error("SELECT SUM(name) FROM Emp", "type", "needs a numeric column")

    def test_order_by_names_an_output_column_first(self):
        self.assertEqual(plan("SELECT did AS d FROM Emp ORDER BY d")["keys"], [["d", "asc"]])
        self.assert_error("SELECT did AS age, age FROM Emp ORDER BY age", "ambiguity", "2 output columns")
        p = plan("SELECT d.did AS budget FROM Dept d ORDER BY d.budget DESC")
        self.assertEqual(p["child"]["keys"], [["d.budget", "desc"]])

    def test_not_and_or_precedence(self):
        p = plan("SELECT name FROM Emp WHERE NOT age = 1 AND did = 2 OR salary = 3")
        self.assertEqual(p["child"]["condition"], "not Emp.age = 1 and Emp.did = 2 or Emp.salary = 3")


class TestHandoutExamples(SQLTest):
    def test_5_1_filter_and_projection(self):
        self.assertEqual(plan("SELECT name, age FROM Emp WHERE age > 30;"), {
            "op": "project", "columns": ["Emp.name", "Emp.age"],
            "child": {"op": "select", "condition": "Emp.age > 30", "child": scan("Emp")}})

    def test_5_2_join_with_aliases(self):
        self.assertEqual(plan("SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did "
                              "WHERE d.budget > 1000;"), {
            "op": "project", "columns": ["e.name", "d.title"],
            "child": {"op": "select", "condition": "d.budget > 1000",
                      "child": {"op": "join", "condition": "e.did = d.did",
                                "left": rename("e", scan("Emp")), "right": rename("d", scan("Dept"))}}})

    def test_5_3_comma_join(self):
        p = plan("SELECT * FROM Emp e, Dept d WHERE e.did = d.did;")
        self.assertEqual(ops(p), ["project", "select", "cross", "rename", "scan", "rename", "scan"])
        self.assertEqual(p["child"]["condition"], "e.did = d.did")

    def test_5_4_grouping_with_having(self):
        self.assertEqual(plan("SELECT did, COUNT(*) AS n, AVG(salary) AS avg_sal FROM Emp WHERE age > 25 "
                              "GROUP BY did HAVING COUNT(*) > 5 ORDER BY n DESC LIMIT 10;"), {
            "op": "limit", "n": 10,
            "child": {"op": "sort", "keys": [["n", "desc"]],
                      "child": {"op": "project", "columns": ["Emp.did", "n", "avg_sal"],
                                "child": {"op": "select", "condition": "n > 5",
                                          "child": {"op": "group_by", "keys": ["Emp.did"],
                                                    "aggregates": [["count", "*", "n"],
                                                                   ["avg", "Emp.salary", "avg_sal"]],
                                                    "child": {"op": "select", "condition": "Emp.age > 25",
                                                              "child": scan("Emp")}}}}}})

    def test_5_5_self_join(self):
        self.assertEqual(plan("SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid;")["child"], {
            "op": "join", "condition": "e.mgr = m.eid",
            "left": rename("e", scan("Emp")), "right": rename("m", scan("Emp"))})

    def test_5_6_subquery_in_from(self):
        self.assertEqual(plan("SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t "
                              "WHERE t.n > 3;"), {
            "op": "project", "columns": ["t.did", "t.n"],
            "child": {"op": "select", "condition": "t.n > 3",
                      "child": {"op": "rename", "to": "t",
                                "child": {"op": "project", "columns": ["Emp.did", "n"],
                                          "child": {"op": "group_by", "keys": ["Emp.did"],
                                                    "aggregates": [["count", "*", "n"]],
                                                    "child": scan("Emp")}}}}})

    def test_5_7_union_and_union_all(self):
        def side(t):
            return {"op": "project", "columns": [f"{t}.city"], "child": scan(t)}
        self.assertEqual(plan("SELECT city FROM Emp UNION ALL SELECT city FROM Dept;"),
                         {"op": "union_all", "left": side("Emp"), "right": side("Dept")})
        self.assertEqual(plan("SELECT city FROM Emp UNION SELECT city FROM Dept;"),
                         {"op": "union", "left": side("Emp"), "right": side("Dept")})


class TestRequiredCases(SQLTest):
    def test_01_self_join_two_renames(self):
        p = plan("SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid")
        self.assertEqual(ops(p).count("rename"), 2)

    def test_02_star_over_three_tables_sharing_a_column(self):
        p = plan("SELECT * FROM Emp e JOIN Dept d ON e.did = d.did JOIN Proj p ON p.did = d.did")
        self.assertEqual(p["columns"][:2], ["e.eid", "e.name"])
        self.assertIn("e.did", p["columns"])
        self.assertIn("d.did", p["columns"])
        self.assertIn("p.did", p["columns"])
        self.assertEqual(len(p["columns"]), 7 + 4 + 3)

    def test_03_unqualified_column_in_two_tables(self):
        self.assert_error("SELECT city FROM Emp, Dept", "ambiguity", "Emp.city, Dept.city")

    def test_04_union_and_union_all_differ(self):
        a = plan("SELECT city FROM Emp UNION SELECT city FROM Dept")
        b = plan("SELECT city FROM Emp UNION ALL SELECT city FROM Dept")
        self.assertNotEqual(a, b)
        self.assertEqual((a["op"], b["op"]), ("union", "union_all"))

    def test_05_where_and_having_on_opposite_sides(self):
        p = plan("SELECT did, COUNT(*) FROM Emp WHERE age > 25 GROUP BY did HAVING COUNT(*) > 5")
        self.assertEqual(ops(p), ["project", "select", "group_by", "select", "scan"])
        self.assertEqual(p["child"]["condition"], "count_star > 5")
        self.assertEqual(p["child"]["child"]["child"]["condition"], "Emp.age > 25")

    def test_06_non_grouped_column(self):
        self.assert_error("SELECT did, name, COUNT(*) FROM Emp GROUP BY did", "aggregate", "Emp.name")

    def test_07_aggregate_in_where(self):
        self.assert_error("SELECT name FROM Emp WHERE COUNT(*) > 2", "aggregate", "WHERE")

    def test_08_subquery_joined_to_base_table(self):
        p = plan("SELECT t.n, d.title FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) t "
                 "JOIN Dept d ON t.did = d.did")
        join = p["child"]
        self.assertEqual(join["op"], "join")
        self.assertEqual(join["left"]["op"], "rename")
        self.assertEqual(join["left"]["child"]["child"]["op"], "group_by")
        self.assertEqual(join["right"], rename("d", scan("Dept")))

    def test_09_order_by_column_not_in_select_list(self):
        p = plan("SELECT name FROM Emp ORDER BY age DESC")
        self.assertEqual(p, {"op": "project", "columns": ["Emp.name"],
                             "child": {"op": "sort", "keys": [["Emp.age", "desc"]],
                                       "child": {"op": "project", "columns": ["Emp.name", "Emp.age"],
                                                 "child": scan("Emp")}}})

    def test_10_int_column_against_string(self):
        self.assert_error("SELECT name FROM Emp WHERE age > '30'", "type", "INT")

    def test_11_unknown_table_and_unknown_column_differ(self):
        table = self.assert_error("SELECT name FROM Empl", "name", "unknown table 'Empl'")
        column = self.assert_error("SELECT nam FROM Emp", "name", "unknown column 'nam'")
        self.assertNotEqual(table.message, column.message)

    def test_12_duplicate_alias(self):
        self.assert_error("SELECT name FROM Emp, Emp", "name", "duplicate table name or alias 'Emp'")
        self.assertTrue(plan("SELECT e.name FROM Emp e, Emp m"))

    def test_13_count_star_without_group_by(self):
        self.assertEqual(plan("SELECT COUNT(*) FROM Emp"), {
            "op": "project", "columns": ["count_star"],
            "child": {"op": "group_by", "keys": [], "aggregates": [["count", "*", "count_star"]],
                      "child": scan("Emp")}})

    def test_14_union_type_mismatch(self):
        self.assert_error("SELECT name FROM Emp UNION SELECT budget FROM Dept", "schema")

    def test_15_four_table_chain_commas_against_join_on(self):
        commas = plan("SELECT e.name FROM Emp e, Dept d, Proj p, Emp m "
                      "WHERE e.did = d.did AND p.did = d.did AND e.mgr = m.eid")
        joins = plan("SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did JOIN Proj p ON p.did = d.did "
                     "JOIN Emp m ON e.mgr = m.eid")
        self.assertEqual(ops(commas), ["project", "select", "cross", "cross", "cross",
                                       "rename", "scan", "rename", "scan", "rename", "scan", "rename", "scan"])
        self.assertEqual(ops(joins), ["project", "join", "join", "join",
                                      "rename", "scan", "rename", "scan", "rename", "scan", "rename", "scan"])
        self.assertEqual(commas["child"]["condition"], "e.did = d.did and p.did = d.did and e.mgr = m.eid")


class TestGuarantees(SQLTest):
    QUERIES = [
        "SELECT * FROM Emp e, Dept d WHERE e.did = d.did",
        "SELECT DISTINCT city FROM Emp ORDER BY city",
        "SELECT did, MAX(name), MIN(age) FROM Emp GROUP BY did ORDER BY COUNT(*) DESC LIMIT 3",
        "SELECT t.did FROM (SELECT did, SUM(salary) AS s FROM Emp GROUP BY did) t WHERE t.s > 10",
        "(SELECT city FROM Emp) INTERSECT (SELECT city FROM Dept)",
    ]

    def test_no_physical_names_and_every_column_qualified_or_an_output_name(self):
        physical = {"seqscan", "filter", "hash_join", "sort_merge_join", "nested_loop_join"}
        for q in self.QUERIES:
            p = plan(q)
            self.assertFalse(physical & set(ops(p)), q)
            for c in columns_in(p):
                self.assertTrue("." in c or c == "*" or c in ("count_star", "max_Emp_name", "min_Emp_age",
                                                              "s"), (q, c))

    def test_distinct_above_projection_only_when_written(self):
        self.assertEqual(ops(plan("SELECT DISTINCT city FROM Emp")), ["distinct", "project", "scan"])
        self.assertEqual(ops(plan("SELECT city FROM Emp")), ["project", "scan"])
        self.assertNotIn("distinct", ops(plan("SELECT DISTINCT did, COUNT(*) FROM Emp GROUP BY did")))

    def test_create_and_insert_checks(self):
        self.assertEqual(compile_sql("CREATE TABLE T (a INT, b VARCHAR(3)); SELECT a FROM T"),
                         [{"op": "project", "columns": ["T.a"], "child": scan("T")}])
        self.assert_error("INSERT INTO Dept VALUES (1, 'x', 2)", "schema")
        self.assert_error("INSERT INTO Dept VALUES ('1', 'x', 2, 'y')", "type")
        self.assert_error("CREATE TABLE Emp (a INT)", "name")
        self.assert_error("CREATE TABLE Q (a VARCHAR(300))", "type")

    def test_create_table_follows_the_catalogs_name_rules(self):
        self.assert_error("CREATE TABLE catalog (a INT)", "name", "catalog's own file")
        self.assert_error("CREATE TABLE EMP (a INT)", "name", "would share a file with table Emp")
        self.assert_error("CREATE TABLE T (a INT); CREATE TABLE t (a INT)", "name", "would share a file with table T")

    def test_json_round_trip_of_compiled_plans(self):
        from dbms.ra.tree import from_json
        for q in self.QUERIES:
            p = plan(q)
            self.assertEqual(to_json(from_json(json.loads(json.dumps(p)))), p)


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        db = Database(self.dir, 4)
        db.create_table("emp", parse_schema("a:INT"))
        db.close()

    def check(self, data):
        path = os.path.join(self.dir, "q.sql")
        with open(path, "wb") as f:
            f.write(data)
        return run_command(sql_main, ["check", "--db", self.dir, "--file", path])

    def test_check_refuses_the_table_names_the_catalog_refuses(self):
        self.assertEqual(self.check(b"CREATE TABLE b (a INT); SELECT a FROM b;"), (0, "", ""))
        code, out, _ = self.check(b"CREATE TABLE catalog (a INT);")
        self.assertEqual((code, out), (1, "error: name: The name catalog is taken by the catalog's own file, "
                                          "catalog.db at line 1, column 1\n"))
        code, out, _ = self.check(b"SELECT a FROM emp;\nCREATE TABLE EMP (a INT);")
        self.assertEqual((code, out), (1, "error: name: Table EMP would share a file with table emp: table names "
                                          "may not differ only in case at line 2, column 1\n"))

    def test_a_file_that_is_not_utf8_is_a_one_line_error(self):
        code, _, err = self.check(b"SELECT a FROM emp WHERE a = '\xe9';")
        self.assertEqual(code, 1)
        self.assertRegex(err, "^error: .*can't decode byte 0xe9.*\n$")


if __name__ == "__main__":
    unittest.main()
