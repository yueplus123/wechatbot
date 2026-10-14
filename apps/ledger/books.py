"""账本常量与解析（供面板 / 网关共用）。"""

from __future__ import annotations

import re

from apps.ledger.models import LedgerEntry


BOOK_CHOICES = list(LedgerEntry.Book.choices)
BOOK_LABELS = dict(LedgerEntry.Book.choices)
VALID_BOOKS = {c.value for c in LedgerEntry.Book}

# 面板主导航
BOOK_NAV: list[tuple[str, str]] = [
    (LedgerEntry.Book.STAFF_ORDER, "员工记单"),
    (LedgerEntry.Book.LEDGER, "记账"),
    (LedgerEntry.Book.USDT, "USDT"),
]


def load_keyword_books(chat_name: str | None = None) -> dict[str, str]:
    """启用中的关键词 → 账本。可按群过滤（账本群作用域）。"""
    try:
        from apps.rules.models import Keyword

        rows = list(
            Keyword.objects.filter(enabled=True)
            .exclude(book="")
            .values_list("keyword", "book")
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for kw, book in rows:
        if kw and book in VALID_BOOKS:
            out[str(kw)] = str(book)
    if not chat_name or not out:
        return out
    allowed = allowed_books_for_chat(chat_name)
    if allowed is None:
        return out
    return {kw: book for kw, book in out.items() if book in allowed}


def allowed_books_for_chat(chat_name: str) -> set[str] | None:
    """
    返回本群可用的账本集合（按群判定）。

    None = 该群尚未配置任何作用域行（兼容旧数据）。
    set() = 群不存在，或已配置但未勾选任何账本。
    """
    try:
        from apps.chatrooms.models import ChatRoom
        from apps.rules.models import BookRoomScope

        room = ChatRoom.objects.filter(name=chat_name).first()
        if room is None:
            return set()
        if not BookRoomScope.objects.filter(chat_room=room).exists():
            return None
        return set(
            BookRoomScope.objects.filter(chat_room=room, enabled=True).values_list(
                "book", flat=True
            )
        )
    except Exception:  # noqa: BLE001
        return None


def book_enabled_for_chat(chat_name: str, book: str) -> bool:
    """本群是否启用某账本。未配置过时：记账+USDT 可用，员工记单需显式勾选。"""
    allowed = allowed_books_for_chat(chat_name)
    if allowed is None:
        return book in {
            LedgerEntry.Book.LEDGER,
            LedgerEntry.Book.USDT,
        }
    return book in allowed


def staff_order_enabled_for_chat(chat_name: str) -> bool:
    return book_enabled_for_chat(chat_name, LedgerEntry.Book.STAFF_ORDER)


def load_staff_biz_for_chat(chat_name: str) -> dict[str, str]:
    """本群已启用的员工记单触发词 → 规范业务名。未开员工记单账本则空。"""
    if not chat_name or not staff_order_enabled_for_chat(chat_name):
        return {}
    try:
        from apps.chatrooms.models import ChatRoom
        from apps.ledger.staff_order_catalog import expand_triggers
        from apps.rules.models import StaffBizRoomScope

        room = ChatRoom.objects.filter(name=chat_name).first()
        if room is None:
            return {}
        keys = set(
            StaffBizRoomScope.objects.filter(
                chat_room=room, enabled=True
            ).values_list("biz_key", flat=True)
        )
        return expand_triggers(keys)
    except Exception:  # noqa: BLE001
        return {}


def list_room_staff_biz_scopes(listening_names: list[str] | None = None) -> list[dict]:
    """听群视角：每群员工记单业务关键词勾选状态。"""
    from apps.ledger.staff_order_catalog import STAFF_BIZ_CATALOG
    from apps.rules.models import StaffBizRoomScope

    rooms = _ensure_listening_rooms(listening_names)
    out: list[dict] = []
    for room in rooms:
        existing = {
            s.biz_key: s.enabled
            for s in StaffBizRoomScope.objects.filter(chat_room=room)
        }
        # 旧 key 勾选状态映射到新 key（展示用）
        from apps.ledger.staff_order_catalog import LEGACY_BIZ_KEY_MAP

        for old_key, new_key in LEGACY_BIZ_KEY_MAP.items():
            if existing.get(old_key) and not existing.get(new_key):
                existing[new_key] = True
        items = []
        for biz in STAFF_BIZ_CATALOG:
            items.append(
                {
                    "biz_key": biz.key,
                    "label": biz.label,
                    "checked": bool(existing.get(biz.key, False)),
                }
            )
        out.append(
            {
                "room_id": room.id,
                "name": room.name,
                "biz_items": items,
            }
        )
    return out


def save_room_staff_biz_scopes(room_id: int, biz_keys: list[str]) -> None:
    """保存某群勾选的员工记单业务关键词。"""
    from apps.chatrooms.models import ChatRoom
    from apps.ledger.staff_order_catalog import STAFF_BIZ_KEYS
    from apps.rules.models import StaffBizRoomScope

    room = ChatRoom.objects.filter(id=int(room_id)).first()
    if room is None:
        raise ValueError(f"群不存在: {room_id}")
    wanted = {k for k in biz_keys if k in STAFF_BIZ_KEYS}
    for key in STAFF_BIZ_KEYS:
        enabled = key in wanted
        obj, created = StaffBizRoomScope.objects.get_or_create(
            chat_room=room,
            biz_key=key,
            defaults={"enabled": enabled},
        )
        if not created and obj.enabled != enabled:
            obj.enabled = enabled
            obj.save(update_fields=["enabled", "updated_at"])


_SKIP_LISTEN_NAMES = {"主窗口", "微信", "Weixin", ""}


def _normalize_listening_names(listening_names: list[str] | None) -> list[str]:
    from gateway.chat_names import normalize_chat_name

    names: list[str] = []
    seen: set[str] = set()
    for raw in listening_names or []:
        name = normalize_chat_name(str(raw or "").strip()) or str(raw or "").strip()
        if not name or name in _SKIP_LISTEN_NAMES or name in seen:
            continue
        if "的聊天记录" in name or name.startswith("“") or name.startswith('"'):
            continue
        seen.add(name)
        names.append(name)
    return names


def _ensure_listening_rooms(listening_names: list[str] | None) -> list:
    from apps.chatrooms.models import ChatRoom

    rooms = []
    for name in _normalize_listening_names(listening_names):
        room, _ = ChatRoom.objects.get_or_create(
            name=name,
            defaults={
                "enabled": True,
                "strict_prefix": True,
                "alias": "听群同步",
            },
        )
        rooms.append(room)
    return rooms


def list_room_book_scopes(listening_names: list[str] | None = None) -> list[dict]:
    """听群视角：每个监听群的账本勾选状态。"""
    from apps.rules.models import BookRoomScope

    rooms = _ensure_listening_rooms(listening_names)
    out: list[dict] = []
    for room in rooms:
        existing = {
            s.book: s.enabled for s in BookRoomScope.objects.filter(chat_room=room)
        }
        configured = bool(existing)
        books = []
        for book, label in BOOK_NAV:
            if configured:
                checked = bool(existing.get(book, False))
            else:
                # 未保存过：默认开记账+USDT，员工记单需显式勾选
                checked = book in {
                    LedgerEntry.Book.LEDGER,
                    LedgerEntry.Book.USDT,
                }
            books.append({"book": book, "label": label, "checked": checked})
        out.append(
            {
                "room_id": room.id,
                "name": room.name,
                "alias": room.alias or "",
                "room_enabled": room.enabled,
                "configured": configured,
                "books": books,
            }
        )
    return out


def save_room_book_scopes(room_id: int, books: list[str]) -> None:
    """保存某群勾选的账本。"""
    from apps.chatrooms.models import ChatRoom
    from apps.rules.models import BookRoomScope

    room = ChatRoom.objects.filter(id=int(room_id)).first()
    if room is None:
        raise ValueError(f"群不存在: {room_id}")
    wanted = {b for b in books if b in VALID_BOOKS}
    for book in VALID_BOOKS:
        enabled = book in wanted
        obj, created = BookRoomScope.objects.get_or_create(
            book=book,
            chat_room=room,
            defaults={"enabled": enabled},
        )
        if not created and obj.enabled != enabled:
            obj.enabled = enabled
            obj.save(update_fields=["enabled", "updated_at"])


def list_book_room_scopes(
    book: str, listening_names: list[str] | None = None
) -> list[dict]:
    """兼容：某账本对「当前听群」的勾选状态。"""
    from apps.rules.models import BookRoomScope

    rooms = _ensure_listening_rooms(listening_names)
    existing = {
        s.chat_room_id: s.enabled for s in BookRoomScope.objects.filter(book=book)
    }
    room_configured = set(
        BookRoomScope.objects.filter(
            chat_room_id__in=[r.id for r in rooms]
        ).values_list("chat_room_id", flat=True)
    )
    out = []
    for r in rooms:
        if r.id in room_configured:
            checked = bool(existing.get(r.id, False))
        else:
            checked = book in {
                LedgerEntry.Book.LEDGER,
                LedgerEntry.Book.USDT,
            }
        out.append(
            {
                "room_id": r.id,
                "name": r.name,
                "alias": r.alias or "",
                "room_enabled": r.enabled,
                "checked": checked,
            }
        )
    return out


def save_book_room_scopes(book: str, room_ids: list[int]) -> None:
    """兼容：保存某账本勾选的群。"""
    from apps.chatrooms.models import ChatRoom
    from apps.rules.models import BookRoomScope

    if book not in VALID_BOOKS:
        raise ValueError(f"无效账本: {book}")
    wanted = set(int(x) for x in room_ids)
    valid_ids = set(
        ChatRoom.objects.filter(id__in=wanted).values_list("id", flat=True)
    )
    BookRoomScope.objects.filter(book=book).exclude(chat_room_id__in=valid_ids).update(
        enabled=False
    )
    for rid in valid_ids:
        obj, _ = BookRoomScope.objects.get_or_create(
            book=book, chat_room_id=rid, defaults={"enabled": True}
        )
        if not obj.enabled:
            obj.enabled = True
            obj.save(update_fields=["enabled", "updated_at"])


def is_book_command_text(text: str, chat_name: str | None = None) -> bool:
    """是否像记账 / 员工记单指令。"""
    from apps.rules.parser import normalize_text

    t = normalize_text(text or "")
    if not t:
        return False
    try:
        from gateway.chat_names import is_bot_receipt_text

        if is_bot_receipt_text(t):
            return False
    except Exception:
        pass
    if t.startswith(
        (
            "记账成功",
            "記賬成功",
            "入账成功",
            "入帳成功",
            "记录成功",
            "記錄成功",
            "✅ 订单记录成功",
            "✅订单记录成功",
        )
    ):
        return False
    if "订单记录成功" in t:
        return False
    if t.startswith(("记账", "記賬")):
        return True
    if t in {
        "查询",
        "查詢",
        "查询记账",
        "查詢記賬",
        "记账查询",
        "記賬查詢",
        "查询本月",
        "查询上月",
        "查询本周",
        "清账",
        "清帳",
        "记账",
        "記賬",
        "已转账",
        "已轉賬",
        "查询usdt",
        "查詢usdt",
        "usdt查询",
        "usdt查詢",
    }:
        return True
    # 管理员按员工查记单：查询-李振宇
    if re.fullmatch(r"查询\s*[-－—]\s*.+", t):
        return True
    # 员工记单查询/清空：查询高级会员-张超 / 清空高级会员-张超
    if re.match(r"(?:查询|清空)\s*.+[-－—].+", t):
        return True
    # 记账表：项目名 收入/支出/利润（兼容金额/给你/给我）；USDT 刀格式
    if re.search(r"(收入|支出|利润|金额|给你|给我)\s*-?\d", t):
        return True
    if re.fullmatch(
        r"(给我|usdt)\s*(?:\d+(?:\.\d{1,2})?\s*[，,]\s*)?[+-]?\d+(?:\.\d{1,2})?\s*刀",
        t,
        flags=re.IGNORECASE,
    ):
        return True
    if re.fullmatch(r"(清账|清帳|清空)\s*usdt", t, flags=re.IGNORECASE) or re.fullmatch(
        r"usdt\s*(清账|清帳|清空)", t, flags=re.IGNORECASE
    ):
        return True
    # 员工记单：本群已绑关键词
    staff_map = load_staff_biz_for_chat(chat_name) if chat_name else {}
    if staff_map:
        from apps.ledger.staff_order_catalog import is_premium_implicit_plus

        for kw in sorted(staff_map.keys(), key=len, reverse=True):
            esc = re.escape(kw)
            if re.search(rf"(?:查询|清空)\s*{esc}", t, flags=re.IGNORECASE):
                return True
            if re.search(rf"{esc}\s*(?:查询|清空)", t, flags=re.IGNORECASE):
                return True
            t_cf = t.casefold()
            kw_cf = kw.casefold()
            if t_cf == kw_cf or t_cf.startswith(kw_cf) or t_cf.endswith(kw_cf):
                if is_premium_implicit_plus(kw) and t_cf == kw_cf:
                    return True
                if (
                    "查询" in t
                    or "清空" in t
                    or "增加" in t
                    or "减少" in t
                    or re.search(rf"{esc}\s*[+-]?\d", t, flags=re.IGNORECASE)
                ):
                    return True
    kw_books = load_keyword_books(chat_name)
    for kw in sorted(kw_books.keys(), key=len, reverse=True):
        if t == kw or t.startswith(kw):
            rest = t[len(kw) :]
            if not rest or rest[0].isdigit() or rest[0].isspace() or rest[0] in "+-":
                return True
    return False


def resolve_ledger_book(
    raw_text: str, currency: str = "", preferred: str = "", chat_name: str | None = None
) -> str:
    """账本归属。"""
    _ = raw_text, currency
    if preferred and preferred in VALID_BOOKS:
        allowed = allowed_books_for_chat(chat_name) if chat_name else None
        if allowed is None or preferred in allowed:
            return preferred
    return LedgerEntry.Book.LEDGER
