"""Hand-written scanner and recursive descent parser for the SQL subset (see GRAMMAR_SQL.md)."""

from ..ra.tokenizer import is_letter, is_digit
from ..ra.tree import AGGREGATE_FUNCTIONS
from .ast import (
    SQLError, CreateTable, ColumnDefinition, Insert, Literal, ColumnRef, Aggregate, SelectItem,
    TableRef, SubqueryRef, Comparison, BoolOp, NotOp, OrderItem, SelectBlock, SetOp,
    Explain, Analyze, SetFrames, Load,
)

AGGREGATES = tuple(func.upper() for func in AGGREGATE_FUNCTIONS)
KEYWORDS = {
    "SELECT", "DISTINCT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC",
    "LIMIT", "UNION", "ALL", "INTERSECT", "EXCEPT", "JOIN", "ON", "AS", "AND", "OR", "NOT",
    "CREATE", "TABLE", "INSERT", "INTO", "VALUES", "INT", "VARCHAR",
    "EXPLAIN", "ANALYZE", "SET", "LOAD", *AGGREGATES,
}
COMPARISONS = ("=", "<>", "!=", "<", "<=", ">", ">=")


class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def describe(self):
        if self.kind == "EOF":
            return "end of input"
        if self.kind == "STRING":
            return f"string '{self.value}'"
        return f"'{self.value}'"


def tokenize(text):
    """Tokens one at a time, as the parser asks for them, so that a lexical error is
    met where it stands in the script, as a syntax error is."""
    i, line, col = 0, 1, 1
    end = len(text)

    def advance(count=1):
        nonlocal i, line, col
        for _ in range(count):
            if text[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < end:
        ch = text[i]
        start_line, start_col = line, col
        nxt = text[i + 1] if i + 1 < end else ""
        if ch in " \t\r\n":
            advance()
        elif ch == "-" and nxt == "-":
            while i < end and text[i] != "\n":
                advance()
        elif is_letter(ch):
            j = i
            while j < end and (is_letter(text[j]) or is_digit(text[j])):
                j += 1
            word = text[i:j]
            if word.upper() in KEYWORDS:
                yield Token("KEYWORD", word.upper(), start_line, start_col)
            else:
                yield Token("IDENT", word, start_line, start_col)
            advance(j - i)
        elif is_digit(ch) or (ch in "+-" and is_digit(nxt)):
            j = i + 1
            while j < end and is_digit(text[j]):
                j += 1
            yield Token("INT", int(text[i:j]), start_line, start_col)
            advance(j - i)
        elif ch == "'":
            advance()
            chars = []
            while True:
                if i >= end:
                    raise SQLError("lexical", "unterminated string literal", start_line, start_col)
                if text[i] == "'":
                    if i + 1 < end and text[i + 1] == "'":
                        chars.append("'")
                        advance(2)
                        continue
                    advance()
                    break
                chars.append(text[i])
                advance()
            yield Token("STRING", "".join(chars), start_line, start_col)
        elif ch + nxt in ("<=", ">=", "<>", "!="):
            yield Token("SYMBOL", ch + nxt, start_line, start_col)
            advance(2)
        elif ch in "(),;.*=<>":
            yield Token("SYMBOL", ch, start_line, start_col)
            advance()
        else:
            raise SQLError("lexical", f"unexpected character {ch!r}", start_line, start_col)
    yield Token("EOF", None, line, col)


class Parser:
    def __init__(self, text):
        self.tokens = tokenize(text)
        self.current = None

    @property
    def tok(self):
        if self.current is None:
            self.current = next(self.tokens)
        return self.current

    def at(self, kind, value=None):
        t = self.tok
        return t.kind == kind and (value is None or t.value == value)

    def at_keyword(self, *words):
        return self.tok.kind == "KEYWORD" and self.tok.value in words

    def at_symbol(self, *symbols):
        return self.tok.kind == "SYMBOL" and self.tok.value in symbols

    def advance(self):
        t = self.tok
        if t.kind != "EOF":
            self.current = None
        return t

    def error(self, expected):
        t = self.tok
        raise SQLError("syntax", f"expected {expected}, found {t.describe()}", t.line, t.col)

    def keyword(self, word):
        if not self.at_keyword(word):
            self.error(word)
        return self.advance()

    def symbol(self, value):
        if not self.at_symbol(value):
            self.error(f"'{value}'")
        return self.advance()

    def ident(self, what="an identifier"):
        if not self.at("IDENT"):
            self.error(what)
        return self.advance()

    def parse_script(self):
        statements = []
        while not self.at("EOF"):
            if self.at_symbol(";"):
                self.advance()
                continue
            statements.append(self.parse_statement())
            if not self.at("EOF"):
                self.symbol(";")
        return statements

    def parse_statement(self):
        t = self.tok
        if self.at_keyword("CREATE"):
            return self.parse_create()
        if self.at_keyword("INSERT"):
            return self.parse_insert()
        if self.at_keyword("EXPLAIN"):
            self.advance()
            analyze = self.at_keyword("ANALYZE")
            if analyze:
                self.advance()
            return Explain(self.parse_query(), analyze, t.line, t.col)
        if self.at_keyword("ANALYZE"):
            self.advance()
            return Analyze(self.ident("a table name").value, t.line, t.col)
        if self.at_keyword("SET"):
            self.advance()
            name = self.ident("a setting name").value
            self.symbol("=")
            if not self.at("INT"):
                self.error("an integer")
            return SetFrames(name, self.advance().value, t.line, t.col)
        if self.at_keyword("LOAD"):
            self.advance()
            table = self.ident("a table name").value
            self.keyword("FROM")
            if not self.at("STRING"):
                self.error("a quoted file name")
            return Load(table, self.advance().value, t.line, t.col)
        if self.at_keyword("SELECT") or self.at_symbol("("):
            return self.parse_query()
        self.error("a statement (SELECT, CREATE TABLE, INSERT, EXPLAIN, ANALYZE, SET or LOAD)")

    def parse_create(self):
        t = self.keyword("CREATE")
        self.keyword("TABLE")
        name = self.ident("a table name").value
        self.symbol("(")
        columns = [self.parse_column_definition()]
        while self.at_symbol(","):
            self.advance()
            columns.append(self.parse_column_definition())
        self.symbol(")")
        return CreateTable(name, columns, t.line, t.col)

    def parse_column_definition(self):
        name = self.ident("a column name")
        if self.at_keyword("INT"):
            self.advance()
            return ColumnDefinition(name.value, "INT", None, name.line, name.col)
        if self.at_keyword("VARCHAR"):
            self.advance()
            self.symbol("(")
            if not self.at("INT"):
                self.error("a length")
            length = self.advance().value
            self.symbol(")")
            return ColumnDefinition(name.value, "VARCHAR", length, name.line, name.col)
        self.error("INT or VARCHAR")

    def parse_insert(self):
        t = self.keyword("INSERT")
        self.keyword("INTO")
        table = self.ident("a table name").value
        self.keyword("VALUES")
        self.symbol("(")
        values = [self.parse_literal()]
        while self.at_symbol(","):
            self.advance()
            values.append(self.parse_literal())
        self.symbol(")")
        return Insert(table, values, t.line, t.col)

    def parse_literal(self):
        if self.at("INT") or self.at("STRING"):
            t = self.advance()
            return Literal(t.value, t.line, t.col)
        self.error("a number or a string")

    def parse_query(self):
        """query ::= block { setop block }, left associative. ORDER BY and LIMIT
        written after the last block of a chain belong to the whole chain."""
        left = self.parse_block()
        last = left
        while self.at_keyword("UNION", "INTERSECT", "EXCEPT"):
            t = self.advance()
            op = t.value.lower()
            if op == "union" and self.at_keyword("ALL"):
                self.advance()
                op = "union_all"
            last = self.parse_block()
            left = SetOp(op, left, last, t.line, t.col)
        if left is not last and isinstance(last, SelectBlock) and not last.parenthesized:
            left.order_by, left.limit = last.order_by, last.limit
            last.order_by, last.limit = [], None
        elif left is not last or left.parenthesized:
            self.parse_order_by_and_limit(left)
        return left

    def parse_block(self):
        if self.at_symbol("("):
            self.advance()
            query = self.parse_query()
            self.symbol(")")
            query.parenthesized = True
            return query
        return self.parse_select()

    def parse_select(self):
        t = self.keyword("SELECT")
        block = SelectBlock(t.line, t.col)
        if self.at_keyword("DISTINCT"):
            self.advance()
            block.distinct = True
        if self.at_symbol("*"):
            self.advance()
            block.star = True
        else:
            block.items.append(self.parse_select_item())
            while self.at_symbol(","):
                self.advance()
                block.items.append(self.parse_select_item())
        self.keyword("FROM")
        self.parse_from(block)
        if self.at_keyword("WHERE"):
            self.advance()
            block.where = self.parse_condition()
        if self.at_keyword("GROUP"):
            self.advance()
            self.keyword("BY")
            block.group_by = [self.parse_column()]
            while self.at_symbol(","):
                self.advance()
                block.group_by.append(self.parse_column())
            if self.at_keyword("HAVING"):
                self.advance()
                block.having = self.parse_condition()
        self.parse_order_by_and_limit(block)
        return block

    def parse_order_by_and_limit(self, query):
        """A parenthesized query may take the clause it lacks after its ")", as in
        PostgreSQL, where it joins the query's own: (... LIMIT 5) ORDER BY x sorts, then limits."""
        if self.at_keyword("ORDER"):
            if query.order_by:
                self.refuse_second_clause("ORDER BY")
            self.advance()
            self.keyword("BY")
            query.order_by = [self.parse_order_item()]
            while self.at_symbol(","):
                self.advance()
                query.order_by.append(self.parse_order_item())
        if self.at_keyword("LIMIT"):
            if query.limit is not None:
                self.refuse_second_clause("LIMIT")
            self.advance()
            if not self.at("INT") or self.tok.value < 0:
                self.error("a non-negative integer")
            query.limit = self.advance().value

    def refuse_second_clause(self, clause):
        t = self.tok
        raise SQLError("syntax", f"this parenthesized query already has its own {clause}; to apply another, "
                                 f"use it as a subquery in FROM", t.line, t.col)

    def parse_select_item(self):
        t = self.tok
        expr = self.parse_aggregate() if self.at_keyword(*AGGREGATES) else self.parse_column()
        alias = None
        if self.at_keyword("AS"):
            self.advance()
            alias = self.ident("an alias").value
        elif self.at("IDENT"):
            alias = self.advance().value
        return SelectItem(expr, alias, t.line, t.col)

    def parse_aggregate(self):
        t = self.advance()
        self.symbol("(")
        if self.at_symbol("*"):
            star = self.advance()
            if t.value != "COUNT":
                raise SQLError("syntax", f"{t.value}(*) is not allowed, only COUNT(*)", star.line, star.col)
            arg = None
        else:
            arg = self.parse_column()
        self.symbol(")")
        return Aggregate(t.value.lower(), arg, t.line, t.col)

    def parse_column(self):
        first = self.ident("a column name")
        if self.at_symbol("."):
            self.advance()
            second = self.ident("a column name")
            return ColumnRef(first.value, second.value, first.line, first.col)
        return ColumnRef(None, first.value, first.line, first.col)

    def parse_from(self, block):
        block.from_refs.append(self.parse_table_ref())
        if self.at_keyword("JOIN"):
            block.join_conditions = []
            while self.at_keyword("JOIN"):
                self.advance()
                block.from_refs.append(self.parse_table_ref())
                self.keyword("ON")
                block.join_conditions.append(self.parse_condition())
        else:
            while self.at_symbol(","):
                self.advance()
                block.from_refs.append(self.parse_table_ref())

    def parse_table_ref(self):
        t = self.tok
        if self.at_symbol("("):
            self.advance()
            query = self.parse_query()
            self.symbol(")")
            if self.at_keyword("AS"):
                self.advance()
            alias = self.ident("an alias for the subquery (a subquery in FROM must be named)").value
            return SubqueryRef(query, alias, t.line, t.col)
        name = self.ident("a table name").value
        alias = None
        if self.at_keyword("AS"):
            self.advance()
            alias = self.ident("an alias").value
        elif self.at("IDENT"):
            alias = self.advance().value
        return TableRef(name, alias, t.line, t.col)

    def parse_order_item(self):
        t = self.tok
        expr = self.parse_aggregate() if self.at_keyword(*AGGREGATES) else self.parse_column()
        direction = "asc"
        if self.at_keyword("ASC", "DESC"):
            direction = self.advance().value.lower()
        return OrderItem(expr, direction, t.line, t.col)

    def parse_condition(self):
        """OR of ANDs of NOTs of comparisons."""
        left = self.parse_and()
        while self.at_keyword("OR"):
            t = self.advance()
            left = BoolOp("or", left, self.parse_and(), t.line, t.col)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at_keyword("AND"):
            t = self.advance()
            left = BoolOp("and", left, self.parse_not(), t.line, t.col)
        return left

    def parse_not(self):
        if self.at_keyword("NOT"):
            t = self.advance()
            return NotOp(self.parse_not(), t.line, t.col)
        if self.at_symbol("("):
            self.advance()
            cond = self.parse_condition()
            self.symbol(")")
            return cond
        return self.parse_comparison()

    def parse_comparison(self):
        t = self.tok
        left = self.parse_operand()
        if not self.at_symbol(*COMPARISONS):
            self.error("a comparison operator (=, <>, <, <=, >, >=)")
        op = self.advance().value
        return Comparison("!=" if op == "<>" else op, left, self.parse_operand(), t.line, t.col)

    def parse_operand(self):
        if self.at("INT") or self.at("STRING"):
            t = self.advance()
            return Literal(t.value, t.line, t.col)
        if self.at_keyword(*AGGREGATES):
            return self.parse_aggregate()
        if not self.at("IDENT"):
            self.error("a column, a number or a string")
        return self.parse_column()


def parse_script(text):
    return Parser(text).parse_script()
