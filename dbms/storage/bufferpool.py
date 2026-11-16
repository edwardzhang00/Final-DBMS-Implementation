"""Buffer pool with pinning, dirty tracking and LRU or CLOCK replacement.

The pool also enforces the memory budget of the executor. An operator that
buffers tuples reserves frames, which takes them out of the pool, so cached
pages, pinned pages and operator buffers together never exceed B frames.
"""

from collections import OrderedDict

from .disk import PAGE_SIZE, DiskManager, StorageError


class BufferPoolError(StorageError):
    pass


class Frame:
    __slots__ = ("disk", "page_id", "data", "pin_count", "dirty", "reserved")

    def __init__(self):
        self.disk = None
        self.page_id = None
        self.data = None
        self.pin_count = 0
        self.dirty = False
        self.reserved = False

    def evictable(self):
        return self.disk is not None and self.pin_count == 0 and not self.reserved


class LRUPolicy:
    def __init__(self, frames):
        self.frames = frames
        self.order = OrderedDict()

    def on_access(self, frame_index):
        self.order.pop(frame_index, None)
        self.order[frame_index] = True

    def on_load(self, frame_index):
        self.on_access(frame_index)

    def forget(self, frame_index):
        self.order.pop(frame_index, None)

    def choose_victim(self):
        for frame_index in self.order:
            if self.frames[frame_index].evictable():
                return frame_index
        return None


class ClockPolicy:
    def __init__(self, frames):
        self.frames = frames
        self.ref = [0] * len(frames)
        self.hand = 0

    def on_access(self, frame_index):
        self.ref[frame_index] = 1

    def on_load(self, frame_index):
        self.ref[frame_index] = 1
        self.hand = (frame_index + 1) % len(self.frames)

    def forget(self, frame_index):
        self.ref[frame_index] = 0

    def choose_victim(self):
        size = len(self.frames)
        swept = 0
        while True:
            frame_index = self.hand
            frame = self.frames[frame_index]
            if not frame.evictable():
                self.hand = (self.hand + 1) % size
                swept += 1
            elif self.ref[frame_index] == 1:
                self.ref[frame_index] = 0
                self.hand = (self.hand + 1) % size
                swept += 1
            else:
                self.hand = (self.hand + 1) % size
                return frame_index
            if swept > 2 * size:
                return None


POLICIES = {"lru": LRUPolicy, "clock": ClockPolicy}


class BufferPool:
    def __init__(self, frames, policy="lru"):
        if frames < 1:
            raise BufferPoolError("A buffer pool needs at least one frame")
        self.size = frames
        self.frames = [Frame() for _ in range(frames)]
        self.policy = POLICIES[policy](self.frames)
        self.page_table = {}
        self.hits = self.misses = self.evictions = self.reads = self.writes = 0
        self.pinned = 0
        self.reserved = 0
        self.peak_held = 0

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "reads": self.reads, "writes": self.writes}

    def reset_stats(self):
        self.hits = self.misses = self.evictions = self.reads = self.writes = 0
        self.peak_held = self.held()

    def held(self):
        """Frames in use by pinned pages and operator buffers."""
        return self.pinned + self.reserved

    def note_held(self):
        self.peak_held = max(self.peak_held, self.held())

    def take_frame(self):
        """An empty frame, else an evicted one. Returns (index, replaced_a_page)."""
        for frame_index, frame in enumerate(self.frames):
            if frame.disk is None and not frame.reserved:
                return frame_index, False
        frame_index = self.policy.choose_victim()
        if frame_index is None:
            raise BufferPoolError("no unpinned frame available")
        frame = self.frames[frame_index]
        if frame.dirty:
            frame.disk.write_page(frame.page_id, frame.data)
            self.writes += 1
        del self.page_table[(frame.disk, frame.page_id)]
        frame.disk = frame.page_id = frame.data = None
        frame.dirty = False
        self.policy.forget(frame_index)
        return frame_index, True

    def fetch_page(self, disk, page_id):
        frame_index = self.page_table.get((disk, page_id))
        if frame_index is not None:
            frame = self.frames[frame_index]
            if frame.pin_count == 0:
                self.pinned += 1
            frame.pin_count += 1
            self.policy.on_access(frame_index)
            self.hits += 1
            self.note_held()
            return frame.data
        frame_index, replaced = self.take_frame()
        frame = self.frames[frame_index]
        frame.data = bytearray(disk.read_page(page_id))
        self.reads += 1
        self.misses += 1
        if replaced:
            self.evictions += 1
        frame.disk = disk
        frame.page_id = page_id
        frame.pin_count = 1
        frame.dirty = False
        self.pinned += 1
        self.page_table[(disk, page_id)] = frame_index
        self.policy.on_load(frame_index)
        self.note_held()
        return frame.data

    def unpin_page(self, disk, page_id, is_dirty):
        frame_index = self.page_table.get((disk, page_id))
        if frame_index is None:
            raise BufferPoolError(f"Cannot unpin page {page_id}: it is not resident")
        frame = self.frames[frame_index]
        if frame.pin_count == 0:
            raise BufferPoolError(f"Cannot unpin page {page_id}: pin count is already 0")
        frame.dirty = frame.dirty or is_dirty
        frame.pin_count -= 1
        if frame.pin_count == 0:
            self.pinned -= 1

    def new_page(self, disk):
        """Allocate a page on disk and return (page_id, data): zero filled, pinned, dirty."""
        frame_index, _ = self.take_frame()
        page_id = disk.allocate_page()
        frame = self.frames[frame_index]
        frame.disk = disk
        frame.page_id = page_id
        frame.data = bytearray(PAGE_SIZE)
        frame.pin_count = 1
        frame.dirty = True
        self.pinned += 1
        self.page_table[(disk, page_id)] = frame_index
        self.policy.on_load(frame_index)
        self.note_held()
        return page_id, frame.data

    def flush_all(self):
        for frame in self.frames:
            if frame.disk is not None and frame.dirty:
                frame.disk.write_page(frame.page_id, frame.data)
                self.writes += 1
                frame.dirty = False

    def reserve(self, count):
        """Take count frames out of the pool for an operator's own buffers: all of them or none."""
        for taken in range(count):
            try:
                frame_index, _ = self.take_frame()
            except BufferPoolError:
                message = (f"Memory budget exceeded: cannot reserve a frame, {self.pinned} pinned and "
                           f"{self.reserved} reserved out of {self.size}")
                self.release(taken)
                raise BufferPoolError(message) from None
            self.frames[frame_index].reserved = True
            self.reserved += 1
        self.note_held()

    def release(self, count):
        for frame in reversed(self.frames):
            if count == 0:
                break
            if frame.reserved:
                frame.reserved = False
                self.reserved -= 1
                count -= 1

    def clear(self):
        """Write dirty pages and empty every unpinned frame, so the next
        statement starts from a cold pool."""
        self.flush_all()
        for frame_index, frame in enumerate(self.frames):
            if frame.disk is not None and frame.pin_count == 0:
                del self.page_table[(frame.disk, frame.page_id)]
                frame.disk = frame.page_id = frame.data = None
                self.policy.forget(frame_index)


def replay_trace(path, page_ids, frames, policy):
    """Fetch and unpin each page id in turn, clean, through a fresh pool over a
    scratch file at path; the file is deleted afterwards. Returns the pool."""
    disk = DiskManager(path, fresh=True)
    while disk.num_pages() <= max(page_ids, default=0):
        disk.allocate_page()
    pool = BufferPool(frames, policy)
    for page_id in page_ids:
        pool.fetch_page(disk, page_id)
        pool.unpin_page(disk, page_id, False)
    disk.delete()
    return pool
