"""Relational algebra tree: plan nodes, conditions, JSON form and printers.

The same tree is the logical plan of the whole system. JSON is the contract
between modules, so every node can be written with to_json and read back with
from_json.
"""

OP_NAMES = {"=": "Eq", "!=": "Neq", "<": "Lt", "<=": "Leq", ">": "Gt", ">=": "Geq"}


def quote_string(value):
    """A string literal as SQL and the relational algebra write it: in single quotes, '' for '."""
    return "'" + value.replace("'", "''") + "'"


class AttrRef:
    def __init__(self, relation, attr):
        self.relation = relation
        self.attr = attr

    def to_string(self):
        return f"Attr({self.text})"

    @property
    def text(self):
        return f"{self.relation}.{self.attr}" if self.relation else self.attr

    def __eq__(self, other):
        return (isinstance(other, AttrRef) and self.relation == other.relation
                and self.attr == other.attr)

    def __hash__(self):
        return hash((self.relation, self.attr))

    def __repr__(self):
        return self.text


class Integer:
    def __init__(self, v): self.value = v
    def to_string(self): return f"Num({self.value})"

    @property
    def text(self):
        return str(self.value)


class String:
    def __init__(self, v): self.value = v
    def to_string(self): return f"Str({self.value!r})"

    @property
    def text(self):
        return quote_string(self.value)


class Comparison:
    def __init__(self, op, l, r): self.op = op; self.left = l; self.right = r

    def to_string(self):
        return f"{OP_NAMES[self.op]}({self.left.to_string()}, {self.right.to_string()})"


class And:
    def __init__(self, l, r): self.left = l; self.right = r
    def to_string(self): return f"And({self.left.to_string()}, {self.right.to_string()})"


class Or:
    def __init__(self, l, r): self.left = l; self.right = r
    def to_string(self): return f"Or({self.left.to_string()}, {self.right.to_string()})"


class Not:
    def __init__(self, c): self.cond = c
    def to_string(self): return f"Not({self.cond.to_string()})"


CONDITION_PRECEDENCE = {Or: 1, And: 2, Not: 3, Comparison: 4}


def condition_text(cond):
    """Write a condition in the concrete syntax parse_condition reads back."""
    if isinstance(cond, Comparison):
        return f"{cond.left.text} {cond.op} {cond.right.text}"
    if isinstance(cond, Not):
        inner = condition_text(cond.cond)
        if CONDITION_PRECEDENCE[type(cond.cond)] < CONDITION_PRECEDENCE[Not]:
            inner = f"({inner})"
        return f"not {inner}"
    word = "and" if isinstance(cond, And) else "or"
    level = CONDITION_PRECEDENCE[type(cond)]
    left = condition_text(cond.left)
    right = condition_text(cond.right)
    if CONDITION_PRECEDENCE[type(cond.left)] < level:
        left = f"({left})"
    # Binary connectives are left-associative, so an equal-precedence right
    # operand needs parentheses to come back as the same tree.
    if CONDITION_PRECEDENCE[type(cond.right)] <= level:
        right = f"({right})"
    return f"{left} {word} {right}"


def condition_attrs(cond):
    """Every attribute reference in the condition, in textual order."""
    if isinstance(cond, Comparison):
        return [o for o in (cond.left, cond.right) if isinstance(o, AttrRef)]
    if isinstance(cond, Not):
        return condition_attrs(cond.cond)
    return condition_attrs(cond.left) + condition_attrs(cond.right)


def map_condition_attrs(cond, fn):
    """Copy of the condition with every AttrRef replaced by fn(AttrRef)."""
    if isinstance(cond, Comparison):
        left = fn(cond.left) if isinstance(cond.left, AttrRef) else cond.left
        right = fn(cond.right) if isinstance(cond.right, AttrRef) else cond.right
        return Comparison(cond.op, left, right)
    if isinstance(cond, Not):
        return Not(map_condition_attrs(cond.cond, fn))
    return type(cond)(map_condition_attrs(cond.left, fn), map_condition_attrs(cond.right, fn))


def conjuncts(cond):
    if isinstance(cond, And):
        return conjuncts(cond.left) + conjuncts(cond.right)
    return [cond]


def is_equality(cond):
    """Whether cond is column = column."""
    return (isinstance(cond, Comparison) and cond.op == "=" and isinstance(cond.left, AttrRef)
            and isinstance(cond.right, AttrRef))


def conjunction(conds):
    result = conds[0]
    for c in conds[1:]:
        result = And(result, c)
    return result


class Node:
    def label(self): return self.__class__.__name__
    def children(self): return []


class RelRef(Node):
    def __init__(self, name): self.name = name
    def label(self): return f"Relation({self.name})"


class Select(Node):
    def __init__(self, cond, input): self.cond = cond; self.input = input
    def label(self): return f"Select(cond={self.cond.to_string()})"
    def children(self): return [self.input]


class Project(Node):
    def __init__(self, attrs, input, names=None):
        """names: one output name per attribute, None where it keeps its own name."""
        self.attrs = attrs
        self.input = input
        self.names = [None] * len(attrs) if names is None else names

    @property
    def renames(self):
        return any(name is not None for name in self.names)

    @property
    def columns_text(self):
        return ", ".join(a.text if name is None else f"{a.text} AS {name}" for a, name in zip(self.attrs, self.names))

    def label(self):
        return f"Project(attrs=[{self.columns_text}])"

    def children(self): return [self.input]


class Rename(Node):
    def __init__(self, new_name, input): self.new_name = new_name; self.input = input
    def label(self): return f"Rename({self.new_name})"
    def children(self): return [self.input]


class Distinct(Node):
    def __init__(self, input): self.input = input
    def children(self): return [self.input]


AGGREGATE_FUNCTIONS = ("count", "sum", "avg", "min", "max")


class Aggregate:
    def __init__(self, func, arg, name):
        self.func = func
        self.arg = arg
        self.name = name

    @property
    def arg_text(self):
        return "*" if self.arg is None else self.arg.text

    @property
    def text(self):
        return f"{self.func}({self.arg_text}) AS {self.name}"

    def __eq__(self, other):
        return (isinstance(other, Aggregate) and self.func == other.func
                and self.arg == other.arg and self.name == other.name)


def default_aggregate_name(func, arg):
    if arg is None:
        return f"{func}_star"
    return f"{func}_{arg.text.replace('.', '_')}"


class GroupBy(Node):
    def __init__(self, keys, aggregates, input):
        self.keys = keys
        self.aggregates = aggregates
        self.input = input

    @property
    def aggregates_text(self):
        return ", ".join(a.text for a in self.aggregates)

    def label(self):
        return f"GroupBy(keys=[{', '.join(k.text for k in self.keys)}], aggregates=[{self.aggregates_text}])"

    def children(self): return [self.input]


def sort_keys_text(keys):
    """[(AttrRef, "asc"|"desc")] as "R.a asc, R.b desc"."""
    return ", ".join(f"{k.text} {d}" for k, d in keys)


class Sort(Node):
    def __init__(self, keys, input): self.keys = keys; self.input = input

    def label(self):
        return f"Sort(keys=[{sort_keys_text(self.keys)}])"

    def children(self): return [self.input]


class Limit(Node):
    def __init__(self, n, input): self.n = n; self.input = input
    def label(self): return f"Limit({self.n})"
    def children(self): return [self.input]


class Binary(Node):
    def __init__(self, l, r): self.left = l; self.right = r
    def children(self): return [self.left, self.right]


class Union(Binary): pass
class UnionAll(Binary): pass
class Intersect(Binary): pass
class Minus(Binary): pass
class Times(Binary): pass


class Join(Node):
    def __init__(self, cond, l, r): self.cond = cond; self.left = l; self.right = r
    def label(self): return f"Join(cond={self.cond.to_string()})"
    def children(self): return [self.left, self.right]


class RelationDef:
    def __init__(self, name, attrs, tuples):
        self.name = name
        self.attrs = attrs
        self.tuples = tuples


class Program:
    def __init__(self, relations, query):
        self.relations = relations
        self.query = query


BINARY_OPS = {Union: "union", UnionAll: "union_all", Intersect: "intersect",
              Minus: "except", Times: "cross"}
BINARY_CLASSES = {name: cls for cls, name in BINARY_OPS.items()}


def to_json(node):
    if isinstance(node, RelRef):
        return {"op": "scan", "table": node.name}
    if isinstance(node, Select):
        return {"op": "select", "condition": condition_text(node.cond), "child": to_json(node.input)}
    if isinstance(node, Project):
        out = {"op": "project", "columns": [a.text for a in node.attrs]}
        if node.renames:
            out["names"] = node.names
        out["child"] = to_json(node.input)
        return out
    if isinstance(node, Rename):
        return {"op": "rename", "to": node.new_name, "child": to_json(node.input)}
    if isinstance(node, Distinct):
        return {"op": "distinct", "child": to_json(node.input)}
    if isinstance(node, GroupBy):
        return {"op": "group_by", "keys": [k.text for k in node.keys],
                "aggregates": [[a.func, a.arg_text, a.name] for a in node.aggregates],
                "child": to_json(node.input)}
    if isinstance(node, Sort):
        return {"op": "sort", "keys": [[k.text, d] for k, d in node.keys], "child": to_json(node.input)}
    if isinstance(node, Limit):
        return {"op": "limit", "n": node.n, "child": to_json(node.input)}
    if isinstance(node, Join):
        return {"op": "join", "condition": condition_text(node.cond),
                "left": to_json(node.left), "right": to_json(node.right)}
    return {"op": BINARY_OPS[type(node)], "left": to_json(node.left), "right": to_json(node.right)}


def from_json(obj):
    from .parser import parse_condition, parse_column

    op = obj["op"]
    if op == "scan":
        return RelRef(obj["table"])
    if op == "select":
        return Select(parse_condition(obj["condition"]), from_json(obj["child"]))
    if op == "project":
        return Project([parse_column(c) for c in obj["columns"]], from_json(obj["child"]),
                       obj.get("names"))
    if op == "rename":
        return Rename(obj["to"], from_json(obj["child"]))
    if op == "distinct":
        return Distinct(from_json(obj["child"]))
    if op == "group_by":
        aggregates = []
        for func, arg, name in obj["aggregates"]:
            aggregates.append(Aggregate(func, None if arg == "*" else parse_column(arg), name))
        return GroupBy([parse_column(k) for k in obj["keys"]], aggregates, from_json(obj["child"]))
    if op == "sort":
        keys = [(parse_column(c), d) for c, d in obj["keys"]]
        if any(d not in ("asc", "desc") for _, d in keys):
            raise ValueError(f"sort directions must be \"asc\" or \"desc\", got {[d for _, d in keys]}")
        return Sort(keys, from_json(obj["child"]))
    if op == "limit":
        if not isinstance(obj["n"], int) or obj["n"] < 0:
            raise ValueError(f"limit n must be an integer >= 0, got {obj['n']!r}")
        return Limit(obj["n"], from_json(obj["child"]))
    if op == "join":
        return Join(parse_condition(obj["condition"]), from_json(obj["left"]), from_json(obj["right"]))
    return BINARY_CLASSES[op](from_json(obj["left"]), from_json(obj["right"]))


def render_tree(node):
    """The tree drawn with box-drawing characters, one node per line."""
    lines = []

    def walk(n, prefix, is_last, is_root):
        if is_root:
            lines.append(n.label())
        else:
            lines.append(prefix + ("└── " if is_last else "├── ") + n.label())
        kids = n.children()
        new_prefix = "" if is_root else prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(kids):
            walk(child, new_prefix, i == len(kids) - 1, False)

    walk(node, "", True, True)
    return "\n".join(lines)


def explain_label(node):
    if isinstance(node, RelRef):
        return f"scan {node.name}"
    if isinstance(node, Select):
        return f"select ({condition_text(node.cond)})"
    if isinstance(node, Project):
        return f"project [{node.columns_text}]"
    if isinstance(node, Rename):
        return f"rename {node.new_name}"
    if isinstance(node, Distinct):
        return "distinct"
    if isinstance(node, GroupBy):
        return f"group_by keys [{', '.join(k.text for k in node.keys)}] aggregates [{node.aggregates_text}]"
    if isinstance(node, Sort):
        return f"sort [{sort_keys_text(node.keys)}]"
    if isinstance(node, Limit):
        return f"limit {node.n}"
    if isinstance(node, Join):
        return f"join ({condition_text(node.cond)})"
    return BINARY_OPS[type(node)]


def explain_tree(node):
    """Indented plan, two spaces per level."""
    lines = []

    def walk(n, depth):
        lines.append("  " * depth + explain_label(n))
        for child in n.children():
            walk(child, depth + 1)

    walk(node, 0)
    return "\n".join(lines)
