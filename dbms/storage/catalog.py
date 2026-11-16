"""Catalog stored in catalog.db and read through the buffer pool.

Every catalog page:
  offset 0  uint32 page_id
  offset 4  uint16 bytes of payload on this page
  offset 6  payload chunk (up to CHUNK_SIZE bytes)
The payload, concatenated across pages in page order, is a sequence of
little-endian fields (str = uint16 length + UTF-8 bytes):
  uint16 table_count, then per table:
    str name, str file, uint32 num_pages, uint32 data_pages, uint32 num_tuples,
    uint16 column_count,
    per column: str name, uint8 type (0 INT, 1 VARCHAR), uint16 length
    uint8 has_stats, and when 1 (written by ANALYZE):
      uint32 n_tuples, uint32 n_pages, float64 width
      per column: uint32 ndv, float64 avg_width, uint8 has_range,
        [value min, value max], uint16 bucket_count,
        per bucket: value lo, value hi, uint32 count, uint32 ndv
  value = the column's own encoding (INT 4 bytes, VARCHAR uint16 length + bytes)
The whole catalog is rewritten on every change, so the same content always
gives the same bytes. A table's first directory page is always page 0 of its
file, so it is not stored.
"""

import struct

from .disk import PAGE_SIZE, StorageError
from .record import ColumnDef, LENGTH, encode_field, decode_field

CHUNK_HEADER = struct.Struct("<IH")
CHUNK_SIZE = PAGE_SIZE - CHUNK_HEADER.size
TYPE_CODES = {"INT": 0, "VARCHAR": 1}
TYPE_NAMES = {code: type_name for type_name, code in TYPE_CODES.items()}


class CatalogError(StorageError):
    pass


def is_identifier(name):
    first = name[:1]
    return (first == "_" or first.isascii() and first.isalpha()) and all(
        ch == "_" or ch.isascii() and ch.isalnum() for ch in name)


def check_table_name(name, existing):
    """Raise CatalogError unless a new table may be called name beside the existing names."""
    if not is_identifier(name):
        raise CatalogError(f"Table name {name!r} must be a letter or underscore followed by letters, "
                           f"digits and underscores")
    if name.lower() == "catalog":
        raise CatalogError(f"The name {name} is taken by the catalog's own file, catalog.db")
    if name in existing:
        raise CatalogError(f"Table {name} already exists")
    # A table's file is <name>.db, and file systems such as Windows' ignore case.
    same_file = [t for t in existing if t.lower() == name.lower()]
    if same_file:
        raise CatalogError(f"Table {name} would share a file with table {same_file[0]}: "
                           f"table names may not differ only in case")


class Bucket:
    __slots__ = ("lo", "hi", "count", "ndv")

    def __init__(self, lo, hi, count, ndv):
        self.lo = lo
        self.hi = hi
        self.count = count
        self.ndv = ndv


class ColumnStats:
    def __init__(self, ndv, avg_width, min_value, max_value, buckets):
        self.ndv = ndv
        self.avg_width = avg_width
        self.min = min_value
        self.max = max_value
        self.buckets = buckets


class TableStats:
    def __init__(self, n_tuples, n_pages, width, columns):
        self.n_tuples = n_tuples
        self.n_pages = n_pages
        self.width = width
        self.columns = columns


class TableInfo:
    def __init__(self, name, file, schema, num_pages=0, data_pages=0, num_tuples=0, stats=None):
        self.name = name
        self.file = file
        self.schema = schema
        self.num_pages = num_pages
        self.data_pages = data_pages
        self.num_tuples = num_tuples
        self.stats = stats


class Writer:
    def __init__(self):
        self.out = bytearray()

    def pack(self, fmt, *values):
        self.out += struct.pack("<" + fmt, *values)

    def text(self, text):
        data = text.encode("utf-8")
        self.out += LENGTH.pack(len(data)) + data

    def value(self, column, value):
        self.out += encode_field(column, value)


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def unpack(self, fmt):
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return values if len(values) > 1 else values[0]

    def text(self):
        return self.field("VARCHAR")

    def value(self, column):
        return self.field(column.type)

    def field(self, column_type):
        value, self.pos = decode_field(column_type, self.data, self.pos)
        return value


def encode_catalog(tables):
    writer = Writer()
    writer.pack("H", len(tables))
    for table in tables:
        writer.text(table.name)
        writer.text(table.file)
        writer.pack("III", table.num_pages, table.data_pages, table.num_tuples)
        writer.pack("H", len(table.schema))
        for column in table.schema:
            writer.text(column.name)
            writer.pack("BH", TYPE_CODES[column.type], column.length or 0)
        writer.pack("B", 1 if table.stats else 0)
        if table.stats:
            stats = table.stats
            writer.pack("IId", stats.n_tuples, stats.n_pages, stats.width)
            for column, column_stats in zip(table.schema, stats.columns):
                writer.pack("IdB", column_stats.ndv, column_stats.avg_width, 0 if column_stats.min is None else 1)
                if column_stats.min is not None:
                    writer.value(column, column_stats.min)
                    writer.value(column, column_stats.max)
                writer.pack("H", len(column_stats.buckets))
                for bucket in column_stats.buckets:
                    writer.value(column, bucket.lo)
                    writer.value(column, bucket.hi)
                    writer.pack("II", bucket.count, bucket.ndv)
    return bytes(writer.out)


def decode_catalog(data):
    reader = Reader(data)
    tables = []
    for _ in range(reader.unpack("H")):
        name = reader.text()
        file = reader.text()
        num_pages, data_pages, num_tuples = reader.unpack("III")
        schema = []
        for _ in range(reader.unpack("H")):
            column_name = reader.text()
            code, length = reader.unpack("BH")
            schema.append(ColumnDef(column_name, TYPE_NAMES[code], length if code == TYPE_CODES["VARCHAR"] else None))
        stats = None
        if reader.unpack("B"):
            n_tuples, n_pages, width = reader.unpack("IId")
            columns = []
            for column in schema:
                ndv, avg_width, has_range = reader.unpack("IdB")
                min_value = max_value = None
                if has_range:
                    min_value = reader.value(column)
                    max_value = reader.value(column)
                buckets = []
                for _ in range(reader.unpack("H")):
                    lo = reader.value(column)
                    hi = reader.value(column)
                    count, bucket_ndv = reader.unpack("II")
                    buckets.append(Bucket(lo, hi, count, bucket_ndv))
                columns.append(ColumnStats(ndv, avg_width, min_value, max_value, buckets))
            stats = TableStats(n_tuples, n_pages, width, columns)
        tables.append(TableInfo(name, file, schema, num_pages, data_pages, num_tuples, stats))
    return tables


class Catalog:
    def __init__(self, pool, disk):
        self.pool = pool
        self.disk = disk
        self.tables = {}
        payload = bytearray()
        for page_id in range(disk.num_pages()):
            page = pool.fetch_page(disk, page_id)
            _, used = CHUNK_HEADER.unpack_from(page, 0)
            payload += page[CHUNK_HEADER.size:CHUNK_HEADER.size + used]
            pool.unpin_page(disk, page_id, False)
        if payload:
            for table in decode_catalog(payload):
                self.tables[table.name] = table

    def create_table(self, name, schema):
        check_table_name(name, self.tables)
        info = TableInfo(name, f"{name}.db", schema)
        self.tables[name] = info
        return info

    def schemas(self):
        return {name: info.schema for name, info in self.tables.items()}

    def get_table(self, name):
        info = self.tables.get(name)
        if info is None:
            raise CatalogError(f"Unknown table {name}")
        return info

    def save(self):
        payload = encode_catalog([self.tables[n] for n in sorted(self.tables)])
        chunks = [payload[i:i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)]
        for page_id in range(max(len(chunks), self.disk.num_pages())):
            chunk = chunks[page_id] if page_id < len(chunks) else b""
            if page_id < self.disk.num_pages():
                page = self.pool.fetch_page(self.disk, page_id)
            else:
                page_id, page = self.pool.new_page(self.disk)
            page[:] = bytes(PAGE_SIZE)
            CHUNK_HEADER.pack_into(page, 0, page_id, len(chunk))
            page[CHUNK_HEADER.size:CHUNK_HEADER.size + len(chunk)] = chunk
            self.pool.unpin_page(self.disk, page_id, True)
