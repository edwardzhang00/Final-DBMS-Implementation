"""Semantic analysis and translation of SQL to a logical plan (see DESIGN_M3.md).

The plan is built bottom to top in the canonical order: FROM (scans, renames
for aliases, joins or cross products left to right), WHERE, GROUP BY, HAVING,
SELECT (project, then distinct), ORDER BY, LIMIT, and set operators between
whole blocks. No optimization happens here and no predicate is moved.
"""

import typing

from ..execution.plan import MIN_FRAMES
from ..ra.operators import type_class
from ..ra.tree import (
    RelRef, Select, Project, Rename, Distinct, GroupBy, Aggregate as PlanAggregate, Sort, Limit,
    Times, Join, AttrRef, Integer, String, Comparison as PlanComparison, And, Or, Not,
    BINARY_CLASSES, default_aggregate_name,
)
from ..storage.catalog import CatalogError, check_table_name
from ..storage.record import ColumnDef, INT_MIN, INT_MAX, MAX_VARCHAR, aggregate_result_definition
from .ast import (
    CreateTable, Insert, Literal, ColumnRef, Aggregate, SelectItem, SubqueryRef, BoolOp, NotOp,
    SetOp, Explain, Analyze, SetFrames, Load,
)


def with_distinct_names(plan, columns):
    """A subquery's alias requalifies all its columns, so two of them with one name
    (t.pid and w.pid) would collide: the later ones are named pid_2, pid_3, ..."""
    names = []
    for c in columns:
        name, suffix = c.name, 2
        while name in names:
            name = f"{c.name}_{suffix}"
            suffix += 1
        names.append(name)
    if names == [c.name for c in columns]:
        return plan, columns
    plan = Project([c.ref for c in columns], plan,
                   [None if name == c.name else name for name, c in zip(names, columns)])
    return plan, [c if name == c.name else OutputColumn(None, name, c.definition)
                  for name, c in zip(names, columns)]


class OutputColumn:
    """A column a query produces: its qualifier in the plan (None for a bare
    name such as an aggregate result or an alias), its name and its type."""

    def __init__(self, qualifier, name, definition):
        self.qualifier = qualifier
        self.name = name
        self.definition = definition

    @property
    def ref(self):
        return AttrRef(self.qualifier, self.name)


class Output(typing.NamedTuple):
    """A select-list output: the column it reads, the name it is given (None to keep the
    column's own) and the column it becomes."""

    attr: AttrRef
    alias: typing.Optional[str]
    column: OutputColumn


class ScopeEntry:
    def __init__(self, name, columns):
        self.name = name
        self.columns: list[tuple[str, ColumnDef]] = columns


class Statement:
    def __init__(self, kind, **fields):
        self.kind = kind
        self.__dict__.update(fields)


class Analyzer:
    def __init__(self, tables):
        """tables: {name: [ColumnDef]}, the tables already in the catalog."""
        self.tables = tables
        self.created = {}

    def schema_of(self, name):
        if name in self.created:
            return self.created[name]
        return self.tables.get(name)

    def statement(self, stmt):
        if isinstance(stmt, CreateTable):
            return self.create_table(stmt)
        if isinstance(stmt, Insert):
            return self.insert(stmt)
        if isinstance(stmt, Explain):
            plan, columns = self.query(stmt.query)
            return Statement("explain", plan=plan, columns=columns, analyze=stmt.analyze)
        if isinstance(stmt, Analyze):
            if self.schema_of(stmt.table) is None:
                stmt.fail("name", f"unknown table '{stmt.table}'")
            return Statement("analyze", table=stmt.table)
        if isinstance(stmt, SetFrames):
            if stmt.name.lower() != "frames":
                stmt.fail("name", f"unknown setting '{stmt.name}', only frames can be set")
            if stmt.value < MIN_FRAMES:
                stmt.fail("type", f"frames must be at least {MIN_FRAMES} (a join needs B - 2 frames for its block)")
            return Statement("set", value=stmt.value)
        if isinstance(stmt, Load):
            if self.schema_of(stmt.table) is None:
                stmt.fail("name", f"unknown table '{stmt.table}'")
            return Statement("load", table=stmt.table, path=stmt.path)
        plan, columns = self.query(stmt)
        return Statement("query", plan=plan, columns=columns)

    def create_table(self, stmt):
        try:
            check_table_name(stmt.name, [*self.tables, *self.created])
        except CatalogError as e:
            stmt.fail("name", str(e))
        columns = []
        for c in stmt.columns:
            if any(c.name == d.name for d in columns):
                c.fail("name", f"duplicate column '{c.name}' in table '{stmt.name}'")
            if c.type == "VARCHAR" and not 1 <= c.length <= MAX_VARCHAR:
                c.fail("type", f"VARCHAR length must be between 1 and {MAX_VARCHAR}, got {c.length}")
            columns.append(ColumnDef(c.name, c.type, c.length))
        self.created[stmt.name] = columns
        return Statement("create", table=stmt.name, schema=columns)

    def insert(self, stmt):
        schema = self.schema_of(stmt.table)
        if schema is None:
            stmt.fail("name", f"unknown table '{stmt.table}'")
        if len(stmt.values) != len(schema):
            stmt.fail("schema", f"table '{stmt.table}' has {len(schema)} columns but {len(stmt.values)} "
                                f"values were given")
        for value, column in zip(stmt.values, schema):
            if value.type != column.type:
                value.fail("type", f"column '{column.name}' is {column.type_text()}, "
                                   f"got {value.type} value {value.text()}")
            if column.type == "INT" and not INT_MIN <= value.value <= INT_MAX:
                value.fail("type", f"{value.value} does not fit in a 4-byte INT")
            if column.type == "VARCHAR" and len(value.value.encode("utf-8")) > column.length:
                value.fail("type", f"{value.text()} is longer than VARCHAR({column.length}) column "
                                   f"'{column.name}'")
        return Statement("insert", table=stmt.table, values=tuple(v.value for v in stmt.values))

    def query(self, q):
        if isinstance(q, SetOp):
            left_plan, left_cols = self.query(q.left)
            right_plan, right_cols = self.query(q.right)
            what = q.op.replace("_", " ").upper()
            if len(left_cols) != len(right_cols):
                q.fail("schema", f"{what} inputs have {len(left_cols)} and {len(right_cols)} columns")
            for i, (a, b) in enumerate(zip(left_cols, right_cols)):
                if type_class(a.definition.type) != type_class(b.definition.type):
                    q.fail("schema", f"{what} column {i + 1} is {a.definition.type_text()} on the left "
                                     f"and {b.definition.type_text()} on the right")
            plan = BINARY_CLASSES[q.op](left_plan, right_plan)
            if q.order_by:
                plan = Sort([(self.output_column(item.expr, left_cols).ref, item.direction)
                             for item in q.order_by], plan)
            if q.limit is not None:
                plan = Limit(q.limit, plan)
            return plan, left_cols
        return BlockAnalyzer(self, q).translate()

    def output_column(self, expr, columns):
        """ORDER BY after a set operation names one of its output columns."""
        if isinstance(expr, Aggregate):
            expr.fail("aggregate", f"{expr.text()} cannot order the result of a set operation")
        if expr.qualifier is not None:
            expr.fail("name", f"ORDER BY {expr.text()}: the result of a set operation has no table "
                              f"'{expr.qualifier}', only column names")
        matches = [c for c in columns if c.name == expr.name]
        if not matches:
            expr.fail("name", f"ORDER BY {expr.text()} is not a column of the set operation's result")
        if len(matches) > 1:
            expr.fail("ambiguity", f"ORDER BY {expr.text()} matches more than one result column")
        return matches[0]


class BlockAnalyzer:
    def __init__(self, analyzer, block):
        self.analyzer = analyzer
        self.block = block
        self.scope = []

    def entry(self, name):
        for e in self.scope:
            if e.name == name:
                return e
        return None

    def add_ref(self, ref):
        if isinstance(ref, SubqueryRef):
            plan, columns = with_distinct_names(*self.analyzer.query(ref.query))
            name = ref.alias
            entry = ScopeEntry(name, [(c.name, c.definition) for c in columns])
            plan = Rename(name, plan)
        else:
            schema = self.analyzer.schema_of(ref.name)
            if schema is None:
                ref.fail("name", f"unknown table '{ref.name}'")
            name = ref.alias or ref.name
            entry = ScopeEntry(name, [(c.name, c) for c in schema])
            plan = RelRef(ref.name) if ref.alias is None else Rename(ref.alias, RelRef(ref.name))
        if self.entry(name) is not None:
            ref.fail("name", f"duplicate table name or alias '{name}' in FROM; give each one a distinct alias")
        self.scope.append(entry)
        return plan

    def translate_from(self):
        b = self.block
        plan = self.add_ref(b.from_refs[0])
        for i, ref in enumerate(b.from_refs[1:]):
            right = self.add_ref(ref)
            if b.join_conditions is None:
                plan = Times(plan, right)
            else:
                plan = Join(self.condition(b.join_conditions[i], "JOIN ... ON"), plan, right)
        return plan

    def resolve(self, ref):
        """Resolve a column against the FROM scope: (qualified AttrRef, ColumnDef)."""
        if ref.qualifier is not None:
            entry = self.entry(ref.qualifier)
            if entry is None:
                ref.fail("name", f"unknown table or alias '{ref.qualifier}' in {ref.text()}")
            matches = [definition for name, definition in entry.columns if name == ref.name]
            if not matches:
                ref.fail("name", f"unknown column '{ref.name}' in table '{ref.qualifier}'")
            if len(matches) > 1:
                ref.fail("ambiguity", f"column {ref.text()} is ambiguous: '{ref.qualifier}' has "
                                      f"{len(matches)} columns named '{ref.name}'")
            return AttrRef(entry.name, ref.name), matches[0]
        candidates = [(e.name, definition)
                      for e in self.scope for name, definition in e.columns if name == ref.name]
        if not candidates:
            ref.fail("name", f"unknown column '{ref.name}'")
        if len(candidates) > 1:
            names = ", ".join(f"{q}.{ref.name}" for q, _ in candidates)
            ref.fail("ambiguity", f"column '{ref.name}' is ambiguous: it could be {names}")
        return AttrRef(candidates[0][0], ref.name), candidates[0][1]

    def aggregate_type(self, agg):
        if agg.arg is None:
            return None, aggregate_result_definition(None, agg.func, None)
        attr, definition = self.resolve(agg.arg)
        if agg.func in ("sum", "avg") and definition.type == "VARCHAR":
            agg.fail("type", f"{agg.func.upper()} needs a numeric column, {attr.text} is {definition.type_text()}")
        return attr, aggregate_result_definition(None, agg.func, definition)

    def condition(self, cond, clause):
        """Translate a WHERE/ON condition; aggregates are not allowed there."""
        def operand(o):
            if isinstance(o, Aggregate):
                o.fail("aggregate", f"aggregate {o.text()} is not allowed in {clause}, which runs before grouping")
            return self.resolve(o)
        return self.translate_condition(cond, operand)

    def translate_condition(self, cond, operand):
        if isinstance(cond, BoolOp):
            left = self.translate_condition(cond.left, operand)
            right = self.translate_condition(cond.right, operand)
            return And(left, right) if cond.op == "and" else Or(left, right)
        if isinstance(cond, NotOp):
            return Not(self.translate_condition(cond.cond, operand))
        sides = []
        for o in (cond.left, cond.right):
            if isinstance(o, Literal):
                sides.append((Integer(o.value) if o.type == "INT" else String(o.value), o.type, o.text()))
            else:
                attr, definition = operand(o)
                sides.append((attr, definition.type, o.text()))
        (left, left_type, left_text), (right, right_type, right_text) = sides
        if type_class(left_type) != type_class(right_type):
            cond.fail("type", f"cannot compare {left_text} ({left_type}) with {right_text} ({right_type})")
        return PlanComparison(cond.op, left, right)

    def translate(self):
        b = self.block
        plan = self.translate_from()
        if b.where is not None:
            plan = Select(self.condition(b.where, "WHERE"), plan)

        items = self.expand_items()
        aggregating = b.group_by is not None or any(isinstance(i.expr, Aggregate) for i in items)
        if aggregating:
            plan, outputs = self.translate_grouping(plan, items)
        else:
            outputs = [self.select_output(item, *self.resolve(item.expr)) for item in items]
        outputs = self.unique_outputs(outputs)

        order_keys, hidden = self.translate_order_by(outputs, aggregating)
        columns = [out.attr for out in outputs] + [attr for attr, _ in hidden]
        names = [out.alias for out in outputs] + [None] * len(hidden)
        plan = Project(columns, plan, names)

        if b.distinct and not (aggregating and self.groups_all_projected):
            plan = Distinct(plan)
        if order_keys:
            plan = Sort(order_keys, plan)
            if hidden:
                plan = Project([out.column.ref for out in outputs], plan)
        if b.limit is not None:
            plan = Limit(b.limit, plan)
        return plan, [out.column for out in outputs]

    def select_output(self, item, attr, definition):
        """The output of a select item that reads column attr."""
        if item.alias is None:
            return Output(attr, None, OutputColumn(attr.relation, attr.attr, definition))
        return Output(attr, item.alias, OutputColumn(None, item.alias, definition))

    def expand_items(self):
        b = self.block
        if not b.star:
            return b.items
        # SELECT * : every column of every FROM entry, FROM order then schema order.
        return [SelectItem(ColumnRef(e.name, name, b.line, b.col), None, b.line, b.col)
                for e in self.scope for name, _ in e.columns]

    def unique_outputs(self, outputs):
        """A schema cannot hold two columns with one qualified name, so a column
        selected again without an alias is named <column>_2 (then _3, ...)."""
        seen = set()
        result = []
        for i, (attr, alias, out) in enumerate(outputs):
            key = (out.qualifier, out.name)
            if key in seen:
                if alias is not None:
                    self.block.items[i].fail("schema", f"the select list produces two columns named {out.ref.text}")
                suffix = 2
                while (None, f"{out.name}_{suffix}") in seen:
                    suffix += 1
                alias = f"{out.name}_{suffix}"
                out = OutputColumn(None, alias, out.definition)
                key = (None, alias)
            seen.add(key)
            result.append(Output(attr, alias, out))
        return result

    def group_by_target(self, col):
        """A GROUP BY name is a FROM column first and otherwise, as in PostgreSQL, the
        column a select-list alias names."""
        if col.qualifier is None and not any(name == col.name for e in self.scope for name, _ in e.columns):
            aliased = [i.expr for i in self.block.items if i.alias == col.name and isinstance(i.expr, ColumnRef)]
            if len(aliased) == 1:
                return aliased[0]
        return col

    def translate_grouping(self, plan, items):
        b = self.block
        self.keys = []
        for col in b.group_by or []:
            attr, definition = self.resolve(self.group_by_target(col))
            if attr not in [k for k, _ in self.keys]:
                self.keys.append((attr, definition))
        self.aggregates: list[tuple[PlanAggregate, ColumnDef]] = []

        outputs = []
        for item in items:
            if isinstance(item.expr, Aggregate):
                name, definition = self.aggregate(item.expr, item.alias)
                outputs.append(Output(AttrRef(None, name), None, OutputColumn(None, name, definition)))
            else:
                outputs.append(self.select_output(item, *self.grouping_column(item.expr, "the select list")))

        having = None
        if b.having is not None:
            def operand(o):
                if isinstance(o, Aggregate):
                    name, definition = self.aggregate(o, None)
                    return AttrRef(None, name), definition
                return self.grouping_column(o, "HAVING")
            having = self.translate_condition(b.having, operand)

        for item in b.order_by:
            if isinstance(item.expr, Aggregate):
                self.aggregate(item.expr, None)

        plan = GroupBy([k for k, _ in self.keys], [aggregate for aggregate, _ in self.aggregates], plan)
        if having is not None:
            plan = Select(having, plan)
        projected = {out.attr for out in outputs}
        self.groups_all_projected = all(k in projected for k, _ in self.keys)
        return plan, outputs

    def grouping_column(self, ref, clause):
        attr, definition = self.resolve(ref)
        if attr not in [k for k, _ in self.keys]:
            ref.fail("aggregate", f"column {attr.text} in {clause} must appear in GROUP BY or be used "
                                  f"in an aggregate function")
        return attr, definition

    def aggregate(self, agg, alias):
        """The output name of an aggregate, adding it to the group_by if new."""
        arg, definition = self.aggregate_type(agg)
        if alias is None:
            for existing, existing_definition in self.aggregates:
                if existing.func == agg.func and existing.arg == arg:
                    return existing.name, existing_definition
            alias = default_aggregate_name(agg.func, arg)
            taken = ({existing.name for existing, _ in self.aggregates}
                     | {i.alias for i in self.block.items if i.alias})
            base, suffix = alias, 2
            while alias in taken:
                alias = f"{base}_{suffix}"
                suffix += 1
        definition = ColumnDef(alias, definition.type, definition.length)
        self.aggregates.append((PlanAggregate(agg.func, arg, alias), definition))
        return alias, definition

    def translate_order_by(self, outputs, aggregating):
        """Sort keys, and columns the sort needs that the select list drops."""
        b = self.block
        keys = []
        hidden = []
        for item in b.order_by:
            expr = item.expr
            if isinstance(expr, Aggregate):
                if not aggregating:
                    expr.fail("aggregate", f"{expr.text()} in ORDER BY needs a query with GROUP BY or an "
                                           f"aggregate in its select list")
                name, _ = self.aggregate(expr, None)
                attr = AttrRef(None, name)
                visible = attr
            else:
                # An unqualified name is first an output column name (an alias, an
                # aggregate's name, or a selected column's own name), as in SQL.
                named = [out.column for out in outputs if expr.qualifier is None and out.column.name == expr.name]
                if len(named) > 1:
                    expr.fail("ambiguity", f"ORDER BY {expr.name} is ambiguous: {len(named)} output "
                                           f"columns are named {expr.name}")
                if named:
                    keys.append((named[0].ref, item.direction))
                    continue
                attr, _ = (self.grouping_column(expr, "ORDER BY") if aggregating
                           else self.resolve(expr))
                visible = next((out.column.ref for out in outputs if out.attr == attr), None)
            if visible is None or visible not in [out.column.ref for out in outputs]:
                if b.distinct:
                    expr.fail("schema", f"with SELECT DISTINCT, ORDER BY {expr.text()} must appear in "
                                        f"the select list")
                if attr not in [a for a, _ in hidden]:
                    hidden.append((attr, item.direction))
                keys.append((attr, item.direction))
            else:
                keys.append((visible, item.direction))
        return keys, hidden
