"""Recursive descent parser for relational algebra queries and conditions."""

from .tokenizer import Tokenizer
from .tree import (
    RelRef, Select, Project, Rename, Distinct, Union, Intersect, Minus, Times, Join,
    Comparison, And, Or, Not, AttrRef, Integer, String, RelationDef, Program,
)

COMPARISON_OPS = ("=", "!=", "<", "<=", ">", ">=")


class ParseError(Exception):
    def __init__(self, message, token):
        self.message = message
        self.token = token
        super().__init__(f"{message} at line {token.line}, column {token.col}")


class TokenCursor:
    """A position in a token list that ends with EOF."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset=0):
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def advance(self):
        t = self.peek()
        if t.kind != "EOF":
            self.pos += 1
        return t

    def expect(self, kind, value=None):
        t = self.peek()
        if t.kind != kind:
            raise ParseError(f"Expected {kind}, got {t.kind}", t)
        if value is not None and t.value != value:
            raise ParseError(f"Expected {value!r}, got {t.value!r}", t)
        return self.advance()

    def match(self, kind, value=None):
        t = self.peek()
        if t.kind == kind and (value is None or t.value == value):
            return self.advance()
        return None

    def is_ident_like(self, token):
        return token.kind in ("IDENT", "KEYWORD")

    def consume_ident_like(self):
        t = self.peek()
        if self.is_ident_like(t):
            return self.advance().value
        raise ParseError(f"Expected identifier, got {t.kind}", t)


class Parser(TokenCursor):
    def __init__(self, tokens):
        super().__init__([t for t in tokens if t.kind != "NEWLINE"])

    def expect_end(self):
        if self.peek().kind != "EOF":
            raise ParseError("Unexpected token after end of input", self.peek())

    def parse_query(self):
        expr = self.parse_expr()
        if self.peek().kind != "EOF":
            raise ParseError("Unexpected token after query", self.peek())
        return expr

    def parse_expr(self):
        left = self.parse_intersect_expr()
        while True:
            t = self.peek()
            if t.kind == "KEYWORD" and t.value in ("union", "minus"):
                op = self.advance().value
                right = self.parse_intersect_expr()
                left = Union(left, right) if op == "union" else Minus(left, right)
            else:
                break
        return left

    def parse_intersect_expr(self):
        left = self.parse_times_join_expr()
        while True:
            t = self.peek()
            if t.kind == "KEYWORD" and t.value == "intersect":
                self.advance()
                right = self.parse_times_join_expr()
                left = Intersect(left, right)
            else:
                break
        return left

    def parse_times_join_expr(self):
        left = self.parse_unary_expr()
        while True:
            t = self.peek()
            if t.kind == "KEYWORD" and t.value == "times":
                self.advance()
                right = self.parse_unary_expr()
                left = Times(left, right)
            elif t.kind == "KEYWORD" and t.value == "join" and self.next_is("["):
                self.advance()
                self.expect("SYMBOL", "[")
                cond = self.parse_condition()
                self.expect("SYMBOL", "]")
                right = self.parse_unary_expr()
                left = Join(cond, left, right)
            else:
                break
        return left

    def next_is(self, symbol):
        nxt = self.peek(1)
        return nxt.kind == "SYMBOL" and nxt.value == symbol

    def parse_parenthesised_input(self):
        self.expect("SYMBOL", "(")
        inner = self.parse_expr()
        if not self.match("SYMBOL", ")"):
            raise ParseError("Missing closing ')'", self.peek())
        return inner

    def parse_unary_expr(self):
        t = self.peek()
        if t.kind == "KEYWORD":
            if t.value == "select" and self.next_is("["):
                self.advance()
                self.expect("SYMBOL", "[")
                cond = self.parse_condition()
                self.expect("SYMBOL", "]")
                return Select(cond, self.parse_parenthesised_input())
            if t.value == "project" and self.next_is("["):
                self.advance()
                self.expect("SYMBOL", "[")
                attrs = self.parse_attr_list()
                self.expect("SYMBOL", "]")
                return Project(attrs, self.parse_parenthesised_input())
            if t.value == "rename" and self.next_is("["):
                self.advance()
                self.expect("SYMBOL", "[")
                new_name = self.consume_ident_like()
                self.expect("SYMBOL", "]")
                return Rename(new_name, self.parse_parenthesised_input())
            if t.value == "distinct" and self.next_is("("):
                self.advance()
                return Distinct(self.parse_parenthesised_input())
        if t.kind == "SYMBOL" and t.value == "(":
            return self.parse_parenthesised_input()
        if self.is_ident_like(t):
            return RelRef(self.consume_ident_like())
        raise ParseError(f"Unexpected token in expression: {t.kind} {t.value!r}", t)

    def parse_attr_list(self):
        if self.peek().kind == "SYMBOL" and self.peek().value == "]":
            raise ParseError("Empty attribute list in project", self.peek())
        attrs = [self.parse_attr_ref()]
        while self.match("SYMBOL", ","):
            attrs.append(self.parse_attr_ref())
        return attrs

    def parse_condition(self):
        left = self.parse_and_condition()
        while self.peek().kind == "KEYWORD" and self.peek().value == "or":
            self.advance()
            left = Or(left, self.parse_and_condition())
        return left

    def parse_and_condition(self):
        left = self.parse_not_condition()
        while self.peek().kind == "KEYWORD" and self.peek().value == "and":
            self.advance()
            left = And(left, self.parse_not_condition())
        return left

    def parse_not_condition(self):
        t = self.peek()
        if t.kind == "KEYWORD" and t.value == "not":
            nxt = self.peek(1)
            not_is_operand = nxt.kind == "SYMBOL" and nxt.value in COMPARISON_OPS + (".",)
            if not not_is_operand:
                self.advance()
                return Not(self.parse_not_condition())
        return self.parse_primary_condition()

    def parse_primary_condition(self):
        t = self.peek()
        if t.kind == "SYMBOL" and t.value == "(":
            self.advance()
            cond = self.parse_condition()
            self.expect("SYMBOL", ")")
            return cond
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_operand()
        op_token = self.peek()
        if op_token.kind == "SYMBOL" and op_token.value in COMPARISON_OPS:
            op = self.advance().value
            return Comparison(op, left, self.parse_operand())
        raise ParseError("Expected comparison operator", op_token)

    def parse_operand(self):
        t = self.peek()
        if t.kind == "INTEGER":
            self.advance()
            return Integer(t.value)
        if t.kind == "STRING":
            self.advance()
            return String(t.value)
        return self.parse_attr_ref()

    def parse_attr_ref(self):
        first = self.consume_ident_like()
        if self.match("SYMBOL", "."):
            return AttrRef(first, self.consume_ident_like())
        return AttrRef(None, first)


class ProgramParser(TokenCursor):
    """Relation definitions followed by one query."""

    def skip_newlines(self):
        while self.peek().kind == "NEWLINE":
            self.advance()

    def parse_program(self):
        relations = []
        while True:
            self.skip_newlines()
            if self.peek().kind == "EOF":
                raise ParseError("Expected query after relation definitions", self.peek())
            if not self.is_relation_def_start():
                break
            relations.append(self.parse_relation_def())

        self.skip_newlines()
        query = Parser(self.tokens[self.pos:]).parse_query()
        return Program(relations, query)

    def is_symbol(self, offset, value):
        t = self.peek(offset)
        return t.kind == "SYMBOL" and t.value == value

    def is_relation_def_start(self):
        """Name ( a, b, ... ) = { starts a definition; distinct(R) does not."""
        if not (self.is_ident_like(self.peek()) and self.is_symbol(1, "(")):
            return False
        offset = 2
        while self.is_ident_like(self.peek(offset)):
            offset += 1
            if not self.is_symbol(offset, ","):
                break
            offset += 1
        return self.is_symbol(offset, ")") and self.is_symbol(offset + 1, "=")

    def parse_relation_def(self):
        name = self.consume_ident_like()
        self.expect("SYMBOL", "(")
        attrs = [self.consume_ident_like()]
        while self.match("SYMBOL", ","):
            attrs.append(self.consume_ident_like())
        self.expect("SYMBOL", ")")
        self.expect("SYMBOL", "=")
        self.expect("SYMBOL", "{")
        tuples = self.parse_tuple_list(len(attrs))
        self.expect("SYMBOL", "}")
        return RelationDef(name, attrs, tuples)

    def parse_tuple_list(self, arity):
        tuples = []
        self.skip_newlines()
        if self.peek().kind == "SYMBOL" and self.peek().value == "}":
            return tuples
        while True:
            tuples.append(self.parse_tuple(arity))
            if self.peek().kind == "SYMBOL" and self.peek().value == "}":
                break
            if self.peek().kind != "NEWLINE":
                raise ParseError("Expected newline or '}' after tuple", self.peek())
            self.skip_newlines()
            if self.peek().kind == "SYMBOL" and self.peek().value == "}":
                break
        return tuples

    def parse_tuple(self, arity):
        values = [self.parse_tuple_value()]
        while True:
            save = self.pos
            self.skip_newlines()
            if self.peek().kind == "SYMBOL" and self.peek().value == ",":
                self.advance()
                self.skip_newlines()
                values.append(self.parse_tuple_value())
            else:
                self.pos = save
                break
        if len(values) != arity:
            raise ParseError(f"Tuple has {len(values)} values, expected {arity}", self.peek())
        return tuple(values)

    def parse_tuple_value(self):
        t = self.peek()
        if t.kind in ("INTEGER", "STRING"):
            self.advance()
            return t.value
        raise ParseError(f"Expected tuple value, got {t.kind} {t.value!r}", t)


def parse_query(text):
    return Parser(Tokenizer(text).tokenize()).parse_query()


def parse_condition(text):
    """Parse a condition string such as "R.b = S.b" into a condition tree."""
    parser = Parser(Tokenizer(text).tokenize())
    cond = parser.parse_condition()
    parser.expect_end()
    return cond


def parse_column(text):
    """Parse a column reference such as "Emp.did" or "count_star"."""
    parser = Parser(Tokenizer(text).tokenize())
    ref = parser.parse_attr_ref()
    parser.expect_end()
    return ref


def parse_program(text):
    return ProgramParser(Tokenizer(text).tokenize()).parse_program()
