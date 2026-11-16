"""SQL syntax tree. Every node keeps the line and column where it starts."""

import typing

from ..ra.tree import quote_string


class SQLError(Exception):
    def __init__(self, category, message, line, col):
        self.category = category
        self.message = message
        self.line = line
        self.col = col
        super().__init__(f"error: {category}: {message} at line {line}, column {col}")


class Node:
    def __init__(self, line, col):
        self.line = line
        self.col = col

    def fail(self, category, message):
        raise SQLError(category, message, self.line, self.col)


class CreateTable(Node):
    def __init__(self, name, columns, line, col):
        super().__init__(line, col)
        self.name = name
        self.columns: list[ColumnDefinition] = columns


class ColumnDefinition(Node):
    def __init__(self, name, type, length, line, col):
        super().__init__(line, col)
        self.name = name
        self.type = type
        self.length = length


class Insert(Node):
    def __init__(self, table, values, line, col):
        super().__init__(line, col)
        self.table = table
        self.values: list[Literal] = values


class Literal(Node):
    def __init__(self, value, line, col):
        super().__init__(line, col)
        self.value = value

    @property
    def type(self):
        return "VARCHAR" if isinstance(self.value, str) else "INT"

    def text(self):
        return quote_string(self.value) if isinstance(self.value, str) else str(self.value)


class ColumnRef(Node):
    def __init__(self, qualifier, name, line, col):
        super().__init__(line, col)
        self.qualifier = qualifier
        self.name = name

    def text(self):
        return f"{self.qualifier}.{self.name}" if self.qualifier else self.name


class Aggregate(Node):
    def __init__(self, func, arg, line, col):
        super().__init__(line, col)
        self.func: typing.Literal["count", "sum", "avg", "min", "max"] = func
        self.arg: typing.Optional[ColumnRef] = arg

    def text(self):
        return f"{self.func.upper()}({'*' if self.arg is None else self.arg.text()})"


class SelectItem(Node):
    def __init__(self, expr, alias, line, col):
        super().__init__(line, col)
        self.expr: typing.Union[ColumnRef, Aggregate] = expr
        self.alias = alias


class TableRef(Node):
    def __init__(self, name, alias, line, col):
        super().__init__(line, col)
        self.name = name
        self.alias = alias


class SubqueryRef(Node):
    def __init__(self, query, alias, line, col):
        super().__init__(line, col)
        self.query = query
        self.alias = alias


class Comparison(Node):
    def __init__(self, op, left, right, line, col):
        super().__init__(line, col)
        self.op = op
        self.left = left
        self.right = right


class BoolOp(Node):
    def __init__(self, op, left, right, line, col):
        super().__init__(line, col)
        self.op: typing.Literal["and", "or"] = op
        self.left = left
        self.right = right


class NotOp(Node):
    def __init__(self, cond, line, col):
        super().__init__(line, col)
        self.cond = cond


class OrderItem(Node):
    def __init__(self, expr, direction, line, col):
        super().__init__(line, col)
        self.expr = expr
        self.direction: typing.Literal["asc", "desc"] = direction


class SelectBlock(Node):
    def __init__(self, line, col):
        super().__init__(line, col)
        self.distinct = False
        self.star = False
        self.items = []
        self.from_refs = []
        self.join_conditions = None     # None for a comma list, else one condition per JOIN
        self.where = None
        self.group_by = None
        self.having = None
        self.order_by = []
        self.limit = None
        self.parenthesized = False


class SetOp(Node):
    def __init__(self, op, left, right, line, col):
        super().__init__(line, col)
        self.op: typing.Literal["union", "union_all", "intersect", "except"] = op
        self.left = left
        self.right = right
        self.order_by = []              # ORDER BY and LIMIT written after the last block
        self.limit = None               # apply to the whole chain, as in standard SQL


class Explain(Node):
    def __init__(self, query, analyze, line, col):
        super().__init__(line, col)
        self.query = query
        self.analyze = analyze


class Analyze(Node):
    def __init__(self, table, line, col):
        super().__init__(line, col)
        self.table = table


class SetFrames(Node):
    def __init__(self, name, value, line, col):
        super().__init__(line, col)
        self.name = name
        self.value = value


class Load(Node):
    def __init__(self, table, path, line, col):
        super().__init__(line, col)
        self.table = table
        self.path = path
