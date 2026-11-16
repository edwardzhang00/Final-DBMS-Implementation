"""The relational algebra engine as a library."""
import ast
import os
import unittest

from test_helpers import _HERE  # noqa: F401  (puts the repository on sys.path)

from dbms.ra.tree import (
    to_json, from_json, condition_text, explain_tree,
    RelRef, Select, Project, Rename, Distinct, GroupBy, Aggregate, Sort, Limit,
    Union, UnionAll, Intersect, Minus, Times, Join, AttrRef,
)
from dbms.ra.parser import parse_condition, parse_column, parse_query
from dbms.ra.operators import (
    compile_condition, Column, Relation, eval_expr, dict_lookup, TypeMismatchError,
)


def plan_with_every_node():
    emp = Rename("e", RelRef("Emp"))
    dept = Rename("d", RelRef("Dept"))
    joined = Join(parse_condition("e.did = d.did"), emp, dept)
    crossed = Times(RelRef("A"), RelRef("B"))
    filtered = Select(parse_condition("not (e.age > 30 or d.title = 'O''Brien') and e.did != -7"), joined)
    grouped = GroupBy([AttrRef("e", "did")],
                      [Aggregate("count", None, "n"), Aggregate("avg", AttrRef("e", "salary"), "avg_sal")],
                      filtered)
    projected = Project([AttrRef("e", "did"), AttrRef(None, "n")], grouped, names=["dept", None])
    ordered = Limit(10, Sort([(AttrRef(None, "n"), "desc"), (AttrRef(None, "dept"), "asc")],
                             Distinct(projected)))
    sets = Minus(Intersect(UnionAll(RelRef("A"), RelRef("B")), Union(RelRef("A"), RelRef("B"))), crossed)
    return ordered, sets


class TestJsonRoundTrip(unittest.TestCase):

    def test_every_node_type_round_trips(self):
        for plan in plan_with_every_node():
            text = to_json(plan)
            again = to_json(from_json(text))
            self.assertEqual(text, again)

    def test_op_names_match_the_logical_contract(self):
        ops = set()

        def walk(obj):
            ops.add(obj["op"])
            for key in ("child", "left", "right"):
                if key in obj:
                    walk(obj[key])
        for plan in plan_with_every_node():
            walk(to_json(plan))
        self.assertEqual(ops, {"scan", "select", "project", "join", "cross", "rename", "group_by",
                               "distinct", "sort", "limit", "union", "union_all", "intersect", "except"})

    def test_handout_example_shape(self):
        plan = Project([AttrRef("Emp", "name"), AttrRef("Emp", "age")],
                       Select(parse_condition("Emp.age > 30"), RelRef("Emp")))
        self.assertEqual(to_json(plan), {
            "op": "project", "columns": ["Emp.name", "Emp.age"],
            "child": {"op": "select", "condition": "Emp.age > 30",
                      "child": {"op": "scan", "table": "Emp"}}})

    def test_condition_text_preserves_grouping(self):
        for text in ["a = 1 and (b = 2 and c = 3)", "a = 1 or b = 2 and c = 3",
                     "(a = 1 or b = 2) and c = 3", "not (a = 1 and b = 2)", "not not a = 1"]:
            self.assertEqual(condition_text(parse_condition(text)), text)

    def test_explain_tree_indents_by_depth(self):
        plan = parse_query("project[Emp.name](select[Emp.age>30](Emp))")
        self.assertEqual(explain_tree(plan),
                         "project [Emp.name]\n  select (Emp.age > 30)\n    scan Emp")


class TestJsonValidation(unittest.TestCase):
    def test_sort_direction_and_limit_are_checked(self):
        scan = {"op": "scan", "table": "R"}
        with self.assertRaisesRegex(ValueError, "asc"):
            from_json({"op": "sort", "keys": [["R.a", "sideways"]], "child": scan})
        with self.assertRaisesRegex(ValueError, ">= 0"):
            from_json({"op": "limit", "n": -3, "child": scan})


class TestConditionParserOnItsOwn(unittest.TestCase):

    def test_parse_and_evaluate_against_a_tuple(self):
        cond = parse_condition("R.b = S.b and R.a > 500")
        columns = [Column("R", "a"), Column("R", "b"), Column("S", "b")]
        check = compile_condition(cond, columns, ["INT", "INT", "INT"])
        self.assertTrue(check((501, 7, 7)))
        self.assertFalse(check((500, 7, 7)))
        self.assertFalse(check((501, 7, 8)))

    def test_static_type_error_before_any_tuple(self):
        cond = parse_condition("R.a = 'x'")
        with self.assertRaises(TypeMismatchError):
            compile_condition(cond, [Column("R", "a")], ["INT"])

    def test_qualified_column_parsing(self):
        self.assertEqual(parse_column("Emp.did"), AttrRef("Emp", "did"))
        self.assertEqual(parse_column("count_star"), AttrRef(None, "count_star"))

    def test_qualified_names_in_projection(self):
        db = {"Emp": Relation("Emp", [Column("Emp", "a"), Column("Emp", "b")], [(1, 2), (1, 3)])}
        result = eval_expr(parse_query("project[Emp.a](Emp)"), dict_lookup(db))
        self.assertEqual(list(result.rows), [(1,), (1,)])

    def test_operators_pull_from_a_passed_in_source(self):
        pulled = []

        def source():
            for row in [(1,), (2,), (3,)]:
                pulled.append(row)
                yield row

        class Stream:
            def __iter__(self):
                return source()

        lookup = dict_lookup({"R": Relation("R", [Column("R", "x")], Stream())})
        result = eval_expr(parse_query("select[x>1](R)"), lookup)
        self.assertEqual(pulled, [])
        self.assertEqual(list(result.rows), [(2,), (3,)])


class TestLayersDoNotPrintOrOpenFiles(unittest.TestCase):

    def test_no_print_or_open_calls(self):
        package = os.path.join(_HERE, "..", "dbms", "ra")
        for name in os.listdir(package):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(package, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ("print", "open", "input"), name)


if __name__ == "__main__":
    unittest.main()
