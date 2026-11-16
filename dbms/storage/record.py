"""Record format: field encodings concatenated in schema order, no header.

INT      4 bytes, signed, little-endian.
VARCHAR  uint16 byte length L, then L bytes of UTF-8 (L <= n <= 255).
REAL and BIGINT (8 bytes each) never appear in a table; they are the types of
AVG and SUM results and only reach temporary files of the executor.
"""

import struct

from .disk import StorageError

INT_MIN = -2 ** 31
INT_MAX = 2 ** 31 - 1
MAX_VARCHAR = 255

FIELD_FORMATS = {"INT": "i", "REAL": "d", "BIGINT": "q"}
PACK = {type_name: struct.Struct("<" + code) for type_name, code in FIELD_FORMATS.items()}
FIXED_WIDTH = {type_name: packer.size for type_name, packer in PACK.items()}
LENGTH = struct.Struct("<H")


def aggregate_result_type(func, argument_type):
    """COUNT is INT and AVG is REAL; SUM is BIGINT, or REAL over a REAL; MIN and MAX
    keep their argument's type. argument_type is None for COUNT(*)."""
    if func == "count":
        return "INT"
    if func == "avg" or func == "sum" and argument_type == "REAL":
        return "REAL"
    if func == "sum":
        return "BIGINT"
    return argument_type


def aggregate_result_definition(name, func, argument):
    """The column an aggregate produces over the argument column (None for COUNT(*)):
    MIN and MAX keep a VARCHAR's length."""
    argument_type = None if argument is None else argument.type
    length = argument.length if func in ("min", "max") else None
    return ColumnDef(name, aggregate_result_type(func, argument_type), length)


def common_definition(name, a, b):
    """The type of a set operation's column whose inputs have types a and b, of the
    same class: the longer VARCHAR, or the wider number (INT < BIGINT < REAL)."""
    if a.type == "VARCHAR":
        return ColumnDef(name, "VARCHAR", max(a.length, b.length))
    if a.type == b.type:
        return ColumnDef(name, a.type, a.length)
    return ColumnDef(name, "REAL" if "REAL" in (a.type, b.type) else "BIGINT")


class ColumnDef:
    __slots__ = ("name", "type", "length")

    def __init__(self, name, type, length=None):
        self.name = name
        self.type = type
        self.length = length

    def to_json(self):
        return [self.name, self.type, self.length] if self.type == "VARCHAR" else [self.name, self.type]

    def type_text(self):
        return f"VARCHAR({self.length})" if self.type == "VARCHAR" else self.type

    def __eq__(self, other):
        return (isinstance(other, ColumnDef) and self.name == other.name
                and self.type == other.type and self.length == other.length)

    def __repr__(self):
        return f"{self.name}:{self.type_text()}"


def is_ascii_digits(text):
    return text.isascii() and text.isdigit()


def parse_schema(text):
    """Parse "a:INT,b:VARCHAR(20)" into column definitions."""
    columns = []
    for part in text.split(","):
        name, sep, type_text = part.strip().partition(":")
        if not sep or not name:
            raise StorageError(f"Bad column definition {part.strip()!r}, expected name:TYPE")
        type_text = type_text.strip().upper()
        if type_text == "INT":
            columns.append(ColumnDef(name, "INT"))
        elif type_text.startswith("VARCHAR(") and type_text.endswith(")") and is_ascii_digits(type_text[8:-1]):
            length = int(type_text[8:-1])
            if not 1 <= length <= MAX_VARCHAR:
                raise StorageError(f"VARCHAR length for {name} must be between 1 and {MAX_VARCHAR}")
            columns.append(ColumnDef(name, "VARCHAR", length))
        else:
            raise StorageError(f"Unknown type {type_text!r} for column {name}")
    if len({c.name for c in columns}) != len(columns):
        raise StorageError("Duplicate column name in schema")
    return columns


def encode_field(column, value):
    if column.type == "VARCHAR":
        data = value.encode("utf-8")
        if len(data) > column.length:
            raise StorageError(
                f"Value {value!r} is {len(data)} bytes, longer than VARCHAR({column.length}) column {column.name}")
        return LENGTH.pack(len(data)) + data
    return PACK[column.type].pack(value)


def encode(schema, values):
    return b"".join(encode_field(column, value) for column, value in zip(schema, values))


def decode_field(column_type, data, offset):
    """The value stored at offset and the offset just past it."""
    if column_type == "VARCHAR":
        (length,) = LENGTH.unpack_from(data, offset)
        start = offset + LENGTH.size
        return data[start:start + length].decode("utf-8"), start + length
    return PACK[column_type].unpack_from(data, offset)[0], offset + FIXED_WIDTH[column_type]


def decoder(schema):
    """Return bytes -> tuple for a schema, fast path when every column is fixed width."""
    if all(c.type in FIXED_WIDTH for c in schema):
        return struct.Struct("<" + "".join(FIELD_FORMATS[c.type] for c in schema)).unpack
    types = [c.type for c in schema]

    def decode(data):
        values = []
        offset = 0
        for column_type in types:
            value, offset = decode_field(column_type, data, offset)
            values.append(value)
        return tuple(values)
    return decode


def sizer(schema):
    """Return tuple -> encoded record length without encoding it."""
    fixed = sum(FIXED_WIDTH.get(c.type, LENGTH.size) for c in schema)
    varchar_positions = [i for i, c in enumerate(schema) if c.type == "VARCHAR"]
    if not varchar_positions:
        return lambda values: fixed

    def size(values):
        return fixed + sum(len(values[i].encode("utf-8")) for i in varchar_positions)
    return size
