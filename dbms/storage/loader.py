"""Bulk loader: CSV with no header, one record per line, commas between fields.

A field containing a comma is wrapped in double quotes; inside quotes two
double quotes stand for one. The whole file is checked before the first row
is inserted, so a bad line leaves the table unchanged.
"""

from .disk import StorageError
from .page import MAX_RECORD_SIZE
from .record import INT_MIN, INT_MAX, sizer, is_ascii_digits


def split_csv_line(line, line_no):
    fields = []
    field = []
    i = 0
    quoted = False
    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    field.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                field.append(ch)
        elif ch == '"' and not field:
            quoted = True
        elif ch == ",":
            fields.append("".join(field))
            field = []
        else:
            field.append(ch)
        i += 1
    if quoted:
        raise StorageError(f"Line {line_no}: unterminated quoted field")
    fields.append("".join(field))
    return fields


def parse_int(text):
    digits = text[1:] if text[:1] in ("-", "+") else text
    if not is_ascii_digits(digits):
        return None
    value = int(text)
    return value if INT_MIN <= value <= INT_MAX else None


def parse_row(schema, fields, line_no, record_size):
    if len(fields) != len(schema):
        raise StorageError(f"Line {line_no}: expected {len(schema)} fields, found {len(fields)}")
    values = []
    for col, text in zip(schema, fields):
        if col.type == "INT":
            value = parse_int(text)
            if value is None:
                raise StorageError(f"Line {line_no}, column {col.name}: {text!r} is not a 4-byte INT")
            values.append(value)
        else:
            if len(text.encode("utf-8")) > col.length:
                raise StorageError(
                    f"Line {line_no}, column {col.name}: {text!r} is longer than VARCHAR({col.length})")
            values.append(text)
    if record_size(values) > MAX_RECORD_SIZE:
        raise StorageError(f"Line {line_no}: the record is {record_size(values)} bytes, more than a page holds "
                           f"({MAX_RECORD_SIZE} bytes)")
    return tuple(values)


def read_rows(path, schema):
    record_size = sizer(schema)
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise StorageError(f"Line {line_no}: not valid UTF-8 text") from None
            line = line.rstrip("\n").rstrip("\r")
            yield parse_row(schema, split_csv_line(line, line_no), line_no, record_size)


def load_csv(db, table, path):
    heap = db.heap_file(table)
    for _ in read_rows(path, heap.info.schema):
        pass
    count = 0
    for row in read_rows(path, heap.info.schema):
        heap.insert(row)
        count += 1
    db.save()
    return count
