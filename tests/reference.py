"""A deliberately naive in-memory evaluator for physical plans, used as a test oracle."""
from collections import Counter

from dbms.ra.operators import Column, compile_condition
from dbms.ra.parser import parse_condition, parse_column
from dbms.ra.tree import default_aggregate_name


class Rel:
    def __init__(self, columns, types, rows):
        self.columns = columns
        self.types = types
        self.rows = rows


def resolve(columns, text):
    ref = parse_column(text)
    matches = [i for i, c in enumerate(columns)
               if c.attr_name == ref.attr and (ref.relation is None or c.rel_name == ref.relation)]
    assert len(matches) == 1, (text, columns)
    return matches[0]


def evaluate(plan, tables):
    """tables: name -> (column names, types, rows)."""
    op = plan["op"]
    if op == "seqscan":
        names, types, rows = tables[plan["table"]]
        return Rel([Column(plan["table"], n) for n in names], list(types), list(rows))
    if op in ("filter", "project", "rename", "distinct", "sort", "group_by", "limit"):
        child = evaluate(plan["child"], tables)
        if op == "filter":
            check = compile_condition(parse_condition(plan["condition"]), child.columns, child.types)
            return Rel(child.columns, child.types, [r for r in child.rows if check(r)])
        if op == "project":
            idx = [resolve(child.columns, c) for c in plan["columns"]]
            names = plan.get("names") or [None] * len(idx)
            cols = [child.columns[i] if names[j] is None else Column(None, names[j]) for j, i in enumerate(idx)]
            return Rel(cols, [child.types[i] for i in idx], [tuple(r[i] for i in idx) for r in child.rows])
        if op == "rename":
            return Rel([Column(plan["to"], c.attr_name) for c in child.columns], child.types, child.rows)
        if op == "distinct":
            return Rel(child.columns, child.types, list(dict.fromkeys(child.rows)))
        if op == "sort":
            rows = list(child.rows)
            for col, direction in reversed(plan["keys"]):
                i = resolve(child.columns, col)
                rows.sort(key=lambda r: r[i], reverse=direction == "desc")
            return Rel(child.columns, child.types, rows)
        if op == "limit":
            return Rel(child.columns, child.types, child.rows[:plan["n"]])
        keys = [resolve(child.columns, k) for k in plan["keys"]]
        groups = {}
        for r in child.rows:
            groups.setdefault(tuple(r[i] for i in keys), []).append(r)
        if not keys and not groups:
            groups[()] = []
        aggs = []
        for agg in plan["aggregates"]:
            func, arg = agg[0], agg[1]
            aggs.append((func, None if arg == "*" else resolve(child.columns, arg)))
        out = []
        for key, members in groups.items():
            values = list(key)
            for func, i in aggs:
                vals = [m[i] for m in members] if i is not None else members
                if func == "count":
                    values.append(len(members))
                elif func == "sum":
                    values.append(sum(vals))
                elif func == "avg":
                    values.append(sum(vals) / len(vals))
                elif func == "min":
                    values.append(min(vals))
                else:
                    values.append(max(vals))
            out.append(tuple(values))
        names = [default_aggregate_name(a[0], None if a[1] == "*" else parse_column(a[1]))
                 if len(a) == 2 else a[2] for a in plan["aggregates"]]
        cols = [child.columns[i] for i in keys] + [Column(None, n) for n in names]
        return Rel(cols, [child.types[i] for i in keys] + ["INT"] * len(names), out)
    left = evaluate(plan["left"], tables)
    right = evaluate(plan["right"], tables)
    if op in ("nested_loop_join", "sort_merge_join", "hash_join"):
        cols = left.columns + right.columns
        types = left.types + right.types
        check = (compile_condition(parse_condition(plan["condition"]), cols, types)
                 if "condition" in plan else (lambda row: True))
        return Rel(cols, types, [l + r for l in left.rows for r in right.rows if check(l + r)])
    if op == "union_all":
        return Rel(left.columns, left.types, left.rows + right.rows)
    if op == "union":
        return Rel(left.columns, left.types, list(dict.fromkeys(left.rows + right.rows)))
    right_set = set(right.rows)
    if op == "intersect":
        return Rel(left.columns, left.types, [r for r in dict.fromkeys(left.rows) if r in right_set])
    return Rel(left.columns, left.types, [r for r in dict.fromkeys(left.rows) if r not in right_set])


def bag(rows):
    return Counter(tuple(r) for r in rows)
