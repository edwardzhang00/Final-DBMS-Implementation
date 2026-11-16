"""Temporary files for sort runs and hash partitions.

Like PostgreSQL's BufFile they bypass the shared buffer pool: an operator
reads and writes them directly into frames it has reserved, so every page
moved is exactly one counted disk call and nothing lingers dirty in the pool.
"""

from .disk import DiskManager


class TempFile(DiskManager):
    def __init__(self, path):
        super().__init__(path, fresh=True)
        self.pages = 0

    def append_page(self, data):
        page_id = self.allocate_page()
        self.write_page(page_id, data)
        self.pages += 1
        return page_id
