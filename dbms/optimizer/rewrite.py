"""Rule-based rewrites of the logical plan (DESIGN_M4.md, section 3).

  1 split a conjunctive select into stacked selects
  2 push a select below a join or cross when it uses one side only
  3 turn cross + select of a two-sided equality into a join
  4 push a select below rename, union and union_all (and project, distinct
    and sort, which a selection also commutes with), rewriting names; a select
    only passes through rename, project, distinct and sort when it can then
    move past something below them
  5 push a select below group_by when it uses grouping columns only
  6 insert projections below join, cross, group_by and sort keeping only the
    columns needed above them
  7 remove projections that drop nothing and collapse adjacent selects
"""

import enum

from ..ra.operators import Column, resolve_attr, EvalError
from ..ra.tree import (
    RelRef, Select, Project, Rename, Distinct, GroupBy, Sort, Limit, Union, UnionAll, Intersect, Minus,
    Times, Join, AttrRef, condition_attrs, map_condition_attrs, conjuncts, conjunction, is_equality,
)


class Rule(enum.IntEnum):
    SPLIT_CONJUNCTIONS = 1
    PUSH_SELECT_BELOW_JOIN = 2
    CROSS_TO_JOIN = 3
    PUSH_SELECT_BELOW_RENAME_UNION = 4
    PUSH_SELECT_BELOW_GROUP_BY = 5
    PUSH_PROJECTIONS_DOWN = 6
    REMOVE_NO_OP_PROJECTIONS_MERGE_SELECTS = 7


ALL_RULES = frozenset(Rule)


def output_columns(node, schemas):
    """The columns a logical node produces, as references its parent can use."""
    if isinstance(node, RelRef):
        return [AttrRef(node.name, c.name) for c in schemas(node.name)]
    if isinstance(node, Rename):
        return [AttrRef(node.new_name, c.attr) for c in output_columns(node.input, schemas)]
    if isinstance(node, Project):
        child = output_columns(node.input, schemas)
        out = []
        for a, name in zip(node.attrs, node.names):
            if name is not None:
                out.append(AttrRef(None, name))
            else:
                out.append(child[position(a, child)])
        return out
    if isinstance(node, GroupBy):
        child = output_columns(node.input, schemas)
        return [child[position(k, child)] for k in node.keys] + [AttrRef(None, a.name) for a in node.aggregates]
    if isinstance(node, (Join, Times)):
        return output_columns(node.left, schemas) + output_columns(node.right, schemas)
    if isinstance(node, (Union, UnionAll, Intersect, Minus)):
        return output_columns(node.left, schemas)
    return output_columns(node.input, schemas)


def position(ref, columns):
    return resolve_attr(ref, [Column(c.relation, c.attr) for c in columns])


def try_position(ref, columns):
    try:
        return position(ref, columns)
    except EvalError:
        return None


def candidates(ref, columns):
    """Every column ref could mean, so that pruning never drops one in doubt."""
    i = try_position(ref, columns)
    if i is not None:
        return [i]
    return [j for j, c in enumerate(columns)
            if c.attr == ref.attr and (ref.relation is None or c.relation == ref.relation)]


class Rewriter:
    def __init__(self, schemas, rules=ALL_RULES):
        self.schemas = schemas
        self.rules = rules

    def columns_of(self, node):
        return output_columns(node, self.schemas)

    def rewrite(self, node):
        if Rule.SPLIT_CONJUNCTIONS in self.rules:
            node = self.split(node)
        node = self.push_all(node)
        if Rule.PUSH_PROJECTIONS_DOWN in self.rules:
            node = self.prune(node, None)
        if Rule.REMOVE_NO_OP_PROJECTIONS_MERGE_SELECTS in self.rules:
            node = self.tidy(node)
        return node

    def split(self, node):
        node = rebuild(node, [self.split(c) for c in node.children()])
        if isinstance(node, Select):
            parts = conjuncts(node.cond)
            result = node.input
            for part in reversed(parts):
                result = Select(part, result)
            return result
        return node

    def push_all(self, node):
        node = rebuild(node, [self.push_all(c) for c in node.children()])
        if isinstance(node, Select):
            return self.push(node.cond, node.input)
        return node

    def push(self, cond, child):
        moved = self.try_push(cond, child)
        return Select(cond, child) if moved is None else moved

    def try_push(self, cond, child):
        refs = condition_attrs(cond)
        if isinstance(child, (Join, Times)):
            left_columns, right_columns = self.columns_of(child.left), self.columns_of(child.right)
            in_left = all(try_position(r, left_columns) is not None for r in refs)
            in_right = all(try_position(r, right_columns) is not None for r in refs)
            if Rule.PUSH_SELECT_BELOW_JOIN in self.rules and in_left and not in_right:
                return rebuild(child, [self.push(cond, child.left), child.right])
            if Rule.PUSH_SELECT_BELOW_JOIN in self.rules and in_right and not in_left:
                return rebuild(child, [child.left, self.push(cond, child.right)])
            if (Rule.CROSS_TO_JOIN in self.rules and isinstance(child, Times) and is_equality(cond)
                    and not in_left and not in_right):
                return Join(cond, child.left, child.right)
            return None
        if isinstance(child, Select):
            inner = self.try_push(cond, child.input)
            return None if inner is None else Select(child.cond, inner)
        if Rule.PUSH_SELECT_BELOW_RENAME_UNION in self.rules and isinstance(child, (Rename, Project, Distinct, Sort)):
            # These only pass a select through: it moves below them when it can
            # then get past something further down, so a select over a plain
            # renamed scan stays where it is.
            mapped = self.map_down(cond, child)
            inner = None if mapped is None else self.try_push(mapped, child.input)
            return None if inner is None else rebuild(child, [inner])
        if Rule.PUSH_SELECT_BELOW_RENAME_UNION in self.rules and isinstance(child, (Union, UnionAll)):
            out = self.columns_of(child)
            indices = [try_position(r, out) for r in refs]
            if None in indices:
                return None
            sides = []
            for side in (child.left, child.right):
                side_columns = self.columns_of(side)
                mapping = {r: side_columns[i] for r, i in zip(refs, indices)}
                sides.append(self.push(map_condition_attrs(cond, mapping.get), side))
            return rebuild(child, sides)
        if Rule.PUSH_SELECT_BELOW_GROUP_BY in self.rules and isinstance(child, GroupBy) and child.keys:
            # Without keys a group_by returns one row even for an empty input, so
            # a select (even a constant one) must stay above it.
            out = self.columns_of(child)
            indices = [try_position(r, out) for r in refs]
            if None in indices or any(i >= len(child.keys) for i in indices):
                return None
            mapping = {r: out[i] for r, i in zip(refs, indices)}
            return GroupBy(child.keys, child.aggregates, self.push(map_condition_attrs(cond, mapping.get), child.input))
        return None

    def map_down(self, cond, child):
        """Rewrite a condition over child's output into one over child's input."""
        out = self.columns_of(child)
        below = self.columns_of(child.input)
        mapping = {}
        for r in condition_attrs(cond):
            i = try_position(r, out)
            if i is None:
                return None
            if isinstance(child, Project):
                mapping[r] = below[position(child.attrs[i], below)]
            else:
                mapping[r] = below[i]
        return map_condition_attrs(cond, mapping.get)

    def prune(self, node, needed):
        """needed: references (to node's output) its ancestors use; None means all."""
        if isinstance(node, RelRef):
            return node
        if isinstance(node, Project):
            return Project(node.attrs, self.prune(node.input, node.attrs), node.names)
        if isinstance(node, Select):
            wanted = None if needed is None else needed + condition_attrs(node.cond)
            return Select(node.cond, self.prune(node.input, wanted))
        if isinstance(node, Rename):
            if needed is None:
                return Rename(node.new_name, self.prune(node.input, None))
            out, below = self.columns_of(node), self.columns_of(node.input)
            return Rename(node.new_name, self.prune(node.input, [below[position(r, out)] for r in needed]))
        if isinstance(node, Limit):
            return Limit(node.n, self.prune(node.input, needed))
        if isinstance(node, (Join, Times)):
            wanted = None if needed is None else needed + (
                condition_attrs(node.cond) if isinstance(node, Join) else [])
            sides = [self.narrow(side, wanted) for side in (node.left, node.right)]
            return rebuild(node, sides)
        if isinstance(node, GroupBy):
            wanted = node.keys + [a.arg for a in node.aggregates if a.arg is not None]
            return GroupBy(node.keys, node.aggregates, self.narrow(node.input, wanted))
        if isinstance(node, Sort):
            wanted = None if needed is None else needed + [k for k, _ in node.keys]
            return Sort(node.keys, self.narrow(node.input, wanted))
        return rebuild(node, [self.prune(c, None) for c in node.children()])

    def narrow(self, child, wanted):
        """Prune child and put a projection on top that keeps only `wanted`
        (those of them that are child's columns), in child's column order."""
        columns = self.columns_of(child)
        if wanted is None:
            return self.prune(child, None)
        keep = sorted({i for r in wanted for i in candidates(r, columns)})
        pruned = self.prune(child, [columns[i] for i in keep])
        if len(keep) == len(columns) or not keep:
            return pruned
        return Project([columns[i] for i in keep], pruned)

    def tidy(self, node):
        node = rebuild(node, [self.tidy(c) for c in node.children()])
        if isinstance(node, Project) and not node.renames and node.attrs == self.columns_of(node.input):
            return node.input
        if isinstance(node, Select) and isinstance(node.input, Select):
            return Select(conjunction([node.cond, node.input.cond]), node.input.input)
        return node


def rebuild(node, children):
    """Copy of node with new children (nodes are treated as immutable)."""
    if isinstance(node, RelRef):
        return node
    if isinstance(node, Select):
        return Select(node.cond, children[0])
    if isinstance(node, Project):
        return Project(node.attrs, children[0], node.names)
    if isinstance(node, Rename):
        return Rename(node.new_name, children[0])
    if isinstance(node, Distinct):
        return Distinct(children[0])
    if isinstance(node, GroupBy):
        return GroupBy(node.keys, node.aggregates, children[0])
    if isinstance(node, Sort):
        return Sort(node.keys, children[0])
    if isinstance(node, Limit):
        return Limit(node.n, children[0])
    if isinstance(node, Join):
        return Join(node.cond, children[0], children[1])
    return type(node)(children[0], children[1])
