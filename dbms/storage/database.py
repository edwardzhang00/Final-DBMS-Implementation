"""A database directory: catalog.db, one <table>.db per table, tmp/ for spills."""

import itertools
import os
import shutil

from .bufferpool import BufferPool
from .catalog import Catalog
from .disk import DiskManager, StorageError
from .heapfile import HeapFile

TEMP_NUMBERS = itertools.count(1)


class Database:
    def __init__(self, directory, frames, policy="clock"):
        os.makedirs(directory, exist_ok=True)
        self.directory = directory
        self.pool = BufferPool(frames, policy)
        self.catalog_disk = DiskManager(os.path.join(directory, "catalog.db"))
        self.catalog = Catalog(self.pool, self.catalog_disk)
        self.disks = {}
        self.temp_dir = os.path.join(directory, "tmp")

    def disk_for(self, info):
        disk = self.disks.get(info.name)
        if disk is None:
            disk = DiskManager(os.path.join(self.directory, info.file))
            self.disks[info.name] = disk
        return disk

    def heap_file(self, name):
        info = self.catalog.get_table(name)
        return HeapFile(self.pool, self.disk_for(info), info)

    def create_table(self, name, schema):
        info = self.catalog.create_table(name, schema)
        HeapFile.create(self.pool, self.disk_for(info), info)
        self.save()
        return info

    def save(self):
        self.catalog.save()
        self.pool.flush_all()

    def temp_path(self):
        os.makedirs(self.temp_dir, exist_ok=True)
        return os.path.join(self.temp_dir, f"t{os.getpid()}_{next(TEMP_NUMBERS)}.tmp")

    def remove_temp_dir(self):
        if os.path.isdir(self.temp_dir) and not os.listdir(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def close(self):
        self.pool.flush_all()
        for disk in self.disks.values():
            disk.close()
        self.catalog_disk.close()


def open_database(directory, frames, policy="clock"):
    """Open an existing database; unlike Database() it never creates one."""
    if not os.path.exists(os.path.join(directory, "catalog.db")):
        raise StorageError(f"No database in {directory} (catalog.db not found)")
    return Database(directory, frames, policy)
