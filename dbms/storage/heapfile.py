"""Heap file: data pages listed by a chain of directory pages starting at page 0."""

from .disk import StorageError
from .page import (
    init_data_page, insert_record, get_record, delete_record, live_records, free_space,
    init_directory_page, directory_header, directory_entries, set_directory_entry,
    append_directory_entry, set_next_directory, DIR_CAPACITY, FIRST_DIRECTORY_PAGE, NO_PAGE,
    MAX_RECORD_SIZE, SLOT_SIZE,
)
from .record import encode, decoder


class HeapFile:
    def __init__(self, pool, disk, info):
        self.pool = pool
        self.disk = disk
        self.info = info
        self.decode = decoder(info.schema)

    @classmethod
    def create(cls, pool, disk, info):
        page_id, page = pool.new_page(disk)
        init_directory_page(page, page_id)
        pool.unpin_page(disk, page_id, True)
        info.num_pages = disk.num_pages()
        return cls(pool, disk, info)

    def insert(self, values):
        record = encode(self.info.schema, values)
        if len(record) > MAX_RECORD_SIZE:
            shown = repr(list(values))
            raise StorageError(
                f"Record of {len(record)} bytes for table {self.info.name} does not fit on a page "
                f"(at most {MAX_RECORD_SIZE} bytes plus its slot): row "
                f"{shown if len(shown) <= 80 else shown[:77] + '...'}")
        needed = len(record) + SLOT_SIZE

        dir_id = FIRST_DIRECTORY_PAGE
        while True:
            directory_page = self.pool.fetch_page(self.disk, dir_id)
            for index, (data_id, free_bytes) in enumerate(directory_entries(directory_page)):
                if free_bytes >= needed:
                    data_page = self.pool.fetch_page(self.disk, data_id)
                    slot = insert_record(data_page, record)
                    set_directory_entry(directory_page, index, data_id, free_space(data_page))
                    self.pool.unpin_page(self.disk, data_id, True)
                    self.pool.unpin_page(self.disk, dir_id, True)
                    self.info.num_tuples += 1
                    return data_id, slot
            next_dir = directory_header(directory_page).next_dir_page
            self.pool.unpin_page(self.disk, dir_id, False)
            if next_dir == NO_PAGE:
                break
            dir_id = next_dir

        data_id, data_page = self.pool.new_page(self.disk)
        init_data_page(data_page, data_id)
        slot = insert_record(data_page, record)
        free_bytes = free_space(data_page)
        self.pool.unpin_page(self.disk, data_id, True)

        directory_page = self.pool.fetch_page(self.disk, dir_id)
        if directory_header(directory_page).entry_count < DIR_CAPACITY:
            append_directory_entry(directory_page, data_id, free_bytes)
        else:
            new_dir_id, new_directory_page = self.pool.new_page(self.disk)
            init_directory_page(new_directory_page, new_dir_id)
            append_directory_entry(new_directory_page, data_id, free_bytes)
            self.pool.unpin_page(self.disk, new_dir_id, True)
            set_next_directory(directory_page, new_dir_id)
        self.pool.unpin_page(self.disk, dir_id, True)

        self.info.num_tuples += 1
        self.info.data_pages += 1
        self.info.num_pages = self.disk.num_pages()
        return data_id, slot

    def get(self, rid):
        page_id, slot = rid
        page = self.pool.fetch_page(self.disk, page_id)
        try:
            return self.decode(get_record(page, slot))
        finally:
            self.pool.unpin_page(self.disk, page_id, False)

    def directory_pages(self):
        """(page id, entries) of each directory page in chain order, each unpinned before it is yielded."""
        dir_id = FIRST_DIRECTORY_PAGE
        while dir_id != NO_PAGE:
            directory_page = self.pool.fetch_page(self.disk, dir_id)
            entries = directory_entries(directory_page)
            next_dir = directory_header(directory_page).next_dir_page
            self.pool.unpin_page(self.disk, dir_id, False)
            yield dir_id, entries
            dir_id = next_dir

    def directory_entry(self, page_id, slot):
        """(directory page, entry index) listing data page page_id, or an error."""
        for dir_id, entries in self.directory_pages():
            for index, (data_id, _) in enumerate(entries):
                if data_id == page_id:
                    return dir_id, index
        raise StorageError(f"No record ({page_id}, {slot}) in table {self.info.name}: "
                           f"page {page_id} is not one of its data pages")

    def delete(self, rid):
        page_id, slot = rid
        dir_id, index = self.directory_entry(page_id, slot)
        page = self.pool.fetch_page(self.disk, page_id)
        try:
            delete_record(page, slot)
        except StorageError:
            self.pool.unpin_page(self.disk, page_id, False)
            raise
        free_bytes = free_space(page)
        self.pool.unpin_page(self.disk, page_id, True)
        self.info.num_tuples -= 1

        directory_page = self.pool.fetch_page(self.disk, dir_id)
        _, listed_free_bytes = directory_entries(directory_page)[index]
        changed = listed_free_bytes != free_bytes
        if changed:
            set_directory_entry(directory_page, index, page_id, free_bytes)
        self.pool.unpin_page(self.disk, dir_id, changed)

    def scan(self):
        """Yield (rid, tuple). Never more than one page pinned at a time."""
        for _, entries in self.directory_pages():
            for data_id, _ in entries:
                page = self.pool.fetch_page(self.disk, data_id)
                try:
                    for slot, record in live_records(page):
                        yield (data_id, slot), self.decode(record)
                finally:
                    self.pool.unpin_page(self.disk, data_id, False)
