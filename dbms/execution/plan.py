"""Physical plan JSON: validation, construction of the operator tree, execution.

Every node is an object with "op" and the fields below. Extra fields (such as
the optimizer's est_rows and est_cost) are ignored.

  seqscan           table: string
  filter            condition: string, child: node
  project           columns: [column, ...] (non-empty), names?: [string|null, ...], child
  rename            to: string, child
  distinct          mode: "sort"|"hash", child
  sort              keys: [[column, "asc"|"desc"], ...] (non-empty), child
  group_by          mode: "sort"|"hash", keys: [column, ...],
                    aggregates: [[func, column|"*"] or [func, column|"*", name], ...], child
                    func is count, sum, avg, min or max; "*" only with count
  nested_loop_join  condition?: string (absent = cross product), left, right
  sort_merge_join   condition: column = column [and ...], left, right
  hash_join         condition: column = column [and ...], left (build side), right
  union_all, union, intersect, except   left, right
  limit             n: integer >= 0, child
"""

import json

from ..ra.parser import ParseError, parse_condition, parse_column
from ..ra.tokenizer import LexError
from ..ra.tree import AGGREGATE_FUNCTIONS, conjuncts, is_equality
from .aggregate import GroupBy, Distinct
from .joins import NestedLoopJoin, SortMergeJoin, HashJoin
from .operators import SeqScan, Filter, Project, Rename, Limit, UnionAll, drain
from .runtime import ExecError
from .setops import Union, Intersect, Except
from .sort import Sort
from ..storage.tempfile import TempFile

UNARY = {"filter", "project", "rename", "distinct", "sort", "group_by", "limit"}
BINARY = {"nested_loop_join", "sort_merge_join", "hash_join", "union_all", "union", "intersect", "except"}
DEFAULT_FRAMES = 32
MIN_FRAMES = 3


class PlanError(Exception):
    pass


class TooFewFramesError(ExecError):
    pass


class ExecContext:
    def __init__(self, db):
        self.db = db
        self.pool = db.pool

    def new_temp(self):
        return TempFile(self.db.temp_path())


def load_plan(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f"Malformed JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None


class Builder:
    def __init__(self, ctx):
        self.ctx = ctx

    def fail(self, op, path, message):
        raise PlanError(f"Node '{op}': {message} [at {path}]")

    def field(self, node, op, path, name, kind):
        if name not in node:
            self.fail(op, path, f"missing required field '{name}'.")
        value = node[name]
        if kind == "string" and not isinstance(value, str):
            self.fail(op, path, f"field '{name}' must be a string.")
        if kind == "list" and not isinstance(value, list):
            self.fail(op, path, f"field '{name}' must be an array.")
        return value

    def condition(self, op, path, text, name="condition"):
        try:
            return parse_condition(text)
        except (LexError, ParseError) as e:
            self.fail(op, path, f"field '{name}' is not a valid condition: {e}")

    def column(self, op, path, text, name):
        if not isinstance(text, str):
            self.fail(op, path, f"field '{name}' must contain column names (strings), got {json.dumps(text)}.")
        try:
            return parse_column(text)
        except (LexError, ParseError):
            self.fail(op, path, f"field '{name}' has an invalid column name {text!r}.")

    def mode(self, node, op, path):
        mode = self.field(node, op, path, "mode", "string")
        if mode not in ("sort", "hash"):
            self.fail(op, path, f"field 'mode' must be \"sort\" or \"hash\", got {json.dumps(mode)}.")
        return mode

    def build(self, node, path="root"):
        if not isinstance(node, dict):
            raise PlanError(f"Node at {path}: expected a JSON object, got {json.dumps(node)[:40]}")
        if "op" not in node:
            raise PlanError(f"Node at {path}: missing required field 'op'.")
        op = node["op"]
        if not isinstance(op, str):
            raise PlanError(f"Node at {path}: field 'op' must be a string.")
        ctx = self.ctx

        if op == "seqscan":
            return SeqScan(ctx, path, self.field(node, op, path, "table", "string"))
        if op in UNARY:
            child_node = self.field(node, op, path, "child", None)
            child = self.build(child_node, f"{path}.child")
            return self.build_unary(node, op, path, child)
        if op in BINARY:
            left = self.build(self.field(node, op, path, "left", None), f"{path}.left")
            right = self.build(self.field(node, op, path, "right", None), f"{path}.right")
            return self.build_binary(node, op, path, left, right)
        self.fail(op, path, f"unknown operator '{op}'.")

    def build_unary(self, node, op, path, child):
        ctx = self.ctx
        if op == "filter":
            text = self.field(node, op, path, "condition", "string")
            return Filter(ctx, path, child, self.condition(op, path, text))
        if op == "project":
            columns = self.field(node, op, path, "columns", "list")
            if not columns:
                self.fail(op, path, "field 'columns' must not be empty.")
            refs = [self.column(op, path, c, "columns") for c in columns]
            names = node.get("names")
            if names is None:
                names = [None] * len(refs)
            elif (not isinstance(names, list) or len(names) != len(columns)
                    or not all(name is None or isinstance(name, str) for name in names)):
                self.fail(op, path, "field 'names' must be an array of strings or nulls, one per column.")
            return Project(ctx, path, child, refs, names)
        if op == "rename":
            return Rename(ctx, path, child, self.field(node, op, path, "to", "string"))
        if op == "distinct":
            return Distinct(ctx, path, child, self.mode(node, op, path))
        if op == "sort":
            keys = self.field(node, op, path, "keys", "list")
            if not keys:
                self.fail(op, path, "field 'keys' must not be empty.")
            parsed = []
            for key in keys:
                if not (isinstance(key, list) and len(key) == 2 and key[1] in ("asc", "desc")):
                    self.fail(op, path, f"each entry of 'keys' must be [column, \"asc\"|\"desc\"], got {json.dumps(key)}.")
                parsed.append((self.column(op, path, key[0], "keys"), key[1]))
            return Sort(ctx, path, child, parsed)
        if op == "group_by":
            mode = self.mode(node, op, path)
            keys = [self.column(op, path, k, "keys") for k in self.field(node, op, path, "keys", "list")]
            aggregates = []
            for agg in self.field(node, op, path, "aggregates", "list"):
                if not (isinstance(agg, list) and len(agg) in (2, 3) and agg[0] in AGGREGATE_FUNCTIONS
                        and (len(agg) == 2 or isinstance(agg[2], str))):
                    self.fail(op, path, f"each aggregate must be [func, column|\"*\"] or [func, column|\"*\", name] "
                                        f"with func in {', '.join(AGGREGATE_FUNCTIONS)}, got {json.dumps(agg)}.")
                func, arg = agg[0], agg[1]
                if arg == "*":
                    if func != "count":
                        self.fail(op, path, f"only count accepts \"*\", got {json.dumps(agg)}.")
                    arg_ref = None
                else:
                    arg_ref = self.column(op, path, arg, "aggregates")
                aggregates.append((func, arg_ref, agg[2] if len(agg) == 3 else None))
            return GroupBy(ctx, path, child, mode, keys, aggregates)
        n = self.field(node, op, path, "n", None)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            self.fail(op, path, "field 'n' must be a non-negative integer.")
        return Limit(ctx, path, child, n)

    def build_binary(self, node, op, path, left, right):
        ctx = self.ctx
        if op == "nested_loop_join":
            condition = None
            if "condition" in node:
                text = self.field(node, op, path, "condition", "string")
                condition = self.condition(op, path, text)
            return NestedLoopJoin(ctx, path, left, right, condition)
        if op in ("sort_merge_join", "hash_join"):
            text = self.field(node, op, path, "condition", "string")
            condition = self.condition(op, path, text)
            for c in conjuncts(condition):
                if not is_equality(c):
                    self.fail(op, path, f"field 'condition' must be column = column equalities joined by "
                                        f"'and', got {json.dumps(text)}.")
            cls = SortMergeJoin if op == "sort_merge_join" else HashJoin
            return cls(ctx, path, left, right, condition)
        cls = {"union_all": UnionAll, "union": Union, "intersect": Intersect, "except": Except}[op]
        return cls(ctx, path, [left, right])


def prepare(db, plan):
    """Validate and bind a plan and give it the buffer pool's B frames. Nothing runs yet."""
    frames = db.pool.size
    ctx = ExecContext(db)
    root = Builder(ctx).build(plan)
    root.bind()
    root.mark_readers(0, False)
    if frames < root.min_frames():
        root.mark_readers(0, True)
    needed = root.min_frames()
    if frames < needed:
        raise TooFewFramesError(f"This plan needs at least {needed} frames, got {frames}")
    root.configure(frames, frames)
    return root


def execute(db, root):
    db.pool.reset_stats()
    try:
        root.open()
        rows = list(drain(root))
    finally:
        root.close()
        db.remove_temp_dir()
    return rows


def walk(op, depth=0):
    yield op, depth
    for child in op.children:
        yield from walk(child, depth + 1)


def totals(root, rows):
    out = {"tuples_out": len(rows), "comparisons": 0, "page_reads": 0, "page_writes": 0}
    for op, _ in walk(root):
        c = op.counters()
        for key in ("comparisons", "page_reads", "page_writes"):
            out[key] += c[key]
    return out
