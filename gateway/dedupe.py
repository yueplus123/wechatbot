"""本地指纹辅助（文档 10.1）。正式去重以数据库唯一约束为最后防线。"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict


def make_fingerprint(
    gateway_id: str,
    chat: str,
    sender: str,
    normalized_text: str,
    ui_timestamp: str = "",
    local_sequence: int | str = "",
) -> str:
    raw = "|".join(
        [
            gateway_id,
            chat,
            sender,
            normalized_text,
            str(ui_timestamp or ""),
            str(local_sequence or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SlidingWindowDedupe:
    """无 UI 时间/序号时的短时滑动窗口防重（默认 60 秒）。"""

    def __init__(self, window_seconds: int = 60, max_size: int = 2000) -> None:
        self.window = window_seconds
        self.max_size = max_size
        self._items: OrderedDict[str, float] = OrderedDict()

    def contains(self, key: str) -> bool:
        """只查询，不写入。"""
        now = time.time()
        self._purge(now)
        return key in self._items

    def mark(self, key: str) -> None:
        now = time.time()
        self._purge(now)
        self._items[key] = now
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)

    def seen(self, key: str) -> bool:
        """兼容旧用法：已存在则 True；否则标记并返回 False。"""
        if self.contains(key):
            return True
        self.mark(key)
        return False

    def _purge(self, now: float) -> None:
        expired = [k for k, t in self._items.items() if now - t > self.window]
        for k in expired:
            del self._items[k]
