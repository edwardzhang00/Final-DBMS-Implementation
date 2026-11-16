"""Byte layouts of the two page kinds: slotted data pages and directory pages.

Slotted data page
  offset 0  uint32 page_id
  offset 4  uint16 slot_count   (includes dead slots)
  offset 6  uint16 free_end     (lowest byte offset used by record data)
  offset 8  slot i at 8 + 4i:  uint16 offset, uint16 length  (offset 0 = dead)
  record data grows backward from the end of the page

Directory page (page 0 of every table file is the first one)
  offset 0  uint32 page_id
  offset 4  uint32 next_dir_page (0xFFFFFFFF if none)
  offset 8  uint16 entry_count
  offset 10 entry i at 10 + 6i: uint32 data_page_id, uint16 free_bytes
"""

import struct
from collections import namedtuple

from .disk import PAGE_SIZE, StorageError

PAGE_HEADER = struct.Struct("<IHH")
SLOT = struct.Struct("<HH")
HEADER_SIZE = PAGE_HEADER.size
SLOT_SIZE = SLOT.size
USABLE_SPACE = PAGE_SIZE - HEADER_SIZE
MAX_RECORD_SIZE = USABLE_SPACE - SLOT_SIZE

DIR_HEADER = struct.Struct("<IIH")
DIR_ENTRY = struct.Struct("<IH")
DIR_HEADER_SIZE = DIR_HEADER.size
DIR_ENTRY_SIZE = DIR_ENTRY.size
DIR_CAPACITY = (PAGE_SIZE - DIR_HEADER_SIZE) // DIR_ENTRY_SIZE
FIRST_DIRECTORY_PAGE = 0
NO_PAGE = 0xFFFFFFFF

PageHeader = namedtuple("PageHeader", "page_id slot_count free_end")
DirectoryHeader = namedtuple("DirectoryHeader", "page_id next_dir_page entry_count")


def init_data_page(page, page_id):
    PAGE_HEADER.pack_into(page, 0, page_id, 0, PAGE_SIZE)


def page_header(page):
    return PageHeader(*PAGE_HEADER.unpack_from(page, 0))


def free_space(page):
    _, slot_count, free_end = PAGE_HEADER.unpack_from(page, 0)
    return free_end - (HEADER_SIZE + SLOT_SIZE * slot_count)


def insert_record(page, record):
    """Insert and return the slot number, or None when the page has no room."""
    page_id, slot_count, free_end = PAGE_HEADER.unpack_from(page, 0)
    length = len(record)
    if free_end - (HEADER_SIZE + SLOT_SIZE * slot_count) < length + SLOT_SIZE:
        return None
    free_end -= length
    page[free_end:free_end + length] = record
    SLOT.pack_into(page, HEADER_SIZE + SLOT_SIZE * slot_count, free_end, length)
    PAGE_HEADER.pack_into(page, 0, page_id, slot_count + 1, free_end)
    return slot_count


def get_record(page, slot):
    page_id, slot_count, _ = PAGE_HEADER.unpack_from(page, 0)
    if slot >= slot_count:
        raise StorageError(f"Slot {slot} does not exist on page {page_id} (rid ({page_id}, {slot}))")
    offset, length = SLOT.unpack_from(page, HEADER_SIZE + SLOT_SIZE * slot)
    if offset == 0:
        raise StorageError(f"Record ({page_id}, {slot}) was deleted")
    return page[offset:offset + length]


def delete_record(page, slot):
    get_record(page, slot)
    SLOT.pack_into(page, HEADER_SIZE + SLOT_SIZE * slot, 0, 0)


def slot_entries(page):
    """Every slot as (slot, offset, length), dead ones included."""
    _, slot_count, _ = PAGE_HEADER.unpack_from(page, 0)
    return [(i,) + SLOT.unpack_from(page, HEADER_SIZE + SLOT_SIZE * i) for i in range(slot_count)]


def live_records(page):
    _, slot_count, _ = PAGE_HEADER.unpack_from(page, 0)
    for i in range(slot_count):
        offset, length = SLOT.unpack_from(page, HEADER_SIZE + SLOT_SIZE * i)
        if offset != 0:
            yield i, page[offset:offset + length]


def init_directory_page(page, page_id):
    DIR_HEADER.pack_into(page, 0, page_id, NO_PAGE, 0)


def directory_header(page):
    return DirectoryHeader(*DIR_HEADER.unpack_from(page, 0))


def directory_entries(page):
    _, _, count = DIR_HEADER.unpack_from(page, 0)
    return [DIR_ENTRY.unpack_from(page, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * i) for i in range(count)]


def set_directory_entry(page, index, data_page_id, free_bytes):
    DIR_ENTRY.pack_into(page, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * index, data_page_id, free_bytes)


def append_directory_entry(page, data_page_id, free_bytes):
    page_id, next_page, count = DIR_HEADER.unpack_from(page, 0)
    set_directory_entry(page, count, data_page_id, free_bytes)
    DIR_HEADER.pack_into(page, 0, page_id, next_page, count + 1)


def set_next_directory(page, next_page):
    page_id, _, count = DIR_HEADER.unpack_from(page, 0)
    DIR_HEADER.pack_into(page, 0, page_id, next_page, count)


class PageBuilder:
    """Packs records into a slotted page image exactly as insert_record would."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.page = bytearray(PAGE_SIZE)
        init_data_page(self.page, 0)
        self.count = 0

    def fits(self, length):
        return free_space(self.page) >= length + SLOT_SIZE

    def add(self, record):
        check_record_fits(len(record))
        insert_record(self.page, record)
        self.count += 1


def check_record_fits(length):
    """A tuple an operator holds or spills must fit on one page, as a stored record must."""
    if length > MAX_RECORD_SIZE:
        raise StorageError(f"A tuple of {length} bytes does not fit on a page "
                           f"(at most {MAX_RECORD_SIZE} bytes)")


def records_per_page(length):
    return USABLE_SPACE // (length + SLOT_SIZE)
