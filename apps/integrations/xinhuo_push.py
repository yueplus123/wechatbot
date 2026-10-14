"""薪火对接：

- 普通记账 ledger → /api/external/ledger/push/（记账系统）
- 员工记单 staff_order + USDT → /api/external/wechat-bot/push/（微信BOT数据页）
- 查询前从薪火对账（仅 ledger）

HTTP 一律经 apps.integrations.xinhuo_bridge（能力表 + 统一鉴权/重试）。
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.integrations.xinhuo_bridge import get_bridge

if TYPE_CHECKING:
    from apps.chatrooms.models import ChatRoom, Employee
    from apps.ledger.models import LedgerEntry, MessageEvent

logger = logging.getLogger(__name__)

# 推到「微信BOT数据」的本地账本
BOT_PAGE_BOOKS = frozenset({"staff_order", "usdt"})
# 推到薪火「记账系统」的本地账本
LEDGER_PAGE_BOOKS = frozenset({"ledger"})

# 规范业务名 → 薪火 WechatBotRecord.table_type（与薪火 _TABLE_ALIASES 对齐）
_BIZ_TO_TABLE_TYPE: dict[str, str] = {
    "充币": "pixivcoban",
    "一次卡": "once_card",
    "共享": "shared",
    "打包": "package",
    "openrouter": "openrouter",
    "op": "openrouter",
    "patreon": "patreon",
    "pat": "patreon",
    "约稿": "commission",
    "booth": "booth",
    "pictspace": "pictspace",
    "pict": "pictspace",
    "高级会员": "premium",
    "会员": "premium",
    "mega": "mega",
    "gpt": "chatgpt",
    "plus": "chatgpt",
    "usdt": "usdt",
    "USDT": "usdt",
}


def resolve_bot_table_type(business_name: str = "", *, book: str = "") -> str:
    """解析薪火 BOT 页 table_type；未知时返回空串。"""
    b = (book or "").strip()
    if b == "usdt":
        return "usdt"
    biz = (business_name or "").strip()
    if biz in _BIZ_TO_TABLE_TYPE:
        return _BIZ_TO_TABLE_TYPE[biz]
    low = biz.lower()
    if low in _BIZ_TO_TABLE_TYPE:
        return _BIZ_TO_TABLE_TYPE[low]
    return ""


def query_products_from_xinhuo(*, q: str = "", limit: int = 50) -> dict[str, Any]:
    """
    向薪火拉取商品目录（仅 platform_name + product_name）。
    返回 {"ok": bool, "text": str, "count": int, "results": list}。
    """
    return get_bridge().product_query(q=q, limit=limit)


def entry_event_id(entry: "LedgerEntry") -> str:
    event = getattr(entry, "event", None)
    event_uuid = getattr(event, "event_uuid", "") or ""
    return f"wxbot-{event_uuid or entry.record_no}"[:128]


def build_ledger_payload(entry: "LedgerEntry") -> dict | None:
    """普通记账 → 薪火 LedgerRecord（不进微信BOT数据页）。"""
    if (entry.book or "").strip() not in LEDGER_PAGE_BOOKS:
        return None

    event = getattr(entry, "event", None)
    event_id = entry_event_id(entry)

    employee = getattr(entry, "employee", None)
    chat_room = getattr(entry, "chat_room", None)
    occurred = entry.occurred_at
    occurred_s = occurred.isoformat() if occurred else ""

    return {
        "event_id": event_id,
        "book": "ledger",
        "record_no": entry.record_no or "",
        "direction": entry.direction or "",
        "amount": str(entry.receive_amount or entry.amount or 0),
        "receive_amount": str(entry.receive_amount or 0),
        "currency": entry.currency or "CNY",
        "business_name": entry.business_name or "",
        "amount_to_you": str(entry.amount_to_you or 0),
        "amount_to_me": str(entry.amount_to_me or 0),
        "profit_amount": str(entry.profit_amount or 0),
        "employee_name": getattr(employee, "display_name", "") or "",
        "chat_name": getattr(chat_room, "name", "") or getattr(event, "chat_name", "") or "",
        "raw_text": (getattr(event, "raw_text", "") or "")[:500],
        "remark": entry.remark or "",
        "status": entry.status or "ACTIVE",
        "occurred_at": occurred_s,
    }


def build_bot_record_payload(entry: "LedgerEntry") -> dict | None:
    """员工记单 / USDT → 薪火 WechatBotRecord（微信BOT数据）。"""
    book = (entry.book or "").strip()
    if book not in BOT_PAGE_BOOKS:
        return None

    event = getattr(entry, "event", None)
    event_id = entry_event_id(entry)
    employee = getattr(entry, "employee", None)
    chat_room = getattr(entry, "chat_room", None)
    occurred = entry.occurred_at
    occurred_s = occurred.isoformat() if occurred else ""
    emp_name = getattr(employee, "display_name", "") or ""
    chat_name = getattr(chat_room, "name", "") or getattr(event, "chat_name", "") or ""
    raw_text = (getattr(event, "raw_text", "") or "")[:500]

    if book == "usdt":
        return {
            "event_id": event_id,
            "table_type": "usdt",
            "book": "usdt",
            "record_no": entry.record_no or "",
            "direction": entry.direction or "",
            "quantity": 0,
            "amount": str(entry.amount_to_me or 0),
            "amount_cny": str(entry.amount_to_me or 0),
            "amount_usd": str(entry.receive_amount or entry.amount or 0),
            "currency": "USD",
            "business_name": "USDT",
            "amount_to_you": str(entry.amount_to_you or 0),
            "amount_to_me": str(entry.amount_to_me or 0),
            "profit_amount": str(entry.profit_amount or 0),
            "employee_name": emp_name,
            "chat_name": chat_name,
            "raw_text": raw_text,
            "remark": entry.remark or "",
            "status": entry.status or "ACTIVE",
            "occurred_at": occurred_s,
        }

    # staff_order：显式 table_type，避免线上旧版只认英文 book
    biz = (entry.business_name or "").strip()
    sku = (entry.remark or "").strip()
    display_biz = sku if sku and sku != biz else biz
    table_type = resolve_bot_table_type(biz, book=book)
    try:
        qty = int(Decimal(entry.amount or 0))
    except Exception:
        qty = 0
    direction = (entry.direction or "").upper()
    if direction == "EXPENSE":
        qty = -abs(qty) if qty else 0
    else:
        qty = abs(qty) if qty else 0

    payload = {
        "event_id": event_id,
        "book": biz,
        "record_no": entry.record_no or "",
        "direction": entry.direction or "",
        "quantity": qty,
        "amount": str(abs(qty)),
        "amount_usd": "0",
        "currency": entry.currency or "CNY",
        "business_name": display_biz[:128],
        "amount_to_you": "0",
        "amount_to_me": "0",
        "profit_amount": "0",
        "employee_name": emp_name,
        "chat_name": chat_name,
        "raw_text": raw_text,
        "remark": (entry.remark or "")[:200],
        "status": entry.status or "ACTIVE",
        "occurred_at": occurred_s,
    }
    if table_type:
        payload["table_type"] = table_type
    return payload


def build_settlement_payload(
    room: "ChatRoom",
    employee: "Employee | None" = None,
    *,
    event: "MessageEvent | None" = None,
) -> dict:
    event_uuid = getattr(event, "event_uuid", "") or uuid.uuid4().hex
    occurred = timezone.now()
    return {
        "action": "settle",
        "event_id": f"wxbot-settle-{event_uuid}"[:128],
        "chat_name": room.name,
        "employee_name": getattr(employee, "display_name", "") or "",
        "occurred_at": occurred.isoformat(),
    }


def build_status_payload(entry: "LedgerEntry") -> dict | None:
    book = (entry.book or "").strip()
    if book not in LEDGER_PAGE_BOOKS | BOT_PAGE_BOOKS:
        return None
    return {
        "action": "status",
        "event_id": entry_event_id(entry),
        "status": entry.status or "ACTIVE",
        "occurred_at": timezone.now().isoformat(),
    }


def build_delete_payload(entry: "LedgerEntry") -> dict | None:
    book = (entry.book or "").strip()
    if book not in LEDGER_PAGE_BOOKS | BOT_PAGE_BOOKS:
        return None
    return {
        "action": "delete",
        "event_id": entry_event_id(entry),
        "occurred_at": timezone.now().isoformat(),
    }


def _push_url() -> str:
    """记账推送 URL（兼容旧管理命令）。"""
    return get_bridge().url_for("ledger_push")


def _xinhuo_origin() -> str:
    bridge = get_bridge()
    if bridge.api_base:
        return bridge.api_base
    return ""


def xinhuo_connection_info() -> dict[str, Any]:
    """桌面「薪火对接」页展示用（不回传完整 Key）。"""
    return get_bridge(refresh=True).connection_info()


def test_xinhuo_connection() -> dict[str, Any]:
    """轻量探测：bridge/health 或 ledger pull ping。"""
    return get_bridge(refresh=True).health_check()


def _post_payload(payload: dict, *, log_label: str):
    """兼容旧调用：默认走 ledger_push。返回 BridgeResult。"""
    return get_bridge().ledger_post(payload, log_label=log_label)


def _post_bot(payload: dict, *, log_label: str):
    return get_bridge().bot_record_post(payload, log_label=log_label)


def push_ledger_entry_to_xinhuo(entry_id: int) -> None:
    from apps.integrations.startup_guard import mark_xinhuo_pushed
    from apps.ledger.models import LedgerEntry

    try:
        entry = (
            LedgerEntry.objects.select_related("event", "employee", "chat_room")
            .filter(pk=entry_id)
            .first()
        )
        if not entry:
            return
        # 已成功推过则跳过（闪退重启后禁止把旧账再推一遍）
        if getattr(entry, "xinhuo_pushed_at", None):
            return
        payload = build_ledger_payload(entry)
        if not payload:
            return
        result = _post_payload(payload, log_label=f"ledger entry_id={entry_id}")
        if getattr(result, "ok", False):
            mark_xinhuo_pushed(entry_id)
    except Exception:
        logger.exception("xinhuo ledger push error entry_id=%s", entry_id)


def push_bot_record_to_xinhuo(entry_id: int) -> None:
    from apps.integrations.startup_guard import mark_xinhuo_pushed
    from apps.ledger.models import LedgerEntry

    try:
        entry = (
            LedgerEntry.objects.select_related("event", "employee", "chat_room")
            .filter(pk=entry_id)
            .first()
        )
        if not entry:
            return
        if getattr(entry, "xinhuo_pushed_at", None):
            return
        payload = build_bot_record_payload(entry)
        if not payload:
            return
        result = _post_bot(payload, log_label=f"bot_record entry_id={entry_id}")
        if getattr(result, "ok", False):
            mark_xinhuo_pushed(entry_id)
    except Exception:
        logger.exception("xinhuo bot_record push error entry_id=%s", entry_id)


def push_settlement_to_xinhuo(
    room_id: int,
    employee_id: int | None = None,
    *,
    event_uuid: str = "",
) -> None:
    from apps.chatrooms.models import ChatRoom, Employee
    from apps.ledger.models import MessageEvent

    try:
        room = ChatRoom.objects.filter(pk=room_id).first()
        if not room:
            return
        employee = Employee.objects.filter(pk=employee_id).first() if employee_id else None
        event = MessageEvent.objects.filter(event_uuid=event_uuid).first() if event_uuid else None
        payload = build_settlement_payload(room, employee, event=event)
        _post_payload(payload, log_label=f"settle chat={room.name}")
    except Exception:
        logger.exception("xinhuo settlement push error room_id=%s", room_id)


def push_status_to_xinhuo(entry_id: int) -> None:
    from apps.ledger.models import LedgerEntry

    try:
        entry = (
            LedgerEntry.objects.select_related("event", "employee", "chat_room")
            .filter(pk=entry_id)
            .first()
        )
        if not entry:
            return
        payload = build_status_payload(entry)
        if not payload:
            return
        book = (entry.book or "").strip()
        if book in BOT_PAGE_BOOKS:
            _post_bot(payload, log_label=f"bot_status entry_id={entry_id}")
        else:
            _post_payload(payload, log_label=f"status entry_id={entry_id}")
    except Exception:
        logger.exception("xinhuo status push error entry_id=%s", entry_id)


def schedule_push_ledger_entry(entry: "LedgerEntry") -> None:
    """入账成功后推送：ledger→记账系统；staff_order/usdt→微信BOT数据。

    仅在当次入账成功时调度；进程闪退后不会自动补推未成功推送的旧记录。
    """
    book = (entry.book or "").strip()
    entry_id = int(entry.pk)
    if book in LEDGER_PAGE_BOOKS:
        transaction.on_commit(lambda: push_ledger_entry_to_xinhuo(entry_id))
    elif book in BOT_PAGE_BOOKS:
        transaction.on_commit(lambda: push_bot_record_to_xinhuo(entry_id))


def schedule_push_bot_record(entry: "LedgerEntry") -> None:
    """仅当次入账调度；不在启动时回扫历史。"""
    if (entry.book or "").strip() not in BOT_PAGE_BOOKS:
        return
    entry_id = int(entry.pk)
    transaction.on_commit(lambda: push_bot_record_to_xinhuo(entry_id))


def schedule_push_settlement(
    room: "ChatRoom",
    employee: "Employee | None" = None,
    *,
    event: "MessageEvent | None" = None,
) -> None:
    room_id = int(room.pk)
    employee_id = int(employee.pk) if employee else None
    event_uuid = getattr(event, "event_uuid", "") or ""

    def _job() -> None:
        push_settlement_to_xinhuo(room_id, employee_id, event_uuid=event_uuid)

    transaction.on_commit(_job)


def schedule_push_status(entry: "LedgerEntry") -> None:
    book = (entry.book or "").strip()
    if book not in LEDGER_PAGE_BOOKS | BOT_PAGE_BOOKS:
        return
    entry_id = int(entry.pk)
    transaction.on_commit(lambda: push_status_to_xinhuo(entry_id))


def push_delete_to_xinhuo(entry: "LedgerEntry") -> None:
    """桌面删除前调用：在薪火侧删除对应记录。"""
    try:
        payload = build_delete_payload(entry)
        if not payload:
            return
        book = (entry.book or "").strip()
        label = f"delete event_id={payload.get('event_id')}"
        if book in BOT_PAGE_BOOKS:
            _post_bot(payload, log_label=label)
        else:
            _post_payload(payload, log_label=label)
    except Exception:
        logger.exception("xinhuo delete push error entry_id=%s", getattr(entry, "pk", None))


def _norm_chat(name: str) -> str:
    return "".join((name or "").split()).casefold()


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _normalize_remote_records(raw_rows: list) -> list[dict]:
    out: list[dict] = []
    for item in raw_rows or []:
        if not isinstance(item, dict):
            continue
        # push/pull 格式
        eid = str(item.get("event_id") or item.get("externalId") or "").strip()
        status = str(item.get("status") or "ACTIVE").strip().upper()
        chat = str(item.get("chat_name") or item.get("chatName") or "").strip()
        if not eid and not chat:
            # 无 externalId 的手动补记：用 id 占位，避免误伤本地 bot 记录
            rid = str(item.get("id") or "").strip()
            eid = f"manual-{rid}" if rid else ""
        if not eid:
            continue
        out.append(
            {
                "event_id": eid[:128],
                "status": status or "ACTIVE",
                "chat_name": chat,
                "record_no": str(item.get("record_no") or item.get("recordNo") or ""),
                "business_name": str(
                    item.get("business_name") or item.get("businessName") or item.get("projectName") or ""
                ),
                "receive_amount": str(
                    item.get("receive_amount")
                    if item.get("receive_amount") is not None
                    else item.get("amount")
                    if item.get("amount") is not None
                    else 0
                ),
                "amount_to_you": str(
                    item.get("amount_to_you")
                    if item.get("amount_to_you") is not None
                    else item.get("giveAmount")
                    if item.get("giveAmount") is not None
                    else 0
                ),
                "amount_to_me": str(
                    item.get("amount_to_me")
                    if item.get("amount_to_me") is not None
                    else item.get("takeAmount")
                    if item.get("takeAmount") is not None
                    else 0
                ),
                "profit_amount": str(
                    item.get("profit_amount")
                    if item.get("profit_amount") is not None
                    else item.get("profit")
                    if item.get("profit") is not None
                    else 0
                ),
                "occurred_at": str(item.get("occurred_at") or item.get("recordAt") or ""),
            }
        )
    return out


def _pull_via_push_api(chat_name: str, *, status: str = "") -> dict | None:
    payload: dict[str, str] = {"action": "pull", "chat_name": chat_name or "*"}
    if status:
        payload["status"] = status
    r = get_bridge().ledger_post(payload, log_label=f"pull chat={chat_name}")
    if not r.ok or not isinstance(r.data, dict) or not r.data.get("ok"):
        return None
    records = _normalize_remote_records(r.data.get("records") or [])
    return {"ok": True, "source": "pull", "chat_name": chat_name or "*", "records": records}


def _pull_via_jwt() -> dict | None:
    """旧服务器无 pull 时：用 CEO 账号拉 /api/ledger/ledger/ 全表。"""
    user = (getattr(settings, "XINHUO_API_USERNAME", "") or "").strip()
    password = (getattr(settings, "XINHUO_API_PASSWORD", "") or "").strip()
    origin = _xinhuo_origin()
    if not user or not password or not origin:
        return None
    timeout = float(getattr(settings, "XINHUO_BOT_PUSH_TIMEOUT", 8) or 8)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            tok = client.post(
                f"{origin}/api/auth/token/",
                json={"username": user, "password": password},
            )
            if tok.status_code >= 400:
                logger.warning("xinhuo jwt login failed status=%s body=%s", tok.status_code, tok.text[:200])
                return None
            access = (tok.json() or {}).get("access") or ""
            if not access:
                return None
            resp = client.get(
                f"{origin}/api/ledger/ledger/",
                headers={"Authorization": f"Bearer {access}"},
            )
            if resp.status_code >= 400:
                logger.warning("xinhuo ledger list failed status=%s body=%s", resp.status_code, resp.text[:200])
                return None
            rows = resp.json()
            if not isinstance(rows, list):
                return None
            records = _normalize_remote_records(rows)
            return {"ok": True, "source": "jwt", "records": records}
    except Exception:
        logger.exception("xinhuo jwt pull error origin=%s", origin)
        return None


def pull_bot_records_from_xinhuo(
    *,
    employee_name: str = "",
    business_name: str = "",
    status: str = "ACTIVE",
) -> dict | None:
    """
    从薪火「微信BOT数据」拉取记录（action=pull）。
    查记单前调用，以薪火页条数为准。
    """
    payload: dict[str, str] = {"action": "pull"}
    st = (status or "").strip()
    if st:
        payload["status"] = st
    emp = (employee_name or "").strip()
    if emp:
        payload["employee_name"] = emp
    biz = (business_name or "").strip()
    if biz:
        payload["business_name"] = biz
        tt = resolve_bot_table_type(biz)
        if tt:
            payload["table_type"] = tt
    r = get_bridge().bot_record_post(
        payload, log_label=f"bot_pull emp={emp or '*'} biz={biz or '*'}"
    )
    if not r.ok or not isinstance(r.data, dict) or not r.data.get("ok"):
        logger.warning(
            "xinhuo bot pull failed status=%s err=%s body=%s",
            r.status_code,
            r.error,
            (r.text or "")[:200],
        )
        return None
    records = r.data.get("records") or []
    if not isinstance(records, list):
        records = []
    return {
        "ok": True,
        "count": int(r.data.get("count") or len(records)),
        "records": records,
    }


def pull_ledger_from_xinhuo(chat_name: str = "", *, status: str = "") -> dict | None:
    """
    从薪火拉取记账。
    chat_name='*' 或空：全量；status='ACTIVE'：仅未清账。
    """
    chat_name = (chat_name or "").strip() or "*"
    data = _pull_via_push_api(chat_name, status=status)
    if data is not None:
        return data
    data = _pull_via_jwt()
    if data is None:
        return None
    if status:
        want = status.strip().upper()
        data = {
            **data,
            "records": [
                r
                for r in (data.get("records") or [])
                if str(r.get("status") or "").upper() == want
            ],
        }
    return data


def remote_active_for_room(room: "ChatRoom", remote: dict) -> list[dict]:
    """选出该群应对齐的未清账远程记录。"""
    from apps.ledger.models import LedgerEntry

    chat_want = _norm_chat(getattr(room, "name", "") or "")
    room_eids = {
        entry_event_id(e)
        for e in LedgerEntry.objects.filter(chat_room=room, book="ledger").select_related("event")
    }
    rows = []
    for item in remote.get("records") or []:
        st = str(item.get("status") or "").upper()
        if st != "ACTIVE":
            continue
        eid = str(item.get("event_id") or "")
        chat = _norm_chat(str(item.get("chat_name") or ""))
        if eid in room_eids or (chat and chat == chat_want):
            rows.append(item)
    return rows


def sum_remote_active(rows: list[dict]) -> dict[str, Decimal]:
    sum_you = sum_me = sum_profit = sum_receive = Decimal("0")
    times: list[str] = []
    for r in rows:
        sum_you += _dec(r.get("amount_to_you"))
        sum_me += _dec(r.get("amount_to_me"))
        sum_profit += _dec(r.get("profit_amount"))
        sum_receive += _dec(r.get("receive_amount"))
        if r.get("occurred_at"):
            times.append(str(r["occurred_at"]))
    times.sort()
    return {
        "sum_you": sum_you,
        "sum_me": sum_me,
        "sum_profit": sum_profit,
        "sum_receive": sum_receive,
        "count": Decimal(len(rows)),
        "first_at": times[0] if times else "",
        "last_at": times[-1] if times else "",
    }


def sync_room_ledger_from_xinhuo(room: "ChatRoom") -> tuple[int, dict | None]:
    """
    以薪火为准同步本地状态，并返回 (变更数, remote_payload)。
    remote_payload 可供查询直接汇总，避免再读本地脏数据。
    """
    from datetime import timedelta

    from apps.ledger.models import LedgerEntry

    chat_name = getattr(room, "name", "") or ""
    remote = pull_ledger_from_xinhuo(chat_name)
    if remote is None:
        return -1, None

    remote_map: dict[str, str] = {}
    for item in remote.get("records") or []:
        eid = str(item.get("event_id") or "").strip()
        st = str(item.get("status") or "").strip().upper()
        if eid and st:
            remote_map[eid] = st

    allowed = {c.value for c in LedgerEntry.Status}
    now = timezone.now()
    cutoff = now - timedelta(seconds=5)
    changed = 0

    qs = (
        LedgerEntry.objects.filter(chat_room=room, book="ledger")
        .select_related("event")
        .exclude(status=LedgerEntry.Status.REVERSED)
    )
    for entry in qs.iterator():
        eid = entry_event_id(entry)
        if eid in remote_map:
            want = remote_map[eid]
            if want not in allowed:
                continue
            if entry.status != want:
                entry.status = want
                if want == LedgerEntry.Status.REVERSED:
                    entry.reverse_reason = "synced from xinhuo (reversed)"
                    entry.reversed_at = now
                elif want == LedgerEntry.Status.PROCESSED:
                    entry.reverse_reason = entry.reverse_reason or "synced from xinhuo (processed)"
                    entry.reversed_at = entry.reversed_at or now
                entry.save(update_fields=["status", "reverse_reason", "reversed_at", "updated_at"])
                changed += 1
        else:
            # 薪火已不存在（硬删）或从未推送成功：创建超过 5 秒的 ACTIVE 视为已删
            created = getattr(entry, "created_at", None) or now
            if entry.status == LedgerEntry.Status.ACTIVE and created <= cutoff:
                entry.status = LedgerEntry.Status.REVERSED
                entry.reverse_reason = "synced from xinhuo (missing/deleted)"
                entry.reversed_at = now
                entry.save(update_fields=["status", "reverse_reason", "reversed_at", "updated_at"])
                changed += 1

    if changed:
        logger.info(
            "xinhuo sync chat=%s changed=%s remote=%s source=%s",
            chat_name,
            changed,
            len(remote_map),
            remote.get("source"),
        )
    return changed, remote
