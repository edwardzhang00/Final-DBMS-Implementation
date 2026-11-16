"""Disk manager: fixed 4096-byte pages addressed by page id, every call counted.

This module and the buffer pool above it are the only code that touches a
table file.
"""

import os

PAGE_SIZE = 4096


class StorageError(Exception):
    pass


class DiskManager:
    def __init__(self, path, fresh=False):
        self.path = path
        mode = "r+b" if os.path.exists(path) and not fresh else "w+b"
        self.file = open(path, mode, buffering=0)
        self.reads = 0
        self.writes = 0

    def read_page(self, page_id):
        if page_id >= self.num_pages():
            raise StorageError(f"Page {page_id} does not exist in {os.path.basename(self.path)}")
        self.file.seek(page_id * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        self.reads += 1
        return data

    def write_page(self, page_id, data):
        self.file.seek(page_id * PAGE_SIZE)
        self.file.write(data)
        self.writes += 1

    def allocate_page(self):
        """Extend the file by one zero page. Not a write_page call, so not counted."""
        page_id = self.num_pages()
        self.file.truncate((page_id + 1) * PAGE_SIZE)
        return page_id

    def num_pages(self):
        return os.fstat(self.file.fileno()).st_size // PAGE_SIZE

    def close(self):
        self.file.close()

    def delete(self):
        self.file.close()
        os.remove(self.path)
