"""
pyweixin 适配器（微信 4.1+ / Windows）

默认听群策略 pinned：
1. 用户把群「单独打开」成独立窗口
2. 点左上角绿色图钉置顶（WS_EX_TOPMOST）
3. Worker 扫描置顶窗 → 只读消息，不搜群、不抢主界面
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.adapters.base import (
    AdapterHealth,
    MessageType,
    RawMessage,
    SendResult,
)
from gateway.chat_names import (
    classify_session_title,
    is_bot_receipt_text,
    is_plausible_group_nickname,
    looks_like_chat_record_list,
    looks_like_chat_record_text,
    nickname_match_key,
    normalize_chat_name,
    normalize_group_nickname,
)
from gateway.sender_resolve import (
    avatar_side_and_hash,
    bubble_looks_self,
    capture_window_image,
    get_window_rect,
    load_avatar_map,
    load_my_alias,
    load_room_nicks,
    match_text_to_candidates,
    ocr_enabled,
    remember_avatar_sender,
    remember_my_alias,
    remember_room_nicks,
    resolve_sender_by_ocr,
    resolve_sender_by_wecom_tag,
)

logger = logging.getLogger(__name__)

_MEMBER_COUNT_RE = re.compile(r"[（(](\d+)[）)]\s*$")
_WS_EX_TOPMOST = 0x00000008
_GWL_EXSTYLE = -20
_PINNED_ALIAS = "置顶窗口自动监听"
# 点头像读资料卡（用户确认可试；成功后会写入头像缓存，减少重复点击）
_AVATAR_CLICK_ENABLED = os.getenv("SENDER_AVATAR_CLICK", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


class PyWeixinAdapter:
    def __init__(
        self,
        *,
        poll_interval: float | None = None,
        pull_number: int = 12,
        listen_mode: str | None = None,
        allow_search: bool | None = None,
    ) -> None:
        self._poll_interval = float(
            poll_interval
            if poll_interval is not None
            else os.getenv("WECHAT_POLL_SECONDS", "2.5")
        )
        self._pull_number = pull_number
        # sticky=独立窗只开一次后只读；search=旧方案（反复搜群，不推荐）
        self._listen_mode = (
            listen_mode or os.getenv("WECHAT_LISTEN_MODE", "pinned")
        ).lower()
        if self._listen_mode in {"window", "sticky"}:
            # sticky/window 仍可用；默认推荐 pinned（用户置顶=监听）
            pass
        if allow_search is None:
            self._allow_search = os.getenv("WECHAT_ALLOW_SEARCH", "0").lower() in {
                "1",
                "true",
                "yes",
            }
        else:
            self._allow_search = allow_search
        self._listen_source = os.getenv("WECHAT_LISTEN_SOURCE", "pinned").lower()
        self._started = False
        self._seq = 0
        # 按「消息列表快照」做增量，避免相同文案重复发送被永久丢掉
        self._last_snapshot: dict[str, list[tuple[str, str, str, str]]] = {}
        self._warmed: set[str] = set()
        self._version = ""
        self._dialogs: dict[str, Any] = {}
        self._open_failed_at: dict[str, float] = {}
        self._missing_warned: set[str] = set()
        # chat_name -> 上次尝试读取「我的群昵称」的 monotonic 时间
        self._alias_last_try: dict[str, float] = {}
        # chat_name -> {avatar_sha1: sender_nick}
        self._avatar_sender: dict[str, dict[str, str]] = {}
        # 已上报过的记账正文（防快照抖动重复上报）
        self._posted_ledger: dict[str, dict[str, float]] = {}
        # 点头像冷却，避免连点抢鼠标
        self._avatar_click_at: dict[str, float] = {}

    def start(self) -> None:
        self._ensure_import()
        self._started = True
        try:
            self._version = self.client_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取微信版本失败: %s", exc)
            self._version = "unknown"
        logger.info(
            "PyWeixinAdapter mode=%s source=%s poll=%ss allow_search=%s",
            self._listen_mode,
            self._listen_source,
            self._poll_interval,
            self._allow_search,
        )

    def _ensure_import(self) -> Any:
        try:
            import pyweixin  # type: ignore

            return pyweixin
        except ImportError as exc:
            raise RuntimeError(
                "未安装 pyweixin。请在 Windows 上: pip install pywechat127"
            ) from exc

    def poll(self, chat_names: list[str]) -> list[RawMessage]:
        if not self._started:
            raise RuntimeError("adapter not started")
        pyweixin = self._ensure_import()
        out: list[RawMessage] = []
        for name in list(self._dialogs):
            if name not in chat_names:
                self._dialogs.pop(name, None)

        # sticky 模式：轮询阶段不挪鼠标；打开窗口单独处理
        for chat in chat_names:
            try:
                if self._listen_mode == "search" and self._allow_search:
                    out.extend(self._poll_chat_search(pyweixin, chat))
                else:
                    out.extend(self._poll_chat_sticky(pyweixin, chat))
            except Exception as exc:  # noqa: BLE001
                logger.warning("监听群失败 chat=%s err=%s", chat, exc)
        return out

    def list_senders_in_chat(self, chat_name: str) -> list[str]:
        """从已打开的独立群窗消息列表提取出现过的昵称（只读，不抢鼠标）。"""
        if not self._started:
            self.start()
        win = self._find_existing_chat_window(chat_name)
        if win is None:
            return []
        items = self._read_dialog_messages_uia(win, chat_name=chat_name)
        seen: list[str] = []
        for sender, _content, _t in items:
            s = str(sender or "").strip()
            if not is_plausible_group_nickname(s):
                continue
            if s not in seen:
                seen.append(s)
        return seen

    def list_group_members(self, chat_name: str) -> list[str]:
        """
        获取群成员昵称。
        @ / 聊天记录弹层需要窗口可交互；仅在刮取侧栏时短暂透明。
        """
        if not self._started:
            self.start()

        win = self._find_existing_chat_window(chat_name)
        if win is None:
            logger.warning(
                "list_group_members: 未找到独立窗 chat=%s（请单独打开并置顶）",
                chat_name,
            )
            return []

        merged: list[str] = []
        cursor = _cursor_pos()
        chat_ghost = None

        expected = self._guess_member_count(win)
        logger.info(
            "list_group_members chat=%s expected=%s display=%s",
            chat_name,
            expected,
            self._chat_window_display_name(win),
        )

        def _merge(items: list[str]) -> None:
            for n in items:
                s = normalize_group_nickname(str(n or ""))
                if not s or not is_plausible_group_nickname(s):
                    continue
                if any(x.casefold() == s.casefold() for x in merged):
                    continue
                merged.append(s)

        def _enough() -> bool:
            if expected is None:
                return False
            return len(merged) >= expected

        def _collect_pane() -> None:
            pane_names = self._read_members_from_chatinfo_pane(win)
            _merge(pane_names)
            logger.info(
                "members pane chat=%s n=%s %s", chat_name, len(pane_names), pane_names
            )

        def _collect_interactive() -> None:
            # @ 与历史窗弹层依赖可见/可命中，不能在 alpha=1 隐身时做
            if not _enough():
                at_names = self._list_members_via_at_mention(win, expected=expected)
                _merge(at_names)
                logger.info(
                    "members via @ chat=%s n=%s expected=%s %s",
                    chat_name,
                    len(at_names),
                    expected,
                    at_names,
                )

            if not _enough():
                hist_names = self._list_members_via_history_tab(
                    win, chat_name, expected=expected
                )
                _merge(hist_names)
                logger.info(
                    "members via history chat=%s n=%s expected=%s %s",
                    chat_name,
                    len(hist_names),
                    expected,
                    hist_names,
                )

            for sender, _c, _t in self._read_dialog_messages_uia(
                win, chat_name=chat_name
            ):
                if is_plausible_group_nickname(sender):
                    _merge([sender])

            my_alias = self._read_my_group_alias(win)
            if my_alias:
                _merge([my_alias])
                remember_my_alias(chat_name, my_alias)

        try:
            # 侧栏刮取可短暂透明
            chat_ghost = _window_ghost(win)
            time.sleep(0.1)
            _collect_pane()
            _window_unghost(chat_ghost)
            chat_ghost = None
            time.sleep(0.15)

            _collect_interactive()

            # 人数明显不够再整段重试一次（仍不隐身）
            need_retry = False
            if expected is not None and len(merged) < max(2, expected // 2):
                need_retry = True
            elif expected is None and len(merged) < 3:
                need_retry = True
            if need_retry:
                logger.warning(
                    "members too few (%s/%s), retry interactive",
                    len(merged),
                    expected,
                )
                merged.clear()
                _collect_pane()
                _collect_interactive()
        finally:
            try:
                _close_chat_history_windows(chat_name)
            except Exception:  # noqa: BLE001
                pass
            _window_unghost(chat_ghost)
            _restore_cursor(cursor)

        logger.info("members merged chat=%s n=%s %s", chat_name, len(merged), merged)
        from gateway.sender_resolve import set_room_nicks

        # 全量覆盖：微信侧改名后旧昵称不得继续参与 OCR 匹配
        set_room_nicks(chat_name, merged)
        return merged

    def _guess_member_count(self, win: Any) -> int | None:
        """从窗内标题 Text / 窗标题解析群人数，如 ttestbbot(3)。"""
        texts: list[str] = []
        try:
            texts.append((win.window_text() or win.element_info.name or "").strip())
        except Exception:  # noqa: BLE001
            pass
        try:
            texts.append(self._chat_window_display_name(win) or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            for t in win.descendants(control_type="Text"):
                s = (t.window_text() or t.element_info.name or "").strip()
                if s and len(s) < 80:
                    texts.append(s)
                if len(texts) > 40:
                    break
        except Exception:  # noqa: BLE001
            pass
        best: int | None = None
        for s in texts:
            m = re.search(r"[（(](\d{1,4})[）)]\s*$", s)
            if not m:
                m = re.search(r"[（(](\d{1,4})[）)]", s)
            if not m:
                continue
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if 2 <= n <= 500:
                if best is None or n > best:
                    best = n
        return best

    def _read_my_group_alias(self, win: Any) -> str:
        """读取侧边栏「我在本群的昵称」后面的值。"""
        try:
            for t in win.descendants(control_type="Text"):
                s = (t.window_text() or t.element_info.name or "").strip()
                if s != "我在本群的昵称":
                    continue
                try:
                    parent = t.parent()
                except Exception:  # noqa: BLE001
                    parent = None
                if parent is not None:
                    for c in parent.descendants():
                        try:
                            val = (
                                c.window_text() or c.element_info.name or ""
                            ).strip()
                        except Exception:  # noqa: BLE001
                            continue
                        if not val or val == "我在本群的昵称":
                            continue
                        if val in {"群聊名称", "群公告", "备注", "查找聊天内容"}:
                            continue
                        if 1 < len(val) <= 40:
                            return val
                texts = []
                for x in win.descendants(control_type="Text"):
                    xs = (x.window_text() or x.element_info.name or "").strip()
                    if xs:
                        texts.append(xs)
                for i, xs in enumerate(texts):
                    if xs == "我在本群的昵称" and i + 1 < len(texts):
                        return texts[i + 1]
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _write_members_debug(self, lines: list[str]) -> None:
        try:
            p = Path(__file__).resolve().parents[2] / "data" / "members_debug.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _list_members_via_at_mention(
        self, win: Any, expected: int | None = None
    ) -> list[str]:
        """
        在本群输入框输入 @，滚动翻完成员列表（不抢鼠标）。
        微信 @ 弹层一次约显示 8 人，必须 Down 才能读完；
        只认「含所有人」的 Mention 列表，避免误读聊天消息。
        """
        debug: list[str] = []
        edit = None
        try:
            edits = list(win.descendants(control_type="Edit"))
        except Exception as exc:  # noqa: BLE001
            self._write_members_debug([f"no edits: {exc}"])
            return []

        for e in reversed(edits):
            try:
                if not e.is_visible() or not e.is_enabled():
                    continue
                name = (e.window_text() or e.element_info.name or "").strip()
                if "成员" in name:
                    continue
                edit = e
                debug.append(f"edit={name!r}")
                break
            except Exception:  # noqa: BLE001
                continue
        if edit is None:
            self._write_members_debug(["edit not found"])
            return []

        names: list[str] = []
        skip = {"所有人", "@所有人", "全体成员", "群成员"}
        pos = _cursor_pos()

        def _add_row(raw: str) -> None:
            first = (raw or "").strip().splitlines()[0].strip()
            if not first or first in skip or first in names:
                return
            if looks_like_chat_record_text(first):
                return
            if not is_plausible_group_nickname(first):
                return
            names.append(normalize_group_nickname(first) or first)

        def _rows_from_list(lst: Any) -> list[str]:
            rows: list[str] = []
            try:
                items = lst.children(control_type="ListItem")
            except Exception:  # noqa: BLE001
                return rows
            for item in items:
                try:
                    raw = (item.window_text() or item.element_info.name or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if raw:
                    rows.append(raw.splitlines()[0].strip())
            return rows

        def _find_mention_list() -> Any | None:
            # 1) MentionPopover 独立窗
            try:
                from pywinauto import Desktop  # type: ignore

                for w in Desktop(backend="uia").windows():
                    try:
                        aid = w.element_info.automation_id or ""
                        cls = w.element_info.class_name or ""
                        if aid != "MentionPopover" and "Mention" not in cls:
                            continue
                        debug.append(f"popover aid={aid} cls={cls}")
                        for lst in w.descendants(control_type="List"):
                            rows = _rows_from_list(lst)
                            if rows:
                                return lst
                    except Exception:  # noqa: BLE001
                        continue
            except Exception as exc:  # noqa: BLE001
                debug.append(f"desktop: {exc}")

            # 2) 嵌在聊天窗内、含「所有人」的 List（勿用消息列表）
            try:
                for lst in win.descendants(control_type="List"):
                    rows = _rows_from_list(lst)
                    if any(x in rows for x in ("所有人", "@所有人", "全体成员")):
                        debug.append(f"inline mention list n={len(rows)}")
                        return lst
            except Exception as exc:  # noqa: BLE001
                debug.append(f"win lists: {exc}")
            return None

        def _collect(lst: Any) -> None:
            for r in _rows_from_list(lst):
                _add_row(r)
            # 仅再扫 MentionPopover（避免其它弹层脏数据）
            try:
                from pywinauto import Desktop  # type: ignore

                for w in Desktop(backend="uia").windows():
                    try:
                        aid = (w.element_info.automation_id or "").strip()
                        if aid != "MentionPopover":
                            continue
                        for item in w.descendants(control_type="ListItem"):
                            raw = (
                                item.window_text() or item.element_info.name or ""
                            ).strip()
                            if raw:
                                _add_row(raw)
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                pass

        try:
            # 微信输入框：短点击再还原光标（纯 set_focus 往往弹不出 @ 列表）
            if not _click_restore(edit):
                if not _uia_focus(edit):
                    debug.append("focus/click edit failed")
                    self._write_members_debug(debug)
                    return []
                debug.append("edit via focus only")
            else:
                debug.append("edit via click_restore")
            time.sleep(0.45)
            try:
                edit.type_keys("^a{BACKSPACE}", pause=0.04)
            except Exception:  # noqa: BLE001
                pass
            try:
                edit.type_keys("@", pause=0.08)
            except Exception as exc:  # noqa: BLE001
                debug.append(f"type @ failed: {exc}")
                try:
                    edit.set_edit_text("@")
                except Exception as exc2:  # noqa: BLE001
                    debug.append(f"set @ failed: {exc2}")
                    self._write_members_debug(debug)
                    return []
            time.sleep(1.6)
            _hide_transient_popovers()

            mention_lst = _find_mention_list()
            if mention_lst is None:
                # 再点一次输入框并重输 @
                debug.append("mention missing, retry click+@")
                _click_restore(edit)
                time.sleep(0.35)
                try:
                    edit.type_keys("^a{BACKSPACE}@", pause=0.06)
                except Exception:  # noqa: BLE001
                    try:
                        edit.set_edit_text("@")
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(1.2)
                _hide_transient_popovers()
                mention_lst = _find_mention_list()
            else:
                _hide_transient_popovers()
            if mention_lst is None:
                debug.append("mention list not found")
                self._write_members_debug(debug)
                return names

            _collect(mention_lst)
            debug.append(f"visible0={names[:12]}")

            _uia_focus(mention_lst)
            time.sleep(0.2)
            _uia_type(mention_lst, "{HOME}", pause=0.05)
            time.sleep(0.25)
            _collect(mention_lst)

            stagnant = 0
            focus_ids: list[Any] = []
            max_steps = max((expected or 20) * 5, 80)
            for step in range(max_steps):
                before = len(names)
                if not _uia_type(mention_lst, "{DOWN}", pause=0.05):
                    debug.append(f"down failed at step={step}")
                    break
                time.sleep(0.18)
                # 列表可能虚拟化，重新定位
                again = _find_mention_list()
                if again is not None:
                    mention_lst = again
                _collect(mention_lst)

                try:
                    focused = [
                        i
                        for i in mention_lst.children(control_type="ListItem")
                        if i.has_keyboard_focus()
                    ]
                    if focused:
                        raw = (
                            focused[0].window_text()
                            or focused[0].element_info.name
                            or ""
                        ).strip()
                        if raw:
                            _add_row(raw)
                        rid = focused[0].element_info.runtime_id
                        focus_ids.append(rid)
                        if (
                            len(focus_ids) >= 4
                            and focus_ids[-1] == focus_ids[-2] == focus_ids[-3]
                        ):
                            member_n = len([n for n in names if n not in skip])
                            if expected is not None and member_n < expected:
                                debug.append(
                                    f"focus stuck@{step} but {member_n}<{expected}, PGDN"
                                )
                                focus_ids.clear()
                                _uia_type(mention_lst, "{PGDN}", pause=0.05)
                                time.sleep(0.25)
                            else:
                                debug.append(f"focus stuck at step={step}")
                                break
                except Exception:  # noqa: BLE001
                    pass

                member_n = len([n for n in names if n not in skip])
                if expected is not None and member_n >= expected:
                    debug.append(f"reached expected={expected} at step={step}")
                    break
                if len(names) == before:
                    stagnant += 1
                else:
                    stagnant = 0
                if stagnant >= 16:
                    debug.append(f"stagnant at step={step} n={len(names)}")
                    break

            # 收起 @：清空输入框，不点消息列表、不 ESC（会关独立窗）
            try:
                _uia_focus(edit)
                edit.set_edit_text("")
            except Exception:  # noqa: BLE001
                try:
                    _uia_focus(edit)
                    edit.type_keys("^a{BACKSPACE}", pause=0.02)
                except Exception:  # noqa: BLE001
                    pass
            debug.append("cleanup without mouse/ESC")
        finally:
            _restore_cursor(pos)

        debug.append(f"names={names}")
        self._write_members_debug(debug)
        return names

    def _list_members_via_history_tab(
        self,
        win: Any,
        chat_name: str,
        expected: int | None = None,
    ) -> list[str]:
        """
        从本群窗打开「聊天记录」→「群成员」，滚动读取全员（不抢鼠标）。
        不搜会话列表；读完只关聊天记录窗，不关置顶群窗。
        """
        debug: list[str] = [f"history_tab chat={chat_name!r} expected={expected}"]
        names: list[str] = []
        pos = _cursor_pos()
        hist = None
        try:
            btn = None
            for title in ("聊天记录", "Chat History", "聊天記錄"):
                try:
                    cands = win.descendants(control_type="Button", title=title)
                    for b in cands:
                        try:
                            if b.is_visible():
                                btn = b
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if btn is not None:
                        break
                except Exception:  # noqa: BLE001
                    continue
            if btn is None:
                try:
                    for b in win.descendants(control_type="Button"):
                        t = (b.window_text() or b.element_info.name or "").strip()
                        if t in {"聊天记录", "Chat History", "聊天記錄"}:
                            btn = b
                            break
                except Exception:  # noqa: BLE001
                    pass
            if btn is None:
                debug.append("chat history button not found")
                self._write_members_debug(debug)
                return []

            if not _uia_activate(btn):
                debug.append("activate chat history button failed")
                self._write_members_debug(debug)
                return []
            debug.append("history button activated")
            time.sleep(1.2)

            from pywinauto import Desktop  # type: ignore

            needle = normalize_chat_name(chat_name)

            def _find_hist_win() -> tuple[Any | None, int | None]:
                for w in Desktop(backend="uia").windows():
                    try:
                        title = (w.window_text() or w.element_info.name or "").strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not title:
                        continue
                    if (
                        "聊天记录" not in title
                        and "Chat History" not in title
                        and "聊天記錄" not in title
                    ):
                        continue
                    if needle:
                        short = needle[:4] if len(needle) >= 4 else needle
                        # 标题常截断：002 饭盒赞助 Gumr...
                        if (
                            needle not in title
                            and needle[:8] not in title
                            and short
                            and short not in title
                        ):
                            continue
                    debug.append(f"hist={title!r}")
                    m = re.search(r"[（(](\d+)[）)]", title)
                    exp = int(m.group(1)) if m else None
                    if exp is not None:
                        debug.append(f"expected_from_hist={exp}")
                    return w, exp
                # 放宽：任意聊天记录窗
                for w in Desktop(backend="uia").windows():
                    try:
                        title = (w.window_text() or "").strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if "聊天记录" in title or "Chat History" in title:
                        debug.append(f"hist_loose={title!r}")
                        m = re.search(r"[（(](\d+)[）)]", title)
                        return w, int(m.group(1)) if m else None
                return None, None

            hist = None
            for attempt in range(4):
                hw, exp2 = _find_hist_win()
                if hw is not None:
                    hist = hw
                    if exp2 is not None:
                        expected = exp2
                    break
                if attempt == 1:
                    # 再真实点一次「聊天记录」
                    _click_restore(btn)
                    debug.append("history button click_restore retry")
                time.sleep(0.7)

            if hist is None:
                debug.append("history window not found")
                self._write_members_debug(debug)
                return []

            # 不要 ghost 聊天记录窗：群成员弹层 (Qt*ToolSaveBits / XPopover) 在隐身时经常出不来
            try:
                import ctypes

                ctypes.windll.user32.ShowWindow(int(hist.handle), 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(int(hist.handle))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)

            # 展开更多 Tab（部分版本需先点 …）
            try:
                more = hist.child_window(
                    control_type="Button", class_name="mmui::XMouseEventView"
                )
                if more.exists(timeout=0.2):
                    _click_restore(more)
                    time.sleep(0.25)
            except Exception:  # noqa: BLE001
                pass

            tab_labels = {"群成员", "Group Members", "群組成員"}
            tab = None
            for label in tab_labels:
                try:
                    t = hist.child_window(control_type="TabItem", title=label)
                    if t.exists(timeout=0.35):
                        tab = t
                        debug.append(f"tab TabItem title={label!r}")
                        break
                except Exception:  # noqa: BLE001
                    continue
            if tab is None:
                try:
                    for ct in ("TabItem", "Button", "Text", "Hyperlink", "ListItem"):
                        for el in hist.descendants(control_type=ct):
                            try:
                                name = (
                                    el.window_text() or el.element_info.name or ""
                                ).strip()
                            except Exception:  # noqa: BLE001
                                continue
                            if name in tab_labels:
                                tab = el
                                debug.append(f"tab {ct} name={name!r}")
                                break
                        if tab is not None:
                            break
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"tab scan: {exc}")

            if tab is None:
                try:
                    seen: list[str] = []
                    for el in hist.descendants(control_type="Text"):
                        name = (el.window_text() or "").strip()
                        if name and name not in seen:
                            seen.append(name)
                        if len(seen) >= 40:
                            break
                    debug.append(f"hist texts sample={seen[:30]}")
                except Exception:  # noqa: BLE001
                    pass
                debug.append("群成员 tab not found")
                self._write_members_debug(debug)
                try:
                    hist.close()
                except Exception:  # noqa: BLE001
                    pass
                return []

            # 必须真实点击：select/invoke 在微信里常假成功，界面仍停在消息列表
            try:
                hist.set_focus()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
            clicked = _click_restore(tab)
            debug.append(f"群成员 click_restore={clicked}")
            if not clicked:
                clicked = _uia_activate(tab)
                debug.append(f"群成员 activate_fallback={clicked}")
            # pyweixin 同款：再强制 click_input 一次，确保弹出成员列表
            try:
                tab.click_input()
                debug.append("群成员 click_input=True")
                clicked = True
            except Exception as exc:  # noqa: BLE001
                debug.append(f"群成员 click_input={exc}")
            time.sleep(1.2)
            _hide_transient_popovers()
            time.sleep(0.4)

            def _rows_of(lst: Any) -> list[str]:
                out: list[str] = []
                try:
                    for item in lst.children(control_type="ListItem"):
                        raw = (
                            item.window_text() or item.element_info.name or ""
                        ).strip()
                        first = raw.splitlines()[0].strip() if raw else ""
                        if first:
                            out.append(first)
                except Exception:  # noqa: BLE001
                    pass
                return out

            def _find_members_list() -> Any | None:
                """对齐 pyweixin：群成员 Tab 后出现 Qt*ToolSaveBits / mmui::XPopover 列表。"""
                candidates: list[Any] = []

                # 1) win32 EnumWindows 找 ToolSaveBits（与 Tools.find_Popover 同款）
                try:
                    import win32gui  # type: ignore
                    from pywinauto import Desktop  # type: ignore

                    matched: list[int] = []

                    def _enum_cb(hwnd: int, _: Any) -> None:
                        try:
                            if not win32gui.IsWindowVisible(hwnd):
                                return
                            cls = win32gui.GetClassName(hwnd) or ""
                            title = win32gui.GetWindowText(hwnd) or ""
                        except Exception:  # noqa: BLE001
                            return
                        if "ToolSaveBits" not in cls:
                            return
                        if title and title not in {"Weixin", "微信", ""}:
                            # 少数版本标题非空
                            pass
                        matched.append(int(hwnd))

                    win32gui.EnumWindows(_enum_cb, None)
                    debug.append(f"ToolSaveBits hwnds={matched[:6]}")
                    desk = Desktop(backend="uia")
                    for hwnd in matched:
                        try:
                            w = desk.window(handle=hwnd)
                            for lst in w.descendants(control_type="List"):
                                candidates.append(lst)
                        except Exception:  # noqa: BLE001
                            continue
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"enum ToolSaveBits: {exc}")

                # 2) pyweixin Tools.find_Popover
                try:
                    from pyweixin.WeChatTools import Tools  # type: ignore

                    pop = Tools.find_Popover()
                    if pop is not None:
                        try:
                            lst = pop.child_window(control_type="List")
                            if lst.exists(timeout=0.4):
                                candidates.append(lst)
                                debug.append("candidate Tools.find_Popover")
                        except Exception as exc:  # noqa: BLE001
                            debug.append(f"popover list: {exc}")
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"popover: {exc}")

                # 3) Desktop 上 XPopover / Popover
                try:
                    from pywinauto import Desktop  # type: ignore

                    for w in Desktop(backend="uia").windows():
                        try:
                            cls = (w.element_info.class_name or "").strip()
                            aid = (w.element_info.automation_id or "").strip()
                            title = (w.window_text() or "").strip()
                        except Exception:  # noqa: BLE001
                            continue
                        if not (
                            "ToolSaveBits" in cls
                            or "XPopover" in cls
                            or "Popover" in cls
                            or "Popover" in aid
                        ):
                            continue
                        try:
                            if hist is not None and int(w.handle) == int(hist.handle):
                                continue
                        except Exception:  # noqa: BLE001
                            pass
                        debug.append(
                            f"tool_win cls={cls!r} aid={aid!r} title={title!r}"
                        )
                        try:
                            for lst in w.descendants(control_type="List"):
                                candidates.append(lst)
                        except Exception:  # noqa: BLE001
                            continue
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"desktop tool scan: {exc}")

                # 4) 聊天记录窗内部 List（部分版本成员嵌在窗内）
                try:
                    for lst in hist.descendants(control_type="List"):
                        candidates.append(lst)
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"hist lists: {exc}")

                best = None
                best_n = -1
                for lst in candidates:
                    rows = _rows_of(lst)
                    if not rows:
                        continue
                    if looks_like_chat_record_list(rows):
                        debug.append(f"skip chat-record list sample={rows[:3]}")
                        continue
                    # 成员列表：多数项应像昵称
                    nick_like = sum(
                        1
                        for r in rows[:20]
                        if is_plausible_group_nickname(r.splitlines()[0].strip())
                    )
                    if nick_like < max(1, min(3, len(rows) // 2)):
                        debug.append(
                            f"skip low-nick list n={len(rows)} nick_like={nick_like} sample={rows[:3]}"
                        )
                        continue
                    debug.append(
                        f"members list cand n={len(rows)} nick_like={nick_like} sample={rows[:5]}"
                    )
                    if len(rows) > best_n:
                        best_n = len(rows)
                        best = lst
                return best

            member_list = _find_members_list()
            if member_list is None:
                debug.append("members list missing, re-click 群成员")
                _click_restore(tab)
                time.sleep(1.5)
                member_list = _find_members_list()

            if member_list is None:
                debug.append(
                    "member list not found (拒绝回退到聊天记录消息列表)"
                )
                self._write_members_debug(debug)
                try:
                    hist.close()
                except Exception:  # noqa: BLE001
                    pass
                return []

            probe = _rows_of(member_list)
            if looks_like_chat_record_list(probe):
                debug.append(f"abort: list is chat records sample={probe[:5]}")
                self._write_members_debug(debug)
                try:
                    hist.close()
                except Exception:  # noqa: BLE001
                    pass
                return []

            _uia_focus(member_list)
            time.sleep(0.2)
            _uia_type(member_list, "{PGUP}", pause=0.05)
            time.sleep(0.3)

            focus_ids: list[Any] = []
            stagnant = 0
            # 只采当前焦点项再 Down（键盘），不抢鼠标
            max_steps = max((expected or 20) * 6, 120)

            for step in range(max_steps):
                before = len(names)
                try:
                    focused = [
                        i
                        for i in member_list.children(control_type="ListItem")
                        if i.has_keyboard_focus()
                    ]
                    if focused:
                        txt = (
                            focused[0].window_text()
                            or focused[0].element_info.name
                            or ""
                        ).strip()
                        first = txt.splitlines()[0].strip() if txt else ""
                        if (
                            first
                            and first not in names
                            and not looks_like_chat_record_text(first)
                            and is_plausible_group_nickname(first)
                        ):
                            names.append(normalize_group_nickname(first) or first)
                        rid = focused[0].element_info.runtime_id
                        focus_ids.append(rid)
                        # 连续同一焦点 = 到底（pyweixin 同款）
                        if (
                            len(focus_ids) >= 2
                            and focus_ids[-1] == focus_ids[-2]
                        ):
                            if expected is not None and len(names) < expected:
                                debug.append(
                                    f"history focus end@{step} n={len(names)}<{expected}, HOME+retry"
                                )
                                if _uia_type(member_list, "{HOME}", pause=0.05):
                                    time.sleep(0.25)
                                    focus_ids.clear()
                                else:
                                    debug.append(f"history focus stuck step={step}")
                                    break
                            else:
                                debug.append(f"history focus stuck step={step}")
                                break
                except Exception:  # noqa: BLE001
                    pass

                if expected is not None and len(names) >= expected:
                    debug.append(
                        f"history reached expected at step={step} n={len(names)}"
                    )
                    break
                if len(names) == before:
                    stagnant += 1
                else:
                    stagnant = 0
                if stagnant >= 25:
                    debug.append(f"history stagnant step={step} n={len(names)}")
                    break

                if not _uia_type(member_list, "{DOWN}", pause=0.05):
                    break
                time.sleep(0.12)

            try:
                hist.close()
            except Exception:  # noqa: BLE001
                pass
            debug.append(f"history names={names}")
            self._write_members_debug(debug)
            return names
        except Exception as exc:  # noqa: BLE001
            debug.append(f"history exc: {exc}")
            self._write_members_debug(debug)
            if hist is not None:
                try:
                    hist.close()
                except Exception:  # noqa: BLE001
                    pass
            return names
        finally:
            _restore_cursor(pos)

    def _chatinfo_pane_root(self, win: Any) -> Any | None:
        """定位已打开的「聊天信息」侧边栏根节点（只读，不点击）。"""
        markers = {"我在本群的昵称", "群聊名称", "群公告", "My Alias in Group"}
        start = None

        try:
            for e in win.descendants(control_type="Edit"):
                name = (e.window_text() or e.element_info.name or "").strip()
                if "搜索" in name and "成员" in name:
                    start = e
                    break
                if name in {"Search", "搜尋群組成員", "搜索成员"}:
                    start = e
                    break
        except Exception:  # noqa: BLE001
            start = None

        if start is None:
            try:
                for t in win.descendants(control_type="Text"):
                    s = (t.window_text() or t.element_info.name or "").strip()
                    if s in markers:
                        start = t
                        break
            except Exception:  # noqa: BLE001
                return None

        if start is None:
            return None

        def _has_marker(node: Any) -> bool:
            try:
                for t in node.descendants(control_type="Text"):
                    s = (t.window_text() or t.element_info.name or "").strip()
                    if s in markers:
                        return True
            except Exception:  # noqa: BLE001
                return False
            return False

        node = start
        best = start
        for _ in range(12):
            try:
                parent = node.parent()
            except Exception:  # noqa: BLE001
                break
            if parent is None:
                break
            node = parent
            best = parent
            # 升到已包含侧边栏特征文案的那一层即停，避免扩到整个聊天窗
            if start is not None and _has_marker(parent):
                # 若起点本身就是 marker，再往上两层以覆盖成员头像区
                try:
                    for _extra in range(2):
                        p2 = best.parent()
                        if p2 is None:
                            break
                        best = p2
                except Exception:  # noqa: BLE001
                    pass
                return best
        return best

    def _read_members_from_chatinfo_pane(self, win: Any) -> list[str]:
        """只读刮取右侧「聊天信息」面板成员名；侧边栏未开则返回空，不点击。"""
        ui_noise = {
            "搜索群成员",
            "搜索成员",
            "群公告",
            "备注",
            "查找聊天内容",
            "消息免打扰",
            "折叠该聊天",
            "置顶聊天",
            "清空聊天记录",
            "查找聊天记录",
            "聊天信息",
            "添加",
            "移出",
            "我在本群的昵称",
            "群聊名称",
            "保存到通讯录",
            "删除并退出",
            "投诉",
            "TestBot",
        }

        pane = self._chatinfo_pane_root(win)
        if pane is None:
            logger.info("chatinfo pane not open; skip member scrape (no mouse click)")
            return []

        found: list[str] = []

        def _add(raw: str) -> None:
            first = (raw or "").strip().splitlines()[0].strip()
            if not first or first in ui_noise:
                return
            # 截断名「悠悠 I...」「啊吧 (...」也允许
            if len(first) > 40:
                return
            if any(
                x in first
                for x in ("记录成功", "记账", "✅", "❌", "类型：", "金额：")
            ):
                return
            if first not in found:
                found.append(first)

        # A) ListItem（含子 Text）
        try:
            for item in pane.descendants(control_type="ListItem"):
                try:
                    raw = (item.window_text() or item.element_info.name or "").strip()
                except Exception:  # noqa: BLE001
                    raw = ""
                if raw:
                    _add(raw)
                try:
                    for t in item.descendants(control_type="Text"):
                        _add(t.window_text() or t.element_info.name or "")
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("pane listitem scrape: %s", exc)

        # B) Button（头像按钮）
        try:
            for b in pane.descendants(control_type="Button"):
                try:
                    raw = (b.window_text() or b.element_info.name or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                _add(raw)
                try:
                    for t in b.descendants(control_type="Text"):
                        _add(t.window_text() or t.element_info.name or "")
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("pane button scrape: %s", exc)

        # C) 扫面板内所有控件的 name（头像名常挂在 Image/Custom 上）
        try:
            for c in pane.descendants():
                try:
                    raw = (c.element_info.name or c.window_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not raw:
                    continue
                _add(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pane all-name scrape: %s", exc)

        # D) 直接扫面板内 Text
        try:
            for t in pane.descendants(control_type="Text"):
                try:
                    raw = (t.window_text() or t.element_info.name or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not raw or raw in ui_noise:
                    continue
                if len(raw) < 2:
                    continue
                if any(
                    k in raw
                    for k in ("免打扰", "置顶", "折叠", "清空", "查找", "公告")
                ):
                    continue
                _add(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pane text scrape: %s", exc)

        # 过滤：群名本身、过像句子的
        cleaned: list[str] = []
        for n in found:
            if n in ui_noise:
                continue
            if is_plausible_group_nickname(n) or (
                2 <= len(n) <= 24 and "记录" not in n and "记账" not in n
            ):
                if n not in cleaned:
                    cleaned.append(n)
        return cleaned

    def listen(self, chat_names: list[str]) -> Iterable[RawMessage]:
        if not self._started:
            raise RuntimeError("adapter not started")

        def _gen() -> Iterator[RawMessage]:
            while self._started:
                yield from self.poll(list(chat_names))
                time.sleep(self._poll_interval)

        return _gen()

    def _poll_chat_sticky(self, pyweixin: Any, chat_name: str) -> list[RawMessage]:
        win = self._get_or_open_dialog(pyweixin, chat_name)
        if win is None:
            return []
        detailed = self._read_dialog_messages_detailed(win, chat_name=chat_name)
        if not detailed:
            if chat_name not in self._missing_warned:
                logger.warning(
                    "独立窗口消息列表为空 chat=%s（保持小窗打开，或在微信里往群里发一条消息）",
                    chat_name,
                )
                self._missing_warned.add(chat_name)
            return []
        items = [(s, c, t, tm) for _item, s, c, t, tm in detailed]
        # 暖机 / 增量
        now = datetime.now(timezone.utc).astimezone()
        curr: list[tuple[str, str, str, str]] = []
        for sender_s, content_s, type_s, ui_time in items:
            content_s = str(content_s or "").strip()
            sender_s = str(sender_s or "").strip() or "未知发送人"
            type_s = str(type_s or "文本")
            ui_time = str(ui_time or "").strip()
            if not content_s or not self._is_text_type(type_s):
                continue
            # 机器人回执不当作群消息（避免快照抖动/误 OCR）
            if is_bot_receipt_text(content_s):
                continue
            curr.append((sender_s, content_s, type_s, ui_time))

        if chat_name not in self._warmed:
            self._last_snapshot[chat_name] = curr
            self._warmed.add(chat_name)
            try:
                alias = self._read_my_group_alias(win) or ""
                if alias:
                    remember_my_alias(chat_name, alias)
                    logger.info("cached my_alias chat=%s alias=%r", chat_name, alias)
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                "chat warmed (skip history): %s items=%s", chat_name, len(curr)
            )
            return []

        prev = self._last_snapshot.get(chat_name, [])
        new_rows = _diff_new_message_rows(prev, curr)
        # 发送人识别：无鼠标优先；仍未知时才点头像读资料卡
        if new_rows:
            my_alias = ""
            try:
                my_alias = self._read_my_group_alias(win) or ""
            except Exception:  # noqa: BLE001
                my_alias = ""
            if not my_alias:
                my_alias = load_my_alias(chat_name) or ""
            if my_alias:
                remember_my_alias(chat_name, my_alias)
            # 绑定表里的本群昵称也作为候选（本机账号常已授权）
            known = list(load_room_nicks(chat_name) or [])
            avatar_cache = self._avatar_sender.setdefault(chat_name, {})
            posted = self._posted_ledger.setdefault(chat_name, {})
            now_ts = time.time()
            # posted 保留 2 分钟：有 UI 时间时用「正文+时间」区分多样；
            # 无时间时仍靠末尾+8 秒区分真实连发与滚动回放。
            for k, ts in list(posted.items()):
                if now_ts - ts > 120:
                    posted.pop(k, None)
            window_img = None
            win_rect = None
            resolved_rows: list[tuple[str, str, str, str, bool]] = []
            newest_content = ""
            newest_ui_time = ""
            if curr:
                newest_content = (curr[-1][1] or "").strip()
                newest_ui_time = (curr[-1][3] or "").strip()
            for sender_s, content_s, type_s, ui_time in new_rows:
                likely_self = False
                try:
                    from apps.ledger.books import is_book_command_text

                    is_ledger = is_book_command_text(content_s, chat_name)
                except Exception:  # noqa: BLE001
                    is_ledger = content_s.startswith(("记账", "記賬"))
                posted_key = f"{sender_s or '?'}\n{content_s}\n{ui_time}"
                prev_ts = posted.get(posted_key)
                if is_ledger and prev_ts is not None:
                    age = now_ts - prev_ts
                    is_newest = bool(content_s) and content_s == newest_content
                    if ui_time:
                        # 同文+同时刻 = 同一气泡；仅当又出现在末尾且间隔够长才认作同分钟连发
                        if is_newest and ui_time == newest_ui_time and age >= 8:
                            pass
                        elif is_newest and age < 8:
                            logger.info(
                                "skip jitter ledger chat=%s text=%s time=%s age=%.1fs",
                                chat_name,
                                content_s[:40],
                                ui_time,
                                age,
                            )
                            continue
                        else:
                            logger.info(
                                "skip resurfaced ledger chat=%s text=%s time=%s age=%.1fs",
                                chat_name,
                                content_s[:40],
                                ui_time,
                                age,
                            )
                            continue
                    elif is_newest and age >= 8:
                        pass
                    elif is_newest and age < 8:
                        logger.info(
                            "skip jitter ledger chat=%s text=%s age=%.1fs",
                            chat_name,
                            content_s[:40],
                            age,
                        )
                        continue
                    else:
                        logger.info(
                            "skip resurfaced ledger chat=%s text=%s age=%.1fs",
                            chat_name,
                            content_s[:40],
                            age,
                        )
                        continue
                # 其它发送人刚报过同一正文(+时间)，且本条不在末尾 → 重放
                if (
                    is_ledger
                    and content_s
                    and content_s != newest_content
                    and any(
                        k.endswith(f"\n{content_s}\n{ui_time}") and now_ts - ts < 120
                        for k, ts in posted.items()
                    )
                ):
                    logger.info(
                        "skip resurfaced ledger(any-sender) chat=%s text=%s time=%s",
                        chat_name,
                        content_s[:40],
                        ui_time or "-",
                    )
                    continue
                if sender_s in {"", "未知发送人"} and is_ledger:
                    item = _find_list_item_for_content(detailed, content_s)
                    if item is not None:
                        if window_img is None:
                            captured = capture_window_image(win)
                            if captured:
                                window_img, win_rect = captured
                        if win_rect is None:
                            win_rect = get_window_rect(win)
                        side, av_hash = avatar_side_and_hash(
                            window_img, win_rect, item
                        )
                        is_self = side == "RIGHT" or (
                            window_img is not None
                            and win_rect is not None
                            and bubble_looks_self(window_img, win_rect, item)
                        )
                        disk_avatars = load_avatar_map(chat_name)
                        # 1) 绿气泡=自己
                        if is_self:
                            likely_self = True
                            if my_alias:
                                hit = _match_known_nick_exact(my_alias, known)
                                sender_s = hit or my_alias
                            elif len(known) == 1:
                                sender_s = known[0]
                                remember_my_alias(chat_name, sender_s)
                                my_alias = sender_s
                        # 2) 已验证的头像指纹（仅信任精确绑定过的）
                        if (
                            sender_s in {"", "未知发送人"}
                            and av_hash
                            and side == "LEFT"
                        ):
                            cached = avatar_cache.get(av_hash) or disk_avatars.get(
                                av_hash
                            )
                            if cached and _match_known_nick_exact(cached, known):
                                sender_s = _match_known_nick_exact(cached, known)
                                avatar_cache[av_hash] = sender_s
                        # 3) 点头像读资料（权威来源；开启后跳过易误判的橙色/OCR）
                        if (
                            sender_s in {"", "未知发送人"}
                            and not is_self
                            and _AVATAR_CLICK_ENABLED
                            and known
                        ):
                            click_hit = self._resolve_sender_by_avatar_click(
                                win, item, known
                            )
                            if click_hit:
                                sender_s = click_hit
                        elif sender_s in {"", "未知发送人"} and not is_self and known:
                            # 未开启点头像时：橙色标签 → OCR
                            tag_hit = resolve_sender_by_wecom_tag(
                                window_img, win_rect, item, known
                            )
                            if tag_hit and _match_known_nick_exact(tag_hit, known):
                                sender_s = _match_known_nick_exact(tag_hit, known)
                            if (
                                sender_s in {"", "未知发送人"}
                                and ocr_enabled()
                            ):
                                ocr_hit = resolve_sender_by_ocr(
                                    window_img,
                                    win_rect,
                                    item,
                                    known,
                                    side="LEFT",
                                )
                                if ocr_hit and _match_known_nick_exact(
                                    ocr_hit, known
                                ):
                                    sender_s = _match_known_nick_exact(
                                        ocr_hit, known
                                    )
                        # 本人气泡一旦落到绑定昵称，立即记住（多绑定群下次丢昵称可兜底）
                        if (
                            is_self
                            and sender_s not in {"", "未知发送人"}
                        ):
                            exact = _match_known_nick_exact(sender_s, known)
                            if exact:
                                sender_s = exact
                            remember_my_alias(chat_name, sender_s)
                            my_alias = sender_s
                        # 仅把「已精确匹配绑定」的结果写入头像缓存
                        if (
                            sender_s not in {"", "未知发送人"}
                            and av_hash
                            and side == "LEFT"
                            and _match_known_nick_exact(sender_s, known)
                        ):
                            exact = _match_known_nick_exact(sender_s, known)
                            old = disk_avatars.get(av_hash) or avatar_cache.get(
                                av_hash
                            )
                            if old and old != exact:
                                logger.warning(
                                    "avatar-hash conflict hash=%s old=%r new=%r (keep new)",
                                    av_hash[:8],
                                    old,
                                    exact,
                                )
                            avatar_cache[av_hash] = exact
                            remember_avatar_sender(chat_name, av_hash, exact)
                        logger.info(
                            "sender-resolve chat=%s side=%s self=%s alias=%r -> %s",
                            chat_name,
                            side,
                            is_self,
                            my_alias,
                            sender_s,
                        )
                # UIA 已带昵称但尚未记本机别名：右侧气泡=自己时补记
                elif (
                    is_ledger
                    and sender_s not in {"", "未知发送人"}
                    and known
                    and not my_alias
                ):
                    exact = _match_known_nick_exact(sender_s, known)
                    if exact:
                        sender_s = exact
                        item = _find_list_item_for_content(detailed, content_s)
                        if item is not None:
                            if window_img is None:
                                captured = capture_window_image(win)
                                if captured:
                                    window_img, win_rect = captured
                            if win_rect is None:
                                win_rect = get_window_rect(win)
                            side, _av = avatar_side_and_hash(
                                window_img, win_rect, item
                            )
                            is_self = side == "RIGHT" or (
                                window_img is not None
                                and win_rect is not None
                                and bubble_looks_self(window_img, win_rect, item)
                            )
                            if is_self:
                                likely_self = True
                                remember_my_alias(chat_name, sender_s)
                                my_alias = sender_s
                                logger.info(
                                    "remember my_alias chat=%s nick=%r (uia+right)",
                                    chat_name,
                                    sender_s,
                                )
                if is_ledger:
                    final_key = f"{sender_s or '?'}\n{content_s}\n{ui_time}"
                    posted[final_key] = now_ts
                resolved_rows.append(
                    (sender_s, content_s, type_s, ui_time, likely_self)
                )
            new_rows = [(s, c, t, tm) for s, c, t, tm, _ in resolved_rows]
            likely_flags = [flag for _s, _c, _t, _tm, flag in resolved_rows]
        else:
            likely_flags = []

        # 快照用 UIA 原文（发送人常为未知）；diff 已按正文+时间比较，不再因 OCR 写回而误判新消息
        self._last_snapshot[chat_name] = curr

        out: list[RawMessage] = []
        for i, (sender_s, content_s, type_s, ui_time) in enumerate(new_rows):
            self._seq += 1
            extra = {"adapter": "pyweixin", "ui_type": type_s}
            if i < len(likely_flags) and likely_flags[i]:
                extra["likely_self"] = True
            out.append(
                RawMessage(
                    chat_name=chat_name,
                    sender_display_name=sender_s,
                    message_type=MessageType.TEXT,
                    raw_text=content_s,
                    client_observed_at=now,
                    local_sequence=self._seq,
                    ui_timestamp=ui_time,
                    extra=extra,
                )
            )
        return out

    def _resolve_sender_by_avatar_click(
        self, win: Any, item: Any, known: list[str]
    ) -> str:
        """
        点击消息左侧头像，从资料卡读取群昵称，再与绑定表精确匹配。
        只点一次；读完 ESC 关卡并还原光标。
        """
        if not _AVATAR_CLICK_ENABLED:
            return ""
        now = time.time()
        last = self._avatar_click_at.get("_global", 0.0)
        if now - last < 0.8:
            logger.info("avatar-click skipped: cooldown")
            return ""
        self._avatar_click_at["_global"] = now

        pos = _cursor_pos()
        nick = ""
        try:
            try:
                import ctypes

                hwnd = int(win.handle)
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.08)
            except Exception:  # noqa: BLE001
                pass

            r = item.rectangle()
            # 只点头像中心一次（多点会很慢且易点错）
            ax, ay = int(r.left + 28), int(r.top + 28)
            try:
                from pywinauto import mouse  # type: ignore
            except Exception:  # noqa: BLE001
                mouse = None

            if mouse is not None:
                try:
                    mouse.click(coords=(ax, ay))
                    time.sleep(0.35)
                    nick = _read_profile_popup_nickname()
                    if nick:
                        logger.info(
                            "avatar-click nick=%r at=(%s,%s)", nick, ax, ay
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("avatar mouse click failed: %s", exc)
            if not nick:
                try:
                    item.click_input(coords=(28, 28))
                    time.sleep(0.35)
                    nick = _read_profile_popup_nickname()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("avatar click_input failed: %s", exc)

            try:
                win.type_keys("{ESC}")
            except Exception:  # noqa: BLE001
                pass

            if not nick:
                logger.info("avatar-click no profile nick")
                return ""

            hit = _match_known_nick_exact(nick, known)
            if hit:
                logger.info("avatar-click matched=%r raw=%r", hit, nick)
                return hit
            # 资料卡原文若像昵称，原样返回（服务端再精确匹配）
            if is_plausible_group_nickname(nick):
                out = normalize_group_nickname(nick) or nick
                logger.info("avatar-click unbound nick=%r", out)
                return out
            logger.info("avatar-click ignorable nick=%r", nick)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("avatar-click failed: %s", exc)
            return ""
        finally:
            _restore_cursor(pos)

    def _get_or_open_dialog(self, pyweixin: Any, chat_name: str) -> Any | None:
        existing = self._dialogs.get(chat_name)
        if existing is not None:
            try:
                if existing.exists():
                    return existing
            except Exception:  # noqa: BLE001
                self._dialogs.pop(chat_name, None)

        # 1) 桌面上已有独立聊天窗（含置顶窗），按标题匹配（零鼠标）
        found = self._find_existing_chat_window(chat_name)
        if found is not None:
            self._dialogs[chat_name] = found
            logger.info("reused existing chat window: %s", chat_name)
            return found

        # pinned 模式：绝不自动搜群打开，只听用户已置顶的窗
        if self._listen_source == "pinned" or self._listen_mode == "pinned":
            if chat_name not in self._missing_warned:
                logger.warning(
                    "未找到置顶独立窗 chat=%s。请：单独打开该群 → 点左上角图钉置顶",
                    chat_name,
                )
                self._missing_warned.add(chat_name)
            return None

        # sticky 模式才允许「打开一次」
        now = time.time()
        last_fail = self._open_failed_at.get(chat_name, 0.0)
        if now - last_fail < 60:
            return None

        nav = getattr(pyweixin, "Navigator", None)
        if nav is None or not hasattr(nav, "open_seperate_dialog_window"):
            logger.error("无 Navigator.open_seperate_dialog_window")
            self._open_failed_at[chat_name] = now
            return None

        pos = _cursor_pos()
        try:
            logger.info("opening separate chat window once: %s", chat_name)
            win = nav.open_seperate_dialog_window(
                friend=chat_name,
                window_minimize=True,
                close_weixin=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._open_failed_at[chat_name] = now
            logger.warning("打开独立聊天窗失败 chat=%s err=%s", chat_name, exc)
            return None
        finally:
            _restore_cursor(pos)

        self._dialogs[chat_name] = win
        self._open_failed_at.pop(chat_name, None)
        return win

    def _chat_window_display_name(self, win: Any) -> str:
        """
        解析独立聊天窗的真实会话名。
        微信 4.x 窗标题常为 Weixin，需读窗内标题 Text（如 ttestbbot(3)）。
        """
        try:
            title = (win.window_text() or win.element_info.name or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
        title_line = title.splitlines()[0].strip() if title else ""
        title_norm = normalize_chat_name(title_line)
        if title_norm and title_norm not in {"微信", "Weixin", "主窗口", "WeChat"}:
            return title_norm
        # 窗内标题：优先短文本且带 (人数)
        try:
            texts: list[str] = []
            for t in win.descendants(control_type="Text"):
                s = (t.window_text() or t.element_info.name or "").strip()
                if not s or s in {"发送(S)", "发送", "Send"}:
                    continue
                if len(s) > 80:
                    continue
                texts.append(s)
            # 先找带成员数的
            for s in texts:
                if _MEMBER_COUNT_RE.search(s):
                    return normalize_chat_name(s)
            # 再找与 title 不同的首个像会话名的
            for s in texts:
                n = normalize_chat_name(s)
                if not n or n in {"微信", "Weixin", "主窗口"}:
                    continue
                if classify_session_title(s) in {"noise", "system"}:
                    continue
                if re.fullmatch(r"\(?\d+\)?", n):
                    continue
                return n
        except Exception as exc:  # noqa: BLE001
            logger.debug("inner chat name failed: %s", exc)
        return title_norm

    def _find_existing_chat_window(self, chat_name: str) -> Any | None:
        """按标题或窗内群名查找已打开的独立聊天窗口，不点击。"""
        try:
            from pywinauto import Desktop  # type: ignore
        except ImportError:
            return None
        target = normalize_chat_name(chat_name)
        if not target:
            return None
        try:
            desktop = Desktop(backend="uia")
            candidates: list[Any] = []
            for w in desktop.windows():
                try:
                    cls = (w.element_info.class_name or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if "MainWindow" in cls:
                    continue
                if not (
                    "ChatSingle" in cls
                    or "Chat" in cls
                    or "mmui::" in cls
                ):
                    continue
                name = self._chat_window_display_name(w)
                if not name:
                    continue
                if not (
                    name == target
                    or target in name
                    or name in target
                    or (len(target) >= 4 and name.startswith(target[:8]))
                ):
                    continue
                candidates.append(w)
            if candidates:
                return candidates[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug("find chat window failed: %s", exc)
        return None

    def _read_dialog_messages_uia(
        self, win: Any, chat_name: str = ""
    ) -> list[tuple[str, str, str]]:
        return [
            (s, c, t)
            for _item, s, c, t, _tm in self._read_dialog_messages_detailed(
                win, chat_name
            )
        ]

    def _read_dialog_messages_detailed(
        self, win: Any, chat_name: str = ""
    ) -> list[tuple[Any, str, str, str, str]]:
        """只读 UIA，返回 (ListItem, 发送人, 内容, 类型, UI时间)。"""
        best: list[tuple[Any, str, str, str, str]] = []
        best_score = -1
        # 「我的群昵称」几乎不变：优先用缓存，缓存为空时才遍历控件树（且最多每 60 秒一次）
        my_alias = (load_my_alias(chat_name) or "") if chat_name else ""
        if not my_alias:
            now_m = time.monotonic()
            last_try = self._alias_last_try.get(chat_name, 0.0)
            if now_m - last_try >= 60:
                self._alias_last_try[chat_name] = now_m
                try:
                    my_alias = self._read_my_group_alias(win) or ""
                except Exception:  # noqa: BLE001
                    my_alias = ""
                if my_alias and chat_name:
                    remember_my_alias(chat_name, my_alias)
        known: list[str] = []
        if chat_name:
            known = list(load_room_nicks(chat_name) or [])
        if my_alias and my_alias not in known:
            known.append(my_alias)
        try:
            lists = win.descendants(control_type="List")
        except Exception:  # noqa: BLE001
            return best
        for lst in lists:
            rows: list[tuple[Any, str, str, str, str]] = []
            try:
                children = lst.children(control_type="ListItem")
            except Exception:  # noqa: BLE001
                continue
            for item in children:
                sender, content, ui_time = _parse_list_item_message(item)
                if not content or is_bot_receipt_text(content):
                    continue
                if content.startswith("你已添加了") or "拍了拍" in content:
                    continue
                # 未在已知昵称里的「发送人」多半是脏数据，降级为未知
                if sender and sender != "未知发送人":
                    if not is_plausible_group_nickname(sender):
                        sender = "未知发送人"
                    elif known:
                        keys = {nickname_match_key(k) for k in known if k}
                        if sender not in known and nickname_match_key(sender) not in keys:
                            # 允许精确/key 命中已知绑定；否则丢掉（防上条误贴）
                            hit = match_text_to_candidates(sender, known)
                            sender = hit if hit else "未知发送人"
                if known and (not sender or sender == "未知发送人"):
                    # 仅当 item 文本以某已知昵称开头时才认（避免正文误匹配）
                    try:
                        raw_item = (
                            item.window_text() or item.element_info.name or ""
                        ).strip()
                    except Exception:  # noqa: BLE001
                        raw_item = ""
                    for nick in sorted(known, key=len, reverse=True):
                        n = normalize_group_nickname(nick) or nick
                        if not n:
                            continue
                        if raw_item == n or raw_item.startswith(n + "\n") or raw_item.startswith(
                            n + " "
                        ):
                            sender = n
                            break
                if (not sender or sender == "未知发送人") and my_alias:
                    if _message_item_is_self(item, win):
                        sender = my_alias
                rows.append(
                    (item, sender or "未知发送人", content, "文本", ui_time or "")
                )
            if not rows:
                continue
            score = len(rows)
            if any("记账" in r[2] or "記賬" in r[2] for r in rows):
                score += 100
            named = sum(1 for r in rows if r[1] and r[1] != "未知发送人")
            score += named * 2
            timed = sum(1 for r in rows if r[4])
            score += timed
            if score > best_score:
                best_score = score
                best = rows
        return best

    def _poll_chat_search(self, pyweixin: Any, chat_name: str) -> list[RawMessage]:
        """仅当显式允许 search 模式时使用（会占用搜索框）。"""
        messages_mod = getattr(pyweixin, "Messages", None)
        if messages_mod is None or not hasattr(messages_mod, "pull_messages"):
            raise RuntimeError("当前 pyweixin 无 Messages.pull_messages")
        pos = _cursor_pos()
        try:
            history = messages_mod.pull_messages(
                friend=chat_name,
                number=self._pull_number,
                is_json=False,
                search_pages=0,
                is_maximize=False,
                close_weixin=False,
            )
        finally:
            _restore_cursor(pos)
        return self._history_to_messages(chat_name, self._normalize_history(history))

    def _history_to_messages(
        self, chat_name: str, items: list[tuple[str, str, str]]
    ) -> list[RawMessage]:
        """
        用消息列表快照做增量：
        - 首次看到该群：只暖机，不上报历史
        - 之后：上报相对上次快照的新增行（相同文案再发一次也能识别）
        """
        now = datetime.now(timezone.utc).astimezone()
        curr: list[tuple[str, str, str, str]] = []
        for sender, content, msg_type in items:
            content_s = str(content or "").strip()
            sender_s = str(sender or "").strip() or "未知发送人"
            type_s = str(msg_type or "文本")
            if not content_s or not self._is_text_type(type_s):
                continue
            curr.append((sender_s, content_s, type_s, ""))

        if chat_name not in self._warmed:
            self._last_snapshot[chat_name] = curr
            self._warmed.add(chat_name)
            logger.info(
                "chat warmed (skip history): %s items=%s", chat_name, len(curr)
            )
            return []

        prev = self._last_snapshot.get(chat_name, [])
        new_rows = _diff_new_message_rows(prev, curr)
        self._last_snapshot[chat_name] = curr

        out: list[RawMessage] = []
        for sender_s, content_s, type_s, ui_time in new_rows:
            self._seq += 1
            out.append(
                RawMessage(
                    chat_name=chat_name,
                    sender_display_name=sender_s,
                    message_type=MessageType.TEXT,
                    raw_text=content_s,
                    client_observed_at=now,
                    local_sequence=self._seq,
                    ui_timestamp=ui_time,
                    extra={"adapter": "pyweixin", "ui_type": type_s},
                )
            )
        if new_rows:
            logger.info(
                "new messages chat=%s count=%s preview=%s",
                chat_name,
                len(new_rows),
                (new_rows[-1][1][:40] if new_rows else ""),
            )
        return out

    def _normalize_history(self, history: Any) -> list[tuple[str, str, str]]:
        if not history:
            return []
        if isinstance(history, str):
            import json

            history = json.loads(history)
        out: list[tuple[str, str, str]] = []
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                sender = (
                    item.get("消息发送人")
                    or item.get("sender")
                    or item.get("发送人")
                    or ""
                )
                content = (
                    item.get("消息内容")
                    or item.get("content")
                    or item.get("文本内容")
                    or ""
                )
                msg_type = item.get("消息类型") or item.get("type") or "文本"
                if isinstance(msg_type, (list, tuple)):
                    msg_type = msg_type[0] if msg_type else "文本"
                out.append((str(sender), str(content), str(msg_type)))
        return out

    def _is_text_type(self, type_s: str) -> bool:
        t = type_s.lower()
        return not any(
            x in t
            for x in ("图片", "视频", "文件", "链接", "语音", "红包", "image", "video", "file")
        )

    def list_pinned_chats(self) -> list[dict]:
        """
        扫描桌面上独立聊天窗。
        优先 TOPMOST（图钉置顶）；若当前微信不设置 TOPMOST，则凡 ChatSingleWindow 都可听。
        群名优先取窗内标题 Text（标题为 Weixin 时）。
        """
        import ctypes

        user32 = ctypes.windll.user32
        found: dict[str, dict] = {}
        try:
            from pywinauto import Desktop  # type: ignore
        except ImportError:
            return []

        try:
            desktop = Desktop(backend="uia")
            singles: list[tuple[Any, int, bool]] = []
            for w in desktop.windows():
                try:
                    cls = w.element_info.class_name or ""
                    handle = int(w.handle)
                except Exception:  # noqa: BLE001
                    continue
                if "MainWindow" in cls:
                    continue
                if "ChatSingle" not in cls and not (
                    "Chat" in cls and "mmui::" in cls and "Main" not in cls
                ):
                    continue
                try:
                    ex = int(user32.GetWindowLongW(handle, _GWL_EXSTYLE))
                    topmost = bool(ex & _WS_EX_TOPMOST)
                except Exception:  # noqa: BLE001
                    topmost = False
                singles.append((w, handle, topmost))

            # 有置顶则只听置顶；全无置顶则听全部独立小窗
            use = [x for x in singles if x[2]] or singles
            for w, handle, topmost in use:
                name = self._chat_window_display_name(w)
                if not name or name in {"主窗口", "微信", "Weixin", "WeChat"}:
                    continue
                if classify_session_title(name) in {"noise", "system"}:
                    continue
                found[name] = {
                    "name": name,
                    "raw_title": name,
                    "member_count": None,
                    "source": "pinned_window" if topmost else "separate_window",
                    "kind": "group",
                    "hwnd": handle,
                }
                self._dialogs[name] = w
        except Exception as exc:  # noqa: BLE001
            logger.warning("扫描置顶聊天窗失败: %s", exc)

        items = sorted(found.values(), key=lambda x: x["name"].casefold())
        logger.info(
            "pinned chats: %s",
            [x["name"] for x in items] or "(none)",
        )
        return items

    def list_groups(self) -> list[dict]:
        """只读会话列表（不点通讯录），过滤噪音。"""
        if not self._started:
            self.start()
        found: dict[str, dict] = {}
        session_items: list[dict] = []
        for raw in self._list_session_titles(raw=True):
            kind = classify_session_title(raw)
            if kind in {"noise", "system"}:
                continue
            key = normalize_chat_name(raw)
            if not key or re.fullmatch(r"\d{1,2}:\d{2}", key):
                continue
            mc = None
            m = _MEMBER_COUNT_RE.search(raw.strip().splitlines()[0])
            if m:
                try:
                    mc = int(m.group(1))
                except ValueError:
                    mc = None
            item_kind = "group" if kind == "group" else "unknown"
            session_items.append(
                {
                    "name": key,
                    "member_count": mc,
                    "source": "session_list",
                    "kind": item_kind,
                }
            )
        groups = [x for x in session_items if x["kind"] == "group"]
        for item in groups if groups else session_items:
            found[item["name"]] = item
        return sorted(
            found.values(),
            key=lambda x: (x.get("kind") != "group", x["name"].casefold()),
        )

    def _list_session_titles(self, *, raw: bool = False) -> list[str]:
        try:
            from pywinauto import Desktop  # type: ignore
        except ImportError:
            return []
        titles: list[str] = []
        try:
            desktop = Desktop(backend="uia")
            wins = [
                w
                for w in desktop.windows()
                if "mmui::MainWindow" in (w.element_info.class_name or "")
            ]
            if not wins:
                return []
            main = wins[0]
            for lst in main.descendants(control_type="List"):
                try:
                    for item in lst.children(control_type="ListItem"):
                        text = (item.window_text() or item.element_info.name or "").strip()
                        if not text:
                            continue
                        first = text.splitlines()[0].strip()
                        if not first or len(first) > 80:
                            continue
                        value = first if raw else normalize_chat_name(first)
                        if value and (first if raw else value) not in titles:
                            titles.append(first if raw else value)
                except Exception:  # noqa: BLE001
                    continue
                if len(titles) >= 40:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取会话列表失败: %s", exc)
        return titles

    def send_text(self, chat_name: str, text: str) -> SendResult:
        if not self._started:
            return SendResult(ok=False, detail="adapter_not_started")
        pyweixin = self._ensure_import()
        pos = _cursor_pos()
        try:
            win = self._get_or_open_dialog(pyweixin, chat_name)
            if win is not None and self._send_in_dialog(win, text):
                return SendResult(ok=True, detail="ok_sticky_window")

            if not self._allow_search:
                return SendResult(
                    ok=False,
                    detail="no_sticky_window_and_search_disabled",
                )

            messages_mod = getattr(pyweixin, "Messages", None)
            fn = getattr(messages_mod, "send_messages_to_friend", None)
            if not callable(fn):
                return SendResult(ok=False, detail="no_send_api")
            fn(
                friend=chat_name,
                messages=[text],
                search_pages=0,
                is_maximize=False,
                close_weixin=False,
            )
            return SendResult(ok=True, detail="ok_search_send")
        except Exception as exc:  # noqa: BLE001
            logger.exception("发送失败 chat=%s", chat_name)
            return SendResult(ok=False, detail=str(exc), uncertain=False)
        finally:
            _restore_cursor(pos)

    def _send_in_dialog(self, win: Any, text: str) -> bool:
        """往独立聊天窗输入框发文本。中文必须走剪贴板，type_keys 会假成功。"""
        try:
            win.restore()
        except Exception:  # noqa: BLE001
            pass
        try:
            win.set_focus()
        except Exception:  # noqa: BLE001
            pass
        try:
            edits = [
                e
                for e in win.descendants(control_type="Edit")
                if e.is_visible() and e.is_enabled()
            ]
            edit = edits[-1] if edits else None
        except Exception:  # noqa: BLE001
            return False
        if edit is None:
            logger.warning("send: no edit box in dialog")
            return False
        try:
            edit.set_focus()
        except Exception:  # noqa: BLE001
            try:
                edit.click_input()
            except Exception:  # noqa: BLE001
                return False

        # 1) 优先 set_edit_text
        try:
            edit.set_edit_text("")
            edit.set_edit_text(text)
            got = ""
            try:
                got = edit.get_value() or edit.window_text() or ""
            except Exception:  # noqa: BLE001
                got = ""
            if text[:8] not in (got or ""):
                raise RuntimeError("set_edit_text not applied")
        except Exception:  # noqa: BLE001
            # 2) 剪贴板 + Ctrl+V（支持中文）
            if not _clipboard_paste(edit, text):
                logger.warning("send: clipboard paste failed")
                return False

        try:
            edit.type_keys("{ENTER}")
            time.sleep(0.35)
            logger.info("send: enter pressed len=%s", len(text))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("send: enter failed: %s", exc)
            return False

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            running=self._started,
            wechat_login_state="logged_in" if self._started else "unknown",
            wechat_version=self._version or self.client_version(),
            detail=f"PyWeixinAdapter/{self._listen_mode}",
        )

    def client_version(self) -> str:
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat")
            try:
                ver, _ = winreg.QueryValueEx(key, "Version")
                return str(ver)
            except OSError:
                return "wechat-unknown"
        except Exception:  # noqa: BLE001
            return self._version or "unknown"

    def close(self) -> None:
        self._started = False
        self._dialogs.clear()


def _clipboard_paste(edit: Any, text: str) -> bool:
    """把文本写入剪贴板并在输入框 Ctrl+V。"""
    try:
        import ctypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            raw = (text + "\0").encode("utf-16-le")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, raw, len(raw))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
        finally:
            user32.CloseClipboard()

        try:
            edit.type_keys("^a", pause=0.02)
            time.sleep(0.05)
            edit.type_keys("^v", pause=0.05)
            time.sleep(0.15)
            return True
        except Exception:  # noqa: BLE001
            # SendInput 兜底
            user32.keybd_event(0x11, 0, 0, 0)  # CTRL
            user32.keybd_event(0x56, 0, 0, 0)  # V
            user32.keybd_event(0x56, 0, 2, 0)
            user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.15)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("clipboard paste error: %s", exc)
        return False


def _diff_new_message_rows(
    prev: list[tuple[str, str, str, str]], curr: list[tuple[str, str, str, str]]
) -> list[tuple[str, str, str, str]]:
    """
    找出相对上次快照的新增消息行。
    重要：微信 4.x UIA 发送人常为「未知发送人」，OCR 解析后若写回快照，
    下一轮 UIA 又变回未知，会被误判成新消息反复上报。故比较时忽略发送人。
    用正文+UI时间绑定同一气泡；不同时间的相同文案视为多条新消息。
    滚动屏幕时列表重绘，禁止把旧气泡再次当作新消息（尤其不能盲目取末尾 2 条）。
    """
    if not curr:
        return []
    if not prev:
        return list(curr)

    def _key(row: tuple[str, str, str, str]) -> tuple[str, str, str]:
        _sender, content, typ, ui_time = row
        return (
            (content or "").strip(),
            (ui_time or "").strip(),
            (typ or "文本").strip(),
        )

    prev_keys = [_key(r) for r in prev]
    curr_keys = [_key(r) for r in curr]
    if curr_keys == prev_keys:
        return []
    # 常见：只在末尾追加
    if len(curr_keys) >= len(prev_keys) and curr_keys[: len(prev_keys)] == prev_keys:
        return curr[len(prev_keys) :]
    # 列表滚动/重绘：找 prev 末尾最长连续段在 curr 中的位置
    max_k = min(len(prev_keys), len(curr_keys))
    for k in range(max_k, 0, -1):
        suffix = prev_keys[-k:]
        for i in range(len(curr_keys) - k + 1):
            if curr_keys[i : i + k] == suffix:
                return curr[i + k :]
    # 完全对不上：只返回 prev 中从未出现过的正文+时间（绝不回放旧消息）
    prev_set = set(prev_keys)
    fresh = [row for row, key in zip(curr, curr_keys) if key not in prev_set]
    if not fresh:
        return []
    return fresh[-3:] if len(fresh) > 3 else fresh


def _find_list_item_for_content(
    detailed: list[tuple[Any, str, str, str, str]], content: str
) -> Any | None:
    """在详细消息列表中按内容找对应 ListItem（仅精确匹配，避免 66 误命中 666）。"""
    content = (content or "").strip()
    if not content:
        return None
    for item, _s, c, _t, _tm in reversed(detailed):
        if (c or "").strip() == content:
            return item
    return None


def _match_known_nick_exact(nick: str, known: list[str]) -> str:
    """与绑定表昵称匹配：先规范化精确比，再按 nickname_match_key 兜底。"""
    n = normalize_group_nickname(nick) or (nick or "").strip()
    if not n:
        return ""
    for cand in known:
        c = normalize_group_nickname(cand) or (cand or "").strip()
        if c and c == n:
            return cand if (cand or "").strip() else c
    n_key = nickname_match_key(n)
    if not n_key:
        return ""
    hits = []
    for cand in known:
        c = (cand or "").strip()
        if c and nickname_match_key(c) == n_key:
            hits.append(cand)
    if len(hits) == 1:
        return hits[0]
    return ""


def _read_profile_popup_nickname() -> str:
    """读取点击头像后弹出的资料卡昵称/群昵称。"""
    try:
        from pywinauto import Desktop  # type: ignore
    except ImportError:
        return ""
    labels = {
        "昵称",
        "昵称：",
        "群昵称",
        "群内昵称",
        "微信号",
        "地区",
        "备注",
        "标签",
        "描述",
        "权限",
        "朋友权限",
        "来源",
        "共同群聊",
        "发消息",
        "语音聊天",
        "视频聊天",
        "企业",
        "企业微信",
        "朋友圈",
        "实名",
        "职务",
        "添加到通讯录",
        "音视频通话",
    }
    try:
        desktop = Desktop(backend="uia")
        popups = []
        # pyweixin 标准资料卡
        try:
            pop = desktop.window(
                title="Weixin",
                control_type="Window",
                class_name="mmui::ProfileUniquePop",
                top_level_only=False,
            )
            if pop.exists(timeout=0.3):
                popups.append(pop)
        except Exception:  # noqa: BLE001
            pass
        for w in desktop.windows():
            try:
                cls = (w.element_info.class_name or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if "ProfileUniquePop" in cls or cls.endswith("ProfilePop"):
                if w not in popups:
                    popups.append(w)
        if not popups:
            for w in desktop.windows():
                try:
                    cls = (w.element_info.class_name or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if "ChatSingle" not in cls:
                    continue
                for g in w.descendants(control_type="Group"):
                    gcls = g.element_info.class_name or ""
                    if "Profile" in gcls:
                        popups.append(g)
                        break
                if popups:
                    break
        for pop in popups:
            texts: list[str] = []
            try:
                for t in pop.descendants(control_type="Text"):
                    s = (t.window_text() or t.element_info.name or "").strip()
                    if s:
                        texts.append(s)
            except Exception:  # noqa: BLE001
                continue
            # 按钮名有时是展示名（朋友圈同款）
            try:
                for b in pop.descendants(control_type="Button"):
                    s = (b.window_text() or b.element_info.name or "").strip()
                    if s and s not in labels and is_plausible_group_nickname(s):
                        return normalize_group_nickname(s) or s
            except Exception:  # noqa: BLE001
                pass
            for i, s in enumerate(texts):
                if s in {"群昵称", "群内昵称", "群內暱稱"} and i + 1 < len(texts):
                    val = texts[i + 1].strip()
                    if val and val not in labels:
                        return normalize_group_nickname(val) or val
            for i, s in enumerate(texts):
                if s in {"昵称", "昵称：", "暱稱"} and i + 1 < len(texts):
                    val = texts[i + 1].strip()
                    if val and val not in labels:
                        return normalize_group_nickname(val) or val
            # 企微：展示名常带 @企业
            for s in texts:
                if s in labels:
                    continue
                if "@" in s and is_plausible_group_nickname(s):
                    return normalize_group_nickname(s) or s
            for s in texts:
                if s in labels:
                    continue
                if is_plausible_group_nickname(s):
                    return normalize_group_nickname(s) or s
            for s in texts:
                if s not in labels and 1 < len(s) <= 48:
                    return normalize_group_nickname(s) or s
    except Exception as exc:  # noqa: BLE001
        logger.debug("read profile popup failed: %s", exc)
    return ""


_UI_TIME_RE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}(?::\d{2})?"
    r"|昨[天日]\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|前[天日]\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|星期[一二三四五六日天]\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|周[一二三四五六日天]\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|今天\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
    r")$"
)


def _looks_like_ui_timestamp(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return bool(_UI_TIME_RE.fullmatch(s))


def _extract_ui_timestamp(texts: list[str]) -> str:
    """从 ListItem 文本节点提取微信气泡旁的时间标签。"""
    for part in texts:
        s = (part or "").strip()
        if _looks_like_ui_timestamp(s):
            return re.sub(r"\s+", " ", s)
    return ""


def _parse_list_item_message(item: Any) -> tuple[str, str, str]:
    """从微信消息 ListItem 提取 (发送人, 内容, UI时间)。

    注意（与 pywechat 作者结论一致）：微信 4.x 群昵称多为图片，UIA 通常读不到。
    禁止从兄弟 ListItem 偷昵称（会把上一条的人错安到本条）。
    """
    texts: list[str] = []
    try:
        for t in item.descendants(control_type="Text"):
            s = (t.window_text() or t.element_info.name or "").strip()
            if s and s not in texts:
                texts.append(s)
    except Exception:  # noqa: BLE001
        texts = []

    try:
        raw = (item.window_text() or item.element_info.name or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""
    if raw:
        for ln in raw.replace("\r", "\n").split("\n"):
            ln = ln.strip()
            if ln and ln not in texts:
                texts.append(ln)

    ui_time = _extract_ui_timestamp(texts)

    # 内容：优先「记账 …」行；整段若是机器人回执则丢弃
    content = ""
    for part in texts:
        if part.startswith("记账") or part.startswith("記賬"):
            if part.startswith(("记账成功", "記賬成功")):
                continue
            content = part
            break
    if not content:
        for part in texts:
            if _looks_like_ui_timestamp(part):
                continue
            if is_bot_receipt_text(part):
                continue
            if is_plausible_group_nickname(part) and len(part) <= 40:
                continue
            if part and not part.startswith(("✅", "❌", "ℹ️")):
                content = part
                break
    if not content and raw:
        for ln in raw.splitlines():
            ln = ln.strip()
            if ln.startswith("记账") or ln.startswith("記賬"):
                if ln.startswith(("记账成功", "記賬成功")):
                    continue
                content = ln
                break
        if not content:
            lines = [
                ln.strip()
                for ln in raw.splitlines()
                if ln.strip() and not _looks_like_ui_timestamp(ln.strip())
            ]
            if len(lines) >= 2:
                content = "\n".join(lines[1:])
            elif lines:
                content = lines[0]

    if not content or is_bot_receipt_text(content):
        return "", "", ""

    # 发送人：仅本 item 内像昵称的短文本（不信任兄弟节点）
    sender = ""
    for part in texts:
        if not part or part == content:
            continue
        if _looks_like_ui_timestamp(part):
            continue
        if part.startswith(("记账", "記賬", "✅", "❌", "ℹ️")):
            continue
        if is_bot_receipt_text(part):
            continue
        if is_plausible_group_nickname(part):
            sender = normalize_group_nickname(part) or part
            break

    if not sender:
        return "未知发送人", content, ui_time
    return sender, content, ui_time


def _message_item_is_self(item: Any, win: Any) -> bool:
    """右侧气泡≈自己发的（微信独立窗常见布局）。"""
    try:
        ir = item.rectangle()
        wr = win.rectangle()
        item_w = max(1, int(ir.right) - int(ir.left))
        win_w = max(1, int(wr.right) - int(wr.left))
        # 微信 4.x ListItem 常是整行宽，左缘判断会失效
        if item_w >= int(win_w * 0.82):
            return False
        mid = (int(wr.left) + int(wr.right)) / 2
        # 气泡左缘在窗口中线右侧，视为自己
        return int(ir.left) >= mid - 20
    except Exception:  # noqa: BLE001
        return False


def _split_message_row(text: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", ""
    if len(lines) == 1:
        # 单行：若以「记账」开头，发送人未知
        return "未知发送人", lines[0]
    sender = lines[0]
    content = "\n".join(lines[1:])
    if len(sender) <= 12 and (":" in sender or "：" in sender) and any(
        ch.isdigit() for ch in sender
    ):
        if len(lines) >= 3:
            return lines[1], "\n".join(lines[2:])
    # 昵称行不应是指令本身或机器人回复
    if not is_plausible_group_nickname(sender) or sender.startswith("记账"):
        return "未知发送人", text.replace("\n", " ").strip()
    return sender, content


def _cursor_pos() -> tuple[int, int] | None:
    try:
        import ctypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:  # noqa: BLE001
        return None
    return None


def _restore_cursor(pos: tuple[int, int] | None) -> None:
    if not pos:
        return
    try:
        import ctypes

        ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1]))
    except Exception:  # noqa: BLE001
        pass


def _window_hwnd(win: Any) -> int | None:
    if win is None:
        return None
    try:
        h = int(win.handle)
        return h if h else None
    except Exception:  # noqa: BLE001
        return None


def _window_ghost(win: Any) -> dict[str, int] | None:
    """
    窗口近乎透明（仍可 UIA 操作），避免挡屏幕。
    返回供 _window_unghost 还原的状态；失败返回 None。
    """
    import ctypes

    hwnd = _window_hwnd(win)
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    gwl_exstyle = -20
    ws_ex_layered = 0x00080000
    lwa_alpha = 0x00000002
    try:
        ex = int(user32.GetWindowLongW(hwnd, gwl_exstyle))
        user32.SetWindowLongW(hwnd, gwl_exstyle, ex | ws_ex_layered)
        # alpha=1：几乎看不见，但 Qt/UIA 仍正常
        user32.SetLayeredWindowAttributes(hwnd, 0, 1, lwa_alpha)
        return {"hwnd": hwnd, "exstyle": ex}
    except Exception:  # noqa: BLE001
        return None


def _window_unghost(state: dict[str, int] | None) -> None:
    if not state:
        return
    import ctypes

    user32 = ctypes.windll.user32
    gwl_exstyle = -20
    lwa_alpha = 0x00000002
    hwnd = int(state.get("hwnd") or 0)
    ex = int(state.get("exstyle") or 0)
    if not hwnd:
        return
    try:
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, lwa_alpha)
        user32.SetWindowLongW(hwnd, gwl_exstyle, ex)
    except Exception:  # noqa: BLE001
        pass


def _window_offscreen(win: Any) -> dict[str, int] | None:
    """兼容旧名：改为透明隐身，不再移到 -32000（会导致微信弹层失效）。"""
    return _window_ghost(win)


def _window_restore(win: Any, state: dict[str, int] | None) -> None:
    """兼容旧名：还原透明隐身。"""
    _window_unghost(state)


def _close_chat_history_windows(chat_name: str) -> None:
    """关掉残留的聊天记录窗（读成员时打开的）。"""
    try:
        from pywinauto import Desktop  # type: ignore
    except ImportError:
        return
    needle = normalize_chat_name(chat_name)
    try:
        for w in Desktop(backend="uia").windows():
            try:
                title = (w.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if "聊天记录" not in title and "Chat History" not in title:
                continue
            if needle and needle[:4] not in title and needle not in title:
                continue
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _hide_transient_popovers() -> None:
    """把 @ 成员列表 / 群成员 Popover 等临时窗设为近乎透明。"""
    try:
        from pywinauto import Desktop  # type: ignore
    except ImportError:
        return
    try:
        for w in Desktop(backend="uia").windows():
            try:
                aid = (w.element_info.automation_id or "").strip()
                cls = (w.element_info.class_name or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not (
                aid == "MentionPopover"
                or "Mention" in aid
                or "Popover" in cls
                or "XPopover" in cls
                or "ToolSaveBits" in cls
            ):
                continue
            _window_ghost(w)
    except Exception:  # noqa: BLE001
        pass


def _uia_focus(ctrl: Any) -> bool:
    """仅设焦点，不移动鼠标。"""
    if ctrl is None:
        return False
    try:
        ctrl.set_focus()
        return True
    except Exception:  # noqa: BLE001
        return False


def _click_restore(ctrl: Any) -> bool:
    """
    真实点击（微信 Qt 弹层常需要），但立刻还原光标，尽量不「抢走」鼠标。
    """
    if ctrl is None:
        return False
    pos = _cursor_pos()
    try:
        ctrl.click_input()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        _restore_cursor(pos)
        # 再设一次，防止部分环境还原偏一点
        _restore_cursor(pos)


def _uia_activate(ctrl: Any) -> bool:
    """
    激活按钮/Tab：先试无鼠标 select/invoke，失败再短点击并还原光标。
    （纯 Space/Enter 在微信里常假成功却打不开窗，故不用作成功依据。）
    """
    if ctrl is None:
        return False
    _uia_focus(ctrl)
    for meth in ("select", "invoke"):
        try:
            fn = getattr(ctrl, meth, None)
            if callable(fn):
                fn()
                return True
        except Exception:  # noqa: BLE001
            continue
    return _click_restore(ctrl)


def _uia_type(ctrl: Any, keys: str, *, pause: float = 0.03) -> bool:
    """向已聚焦控件发按键，不移动鼠标。"""
    if ctrl is None:
        return False
    _uia_focus(ctrl)
    try:
        ctrl.type_keys(keys, pause=pause)
        return True
    except Exception:  # noqa: BLE001
        return False
