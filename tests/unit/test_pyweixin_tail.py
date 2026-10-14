"""
读消息性能：只解析末尾 N 条、缓存消息 List 控件、窗口重置后重新暖机。
用假控件模拟 UIA，不需要 Windows / 微信。
"""

from types import SimpleNamespace

import pytest

from gateway.adapters import pyweixin as pw
from gateway.adapters.pyweixin import (
    PyWeixinAdapter,
    _diff_new_message_rows,
    _tail_list_items,
)


class FakeText:
    def __init__(self, s):
        self._s = s
        self.element_info = SimpleNamespace(name=s)

    def window_text(self):
        return self._s


class FakeItem:
    def __init__(self, sender, content):
        self._parts = [sender, content]
        self.element_info = SimpleNamespace(name="\n".join(self._parts))
        self.parsed = 0

    def descendants(self, control_type=None):
        self.parsed += 1
        return [FakeText(p) for p in self._parts]

    def window_text(self):
        return "\n".join(self._parts)


class FakeList:
    def __init__(self, items):
        self.items = items
        self.alive = True
        self.element_info = SimpleNamespace()  # 无 .element → 走 children() 兜底

    def children(self, control_type=None):
        if not self.alive:
            raise RuntimeError("element gone")
        return list(self.items)


class FakeWin:
    def __init__(self, lst):
        self.lst = lst
        self.list_scans = 0

    def descendants(self, control_type=None):
        if control_type == "List":
            self.list_scans += 1
            return [self.lst]
        return []


def _msgs(n, start=0):
    return [FakeItem("张三", f"记账 飞机号 金额{i}") for i in range(start, start + n)]


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(pw, "load_my_alias", lambda chat: "")
    monkeypatch.setattr(pw, "load_room_nicks", lambda chat: [])
    a = PyWeixinAdapter()
    a._tail_items = 30
    return a


def test_tail_list_items_fallback_keeps_order():
    lst = FakeList(_msgs(100))
    tail = _tail_list_items(lst, 30)
    assert len(tail) == 30
    assert tail[0] is lst.items[70] and tail[-1] is lst.items[99]
    assert len(_tail_list_items(lst, 0)) == 100


def test_only_tail_items_are_parsed(adapter):
    lst = FakeList(_msgs(2000))
    win = FakeWin(lst)
    rows = adapter._read_dialog_messages_detailed(win, chat_name="群A")
    assert len(rows) == 30
    assert rows[-1][2] == "记账 飞机号 金额1999"
    # 前面 1970 条历史气泡完全没有被解析
    assert sum(it.parsed for it in lst.items[:1970]) == 0


def test_message_list_is_cached_between_polls(adapter):
    lst = FakeList(_msgs(50))
    win = FakeWin(lst)
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    assert win.list_scans == 1


def test_stale_cached_list_triggers_one_full_rescan(adapter):
    old = FakeList(_msgs(10))
    win = FakeWin(old)
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    old.alive = False
    win.lst = FakeList(_msgs(10, start=10))
    rows = adapter._read_dialog_messages_detailed(win, chat_name="群A")
    assert win.list_scans == 2
    assert rows[-1][2] == "记账 飞机号 金额19"


def test_cache_expires_after_ttl(adapter):
    adapter._list_cache_ttl = -1
    win = FakeWin(FakeList(_msgs(5)))
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    adapter._read_dialog_messages_detailed(win, chat_name="群A")
    assert win.list_scans == 2


def test_tail_window_diff_finds_appended_messages():
    def rows(lo, hi):
        return [("张三", f"记账 金额{i}", "文本", "") for i in range(lo, hi)]

    prev = rows(70, 100)  # 上一轮末尾 30 条
    curr = rows(72, 102)  # 新来 2 条，窗口整体后移
    assert _diff_new_message_rows(prev, curr) == rows(100, 102)
    assert _diff_new_message_rows(curr, curr) == []


def test_forget_chat_state_rewarms(adapter):
    adapter._warmed.add("群A")
    adapter._last_snapshot["群A"] = [("a", "b", "文本", "")]
    adapter._list_cache["群A"] = (object(), 0.0)
    adapter._dialogs["群A"] = object()
    adapter._hwnd_names[123] = "群A"
    adapter._forget_chat_state("群A", 123)
    assert "群A" not in adapter._warmed
    assert "群A" not in adapter._last_snapshot
    assert "群A" not in adapter._list_cache
    assert "群A" not in adapter._dialogs
    assert 123 not in adapter._hwnd_names


def test_window_name_cached_by_hwnd(adapter, monkeypatch):
    calls = []

    def fake_name(win):
        calls.append(win)
        return "群A"

    monkeypatch.setattr(adapter, "_chat_window_display_name", fake_name)
    w = SimpleNamespace(handle=42)
    assert adapter._window_name_cached(w) == "群A"
    assert adapter._window_name_cached(w) == "群A"
    assert len(calls) == 1
    adapter._prune_hwnd_names(set())
    assert adapter._window_name_cached(w) == "群A"
    assert len(calls) == 2
