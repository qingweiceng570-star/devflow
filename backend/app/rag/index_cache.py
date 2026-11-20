"""Bounded in-process cache for immutable BM25 generations.

The file is stat'ed on every access so deletion/corruption cannot be hidden by a
previously loaded generation. Database authorization still runs on every query.
"""

import sys
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from app.rag.bm25_store import Bm25Index


def _weight(index: Bm25Index) -> int:
    # Counting shared integers more than once deliberately overestimates size.
    size = sys.getsizeof(index) + sys.getsizeof(index.ids) + sys.getsizeof(index.lengths)
    size += sum(sys.getsizeof(value) for value in (*index.ids, *index.lengths))
    size += sys.getsizeof(index.postings)
    for term, docs in index.postings.items():
        size += sys.getsizeof(term) + sys.getsizeof(docs)
        size += sum(sys.getsizeof(doc) + sys.getsizeof(tf) for doc, tf in docs.items())
    return size


class Bm25Cache:
    def __init__(self, max_entries: int = 8, max_bytes: int = 64 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def load(self, path: Path) -> Bm25Index:
        path = path.resolve()
        try:
            stat = path.stat()
        except OSError:
            self.discard(path)
            raise
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        with self._lock:
            entry = self._entries.get(path)
            if entry is not None and entry[0] == signature:
                self._entries.move_to_end(path)
                return entry[1]
            self.discard(path)
        index = Bm25Index.load(path)
        weight = _weight(index)
        after = path.stat()
        if (after.st_mtime_ns, after.st_size, after.st_ino) != signature:
            raise OSError("Index generation changed while loading")
        if weight <= self.max_bytes and self.max_entries > 0:
            with self._lock:
                self.discard(path)
                while self._entries and (
                    len(self._entries) >= self.max_entries or self._bytes + weight > self.max_bytes
                ):
                    _, old = self._entries.popitem(last=False)
                    self._bytes -= old[2]
                self._entries[path] = (signature, index, weight)
                self._bytes += weight
        return index

    def discard(self, path: Path) -> None:
        with self._lock:
            old = self._entries.pop(path.resolve(), None)
            if old is not None:
                self._bytes -= old[2]


bm25_cache = Bm25Cache()
