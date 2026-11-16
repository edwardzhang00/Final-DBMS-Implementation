"""Relational algebra operators on bags.

A Relation carries a schema and any re-iterable source of rows; every operator
returns a new Relation whose rows are produced lazily from its inputs.
Projection keeps duplicates and duplicate removal is the separate distinct
operator.
"""

import operator
from collections import Counter

from .tree import (
    RelRef, Select, Project, Rename, Distinct, Union, UnionAll, Intersect, Minus,
    Times, Join, Comparison, And, Not, Integer, String, is_equality,
)

STATS = {
    "join_pairs": 0,
    "select_tuples": 0,
    "project_tuples": 0,
}


def reset_stats():
    for k in STATS:
        STATS[k] = 0


class EvalError(Exception):
    pass


class NameError_(EvalError):
    pass


class SchemaError(EvalError):
    pass


class TypeMismatchError(EvalError):
    pass


class DuplicateAttrError(EvalError):
    pass


class Column:
    __slots__ = ("rel_name", "attr_name")

    def __init__(self, rel_name, attr_name):
        self.rel_name = rel_name
        self.attr_name = attr_name

    @property
    def text(self):
        return f"{self.rel_name}.{self.attr_name}" if self.rel_name else self.attr_name

    def __repr__(self):
        return self.text

    def __eq__(self, other):
        return (isinstance(other, Column) and self.rel_name == other.rel_name
                and self.attr_name == other.attr_name)

    def __hash__(self):
        return hash((self.rel_name, self.attr_name))


class Rows:
    """Re-iterable view over a generator function."""

    def __init__(self, produce):
        self.produce = produce

    def __iter__(self):
        return iter(self.produce())


class Relation:
    def __init__(self, name, columns, rows):
        self.name = name
        self.columns = columns
        self.rows = rows

    def labels(self):
        counts = Counter(c.attr_name for c in self.columns)
        return [c.text if counts[c.attr_name] > 1 else c.attr_name for c in self.columns]


def resolve_attr(attr_ref, columns):
    """A qualified name must match exactly. A bare name means the column of that
    name with no qualifier (an alias or an aggregate result) if there is one,
    otherwise the qualified column of that name."""
    if attr_ref.relation:
        matches = [i for i, c in enumerate(columns)
                   if c.rel_name == attr_ref.relation and c.attr_name == attr_ref.attr]
    else:
        matches = [i for i, c in enumerate(columns) if c.attr_name == attr_ref.attr]
        unqualified = [i for i in matches if columns[i].rel_name is None]
        if len(unqualified) == 1:
            matches = unqualified
    if not matches:
        raise NameError_(f"Unknown attribute {attr_ref.text}")
    if len(matches) > 1:
        raise NameError_(f"Ambiguous attribute {attr_ref.text}")
    return matches[0]


COMPARE = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
           "<=": operator.le, ">": operator.gt, ">=": operator.ge}

NUMERIC_TYPES = ("INT", "BIGINT", "REAL")


def type_class(type_name):
    return "number" if type_name in NUMERIC_TYPES else "string"


def compile_condition(cond, columns, types=None):
    """Resolve a condition against a schema once and return row -> bool.

    With column types the type check happens here, before any row is read.
    Without them (the in-memory RA engine) values are checked as they meet.
    """
    if isinstance(cond, Comparison):
        left, left_class = compile_operand(cond.left, columns, types)
        right, right_class = compile_operand(cond.right, columns, types)
        compare = COMPARE[cond.op]
        if types is not None:
            if left_class != right_class:
                raise TypeMismatchError(
                    f"Cannot compare {cond.left.text} ({left_class}) with "
                    f"{cond.right.text} ({right_class})")
            return lambda row: compare(left(row), right(row))

        def check(row):
            lv, rv = left(row), right(row)
            if type(lv) is not type(rv):
                raise TypeMismatchError(
                    f"Cannot compare {type(lv).__name__} with {type(rv).__name__}")
            return compare(lv, rv)
        return check
    if isinstance(cond, Not):
        inner = compile_condition(cond.cond, columns, types)
        return lambda row: not inner(row)
    left = compile_condition(cond.left, columns, types)
    right = compile_condition(cond.right, columns, types)
    if isinstance(cond, And):
        return lambda row: left(row) and right(row)
    return lambda row: left(row) or right(row)


def compile_operand(operand, columns, types):
    if isinstance(operand, Integer):
        value = operand.value
        return (lambda row: value), "number"
    if isinstance(operand, String):
        value = operand.value
        return (lambda row: value), "string"
    idx = resolve_attr(operand, columns)
    return (lambda row: row[idx]), (type_class(types[idx]) if types is not None else None)


def op_select(cond, rel):
    check = compile_condition(cond, rel.columns)

    def produce():
        for row in rel.rows:
            STATS["select_tuples"] += 1
            if check(row):
                yield row
    return Relation(rel.name, rel.columns, Rows(produce))


def op_project(attrs, rel):
    indices = []
    for a in attrs:
        idx = resolve_attr(a, rel.columns)
        if idx in indices:
            raise DuplicateAttrError(f"Duplicate attribute in projection: {a.text}")
        indices.append(idx)

    def produce():
        for row in rel.rows:
            STATS["project_tuples"] += 1
            yield tuple(row[i] for i in indices)
    return Relation(None, [rel.columns[i] for i in indices], Rows(produce))


def op_distinct(rel):
    def produce():
        seen = {}
        for row in rel.rows:
            if row not in seen:
                seen[row] = True
                yield row
    return Relation(rel.name, rel.columns, Rows(produce))


def op_rename(new_name, rel):
    return Relation(new_name, [Column(new_name, c.attr_name) for c in rel.columns], rel.rows)


def check_no_collision(left, right, what):
    collision = {(c.rel_name, c.attr_name) for c in left.columns} & \
                {(c.rel_name, c.attr_name) for c in right.columns}
    if collision:
        raise SchemaError(f"Qualified name collision after {what}: {sorted(collision)}")


def op_times(left, right):
    check_no_collision(left, right, "times")

    def produce():
        for lrow in left.rows:
            for rrow in right.rows:
                STATS["join_pairs"] += 1
                yield lrow + rrow
    return Relation(None, left.columns + right.columns, Rows(produce))


def equi_join_columns(cond, left, right):
    """(left index, right index) when cond is one left attribute = one right attribute."""
    if not is_equality(cond):
        return None
    columns = left.columns + right.columns
    try:
        a, b = resolve_attr(cond.left, columns), resolve_attr(cond.right, columns)
    except NameError_:
        return None
    left_width = len(left.columns)
    if a < left_width <= b:
        return a, b - left_width
    if b < left_width <= a:
        return b, a - left_width
    return None


def check_equi_join_types(left, right, li, ri):
    """The fast path compares with ==, which is False rather than an error for
    1 == '1'. One pass over both columns keeps the number-against-string rule."""
    types = {type(row[li]) for row in left.rows} | {type(row[ri]) for row in right.rows}
    if len(types) > 1:
        names = ", ".join(sorted(t.__name__ for t in types))
        raise TypeMismatchError(f"Cannot compare values of different types in join: {names}")


def op_join(cond, left, right):
    check_no_collision(left, right, "join")
    columns = left.columns + right.columns
    fast = equi_join_columns(cond, left, right)
    if fast is not None:
        li, ri = fast

        def produce_equi():
            check_equi_join_types(left, right, li, ri)
            pairs = 0
            right_rows = list(right.rows)
            for lrow in left.rows:
                value = lrow[li]
                for rrow in right_rows:
                    pairs += 1
                    if value == rrow[ri]:
                        yield lrow + rrow
            STATS["join_pairs"] += pairs
        return Relation(None, columns, Rows(produce_equi))

    check = compile_condition(cond, columns)

    def produce():
        for lrow in left.rows:
            for rrow in right.rows:
                STATS["join_pairs"] += 1
                row = lrow + rrow
                if check(row):
                    yield row
    return Relation(None, columns, Rows(produce))


def infer_type(rel, idx):
    for row in rel.rows:
        return type(row[idx]).__name__
    return None


def check_union_compatible(left, right):
    if len(left.columns) != len(right.columns):
        raise SchemaError(
            f"Union incompatible: {len(left.columns)} vs {len(right.columns)} attributes")
    for lc, rc in zip(left.columns, right.columns):
        if lc.attr_name != rc.attr_name:
            raise SchemaError(
                f"Union incompatible: attribute names differ: {lc.attr_name} vs {rc.attr_name}")
    for i in range(len(left.columns)):
        lt = infer_type(left, i)
        rt = infer_type(right, i)
        if lt and rt and lt != rt:
            raise SchemaError(f"Union incompatible: types differ at position {i}: {lt} vs {rt}")


def op_union_all(left, right):
    check_union_compatible(left, right)

    def produce():
        yield from left.rows
        yield from right.rows
    return Relation(None, left.columns, Rows(produce))


def op_union(left, right):
    return op_distinct(op_union_all(left, right))


def distinct_left_rows(left, right, in_right):
    """Each distinct row of left once, if its presence in right is in_right."""
    check_union_compatible(left, right)

    def produce():
        right_rows = {row: True for row in right.rows}
        emitted = {}
        for row in left.rows:
            if (row in right_rows) == in_right and row not in emitted:
                emitted[row] = True
                yield row
    return Relation(None, left.columns, Rows(produce))


def op_intersect(left, right):
    return distinct_left_rows(left, right, True)


def op_minus(left, right):
    return distinct_left_rows(left, right, False)


def eval_expr(node, lookup):
    """Evaluate a tree, asking lookup(name) for each base relation."""
    if isinstance(node, RelRef):
        return lookup(node.name)
    if isinstance(node, Select):
        return op_select(node.cond, eval_expr(node.input, lookup))
    if isinstance(node, Project):
        return op_project(node.attrs, eval_expr(node.input, lookup))
    if isinstance(node, Rename):
        return op_rename(node.new_name, eval_expr(node.input, lookup))
    if isinstance(node, Distinct):
        return op_distinct(eval_expr(node.input, lookup))
    if isinstance(node, Join):
        return op_join(node.cond, eval_expr(node.left, lookup), eval_expr(node.right, lookup))
    binary = {Union: op_union, UnionAll: op_union_all, Intersect: op_intersect,
              Minus: op_minus, Times: op_times}.get(type(node))
    if binary is None:
        raise EvalError(f"The in-memory engine does not evaluate {type(node).__name__}")
    return binary(eval_expr(node.left, lookup), eval_expr(node.right, lookup))


def build_db(program):
    db = {}
    for definition in program.relations:
        if len(set(definition.attrs)) != len(definition.attrs):
            raise DuplicateAttrError(
                f"Duplicate attribute names in relation {definition.name}: {definition.attrs}")
        db[definition.name] = Relation(definition.name, [Column(definition.name, a) for a in definition.attrs],
                                       definition.tuples)
    return db


def dict_lookup(db):
    def lookup(name):
        if name not in db:
            raise NameError_(f"Unknown relation {name}")
        return db[name]
    return lookup
