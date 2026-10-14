"""网页控制台业务层（纯函数，无 UI）。"""

from __future__ import annotations

import os
import threading
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

_django_lock = threading.Lock()
_django_ready = False


def ensure_django() -> None:
    global _django_ready
    if _django_ready:
        return
    with _django_lock:
        if _django_ready:
            return
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        from django.apps import apps

        if not apps.ready:
            django.setup()
        _django_ready = True


_STATUS_MAP = {
    "ACTIVE": "未清账",
    "REVERSED": "已撤销",
    "PROCESSED": "已清账",
}


def load_nicks_for_room(room_name: str) -> list[str]:
    """读取群昵称：绑定 + 消息历史 + 微信成员列表，并写入 room_nicks 缓存。"""
    ensure_django()
    from apps.chatrooms.models import ChatRoom, SenderBinding
    from apps.ledger.models import MessageEvent
    from gateway.chat_names import is_plausible_group_nickname, normalize_group_nickname

    name = (room_name or "").strip()
    if not name:
        raise ValueError("群名为空，请先刷新置顶群")

    names: list[str] = []

    def _add(n: str) -> None:
        n = normalize_group_nickname(n)
        if not n or n in names:
            return
        if not is_plausible_group_nickname(n):
            return
        names.append(n)

    room = ChatRoom.objects.filter(name=name).first()
    if room:
        for n in (
            SenderBinding.objects.filter(chat_room=room, active=True)
            .values_list("sender_display_name", flat=True)
            .distinct()
        ):
            _add(n)
        for n in (
            MessageEvent.objects.filter(chat_room=room)
            .exclude(sender_display_name="")
            .exclude(sender_display_name="未知发送人")
            .order_by("-id")
            .values_list("sender_display_name", flat=True)[:80]
        ):
            _add(n)

    wx_err = ""
    try:
        from gateway.adapters.pyweixin import PyWeixinAdapter
        from gateway.sender_resolve import set_room_nicks

        adapter = PyWeixinAdapter()
        adapter.start()
        for n in adapter.list_group_members(name):
            _add(n)
        set_room_nicks(name, names)
    except Exception as exc:  # noqa: BLE001
        wx_err = str(exc)

    if not names and wx_err:
        raise RuntimeError(
            f"读取「{name}」成员失败：{wx_err}\n请确认该群已单独打开并置顶。"
        )
    if not names:
        raise RuntimeError(
            f"「{name}」未读到昵称。请确认群窗口已单独打开并置顶，再重试「读取人员」。"
        )
    return names


def list_bindings(room_name: str) -> list[dict[str, Any]]:
    ensure_django()
    from apps.chatrooms.models import SenderBinding

    rows = list(
        SenderBinding.objects.filter(chat_room__name=room_name, active=True)
        .select_related("employee")
        .order_by("employee__display_name", "sender_display_name")
    )
    out: list[dict[str, Any]] = []
    for b in rows:
        role = (b.role or "staff").strip() or "staff"
        out.append(
            {
                "id": b.id,
                "nick": b.sender_display_name,
                "display": b.employee.display_name if b.employee_id else "",
                "role": role,
                "role_label": "管理员" if role in {"admin", "finance"} else "员工",
                "employee_id": b.employee_id,
            }
        )
    return out


def add_binding(room_name: str, nick: str, display: str, role: str) -> str:
    ensure_django()
    from apps.chatrooms.models import ChatRoom, Employee, SenderBinding

    nick = (nick or "").strip()
    display = (display or nick).strip()
    role = "admin" if (role or "").strip() in {"admin", "管理员"} else "staff"
    role_label = "管理员" if role == "admin" else "员工"
    if not nick:
        raise ValueError("请选择或填写微信群昵称")

    room, _ = ChatRoom.objects.get_or_create(
        name=room_name,
        defaults={"enabled": True, "strict_prefix": True},
    )
    existed_emp = Employee.objects.filter(display_name=display, enabled=True).first()
    emp = existed_emp or Employee.objects.filter(
        employee_code=nick[:32], enabled=True
    ).first()
    merged = existed_emp is not None
    if emp is None:
        n = Employee.objects.count() + 1
        code = f"{n:03d}"
        while Employee.objects.filter(employee_code=code).exists():
            n += 1
            code = f"{n:03d}"
        emp = Employee.objects.create(
            employee_code=code,
            display_name=display[:64],
            role=role,
            enabled=True,
        )
        merged = False
    else:
        fields = ["updated_at"]
        if display and emp.display_name != display:
            emp.display_name = display[:64]
            fields.append("display_name")
        # 不再改 Employee.role（全局）；角色按群写在绑定上
        emp.save(update_fields=fields)

    obj = SenderBinding.objects.filter(
        chat_room=room, sender_display_name=nick, active=True
    ).first()
    if obj is None:
        SenderBinding.objects.create(
            chat_room=room,
            sender_display_name=nick,
            employee=emp,
            role=role,
            active=True,
        )
    else:
        obj.employee = emp
        obj.role = role
        obj.save(update_fields=["employee", "role"])

    # 同备注名在本群的其它昵称一并同步本群角色
    SenderBinding.objects.filter(
        chat_room=room, employee=emp, active=True
    ).update(role=role)

    nick_count = SenderBinding.objects.filter(
        chat_room=room, employee=emp, active=True
    ).count()
    if merged and nick_count >= 1:
        msg = (
            f"已把微信号「{nick}」并入备注名「{display}」"
            f"（共 {nick_count} 个微信号）"
        )
    else:
        msg = f"已添加：{room_name} / {nick} → 备注「{display}」（{role_label}）"

    try:
        # 只把新昵称并入缓存，绝不能用「仅已绑定」去全量覆盖（否则下拉只剩已绑的人）
        from gateway.sender_resolve import remember_room_nicks

        remember_room_nicks(room_name, [nick])
    except Exception:
        pass

    return msg


def remove_binding(bind_id: int) -> None:
    ensure_django()
    from apps.chatrooms.models import SenderBinding

    SenderBinding.objects.filter(pk=bind_id).update(active=False)
    # 不改 room_nicks 缓存：读取到的成员列表应保留，方便继续下拉绑定


def set_binding_role(bind_id: int, role: str) -> None:
    ensure_django()
    from apps.chatrooms.models import SenderBinding

    role = "admin" if (role or "").strip() in {"admin", "管理员", "finance", "财务"} else "staff"
    b = SenderBinding.objects.select_related("employee").get(pk=bind_id)
    # 本群同备注名全部昵称同步角色（按群授权）
    SenderBinding.objects.filter(
        chat_room_id=b.chat_room_id,
        employee_id=b.employee_id,
        active=True,
    ).update(role=role)


def load_room_settings(room_name: str) -> dict[str, Any]:
    ensure_django()
    from apps.ledger.books import list_room_book_scopes, list_room_staff_biz_scopes
    from apps.ledger.staff_order_catalog import STAFF_BIZ_CATALOG

    name = (room_name or "").strip()
    book_rows = list_room_book_scopes(listening_names=[name])
    biz_rows = list_room_staff_biz_scopes(listening_names=[name])

    room_id = 0
    books: list[dict[str, Any]] = []
    checked_map: dict[str, bool] = {}

    if book_rows:
        row = book_rows[0]
        room_id = int(row.get("room_id") or 0)
        books = [
            {
                "book": b.get("book") or "",
                "label": b.get("label") or "",
                "checked": bool(b.get("checked")),
            }
            for b in row.get("books") or []
        ]

    if biz_rows:
        for it in biz_rows[0].get("biz_items") or []:
            key = (it.get("biz_key") or it.get("key") or "").strip()
            if key:
                checked_map[key] = bool(it.get("checked"))

    # 始终按目录顺序输出，并拆成「短标签 / 多规格」两栏，方便网页整齐排布
    biz_simple: list[dict[str, Any]] = []
    biz_multi: list[dict[str, Any]] = []
    for b in STAFF_BIZ_CATALOG:
        item = {
            "key": b.key,
            "title": b.key,
            "checked": bool(checked_map.get(b.key, False)),
            "multi": bool(b.multi_sku),
            "hint": "",
            "label": b.label,
        }
        if b.multi_sku:
            if b.key == "高级会员":
                item["hint"] = "1y=一个月+1、3y、6y、12y；减记 1y-1。仍可用一个月+1"
            else:
                item["hint"] = "、".join(b.skus)
                if len(b.skus) > 6:
                    item["hint"] = "、".join(b.skus[:6]) + f" 等{len(b.skus)}项"
            biz_multi.append(item)
        else:
            if b.aliases:
                item["hint"] = "别名：" + "、".join(b.aliases)
            biz_simple.append(item)

    if not books:
        books = [
            {"book": "staff_order", "label": "员工记单", "checked": False},
            {"book": "ledger", "label": "记账", "checked": True},
            {"book": "usdt", "label": "USDT", "checked": True},
        ]

    return {
        "room_id": room_id,
        "books": books,
        "biz_simple": biz_simple,
        "biz_multi": biz_multi,
        # 兼容旧模板
        "biz_items": biz_simple + biz_multi,
    }


def save_room_settings(room_id: int, books: list[str], biz_keys: list[str]) -> None:
    ensure_django()
    from apps.ledger.books import save_room_book_scopes, save_room_staff_biz_scopes

    save_room_book_scopes(room_id, books)
    save_room_staff_biz_scopes(room_id, biz_keys)


def list_employees() -> list[str]:
    ensure_django()
    from apps.chatrooms.models import Employee

    return list(
        Employee.objects.filter(enabled=True)
        .order_by("display_name")
        .values_list("display_name", flat=True)
    )


def list_enabled_rooms() -> list[str]:
    ensure_django()
    from apps.chatrooms.models import ChatRoom

    return list(
        ChatRoom.objects.filter(enabled=True)
        .order_by("name")
        .values_list("name", flat=True)
    )


def list_entries(book: str, limit: int = 200, q: str = "") -> list[dict[str, Any]]:
    ensure_django()
    from apps.ledger.models import LedgerEntry

    from django.db.models import Q

    qs = LedgerEntry.objects.filter(book=book)
    if q:
        qs = qs.filter(
            Q(business_name__icontains=q)
            | Q(remark__icontains=q)
            | Q(record_no__icontains=q)
            | Q(employee__display_name__icontains=q)
            | Q(chat_room__name__icontains=q)
        )
    out: list[dict[str, Any]] = []
    for e in qs.select_related("employee", "chat_room").order_by("-id")[:limit]:
        st = _STATUS_MAP.get(e.status, e.status)
        person = e.employee.display_name if e.employee_id else ""
        room_name = e.chat_room.name if e.chat_room_id else ""
        signed = e.amount or Decimal("0")
        direction = e.direction or LedgerEntry.Direction.INCOME
        if book == "staff_order" and direction == LedgerEntry.Direction.EXPENSE:
            signed = -signed
        direction_label = "减少" if direction == LedgerEntry.Direction.EXPENSE else "增加"
        out.append(
            {
                "id": e.id,
                "business_name": e.business_name or e.remark or "",
                "receive_amount": e.receive_amount,
                "amount_to_you": e.amount_to_you,
                "amount_to_me": e.amount_to_me,
                "profit_amount": e.profit_amount,
                "amount": signed,
                "direction": direction,
                "direction_label": direction_label,
                "person": person,
                "room": room_name,
                "status": e.status,
                "status_label": st,
                "record_no": e.record_no,
            }
        )
    return out


def list_keywords(book: str) -> list[dict[str, Any]]:
    ensure_django()
    try:
        from apps.rules.management.commands.seed_keywords import ensure_default_keywords

        ensure_default_keywords(refresh=False)
    except Exception:
        pass

    from apps.rules.models import Keyword

    rows = Keyword.objects.filter(book=book).order_by("sort_order", "id")
    return [
        {
            "id": k.id,
            "keyword": k.keyword,
            "usages": k.usages_display() or (k.remark or ""),
            "enabled": k.enabled,
            "enabled_label": "启用" if k.enabled else "停用",
        }
        for k in rows
    ]


def _parse_decimal(raw: str, default: str = "0") -> Decimal:
    try:
        return Decimal((raw or default).strip() or default)
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("金额格式不正确") from exc


def add_ledger_entry(
    book: str,
    *,
    person: str,
    room: str,
    business_name: str = "",
    receive_amount: str | Decimal = "0",
    amount_to_you: str | Decimal = "0",
    amount_to_me: str | Decimal = "0",
    profit_amount: str | Decimal = "0",
    amount: str | Decimal = "0",
    direction: str = "INCOME",
) -> int:
    ensure_django()
    from django.utils import timezone

    from apps.chatrooms.models import ChatRoom, Employee
    from apps.gateway_api.services import ensure_fallback_category, next_record_no
    from apps.ledger.ledger_ops import ledger_primary_amount
    from apps.ledger.models import LedgerEntry, MessageEvent

    person = (person or "").strip()
    room = (room or "").strip()
    business_name = (business_name or "").strip()
    if not person or not room:
        raise ValueError("请选择提交人与群")
    if not business_name:
        raise ValueError("请填写项目/业务名")

    emp = Employee.objects.filter(display_name=person, enabled=True).first()
    if emp is None:
        raise ValueError(f"未找到人员「{person}」")
    chat = ChatRoom.objects.filter(name=room).first()
    if chat is None:
        raise ValueError(f"未找到群「{room}」")

    now = timezone.now()
    if book == "staff_order":
        amt = _parse_decimal(str(amount))
        if amt == 0:
            raise ValueError("请填写数量")
        dir_val = (
            LedgerEntry.Direction.EXPENSE
            if (direction or "").upper() in {"EXPENSE", "减少", "减"}
            else LedgerEntry.Direction.INCOME
        )
        entry_amount = abs(amt)
        receive = Decimal("0")
        to_you = Decimal("0")
        to_me = Decimal("0")
        profit = Decimal("0")
        primary = entry_amount
    else:
        recv = _parse_decimal(str(receive_amount))
        to_you = _parse_decimal(str(amount_to_you))
        to_me = _parse_decimal(str(amount_to_me))
        profit = _parse_decimal(str(profit_amount))
        if recv == 0 and to_you == 0 and to_me == 0 and profit == 0:
            raise ValueError("金额、给你、给我、利润至少填一项")
        primary = ledger_primary_amount(
            receive_amount=recv,
            amount_to_you=to_you,
            amount_to_me=to_me,
            profit_amount=profit,
        )
        dir_val = LedgerEntry.Direction.INCOME
        entry_amount = primary
        receive = recv

    cat = ensure_fallback_category(dir_val)
    raw_text = f"[桌面补记] {book} {business_name}"
    event = MessageEvent.objects.create(
        event_uuid=str(uuid.uuid4()),
        fingerprint=f"desktop:{book}:{uuid.uuid4().hex}",
        chat_room=chat,
        chat_name=room,
        sender_display_name=person,
        raw_text=raw_text,
        normalized_text=raw_text,
        received_at=now,
        parse_status=MessageEvent.ParseStatus.ACCEPTED,
        adapter="desktop",
    )
    entry = LedgerEntry.objects.create(
        record_no=next_record_no(),
        event=event,
        book=book,
        direction=dir_val,
        amount=entry_amount,
        currency="CNY",
        category=cat,
        employee=emp,
        chat_room=chat,
        remark=business_name,
        business_name=business_name,
        receive_amount=receive,
        amount_to_you=to_you,
        amount_to_me=to_me,
        profit_amount=profit,
        occurred_at=now,
        status=LedgerEntry.Status.ACTIVE,
    )
    try:
        from apps.integrations.xinhuo_push import schedule_push_ledger_entry

        schedule_push_ledger_entry(entry)
    except Exception:
        pass
    return entry.id


def mutate_entry(entry_id: int, action: str, **kwargs: Any) -> None:
    ensure_django()
    from django.utils import timezone

    from apps.ledger.ledger_ops import ledger_primary_amount
    from apps.ledger.models import LedgerEntry

    obj = LedgerEntry.objects.get(pk=entry_id)
    if action == "reverse":
        obj.status = LedgerEntry.Status.REVERSED
        obj.reversed_at = timezone.now()
        obj.reverse_reason = "面板撤销"
        obj.save(
            update_fields=[
                "status",
                "reversed_at",
                "reverse_reason",
                "updated_at",
            ]
        )
        try:
            from apps.integrations.xinhuo_push import schedule_push_status

            schedule_push_status(obj)
        except Exception:
            pass
    elif action == "status":
        status = kwargs.get("status") or LedgerEntry.Status.ACTIVE
        obj.status = status
        if status == LedgerEntry.Status.ACTIVE:
            obj.reversed_at = None
            obj.reverse_reason = ""
        obj.save(
            update_fields=[
                "status",
                "reversed_at",
                "reverse_reason",
                "updated_at",
            ]
        )
        try:
            from apps.integrations.xinhuo_push import schedule_push_status

            schedule_push_status(obj)
        except Exception:
            pass
    elif action == "ledger_fields":
        obj.business_name = (kwargs.get("business_name") or "")[:128]
        obj.receive_amount = kwargs.get("receive_amount") or Decimal("0")
        obj.amount_to_you = kwargs.get("amount_to_you") or Decimal("0")
        obj.amount_to_me = kwargs.get("amount_to_me") or Decimal("0")
        obj.profit_amount = kwargs.get("profit_amount") or Decimal("0")
        obj.remark = obj.business_name
        obj.amount = ledger_primary_amount(
            receive_amount=obj.receive_amount,
            amount_to_you=obj.amount_to_you,
            amount_to_me=obj.amount_to_me,
            profit_amount=obj.profit_amount,
        )
        obj.direction = LedgerEntry.Direction.INCOME
        obj.save(
            update_fields=[
                "business_name",
                "remark",
                "receive_amount",
                "amount_to_you",
                "amount_to_me",
                "profit_amount",
                "amount",
                "direction",
                "updated_at",
            ]
        )
    elif action == "delete":
        try:
            from apps.integrations.xinhuo_push import push_delete_to_xinhuo

            push_delete_to_xinhuo(obj)
        except Exception:
            pass
        event = obj.event
        obj.delete()
        try:
            event.delete()
        except Exception:
            pass
    else:
        raise ValueError(f"未知操作: {action}")


def _split_usages(raw: str) -> list[str]:
    return [
        p.strip()
        for p in (raw or "").replace("\n", "／").split("／")
        if p.strip()
    ]


def add_keyword(book: str, keyword: str, usages: str = "") -> int:
    ensure_django()
    from apps.rules.models import Keyword

    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("请填写关键词")
    usages_raw = (usages or "").strip()
    if not usages_raw:
        usages_raw = f"{keyword}100 ／ 查询{keyword} ／ {keyword}查询"
    parts = _split_usages(usages_raw)
    obj, _created = Keyword.objects.get_or_create(
        keyword=keyword,
        defaults={"book": book, "enabled": True},
    )
    obj.book = book
    obj.enabled = True
    obj.set_usage_list(parts)
    obj.save()
    return obj.id


def save_keyword(kw_id: int, keyword: str, usages: str) -> None:
    ensure_django()
    from apps.rules.models import Keyword

    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("关键词不能为空")
    obj = Keyword.objects.get(pk=kw_id)
    if Keyword.objects.filter(keyword=keyword).exclude(pk=kw_id).exists():
        raise ValueError(f"关键词「{keyword}」已被占用")
    parts = _split_usages(usages)
    obj.keyword = keyword
    obj.set_usage_list(parts)
    obj.save()


def delete_keyword(kw_id: int) -> None:
    ensure_django()
    from apps.rules.models import Keyword

    Keyword.objects.filter(pk=kw_id).delete()


def toggle_keyword(kw_id: int) -> bool:
    ensure_django()
    from apps.rules.models import Keyword

    obj = Keyword.objects.get(pk=kw_id)
    obj.enabled = not obj.enabled
    obj.save(update_fields=["enabled", "updated_at"])
    return obj.enabled
