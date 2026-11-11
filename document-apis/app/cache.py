"""Tiny in-memory TTL cache, so repeat searches don't hit the free sources again."""
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, ttl_seconds: int = 3600, max_items: int = 500):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._data: "OrderedDict[str, tuple[float, object]]" = OrderedDict()

    def get(self, key: str):
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if time.time() > expires:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value) -> None:
        self._data[key] = (time.time() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
