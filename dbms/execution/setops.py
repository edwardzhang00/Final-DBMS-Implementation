"""union, intersect and except. All three have set semantics and are sort based.

Each input is sorted on every column with the external sort and the two sorted
streams are merged, so neither side is held in memory beyond its share of the
budget and each distinct tuple is met once, next to its duplicates. union
emits every distinct tuple of either side, intersect those on both sides,
except those on the left only.
"""

from .operators import promote, promoted_columns, unify_schemas
from .sort import SortedInputs


class SortedSetOp(SortedInputs):
    def derive_schema(self):
        left, right = self.children
        schema = unify_schemas(left.schema, right.schema)
        self.promoted = promoted_columns(schema, self.children)
        every_column = list(range(len(schema)))
        self.sort_inputs(every_column, every_column)
        return schema


class Intersect(SortedSetOp):
    op = "intersect"

    def produce(self):
        left, right = self.left_sort, self.right_sort
        r, s = left.next(), right.next()
        while r is not None and s is not None:
            self.comparisons += 1
            if r < s:
                r = left.next()
            elif r > s:
                s = right.next()
            else:
                yield promote(r, self.promoted)
                value = r
                while r is not None and r == value:
                    r = left.next()
                while s is not None and s == value:
                    s = right.next()


class Except(SortedSetOp):
    op = "except"

    def produce(self):
        left, right = self.left_sort, self.right_sort
        r, s = left.next(), right.next()
        while r is not None:
            while s is not None and s < r:
                self.comparisons += 1
                s = right.next()
            self.comparisons += 1
            if s is None or s != r:
                yield promote(r, self.promoted)
            value = r
            while r is not None and r == value:
                r = left.next()


class Union(SortedSetOp):
    op = "union"

    def produce(self):
        left, right = self.left_sort, self.right_sort
        r, s = left.next(), right.next()
        while r is not None or s is not None:
            if s is None:
                value = r
            elif r is None:
                value = s
            else:
                self.comparisons += 1
                value = r if r <= s else s
            yield promote(value, self.promoted)
            while r is not None and r == value:
                r = left.next()
            while s is not None and s == value:
                s = right.next()
