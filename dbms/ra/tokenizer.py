"""Hand-written scanner for the relational algebra language."""

from dataclasses import dataclass
from typing import Any, List

KEYWORDS = {
    "select", "project", "rename", "distinct",
    "union", "intersect", "minus", "times", "join",
    "and", "or", "not",
}

INT64_MIN = -9223372036854775808
INT64_MAX = 9223372036854775807


@dataclass
class Token:
    kind: str
    value: Any
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


class LexError(Exception):
    def __init__(self, message, start, line, col):
        self.message = message
        self.start = start
        self.line = line
        self.col = col
        super().__init__(f"{message} at line {line}, column {col} (index {start})")


def is_letter(ch):
    return ch == "_" or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def is_digit(ch):
    return "0" <= ch <= "9"


def check_int64(value, lexeme, start, line, col):
    if value < INT64_MIN or value > INT64_MAX:
        raise LexError(f"Integer literal out of signed 64-bit range: {lexeme}", start, line, col)


class Tokenizer:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.length = len(text)

    def eof(self):
        return self.pos >= self.length

    def peek(self, offset=0):
        idx = self.pos + offset
        if idx >= self.length:
            return ""
        return self.text[idx]

    def advance(self):
        ch = self.text[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def tokenize(self) -> List[Token]:
        tokens: List[Token] = []
        brace_depth = 0

        while not self.eof():
            start = self.pos
            line = self.line
            col = self.col
            ch = self.peek()

            if ch in " \t\r":
                self.advance()
                continue

            if ch == "\n":
                self.advance()
                tokens.append(Token("NEWLINE", "\n", line, col))
                continue

            if ch == "/" and self.peek(1) == "/":
                self.advance()
                self.advance()
                while not self.eof() and self.peek() != "\n":
                    self.advance()
                continue

            if ch == "'":
                tokens.append(self.read_string(start, line, col))
                continue

            if ch == "{":
                self.advance()
                brace_depth += 1
                tokens.append(Token("SYMBOL", "{", line, col))
                continue

            if ch == "}":
                self.advance()
                brace_depth -= 1
                tokens.append(Token("SYMBOL", "}", line, col))
                continue

            if ch == ",":
                self.advance()
                tokens.append(Token("SYMBOL", ",", line, col))
                continue

            inside_tuple_list = brace_depth > 0
            if inside_tuple_list:
                tokens.append(self.read_bare_value(start, line, col))
                continue

            if is_digit(ch):
                tokens.append(self.read_integer(start, line, col))
                continue

            if ch == "-":
                if is_digit(self.peek(1)):
                    tokens.append(self.read_integer(start, line, col))
                    continue
                raise LexError("Unexpected '-' (not followed by a digit)", start, line, col)

            if is_letter(ch):
                tokens.append(self.read_ident_or_keyword(start, line, col))
                continue

            two = ch + self.peek(1)
            if two in (">=", "<=", "!="):
                self.advance()
                self.advance()
                tokens.append(Token("SYMBOL", two, line, col))
                continue

            if ch in "()[]=<>.":
                self.advance()
                tokens.append(Token("SYMBOL", ch, line, col))
                continue

            if ch == "!":
                raise LexError("Unexpected '!' (expected '!=')", start, line, col)

            raise LexError(f"Unexpected character {ch!r}", start, line, col)

        tokens.append(Token("EOF", None, self.line, self.col))
        return tokens

    def read_string(self, start, line, col):
        self.advance()
        chars = []
        while True:
            if self.eof():
                raise LexError("Unterminated string literal", start, line, col)
            ch = self.peek()
            if ch == "'":
                if self.peek(1) == "'":
                    chars.append("'")
                    self.advance()
                    self.advance()
                else:
                    self.advance()
                    break
            else:
                chars.append(ch)
                self.advance()
        return Token("STRING", "".join(chars), line, col)

    def read_integer(self, start, line, col):
        if self.peek() == "-":
            self.advance()
        while is_digit(self.peek()):
            self.advance()
        lexeme = self.text[start:self.pos]
        value = int(lexeme)
        check_int64(value, lexeme, start, line, col)
        return Token("INTEGER", value, line, col)

    def read_ident_or_keyword(self, start, line, col):
        while is_letter(self.peek()) or is_digit(self.peek()):
            self.advance()
        word = self.text[start:self.pos]
        return Token("KEYWORD" if word in KEYWORDS else "IDENT", word, line, col)

    def read_bare_value(self, start, line, col):
        chars: List[str] = []
        while not self.eof():
            ch = self.peek()
            if ch in BARE_DELIMS:
                break
            if ch == "/" and self.peek(1) == "/":
                break
            chars.append(ch)
            self.advance()

        if not chars:
            raise LexError(f"Expected tuple value, got {self.peek()!r}", start, line, col)

        lexeme = "".join(chars)
        if looks_like_integer_literal(lexeme):
            value = int(lexeme)
            check_int64(value, lexeme, start, line, col)
            return Token("INTEGER", value, line, col)
        return Token("STRING", lexeme, line, col)


BARE_DELIMS = set(" \t\r\n,(){}'")


def looks_like_integer_literal(s):
    digits = s[1:] if s[0] == "-" else s
    return len(digits) > 0 and all(is_digit(ch) for ch in digits)
