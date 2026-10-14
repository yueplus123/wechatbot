"""网关 HMAC 与消息处理服务（文档 9 / 事务规则）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.chatrooms.models import ChatRoom, Employee, SenderBinding
from apps.ledger.books import is_book_command_text, resolve_ledger_book
from apps.ledger.models import LedgerEntry, MessageEvent, ReplyJob
from apps.rules.models import Category
from apps.rules.parser import ParseStatus, parse_command
from gateway.dedupe import make_fingerprint

logger = logging.getLogger(__name__)


def verify_gateway_signature(
    *,
    gateway_id: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
    max_skew_seconds: int = 300,
) -> bool:
    secret = (settings.GATEWAY_HMAC_SECRET or "").encode("utf-8")
    if not secret:
        # 开发态未配置密钥时仅允许本机调试；生产必须配置
        return settings.DEBUG
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(timezone.now().timestamp())
    if abs(now - ts) > max_skew_seconds:
        return False
    payload = f"{gateway_id}.{timestamp}.{nonce}.".encode("utf-8") + body
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").lower())


def build_category_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cat in Category.objects.filter(enabled=True):
        mapping[cat.name] = cat.name
        for alias in cat.alias_list():
            mapping[alias] = cat.name
    return mapping


def next_record_no(when: datetime | None = None) -> str:
    """记录号：本地日期时间，精确到秒；同一秒多笔则追加 -2、-3…"""
    when = when or timezone.localtime()
    base = when.strftime("%Y-%m-%d %H:%M:%S")
    if not LedgerEntry.objects.filter(record_no=base).exists():
        return base
    for i in range(2, 1000):
        cand = f"{base}-{i}"
        if not LedgerEntry.objects.filter(record_no=cand).exists():
            return cand
    return f"{base}-{uuid.uuid4().hex[:6]}"


def format_success_reply(entry: LedgerEntry) -> str:
    from apps.ledger.ledger_ops import ledger_entry_success_reply, usdt_entry_success_reply

    who = ""
    try:
        who = (entry.employee.display_name or "").strip()
    except Exception:
        who = ""
    if (entry.book or "") == "usdt" or (entry.business_name or "").strip().upper() == "USDT":
        return usdt_entry_success_reply(entry, operator_name=who)
    return ledger_entry_success_reply(entry, operator_name=who)


def ensure_fallback_category(direction: str) -> Category:
    cat, _ = Category.objects.get_or_create(
        type=direction,
        name="其他",
        defaults={"enabled": True, "sort_order": 999},
    )
    if not cat.enabled:
        cat.enabled = True
        cat.save(update_fields=["enabled"])
    return cat


def _collapse_same_employee(hits: list) -> list:
    """
    同一员工可绑定多个群昵称/企微号（如「叽里呱啦」与「叽里呱啦@企业」）。
    命中多条绑定但 employee_id 唯一时，视为同一人，取第一条即可。
    """
    if not hits:
        return []
    emp_ids = {b.employee_id for b in hits if b.employee_id}
    if len(emp_ids) == 1:
        return [hits[0]]
    return list(hits)


@transaction.atomic
def process_gateway_message(payload: dict, gateway_id: str) -> dict:
    """
    返回: status = accepted|ignored|duplicate|rejected
    事务提交后才创建回复任务；绝不先回复成功。
    """
    event_uuid = payload.get("event_uuid") or str(uuid.uuid4())
    from gateway.chat_names import normalize_chat_name, nickname_match_key

    chat_name = normalize_chat_name(payload.get("chat_name") or "")
    sender = (payload.get("sender_display_name") or "").strip()
    raw_text = payload.get("raw_text") or ""
    message_type = (payload.get("message_type") or "text").lower()
    client_observed_at = parse_datetime(payload.get("client_observed_at") or "")
    if client_observed_at is None:
        client_observed_at = timezone.now()
    elif timezone.is_naive(client_observed_at):
        client_observed_at = timezone.make_aware(
            client_observed_at, timezone.get_current_timezone()
        )

    # 清洗脏发送人（机器人回执字段曾被当成昵称）
    from gateway.chat_names import is_bot_receipt_text, is_plausible_group_nickname

    if is_bot_receipt_text(raw_text.strip()):
        return {"status": "ignored", "reason": "bot_receipt"}
    if sender and (
        is_bot_receipt_text(sender) or not is_plausible_group_nickname(sender)
    ):
        sender = "未知发送人"

    if message_type != "text":
        return {"status": "ignored", "reason": "non_text"}

    room = ChatRoom.objects.filter(name=chat_name, enabled=True).first()
    if room is None:
        # 兼容库里存了带人数后缀的旧名
        for candidate in ChatRoom.objects.filter(enabled=True):
            if normalize_chat_name(candidate.name) == chat_name:
                room = candidate
                break
    if room is None:
        return {
            "status": "ignored",
            "reason": "chat_not_whitelisted",
            "chat_name": chat_name,
        }

    normalized_preview = raw_text.strip()
    # 入账指令：指纹含完整发送人；有 UI 时间则按时间区分多样，否则 5 秒桶防抖动
    if is_book_command_text(normalized_preview, chat_name):
        from gateway.chat_names import normalize_group_nickname as _nn

        sender_fp = _nn(sender) or (sender or "")
        if sender_fp in {"", "未知发送人"}:
            sender_fp = ""
        ui_ts = str(payload.get("ui_timestamp") or "").strip()
        time_part = ui_ts or str(int(timezone.now().timestamp() // 5))
        fingerprint = make_fingerprint(
            gateway_id, chat_name, sender_fp, normalized_preview, time_part, ""
        )
    else:
        fingerprint = make_fingerprint(
            gateway_id,
            chat_name,
            sender,
            normalized_preview,
            payload.get("ui_timestamp") or "",
            payload.get("local_sequence") or "",
        )

    # 唯一约束去重
    existing = MessageEvent.objects.filter(fingerprint=fingerprint).first()
    if existing:
        # 上次被拒绝（如 E004）：释放指纹，允许立即重试，避免「连发无回复」
        if existing.parse_status == MessageEvent.ParseStatus.REJECTED:
            existing.fingerprint = f"{existing.fingerprint}:rej:{existing.id}"
            existing.save(update_fields=["fingerprint"])
        else:
            entry = getattr(existing, "ledger_entry", None)
            from apps.ledger.ledger_ops import wrap_reply

            tip = wrap_reply(
                f"ℹ️ 该消息已处理，记录号 #{entry.record_no}."
                if entry
                else "ℹ️ 该消息已处理。"
            )
            # 给用户可见反馈，避免误以为机器人没反应（同文短时重发）
            has_recent = ReplyJob.objects.filter(
                event=existing, body=tip
            ).exclude(status=ReplyJob.Status.CANCELLED).exists()
            if not has_recent:
                try:
                    ReplyJob.objects.create(
                        event=existing,
                        chat_name=chat_name,
                        body=tip,
                        status=ReplyJob.Status.PENDING,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {
                "status": "duplicate",
                "event_uuid": existing.event_uuid,
                "record_no": entry.record_no if entry else "",
                "message": tip,
            }

    event = MessageEvent(
        event_uuid=event_uuid,
        fingerprint=fingerprint,
        chat_room=room,
        chat_name=chat_name,
        sender_display_name=sender,
        message_type=message_type,
        raw_text=raw_text,
        received_at=timezone.now(),
        client_observed_at=client_observed_at,
        adapter=payload.get("adapter") or "",
        wechat_version=payload.get("wechat_version") or "",
        rule_version=settings.RULE_VERSION,
    )

    from apps.ledger.books import load_staff_biz_for_chat

    staff_biz_map = load_staff_biz_for_chat(chat_name)
    parsed = parse_command(
        raw_text,
        trigger_prefix=settings.TRIGGER_PREFIX,
        default_currency=settings.DEFAULT_CURRENCY,
        rule_version=settings.RULE_VERSION,
        staff_biz_map=staff_biz_map,
    )
    event.normalized_text = parsed.normalized_text

    if parsed.status == ParseStatus.IGNORED:
        return {"status": "ignored", "reason": "no_trigger"}

    if parsed.status == ParseStatus.ERROR:
        from apps.ledger.ledger_ops import ledger_error_reply

        event.parse_status = MessageEvent.ParseStatus.REJECTED
        event.error_code = parsed.error_code
        event.error_message = parsed.error_message
        try:
            event.save()
        except IntegrityError:
            return {"status": "duplicate", "message": "ℹ️ 该消息已处理。"}
        _enqueue_reply(
            event, chat_name, ledger_error_reply(f"❌ {parsed.error_message}")
        )
        return {
            "status": "rejected",
            "error_code": parsed.error_code,
            "event_uuid": event.event_uuid,
        }

    # 按群勾选拦截：记账 / USDT / 员工记单互不影响
    from apps.ledger.books import book_enabled_for_chat
    from apps.ledger.ledger_ops import ledger_error_reply

    need_book = (parsed.book or "").strip()
    if parsed.command == "usdt_settlement_clear":
        need_book = "usdt"
    elif parsed.command == "usdt_query":
        need_book = "usdt"
    elif parsed.command in {
        "ledger_query",
        "ledger_clear",
        "ledger_query_month",
        "ledger_query_last_month",
        "ledger_query_week",
        "ledger_help",
    }:
        need_book = "ledger"
    if need_book in {"ledger", "usdt", "staff_order"}:
        if not book_enabled_for_chat(chat_name, need_book):
            label = {"ledger": "记账", "usdt": "USDT", "staff_order": "员工记单"}.get(
                need_book, need_book
            )
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E003"
            event.error_message = f"本群未启用{label}"
            try:
                event.save()
            except IntegrityError:
                return {"status": "duplicate", "message": "ℹ️ 该消息已处理。"}
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 本群未启用「{label}」。请在听群→群设置中勾选后保存。"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E003",
                "event_uuid": event.event_uuid,
            }

    employee = None
    # 仅按群昵称绑定识别提交人（不再使用指令末尾员工编号）
    from gateway.chat_names import is_email_like_nickname, normalize_group_nickname

    sender_norm = normalize_group_nickname(sender) or (sender or "").strip()
    sender_key = nickname_match_key(sender)
    all_bindings = list(
        SenderBinding.objects.filter(chat_room=room, active=True).select_related(
            "employee"
        )
    )
    bindings: list = []
    if sender not in {"", "未知发送人"}:
        # 1) 完整昵称精确匹配
        exact = [
            b
            for b in all_bindings
            if b.sender_display_name == sender
            or (normalize_group_nickname(b.sender_display_name) or "") == sender_norm
        ]
        bindings = _collapse_same_employee(exact) if exact else []
        if not bindings:
            # 2) 去 @ 后缀后的同名：若对应同一员工则通过（双号同一人）
            key_hits = [
                b
                for b in all_bindings
                if sender_key
                and nickname_match_key(b.sender_display_name) == sender_key
            ]
            collapsed = _collapse_same_employee(key_hits)
            if len(collapsed) == 1:
                bindings = collapsed
            elif len(key_hits) > 1:
                # 不同员工：再按是否带 @ 拆开；拆不开时若仅差 @企业后缀则仍可能同人
                sender_has_at = "@" in sender_norm and not is_email_like_nickname(
                    sender_norm
                )

                def _bind_has_at(b: SenderBinding) -> bool:
                    n = normalize_group_nickname(b.sender_display_name) or (
                        b.sender_display_name or ""
                    )
                    return "@" in n and not is_email_like_nickname(n)

                if sender_has_at:
                    narrowed = [b for b in key_hits if _bind_has_at(b)]
                else:
                    narrowed = [b for b in key_hits if not _bind_has_at(b)]
                bindings = _collapse_same_employee(narrowed)
                if len(bindings) != 1:
                    # 兜底：key 相同的绑定若最终只对应一个员工（数据已合并）再试一次全量
                    bindings = _collapse_same_employee(key_hits)
                if len(bindings) != 1:
                    bindings = []
                    logger.info(
                        "sender ambiguous room=%s raw=%r key_hits=%s emps=%s",
                        chat_name,
                        sender,
                        [b.sender_display_name for b in key_hits],
                        sorted({b.employee_id for b in key_hits}),
                    )

    # 读不到昵称：若本群启用绑定实际都是同一人，直接记到该员工（双号同人场景）
    if not bindings and sender in {"", "未知发送人"}:
        same_person = _collapse_same_employee(all_bindings)
        if len(same_person) == 1:
            bindings = same_person
            logger.info(
                "sender same-person fallback room=%s raw=%r -> emp=%s nick=%s",
                chat_name,
                sender,
                bindings[0].employee_id,
                bindings[0].sender_display_name,
            )
        elif len(all_bindings) == 1:
            bindings = all_bindings
            logger.info(
                "sender fallback room=%s raw=%r -> %s",
                chat_name,
                sender,
                bindings[0].sender_display_name,
            )
        else:
            # 本人气泡 / 查询类指令：用本机别名兜底（多绑定群 UIA 常丢昵称）
            use_alias = bool(payload.get("likely_self")) or parsed.command in {
                "staff_order_query",
                "staff_order_query_employee",
                "ledger_query",
                "ledger_query_month",
                "ledger_query_last_month",
                "ledger_query_week",
                "usdt_query",
            }
            if use_alias:
                try:
                    from gateway.sender_resolve import load_my_alias

                    alias = load_my_alias(chat_name)
                except Exception:  # noqa: BLE001
                    alias = ""
                if not alias and len(all_bindings) == 1:
                    bindings = all_bindings
                elif alias:
                    alias_norm = normalize_group_nickname(alias) or alias
                    hits = [
                        b
                        for b in all_bindings
                        if b.sender_display_name == alias
                        or (normalize_group_nickname(b.sender_display_name) or "")
                        == alias_norm
                    ]
                    if not hits:
                        alias_key = nickname_match_key(alias)
                        hits = [
                            b
                            for b in all_bindings
                            if alias_key
                            and nickname_match_key(b.sender_display_name) == alias_key
                        ]
                    hits = _collapse_same_employee(hits)
                    if len(hits) == 1:
                        bindings = hits
                        logger.info(
                            "my_alias fallback room=%s alias=%r -> %s",
                            chat_name,
                            alias,
                            bindings[0].sender_display_name,
                        )
    elif not bindings:
        same_person = _collapse_same_employee(all_bindings)
        if len(same_person) == 1:
            bindings = same_person
            logger.info(
                "sender same-person room=%s raw=%r -> emp=%s",
                chat_name,
                sender,
                bindings[0].employee_id,
            )
        elif len(all_bindings) == 1:
            bindings = all_bindings
            logger.info(
                "sender fallback room=%s raw=%r -> %s",
                chat_name,
                sender,
                bindings[0].sender_display_name,
                )

    # 查商品：不依赖群昵称绑定，可直接回复
    if parsed.command == "product_query":
        from apps.integrations.xinhuo_push import query_products_from_xinhuo
        from apps.ledger.ledger_ops import wrap_reply

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        try:
            event.save()
        except IntegrityError:
            return {"status": "duplicate", "message": "ℹ️ 该消息已处理。"}
        result = query_products_from_xinhuo(q=parsed.remark or "")
        body = wrap_reply(result.get("text") or "未找到相关商品。")
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "product_query",
            "count": result.get("count") or 0,
        }

    if len(bindings) != 1 or not bindings[0].employee.enabled:
        from apps.ledger.ledger_ops import ledger_error_reply

        event.parse_status = MessageEvent.ParseStatus.REJECTED
        event.error_code = "E004"
        event.error_message = "无法确认提交人"
        try:
            event.save()
        except IntegrityError:
            return {"status": "duplicate", "message": "ℹ️ 该消息已处理。"}
        nicks = "、".join(
            b.sender_display_name for b in all_bindings[:5] if b.sender_display_name
        )
        if sender in {"", "未知发送人"}:
            hint = (
                "未能从聊天窗识别群昵称。"
                + (f"当前已绑定：{nicks}。" if nicks else "请先在桌面控制台绑定群昵称。")
            )
        elif nicks:
            hint = f"识别到「{sender}」，但未唯一匹配绑定。当前已绑定：{nicks}。"
        else:
            hint = "请先在桌面控制台「听群」里绑定群昵称。"
        _enqueue_reply(
            event,
            chat_name,
            ledger_error_reply(f"❌ 无法确认提交人。{hint}"),
        )
        return {
            "status": "rejected",
            "error_code": "E004",
            "event_uuid": event.event_uuid,
        }

    employee = bindings[0].employee
    from apps.chatrooms.models import is_admin_in_room

    room_admin = is_admin_in_room(room, employee)
    # 本人气泡成功落到绑定昵称时，记住本机别名（下次 UIA 丢昵称可兜底）
    if payload.get("likely_self") and bindings[0].sender_display_name:
        try:
            from gateway.sender_resolve import remember_my_alias

            remember_my_alias(chat_name, bindings[0].sender_display_name)
        except Exception:  # noqa: BLE001
            pass

    if parsed.command == "ledger_help":
        from apps.ledger.ledger_ops import LEDGER_HELP_TEXT

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        _enqueue_reply(event, chat_name, LEDGER_HELP_TEXT)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "ledger_help",
        }

    def _require_room_admin(cmd_label: str):
        if room_admin:
            return None
        from apps.ledger.ledger_ops import ledger_error_reply

        event.parse_status = MessageEvent.ParseStatus.REJECTED
        event.error_code = "E003"
        event.error_message = f"仅本群管理员可{cmd_label}"
        event.save()
        _enqueue_reply(
            event,
            chat_name,
            ledger_error_reply(f"❌ 仅本群管理员可{cmd_label}。"),
        )
        return {
            "status": "rejected",
            "error_code": "E003",
            "event_uuid": event.event_uuid,
        }

    if parsed.command == "ledger_query":
        denied = _require_room_admin("查询记账")
        if denied:
            return denied
        from apps.ledger.ledger_ops import format_ledger_query

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_ledger_query(room, employee)
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "ledger_query",
        }

    if parsed.command == "ledger_query_month":
        denied = _require_room_admin("查询本月")
        if denied:
            return denied
        from apps.ledger.ledger_ops import format_ledger_period_query

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_ledger_period_query(room, "month")
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "ledger_query_month",
        }

    if parsed.command == "ledger_query_last_month":
        denied = _require_room_admin("查询上月")
        if denied:
            return denied
        from apps.ledger.ledger_ops import format_ledger_period_query

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_ledger_period_query(room, "last_month")
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "ledger_query_last_month",
        }

    if parsed.command == "ledger_query_week":
        denied = _require_room_admin("查询本周")
        if denied:
            return denied
        from apps.ledger.ledger_ops import format_ledger_period_query

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_ledger_period_query(room, "week")
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "ledger_query_week",
        }

    if parsed.command in {"ledger_clear", "settlement_clear"}:
        denied = _require_room_admin("清账")
        if denied:
            return denied
        from apps.ledger.ledger_ops import clear_settlements

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = clear_settlements(room, employee, event=event)
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "settlement_clear",
        }

    if parsed.command == "usdt_query":
        denied = _require_room_admin("查询usdt")
        if denied:
            return denied
        from apps.ledger.ledger_ops import format_usdt_query

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_usdt_query(room)
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "usdt_query",
        }

    if parsed.command == "usdt_settlement_clear":
        denied = _require_room_admin("已转账")
        if denied:
            return denied
        from apps.ledger.ledger_ops import clear_usdt_settlements

        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = clear_usdt_settlements(room, employee, event=event)
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "usdt_settlement_clear",
        }

    if parsed.command == "staff_order_query_employee":
        from apps.ledger.ledger_ops import ledger_error_reply
        from apps.ledger.staff_order_ops import (
            find_room_employee_by_name,
            format_staff_order_query_by_employee,
        )

        # 本人可查本人；员工不可查别人；管理员可查任何人
        target_name = (parsed.remark or "").strip()
        target = find_room_employee_by_name(room, target_name)
        if target is None:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E004"
            event.error_message = f"未找到员工：{target_name}"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 未找到员工「{target_name}」。请核对管理人员里的统一备注名。"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E004",
                "event_uuid": event.event_uuid,
            }
        if not room_admin and employee.id != target.id:
            # 跨群同备注名视为同一人（历史可能重复建档）
            self_dn = (employee.display_name or "").strip()
            target_dn = (target.display_name or "").strip()
            same_person = bool(self_dn) and self_dn == target_dn
            if not same_person:
                event.parse_status = MessageEvent.ParseStatus.REJECTED
                event.error_code = "E003"
                event.error_message = "员工仅可查询本人记单"
                event.save()
                self_name = self_dn or "本人"
                _enqueue_reply(
                    event,
                    chat_name,
                    ledger_error_reply(
                        f"❌ 员工仅可查询本人记单。请发：查询-{self_name}"
                    ),
                )
                return {
                    "status": "rejected",
                    "error_code": "E003",
                    "event_uuid": event.event_uuid,
                }
        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_staff_order_query_by_employee(room, target)
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "staff_order_query_employee",
        }

    if parsed.command == "staff_order_query":
        from apps.ledger.ledger_ops import ledger_error_reply
        from apps.ledger.staff_order_ops import (
            find_room_employee_by_name,
            format_staff_order_query,
        )

        # 本人可查本人；员工不可查别人；管理员可查任何人
        target_name = (parsed.remark or "").strip()
        biz = (parsed.business_name or "").strip()
        if not target_name:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E001"
            event.error_message = "查询需指定员工"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 查询需指定员工。例：查询{biz or '高级会员'}-张越；"
                    "查全部业务请发：查询-张越"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E001",
                "event_uuid": event.event_uuid,
            }
        target = find_room_employee_by_name(room, target_name)
        if target is None:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E004"
            event.error_message = f"未找到员工：{target_name}"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 未找到员工「{target_name}」。请核对管理人员里的统一备注名。"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E004",
                "event_uuid": event.event_uuid,
            }
        if not room_admin and employee.id != target.id:
            self_dn = (employee.display_name or "").strip()
            target_dn = (target.display_name or "").strip()
            same_person = bool(self_dn) and self_dn == target_dn
            if not same_person:
                event.parse_status = MessageEvent.ParseStatus.REJECTED
                event.error_code = "E003"
                event.error_message = "员工仅可查询本人记单"
                event.save()
                self_name = self_dn or "本人"
                _enqueue_reply(
                    event,
                    chat_name,
                    ledger_error_reply(
                        f"❌ 员工仅可查询本人记单。请发：查询{biz or '高级会员'}-{self_name}"
                    ),
                )
                return {
                    "status": "rejected",
                    "error_code": "E003",
                    "event_uuid": event.event_uuid,
                }
        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = format_staff_order_query(
            room, employee, biz, target_employee=target, is_admin=room_admin
        )
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "staff_order_query",
        }

    if parsed.command == "staff_order_clear":
        from apps.ledger.ledger_ops import ledger_error_reply
        from apps.ledger.staff_order_ops import (
            clear_staff_order_business,
            find_room_employee_by_name,
        )

        # 仅管理员可清空（含清空本人）；员工一律不可清空
        if not room_admin:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E003"
            event.error_message = "仅管理员可清空记单"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    "❌ 仅管理员可清空记单（员工不可清空，含本人）。"
                    "例：清空高级会员-张越"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E003",
                "event_uuid": event.event_uuid,
            }
        target_name = (parsed.remark or "").strip()
        if not target_name:
            biz = (parsed.business_name or "").strip() or "业务名"
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E001"
            event.error_message = "清空需指定员工"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 清空需指定员工，避免清掉全员。例：清空{biz}-张越"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E001",
                "event_uuid": event.event_uuid,
            }
        target = find_room_employee_by_name(room, target_name)
        if target is None:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E004"
            event.error_message = f"未找到员工：{target_name}"
            event.save()
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply(
                    f"❌ 未找到员工「{target_name}」。请核对管理人员里的统一备注名。"
                ),
            )
            return {
                "status": "rejected",
                "error_code": "E004",
                "event_uuid": event.event_uuid,
            }
        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        body = clear_staff_order_business(
            room,
            employee,
            parsed.business_name or "",
            target_employee=target,
        )
        _enqueue_reply(event, chat_name, body)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "command": "staff_order_clear",
        }

    if parsed.command == "staff_order_add":
        from apps.ledger.ledger_ops import ledger_error_reply
        from apps.ledger.staff_order_catalog import is_multi_sku
        from apps.ledger.staff_order_ops import (
            net_for_employee,
            net_for_employee_sku,
            staff_order_success_reply,
        )

        assert parsed.direction is not None and parsed.amount is not None
        biz = (parsed.business_name or "").strip()
        if not biz:
            event.parse_status = MessageEvent.ParseStatus.REJECTED
            event.error_code = "E001"
            event.error_message = "缺少业务名"
            event.save()
            _enqueue_reply(
                event, chat_name, ledger_error_reply("❌ 缺少业务名。")
            )
            return {
                "status": "rejected",
                "error_code": "E001",
                "event_uuid": event.event_uuid,
            }

        cat = ensure_fallback_category(parsed.direction.value)
        event.parse_status = MessageEvent.ParseStatus.ACCEPTED
        event.save()
        record_no = next_record_no()
        entry = None
        for _ in range(5):
            try:
                entry = LedgerEntry.objects.create(
                    record_no=record_no,
                    event=event,
                    book=LedgerEntry.Book.STAFF_ORDER,
                    direction=parsed.direction.value,
                    amount=Decimal(parsed.amount),
                    currency=parsed.currency or "CNY",
                    category=cat,
                    employee=employee,
                    chat_room=room,
                    remark=(parsed.remark or biz)[:200],
                    business_name=biz[:128],
                    receive_amount=Decimal("0"),
                    amount_to_you=Decimal("0"),
                    amount_to_me=Decimal("0"),
                    profit_amount=Decimal("0"),
                    occurred_at=client_observed_at,
                    status=LedgerEntry.Status.ACTIVE,
                )
                break
            except IntegrityError:
                record_no = next_record_no()
        if entry is None:
            event.parse_status = MessageEvent.ParseStatus.ERROR
            event.error_code = "E500"
            event.error_message = "record_no_conflict"
            event.save(update_fields=["parse_status", "error_code", "error_message"])
            _enqueue_reply(
                event,
                chat_name,
                ledger_error_reply("⚠️ 本次记单未成功，请稍后重试。"),
            )
            return {"status": "rejected", "error_code": "E500"}

        try:
            from apps.integrations.xinhuo_push import schedule_push_bot_record

            schedule_push_bot_record(entry)
        except Exception:
            logger.exception("schedule xinhuo bot push failed record_no=%s", entry.record_no)

        delta = Decimal(parsed.amount)
        if parsed.direction.value == LedgerEntry.Direction.EXPENSE:
            delta = -delta
        who = (employee.display_name or "").strip()
        # 展示：多 SKU 显示「业务名-规格」（如 高级会员-一个月）；单 SKU 显示规范业务名
        sku_remark = (parsed.remark or "").strip()
        if is_multi_sku(biz):
            if sku_remark and sku_remark != biz:
                display_biz = f"{biz}-{sku_remark}"
            else:
                display_biz = biz
            self_net = net_for_employee_sku(room, biz, employee, sku_remark)
        else:
            display_biz = biz
            self_net = net_for_employee(room, biz, employee)
        reply = staff_order_success_reply(
            business_name=display_biz,
            delta=delta,
            operator_name=who,
            self_net=self_net,
            when=client_observed_at,
            currency=parsed.currency or "",
        )
        _enqueue_reply(event, chat_name, reply)
        return {
            "status": "accepted",
            "event_uuid": event.event_uuid,
            "record_no": entry.record_no,
            "reply_preview": reply,
            "command": "staff_order_add",
        }

    # ledger 入账
    assert parsed.direction is not None and parsed.amount is not None
    book = resolve_ledger_book(
        raw_text,
        parsed.currency,
        preferred=getattr(parsed, "book", "") or "",
        chat_name=chat_name,
    )
    cat = ensure_fallback_category(parsed.direction.value)

    event.parse_status = MessageEvent.ParseStatus.ACCEPTED
    event.save()

    record_no = next_record_no()
    for _ in range(5):
        try:
            entry = LedgerEntry.objects.create(
                record_no=record_no,
                event=event,
                book=book,
                direction=parsed.direction.value,
                amount=Decimal(parsed.amount),
                currency=parsed.currency,
                category=cat,
                employee=employee,
                chat_room=room,
                remark=(parsed.remark or getattr(parsed, "business_name", None) or "")[
                    :200
                ],
                business_name=(getattr(parsed, "business_name", None) or "")[:128],
                receive_amount=getattr(parsed, "receive_amount", None) or Decimal("0"),
                amount_to_you=getattr(parsed, "amount_to_you", None) or Decimal("0"),
                amount_to_me=getattr(parsed, "amount_to_me", None) or Decimal("0"),
                profit_amount=getattr(parsed, "profit_amount", None) or Decimal("0"),
                occurred_at=client_observed_at,
                status=LedgerEntry.Status.ACTIVE,
            )
            break
        except IntegrityError:
            record_no = next_record_no()
    else:
        from apps.ledger.ledger_ops import ledger_error_reply

        event.parse_status = MessageEvent.ParseStatus.ERROR
        event.error_code = "E500"
        event.error_message = "record_no_conflict"
        event.save(update_fields=["parse_status", "error_code", "error_message"])
        _enqueue_reply(
            event,
            chat_name,
            ledger_error_reply("⚠️ 本次记录未成功，请稍后重试或联系管理员。"),
        )
        return {"status": "rejected", "error_code": "E500"}

    # 可选：薪火推送（按业务名匹配 GPT 等）
    try:
        from apps.integrations.xinhuo_push import schedule_push_ledger_entry

        schedule_push_ledger_entry(entry)
    except Exception:
        logger.exception("schedule xinhuo push failed record_no=%s", entry.record_no)

    reply = format_success_reply(entry)
    _enqueue_reply(event, chat_name, reply)
    return {
        "status": "accepted",
        "event_uuid": event.event_uuid,
        "record_no": entry.record_no,
        "reply_preview": reply,
    }


def _enqueue_reply(event: MessageEvent, chat_name: str, body: str) -> ReplyJob:
    return ReplyJob.objects.create(
        event=event,
        chat_name=chat_name,
        body=body,
        status=ReplyJob.Status.PENDING,
    )
