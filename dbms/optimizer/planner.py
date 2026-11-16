"""Physical planning: cost model, join enumeration, algorithm choice (DESIGN_M4.md).

Cost is page I/O. For an input X of a join or sort:
  size(X)     estimated pages of X's output
  read(X)     pages read to get X's tuples once: the table's pages for a base
              access path (a scan under filters, projections and renames, which
              the operator re-reads itself), size(X) for anything else, read
              back as if materialized
  produce(X)  0 for a base access path (read(X) covers it), else X's own cost
With bare scans as inputs (read = size = M) these are the textbook formulas
  nested_loop_join  M + ceil(M / (B-2)) * N
  hash_join         3 (M + N)
  sort_merge_join   sort(M) + sort(N) + M + N,  sort(M) = 2 M (passes - 1) + M
Every node's est_cost is the cumulative cost of producing its output.
"""

import math
from collections import namedtuple
from itertools import combinations

from ..ra.tree import (
    RelRef, Select, Project, Rename, Distinct, GroupBy, Sort, Limit, Join, Times, AttrRef, BINARY_OPS,
    condition_attrs, condition_text, conjuncts, conjunction, is_equality, sort_keys_text,
)
from ..storage.record import FIXED_WIDTH, aggregate_result_type
from .estimate import Estimate, ColumnEstimate, table_estimate, selectivity, join_selectivity, ceil_rows
from .rewrite import ALL_RULES, Rule, Rewriter, output_columns, position, try_position

JOIN_ALGORITHMS = ("nested_loop_join", "sort_merge_join", "hash_join")

JoinOption = namedtuple("JoinOption", "added rest between")
Candidate = namedtuple("Candidate", "cost algorithm outer inner between how")
DPRow = namedtuple("DPRow", "subset plan how alternatives")


def sort_passes(pages, frames):
    """Passes of the executor's external sort: runs of B - 1 pages (the input
    keeps one frame), merged B - 1 at a time."""
    if pages <= frames - 1:
        return 1
    return 1 + math.ceil(math.log(math.ceil(pages / (frames - 1))) / math.log(frames - 1) - 1e-12)


class PhysicalNode:
    def __init__(self, op, fields, children, est, refs, cost, base=None, label=None, restores_order=False):
        self.op = op
        self.restores_order = restores_order
        self.fields = fields
        self.children = children
        self.est = est
        self.refs = refs
        self.cost = cost
        self.base = base
        self.label = label or op

    def lookup(self, ref):
        i = try_position(ref, self.refs)
        return None if i is None else self.est.columns[i]

    def to_json(self):
        out = {"op": self.op}
        out.update(self.fields)
        if len(self.children) == 1:
            out["child"] = self.children[0].to_json()
        elif len(self.children) == 2:
            out["left"] = self.children[0].to_json()
            out["right"] = self.children[1].to_json()
        out["est_rows"] = self.est.rows
        out["est_cost"] = self.cost
        return out

    def explain(self, depth=0):
        lines = ["  " * depth + f"{self.label} est_rows={self.est.rows} est_cost={self.cost}"]
        for child in self.children:
            lines += child.explain(depth + 1)
        return lines


def read_cost(node):
    return node.base if node.base is not None else node.est.pages


def produce_cost(node):
    return 0 if node.base is not None else node.cost


class Predicate:
    def __init__(self, cond, leaves):
        self.cond = cond
        self.leaves = leaves
        self.text = condition_text(cond)
        self.equality = is_equality(cond)


def unoptimized_planner(catalog, frames):
    """The baseline: no rewrite rules, joins in the order written, hash join for
    every equi-join."""
    return Planner(catalog, frames, rules=frozenset(), reorder=False, fixed_join="hash_join")


class Planner:
    def __init__(self, catalog, frames, rules=ALL_RULES, reorder=True, fixed_join=None):
        self.catalog = catalog
        self.frames = frames
        self.rules = rules
        self.reorder = reorder
        self.fixed_join = fixed_join
        self.schemas = lambda name: catalog.get_table(name).schema
        self.dp_tables = []

    def optimize(self, logical):
        rewritten = Rewriter(self.schemas, self.rules).rewrite(logical)
        return rewritten, self.convert(rewritten)

    def sort_extra_cost(self, pages):
        """Page I/O an external sort of `pages` adds to reading its input once."""
        return 2 * pages * (sort_passes(pages, self.frames) - 1)

    def convert(self, node):
        if isinstance(node, RelRef):
            info = self.catalog.get_table(node.name)
            est = table_estimate(info)
            refs = [AttrRef(node.name, c.name) for c in info.schema]
            return PhysicalNode("seqscan", {"table": node.name}, [], est, refs, est.pages, est.pages,
                                f"seqscan {node.name}")
        if isinstance(node, (Join, Times)) or self.is_region_select(node):
            return self.plan_region(node)
        if isinstance(node, Select):
            child = self.convert(node.input)
            return self.filter(child, node.cond)
        if isinstance(node, Project):
            child = self.convert(node.input)
            if child.restores_order:
                # This projection names its columns itself, so the order the
                # joins produce them in does not matter.
                child = child.children[0]
            refs, estimates, columns = [], [], []
            for attr, name in zip(node.attrs, node.names):
                j = position(attr, child.refs)
                refs.append(child.refs[j] if name is None else AttrRef(None, name))
                estimates.append(child.est.columns[j])
                columns.append(attr.text)
            fields = {"columns": columns}
            if node.renames:
                fields["names"] = node.names
            est = Estimate(child.est.rows, estimates)
            return PhysicalNode("project", fields, [child], est, refs, child.cost, child.base,
                                f"project [{', '.join(columns)}]")
        if isinstance(node, Rename):
            child = self.convert(node.input)
            refs = [AttrRef(node.new_name, r.attr) for r in child.refs]
            return PhysicalNode("rename", {"to": node.new_name}, [child], child.est, refs, child.cost,
                                child.base, f"rename {node.new_name}")
        if isinstance(node, Limit):
            child = self.convert(node.input)
            est = Estimate(min(max(1, node.n), child.est.rows), child.est.columns)
            return PhysicalNode("limit", {"n": node.n}, [child], est, child.refs, child.cost, child.base,
                                f"limit {node.n}")
        if isinstance(node, Sort):
            child = self.convert(node.input)
            keys = [[k.text, d] for k, d in node.keys]
            cost = child.cost + self.sort_extra_cost(child.est.pages)
            return PhysicalNode("sort", {"keys": keys}, [child], child.est, child.refs, cost,
                                label=f"sort [{sort_keys_text(node.keys)}]")
        if isinstance(node, (GroupBy, Distinct)):
            return self.aggregate(node)
        left, right = self.convert(node.left), self.convert(node.right)
        op = BINARY_OPS[type(node)]
        if op == "union_all":
            rows, cost = left.est.rows + right.est.rows, left.cost + right.cost
        else:
            rows = {"union": left.est.rows + right.est.rows, "intersect": min(left.est.rows, right.est.rows),
                    "except": left.est.rows}[op]
            cost = sum(c.cost + self.sort_extra_cost(c.est.pages) for c in (left, right))
        return PhysicalNode(op, {}, [left, right], Estimate(rows, left.est.columns), left.refs, cost)

    def filter(self, child, cond):
        sel = selectivity(cond, child.lookup)
        rows = ceil_rows(child.est.rows * sel)
        est = Estimate(rows, [c.capped(rows) for c in child.est.columns])
        text = condition_text(cond)
        return PhysicalNode("filter", {"condition": text}, [child], est, child.refs, child.cost, child.base,
                            f"filter ({text})")

    def aggregate(self, node):
        child = self.convert(node.input)
        if isinstance(node, Distinct):
            key_positions = list(range(len(child.refs)))
        else:
            key_positions = [position(k, child.refs) for k in node.keys]
        ndvs = [child.est.columns[i].ndv for i in key_positions]
        if not key_positions:
            groups = 1
        elif None in ndvs:
            groups = child.est.rows
        else:
            groups = min(child.est.rows, math.prod(ndvs))
        estimates = [child.est.columns[i].capped(groups) for i in key_positions]
        refs = [child.refs[i] for i in key_positions]
        fields = {}
        if isinstance(node, GroupBy):
            for a in node.aggregates:
                type_name = aggregate_result_type(a.func, None if a.arg is None else child.lookup(a.arg).type)
                width = child.lookup(a.arg).width if a.func in ("min", "max") else FIXED_WIDTH[type_name]
                estimates.append(ColumnEstimate(type_name, width, groups))
                refs.append(AttrRef(None, a.name))
        est = Estimate(groups, estimates)
        pages = child.est.pages
        sort_extra = self.sort_extra_cost(pages)
        hash_extra = 0 if est.pages <= (self.frames - 1) // 2 else 2 * pages
        mode = "hash" if key_positions and self.frames >= 4 and hash_extra <= sort_extra else "sort"
        extra = 0 if not key_positions else (hash_extra if mode == "hash" else sort_extra)
        fields["mode"] = mode
        if isinstance(node, Distinct):
            return PhysicalNode("distinct", fields, [child], est, refs, child.cost + extra,
                                label=f"distinct {mode}")
        keys = [k.text for k in node.keys]
        fields.update({"keys": keys, "aggregates": [[a.func, a.arg_text, a.name] for a in node.aggregates]})
        return PhysicalNode("group_by", fields, [child], est, refs, child.cost + extra,
                            label=f"group_by {mode} keys [{', '.join(keys)}] aggregates [{node.aggregates_text}]")

    def join_cost(self, algorithm, left, right):
        """(cost, the formula with numbers filled in)."""
        B = self.frames
        base = produce_cost(left) + produce_cost(right)
        produced = [str(produce_cost(x)) for x in (left, right) if produce_cost(x)]
        if algorithm == "nested_loop_join":
            blocks = math.ceil(left.est.pages / (B - 2))
            cost = base + read_cost(left) + blocks * read_cost(right)
            terms = [f"{read_cost(left)}", f"{blocks}x{read_cost(right)}"]
        elif algorithm == "hash_join":
            sizes = left.est.pages + right.est.pages
            cost = base + read_cost(left) + read_cost(right) + 2 * sizes
            terms = [f"{read_cost(left)}", f"{read_cost(right)}", f"2x{sizes}"]
        else:
            cost = base
            terms = []
            for side in (left, right):
                pages = side.est.pages
                sort_cost = read_cost(side) + self.sort_extra_cost(pages)
                cost += sort_cost + pages
                terms += [f"sort {sort_cost}", f"{pages}"]
        return cost, " + ".join(produced + terms)

    def join_node(self, algorithm, left, right, preds, rows, cost, predicate_selectivity=None):
        """Join node, plus a filter for any non-equality predicates a hash or
        sort-merge join cannot evaluate. `rows` is the estimate after every
        predicate; the join itself is estimated before the filter's ones."""
        refs = left.refs + right.refs
        preds = sorted(preds, key=lambda p: p.text)
        inside = preds if algorithm == "nested_loop_join" else [p for p in preds if p.equality]
        outside = [p for p in preds if p not in inside]
        join_rows = rows
        if outside:
            join_rows = ceil_rows(rows / math.prod(predicate_selectivity(p) for p in outside))
        estimates = [c.capped(join_rows) for c in left.est.columns + right.est.columns]
        fields = {}
        if inside:
            fields["condition"] = condition_text(conjunction([p.cond for p in inside]))
        label = f"{algorithm} ({fields['condition']})" if inside else f"{algorithm} (cross product)"
        node = PhysicalNode(algorithm, fields, [left, right], Estimate(join_rows, estimates), refs, cost, label=label)
        if outside:
            text = condition_text(conjunction([p.cond for p in outside]))
            node = PhysicalNode("filter", {"condition": text}, [node],
                                Estimate(rows, [c.capped(rows) for c in estimates]), refs, cost,
                                label=f"filter ({text})")
        return node

    def is_region_select(self, node):
        """A select sitting directly on joins whose predicates the join planning
        takes over (the baseline takes them all, to find the join conditions)."""
        absorbs = not self.reorder or {Rule.PUSH_SELECT_BELOW_JOIN, Rule.CROSS_TO_JOIN} <= set(self.rules)
        return isinstance(node, Select) and absorbs and (
            isinstance(node.input, (Join, Times)) or self.is_region_select(node.input))

    def collect(self, node, leaves, conds):
        if isinstance(node, (Join, Times)):
            self.collect(node.left, leaves, conds)
            self.collect(node.right, leaves, conds)
            if isinstance(node, Join):
                conds += conjuncts(node.cond)
        elif self.is_region_select(node):
            self.collect(node.input, leaves, conds)
            conds += conjuncts(node.cond)
        elif (isinstance(node, Project) and not node.renames
              and (isinstance(node.input, (Join, Times)) or self.is_region_select(node.input))):
            # A projection that pushing projections down put between two joins:
            # reordering the joins outranks it, and the leaves keep their own projections.
            self.collect(node.input, leaves, conds)
        else:
            leaves.append(node)

    def plan_region(self, node):
        if not self.reorder:
            return self.plan_written_order(node)
        leaves, conds = [], []
        self.collect(node, leaves, conds)
        plans = [self.convert(leaf) for leaf in leaves]
        names = [self.leaf_name(leaf, i) for i, leaf in enumerate(leaves)]
        preds = []
        for cond in conds:
            touched = set()
            for ref in condition_attrs(cond):
                touched |= {i for i, p in enumerate(plans) if try_position(ref, p.refs) is not None}
            preds.append(Predicate(cond, frozenset(touched)))
        single = [p for p in preds if len(p.leaves) <= 1]
        for p in single:
            i = next(iter(p.leaves)) if p.leaves else 0
            plans[i] = self.filter(plans[i], p.cond)
        preds = [p for p in preds if len(p.leaves) > 1]

        full = frozenset(range(len(plans)))
        best, table = self.enumerate(plans, preds, names, allow_cross=False)
        if full not in best:
            best, table = self.enumerate(plans, preds, names, allow_cross=True)
        self.dp_tables.append((names, table))
        plan = best[full]
        written = output_columns(node, self.schemas)
        if written != plan.refs:
            # Reordering changed the column order; put the written order back.
            estimates = [plan.est.columns[position(r, plan.refs)] for r in written]
            plan = PhysicalNode("project", {"columns": [r.text for r in written]}, [plan],
                                Estimate(plan.est.rows, estimates), written, plan.cost,
                                label=f"project [{', '.join(r.text for r in written)}]", restores_order=True)
        return plan

    def leaf_name(self, leaf, i):
        node = leaf
        while isinstance(node, (Select, Project)):
            node = node.input
        if isinstance(node, Rename):
            return node.new_name
        if isinstance(node, RelRef):
            return node.name
        return f"input{i + 1}"

    def subset_rows(self, subset, plans, preds):
        rows = 1.0
        for i in subset:
            rows *= plans[i].est.rows
        for p in preds:
            if p.leaves <= subset:
                rows *= self.predicate_selectivity(p, plans)
        return ceil_rows(rows)

    def predicate_selectivity(self, p, plans):
        lookups = [plans[i].lookup for i in sorted(p.leaves)]

        def lookup(ref):
            for leaf_lookup in lookups:
                stat = leaf_lookup(ref)
                if stat is not None:
                    return stat
            return None
        sel = join_selectivity(p.cond, lookup, lambda ref: None)
        if sel is None:
            sel = 1 / max(plans[i].est.rows for i in p.leaves)
        return sel

    def enumerate(self, plans, preds, names, allow_cross):
        """best: the cheapest plan of each subset of the inputs; table: the DPRows."""
        algorithms = (self.fixed_join,) if self.fixed_join else JOIN_ALGORITHMS
        best = {}
        table = []
        for i, plan in enumerate(plans):
            best[frozenset([i])] = plan
            table.append(DPRow(frozenset([i]), plan, plan.label, []))
        for size in range(2, len(plans) + 1):
            for combo in combinations(range(len(plans)), size):
                subset = frozenset(combo)
                options = []
                for added in combo:
                    rest = subset - {added}
                    if rest not in best:
                        continue
                    between = [p for p in preds if p.leaves <= subset and added in p.leaves and p.leaves & rest]
                    options.append(JoinOption(added, rest, between))
                connected = [option for option in options if option.between]
                if not connected and not allow_cross:
                    table.append(DPRow(subset, None, "no join predicate, skipped", []))
                    continue
                rows = self.subset_rows(subset, plans, preds)
                candidates = []
                for option in (connected or options):
                    outer, inner = best[option.rest], best[frozenset([option.added])]
                    has_equality = any(p.equality for p in option.between)
                    usable = algorithms if has_equality else ("nested_loop_join",)
                    for algorithm in usable:
                        cost, formula = self.join_cost(algorithm, outer, inner)
                        how = (f"{self.subset_name(option.rest, names)} outer ({outer.est.pages} pages), {algorithm}, "
                               f"{names[option.added]} inner: {formula} = {cost}")
                        candidates.append(Candidate(cost, algorithm, outer, inner, option.between, how))
                if not candidates:
                    continue
                chosen = min(candidates, key=lambda c: c.cost)
                best[subset] = self.join_node(chosen.algorithm, chosen.outer, chosen.inner, chosen.between, rows,
                                              chosen.cost, lambda p: self.predicate_selectivity(p, plans))
                alternatives = [c.how for c in candidates if c.how != chosen.how]
                table.append(DPRow(subset, best[subset], chosen.how, alternatives))
        return best, table

    def subset_name(self, subset, names):
        return "{" + ",".join(names[i] for i in sorted(subset)) + "}"

    def plan_written_order(self, node):
        """The unoptimized baseline: FROM items joined left to right, each join
        taking the equalities that link its new item to the earlier ones, every
        other predicate in one filter above all the joins."""
        leaves, conds = [], []
        self.collect(node, leaves, conds)
        plans = [self.convert(leaf) for leaf in leaves]
        pending = []
        for cond in conds:
            touched = frozenset(i for ref in condition_attrs(cond)
                                for i, p in enumerate(plans) if try_position(ref, p.refs) is not None)
            pending.append(Predicate(cond, touched))
        plan = plans[0]
        for i in range(1, len(plans)):
            right = plans[i]
            preds = [p for p in pending if p.equality and i in p.leaves and max(p.leaves) == i
                     and len(p.leaves) == 2]
            pending = [p for p in pending if p not in preds]
            rows = plan.est.rows * right.est.rows
            for p in preds:
                sel = join_selectivity(p.cond, plan.lookup, right.lookup)
                rows *= sel if sel is not None else 1 / max(plan.est.rows, right.est.rows)
            algorithm = self.fixed_join if preds else "nested_loop_join"
            cost, _ = self.join_cost(algorithm, plan, right)
            plan = self.join_node(algorithm, plan, right, preds, ceil_rows(rows), cost)
        if pending:
            plan = self.filter(plan, conjunction([p.cond for p in pending]))
        return plan

    def dp_table_text(self):
        lines = []
        for names, table in self.dp_tables:
            if len(names) < 2:
                continue
            width = max(len(self.subset_name(row.subset, names)) for row in table) + 2
            lines.append(f"{'subset':<{width}}{'rows':>8}{'pages':>8}{'cost':>9}  best plan")
            for subset, plan, how, alternatives in table:
                name = self.subset_name(subset, names)
                if plan is None:
                    lines.append(f"{name:<{width}}{'-':>8}{'-':>8}{'-':>9}  {how}")
                    continue
                lines.append(f"{name:<{width}}{plan.est.rows:>8}{plan.est.pages:>8}{plan.cost:>9}  {how}")
                for alt in alternatives:
                    lines.append(f"{'':<{width}}{'':>25}  alternative: {alt}")
            lines.append("")
        return "\n".join(lines).rstrip()
