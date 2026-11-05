"""员工记单：增减 / 查询 / 清空 与回复模板。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.db.models import QuerySet
from django.utils import timezone

from apps.ledger.models import LedgerEntry

STAFF_BOOK = "staff_order"
REPLY_SEP = "--------------"


def fmt_num(amount) -> str:
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    if a == a.to_integral_value():
        return str(int(a))
    return f"{a:.2f}"


def fmt_orders(amount) -> str:
    """数量展示：5 → 5单。"""
    return f"{fmt_num(amount)}单"


def fmt_signed(amount) -> str:
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    s = fmt_num(abs(a))
    if a > 0:
        return f"+{s}"
    if a < 0:
        return f"-{s}"
    return s


def fmt_signed_orders(amount) -> str:
    """增减展示：+5 → +5单。"""
    return f"{fmt_signed(amount)}单"


def fmt_ts(when=None) -> str:
    when = when or timezone.localtime()
    if isinstance(when, datetime) and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return timezone.localtime(when).strftime("%Y-%m-%d %H:%M:%S")


def entry_delta(entry: LedgerEntry) -> Decimal:
    amt = Decimal(entry.amount or 0)
    if entry.direction == LedgerEntry.Direction.EXPENSE:
        return -amt
    return amt


def _active_qs(room, business_name: str) -> QuerySet:
    """未清空记单（单群）。跨群按人汇总请用 _load_active_by_display_name。"""
    return (
        LedgerEntry.objects.filter(
            chat_room_id=getattr(room, "id", None) or room,
            book=STAFF_BOOK,
            business_name=business_name,
            status=LedgerEntry.Status.ACTIVE,
        )
        .order_by("occurred_at", "id")
    )


def _employee_ids_by_display_name(display_name: str) -> list[int]:
    """所有统一备注名相同的员工 id（跨群同人可能共用一个，也可能历史重复建档）。"""
    from apps.chatrooms.models import Employee

    want = (display_name or "").strip()
    if not want:
        return []
    ids = list(
        Employee.objects.filter(display_name=want, enabled=True).values_list(
            "id", flat=True
        )
    )
    if ids:
        return list(ids)
    return list(
        Employee.objects.filter(display_name__iexact=want, enabled=True).values_list(
            "id", flat=True
        )
    )


def _load_active_by_display_name(
    display_name: str, *, business_name: str | None = None
) -> list[LedgerEntry]:
    """按统一备注名跨群拉取未清空记单（群之间无隔阂）。"""
    ids = _employee_ids_by_display_name(display_name)
    if not ids:
        return []
    qs = (
        LedgerEntry.objects.filter(
            book=STAFF_BOOK,
            employee_id__in=ids,
            status=LedgerEntry.Status.ACTIVE,
        )
        .select_related("employee", "chat_room")
        .order_by("occurred_at", "id")
    )
    if business_name:
        qs = qs.filter(business_name=business_name)
    return list(qs)


def _load_active_entries(room, business_name: str, *, employee=None) -> list[LedgerEntry]:
    """本群业务明细；指定员工时改为按备注名跨群汇总。"""
    if employee is not None:
        name = (getattr(employee, "display_name", None) or "").strip()
        if name:
            return _load_active_by_display_name(name, business_name=business_name)
        return list(
            _active_qs(room, business_name)
            .filter(employee_id=getattr(employee, "id", None))
            .select_related("employee", "chat_room")
        )
    return list(_active_qs(room, business_name).select_related("employee", "chat_room"))


def net_for_employee(room, business_name: str, employee) -> Decimal:
    """本人合计：按统一备注名跨群汇总（room 仅兼容旧调用）。"""
    _ = room
    name = (getattr(employee, "display_name", None) or "").strip()
    total = Decimal("0")
    for e in _load_active_by_display_name(name, business_name=business_name):
        total += entry_delta(e)
    return total.quantize(Decimal("0.01"))


def net_for_employee_sku(
    room, business_name: str, employee, sku_remark: str
) -> Decimal:
    """多 SKU：按备注名跨群 + remark 规格统计本人合计。"""
    _ = room
    sku = (sku_remark or "").strip()
    name = (getattr(employee, "display_name", None) or "").strip()
    total = Decimal("0")
    for e in _load_active_by_display_name(name, business_name=business_name):
        if sku and (e.remark or "").strip() != sku:
            continue
        total += entry_delta(e)
    return total.quantize(Decimal("0.01"))


def staff_order_success_reply(
    *,
    business_name: str,
    delta: Decimal,
    operator_name: str,
    self_net: Decimal,
    when=None,
    currency: str = "",
) -> str:
    when = when or timezone.now()
    _ = currency
    return (
        f"记单成功\n"
        f"{REPLY_SEP}\n"
        f"业务名：{business_name}\n"
        f"变动：{fmt_signed_orders(delta)}\n"
        f"本人合计：{fmt_orders(self_net)}\n"
        f"{REPLY_SEP}\n"
        f"操作人：{(operator_name or '').strip() or '—'}\n"
        f"时间：{fmt_ts(when)}"
    )


def staff_order_query_reply(
    *,
    business_name: str,
    is_admin: bool,
    self_name: str,
    self_net: Decimal,
    self_count: int,
    per_employee: list[tuple[str, Decimal]],
    total_net: Decimal,
    when=None,
) -> str:
    when = when or timezone.now()
    parts = [
        "记单查询",
        f"业务名：{business_name}",
        REPLY_SEP,
    ]
    if is_admin:
        if not per_employee:
            parts.append("暂无记录")
            parts.append(REPLY_SEP)
        else:
            for name, net in per_employee:
                parts.append(f"{name}：{fmt_orders(net)}")
            parts.append(REPLY_SEP)
            parts.append(f"合计：{fmt_orders(total_net)}")
            parts.append(REPLY_SEP)
    else:
        parts.append(f"员工：{(self_name or '').strip() or '—'}")
        parts.append(f"合计：{fmt_orders(self_net)}")
        parts.append(REPLY_SEP)
    parts.append(f"查询时间：{fmt_ts(when)}")
    return "\n".join(parts)


def staff_order_query_variants_reply(
    *,
    business_name: str,
    person_name: str,
    variants: list[tuple[str, Decimal]],
    total_net: Decimal,
    when=None,
) -> str:
    """按子类/时长拆分展示（如三个月、六个月、一年）。"""
    when = when or timezone.now()
    parts = [
        "记单查询",
        f"业务名：{business_name}",
        f"员工：{(person_name or '').strip() or '—'}",
        REPLY_SEP,
    ]
    if not variants:
        parts.append("暂无记录")
        parts.append(REPLY_SEP)
    else:
        for label, net in variants:
            parts.append(f"{label}：{fmt_orders(net)}")
        parts.append(REPLY_SEP)
        parts.append(f"合计：{fmt_orders(total_net)}")
        parts.append(REPLY_SEP)
    parts.append(f"查询时间：{fmt_ts(when)}")
    return "\n".join(parts)


def _entry_variant_label(entry: LedgerEntry, business_name: str) -> str:
    """多 SKU：用 remark（一个月/pro1三个月/pro100刀）；否则用业务名。"""
    remark = (entry.remark or "").strip()
    biz = (entry.business_name or business_name or "").strip()
    if remark:
        return remark
    return biz or "未命名"


def _sort_variant_items(
    items: list[tuple[str, Decimal]], business_name: str
) -> list[tuple[str, Decimal]]:
    try:
        from apps.ledger.staff_order_catalog import sku_order

        order = sku_order(business_name)
    except Exception:
        order = ()
    rank = {name: i for i, name in enumerate(order)}

    def key(item: tuple[str, Decimal]) -> tuple:
        label = item[0]
        return (rank.get(label, 1000), label)

    return sorted(items, key=key)


def staff_order_clear_reply(
    *,
    business_name: str,
    count: int,
    operator_name: str,
    target_name: str = "",
    when=None,
) -> str:
    when = when or timezone.now()
    target_line = (
        f"员工：{(target_name or '').strip() or '—'}\n" if target_name else ""
    )
    return (
        f"清空记单成功\n"
        f"{REPLY_SEP}\n"
        f"业务名：{business_name}\n"
        f"{target_line}"
        f"清空条数：{count}条\n"
        f"{REPLY_SEP}\n"
        f"操作人：{(operator_name or '').strip() or '—'}\n"
        f"清空时间：{fmt_ts(when)}"
    )


def find_room_employee_by_name(room, name: str):
    """按统一备注名解析本群员工。"""
    from apps.chatrooms.models import Employee, SenderBinding

    want = (name or "").strip()
    if not want:
        return None

    bound_ids = list(
        SenderBinding.objects.filter(chat_room=room, active=True).values_list(
            "employee_id", flat=True
        )
    )
    candidates = list(
        Employee.objects.filter(id__in=bound_ids, enabled=True).order_by("id")
    )
    if not candidates:
        # 兜底：本群有记单记录的员工
        ids = (
            LedgerEntry.objects.filter(chat_room=room, book=STAFF_BOOK)
            .exclude(employee_id=None)
            .values_list("employee_id", flat=True)
            .distinct()
        )
        candidates = list(Employee.objects.filter(id__in=ids, enabled=True).order_by("id"))

    exact = [e for e in candidates if (e.display_name or "").strip() == want]
    if exact:
        return exact[0]
    low = want.lower()
    fuzzy = [e for e in candidates if (e.display_name or "").strip().lower() == low]
    if fuzzy:
        return fuzzy[0]

    # 全库精确匹配（同名多条取第一条）
    hit = Employee.objects.filter(display_name=want, enabled=True).order_by("id").first()
    return hit


def _qty_from_remote(rec: dict) -> Decimal:
    try:
        return Decimal(str(rec.get("quantity") if rec.get("quantity") is not None else 0))
    except Exception:
        return Decimal("0")


def _occurred_ts_from_remote(rec: dict) -> str:
    """远程 occurred_at → 北京时间字符串。"""
    from apps.ledger.ledger_ops import parse_remote_datetime

    dt = parse_remote_datetime(rec.get("occurred_at"))
    if dt is None:
        raw = str(rec.get("occurred_at") or "").strip()
        return raw[:19] if raw else ""
    return fmt_ts(dt)


def _remote_variant_label(rec: dict, business_name: str) -> str:
    remark = (str(rec.get("remark") or "")).strip()
    biz = (str(rec.get("business_name") or business_name or "")).strip()
    if remark and remark != biz:
        return remark
    return biz or "未命名"


XINHUO_DOWN_MSG = "⚠️ 薪火暂时连不上，{what}失败，请稍后再试（本次未做任何改动）。"


def _remote_biz_variant(rec: dict) -> tuple[str, str]:
    """薪火记录 → (规范业务名, 规格)。按 table_type 归类，薪火手工录入的记录也能对上。"""
    from apps.integrations.xinhuo_push import TABLE_TYPE_TO_BIZ
    from apps.ledger.staff_order_catalog import is_multi_sku

    tt = str(rec.get("table_type") or "").strip().lower()
    bn = str(rec.get("business_name") or "").strip()
    biz = TABLE_TYPE_TO_BIZ.get(tt) or bn or str(rec.get("book") or "").strip() or "未命名"
    if is_multi_sku(biz):
        return biz, (bn if bn and bn != biz else biz)
    return biz, biz


def _pull_remote(employee_name: str, business_name: str = "") -> list[dict] | None:
    """拉取薪火未清空记单（不含 USDT）。失败返回 None。"""
    try:
        from apps.integrations.xinhuo_push import pull_bot_records_from_xinhuo

        data = pull_bot_records_from_xinhuo(
            employee_name=employee_name,
            business_name=business_name,
            status="ACTIVE",
        )
    except Exception:
        return None
    if data is None:
        return None
    rows = []
    for r in data.get("records") or []:
        if str(r.get("table_type") or "").lower() == "usdt":
            continue
        rows.append(r)
    return rows


def remote_self_net(entry: LedgerEntry, *, sku_remark: str = "") -> Decimal | None:
    """记单成功回执的「本人合计」：薪火未清空合计 + 本条（本条可能还没推到薪火）。"""
    from apps.integrations.xinhuo_push import entry_event_id
    from apps.ledger.staff_order_catalog import is_multi_sku

    name = (getattr(entry.employee, "display_name", None) or "").strip()
    biz = (entry.business_name or "").strip()
    rows = _pull_remote(name, biz)
    if rows is None:
        return None
    this_id = entry_event_id(entry)
    total = Decimal("0")
    for r in rows:
        if str(r.get("event_id") or r.get("external_id") or "") == this_id:
            continue
        rb, variant = _remote_biz_variant(r)
        if rb != biz:
            continue
        if is_multi_sku(biz) and sku_remark and variant != sku_remark:
            continue
        total += _qty_from_remote(r)
    return (total + entry_delta(entry)).quantize(Decimal("0.01"))


def _mark_local_processed(entries: list[LedgerEntry], reason: str, when) -> None:
    if not entries:
        return
    LedgerEntry.objects.filter(id__in=[e.id for e in entries]).update(
        status=LedgerEntry.Status.PROCESSED,
        reverse_reason=reason[:255],
        reversed_at=when,
        updated_at=when,
    )


def format_staff_order_query_by_employee(room, target_employee) -> str:
    """查询-姓名 → 该员工所有未清空记单（以薪火为准，跨群）。"""
    from apps.ledger.ledger_ops import ledger_error_reply
    from apps.ledger.staff_order_catalog import is_multi_sku

    _ = room
    name = (getattr(target_employee, "display_name", None) or "").strip() or "—"
    rows = _pull_remote(name)
    if rows is None:
        return ledger_error_reply(XINHUO_DOWN_MSG.format(what="查询"))

    nested: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    for r in rows:
        biz, variant = _remote_biz_variant(r)
        nested[biz][variant] += _qty_from_remote(r)

    parts = ["记单查询", f"员工：{name}", REPLY_SEP]
    if not nested:
        parts += ["暂无记录", REPLY_SEP]
    else:
        total = Decimal("0")
        for biz in sorted(nested.keys()):
            vm = nested[biz]
            if is_multi_sku(biz):
                items = _sort_variant_items(
                    [(k, v.quantize(Decimal("0.01"))) for k, v in vm.items()], biz
                )
                sub = sum((n for _, n in items), Decimal("0")).quantize(Decimal("0.01"))
                total += sub
                parts.append(f"{biz}：{fmt_orders(sub)}")
                for lab, net in items:
                    parts.append(f"  · {lab}：{fmt_orders(net)}")
            else:
                net = sum(vm.values(), Decimal("0")).quantize(Decimal("0.01"))
                total += net
                parts.append(f"{biz}：{fmt_orders(net)}")
        parts += [REPLY_SEP, f"合计：{fmt_orders(total.quantize(Decimal('0.01')))}", REPLY_SEP]
    parts.append(f"查询时间：{fmt_ts()}")
    return "\n".join(parts)


def format_staff_order_query(
    room, employee, business_name: str, *, target_employee=None, is_admin: bool | None = None
) -> str:
    """查询某业务；指定员工时按备注名跨群汇总。优先薪火页。"""
    from apps.ledger.staff_order_catalog import is_multi_sku
    from apps.chatrooms.models import is_admin_in_room

    if is_admin is None:
        is_admin = is_admin_in_room(room, employee)
    else:
        is_admin = bool(is_admin)

    if target_employee is not None:
        from apps.ledger.ledger_ops import ledger_error_reply

        person = (getattr(target_employee, "display_name", None) or "").strip() or "—"
        rows = _pull_remote(person, business_name)
        if rows is None:
            return ledger_error_reply(XINHUO_DOWN_MSG.format(what="查询"))
        rows = [r for r in rows if _remote_biz_variant(r)[0] == business_name]
        total = sum((_qty_from_remote(r) for r in rows), Decimal("0")).quantize(
            Decimal("0.01")
        )
        if is_multi_sku(business_name):
            buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for r in rows:
                buckets[_remote_biz_variant(r)[1]] += _qty_from_remote(r)
            variants = _sort_variant_items(
                [(k, v.quantize(Decimal("0.01"))) for k, v in buckets.items()],
                business_name,
            )
            return staff_order_query_variants_reply(
                business_name=business_name,
                person_name=person,
                variants=variants,
                total_net=total,
            )
        parts = ["记单查询", f"业务名：{business_name}", f"员工：{person}", REPLY_SEP]
        if not rows:
            parts += ["暂无记录", REPLY_SEP]
        else:
            for r in rows:
                ts = _occurred_ts_from_remote(r) or "—"
                parts.append(f"{ts}  {fmt_signed_orders(_qty_from_remote(r))}")
            parts += [REPLY_SEP, f"合计：{fmt_orders(total)}（{len(rows)}笔）", REPLY_SEP]
        parts.append(f"查询时间：{fmt_ts()}")
        return "\n".join(parts)

    # 未指定员工：仍按本群（避免误把全库混在一起）
    qs = _load_active_entries(room, business_name)

    if is_admin:
        buckets_n: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for e in qs:
            n = (
                (e.employee.display_name if e.employee_id else "") or "—"
            ).strip() or "—"
            buckets_n[n] += entry_delta(e)
        per: list[tuple[str, Decimal]] = [
            (n, net.quantize(Decimal("0.01"))) for n, net in buckets_n.items()
        ]
        per.sort(key=lambda x: x[0])
        total = sum((n for _, n in per), Decimal("0")).quantize(Decimal("0.01"))
        return staff_order_query_reply(
            business_name=business_name,
            is_admin=True,
            self_name="",
            self_net=Decimal("0"),
            self_count=0,
            per_employee=per,
            total_net=total,
        )

    name = (getattr(employee, "display_name", None) or "").strip()
    mine = _load_active_by_display_name(name, business_name=business_name)
    self_net = sum((entry_delta(x) for x in mine), Decimal("0")).quantize(
        Decimal("0.01")
    )
    return staff_order_query_reply(
        business_name=business_name,
        is_admin=False,
        self_name=name,
        self_net=self_net,
        self_count=len(mine),
        per_employee=[],
        total_net=self_net,
    )


def clear_staff_order_business(
    room,
    employee,
    business_name: str,
    *,
    target_employee=None,
) -> str:
    """清空业务-姓名：先在薪火清空该员工该业务（含薪火手工录入的），再同步本机。"""
    from apps.integrations.xinhuo_push import clear_bot_records_on_xinhuo
    from apps.ledger.ledger_ops import ledger_error_reply

    _ = room
    if target_employee is None:
        return ledger_error_reply(f"❌ 清空需指定员工。例：清空{business_name}-张越")

    target_name = (getattr(target_employee, "display_name", None) or "").strip() or "—"
    result = clear_bot_records_on_xinhuo(
        employee_name=target_name, business_name=business_name
    )
    if result is None:
        return ledger_error_reply(XINHUO_DOWN_MSG.format(what="清空"))

    who = (employee.display_name if employee else "") or "操作人"
    when = timezone.now()
    _mark_local_processed(
        _load_active_by_display_name(target_name, business_name=business_name),
        f"清空记单 {business_name}/{target_name} by {who}",
        when,
    )
    count = int(result.get("cleared") or 0)
    if count == 0:
        return ledger_error_reply(
            f"ℹ️ 「{target_name}」在「{business_name}」没有可清空的记单"
        )
    return staff_order_clear_reply(
        business_name=business_name,
        count=count,
        operator_name=who,
        target_name=target_name,
        when=when,
    )


def clear_staff_order_by_employee(room, employee, *, target_employee) -> str:
    """清空-姓名：在薪火清空该员工所有业务未清空记单（不含 USDT / 记账），再同步本机。"""
    from apps.integrations.xinhuo_push import clear_bot_records_on_xinhuo
    from apps.ledger.ledger_ops import ledger_error_reply

    _ = room
    target_name = (getattr(target_employee, "display_name", None) or "").strip() or "—"
    result = clear_bot_records_on_xinhuo(employee_name=target_name)
    if result is None:
        return ledger_error_reply(XINHUO_DOWN_MSG.format(what="清空"))

    who = (employee.display_name if employee else "") or "操作人"
    when = timezone.now()
    _mark_local_processed(
        _load_active_by_display_name(target_name),
        f"清空记单 全部业务/{target_name} by {who}",
        when,
    )
    count = int(result.get("cleared") or 0)
    if count == 0:
        return ledger_error_reply(f"ℹ️ 「{target_name}」没有可清空的记单")

    # by_table 的键是薪火表名（充币 / gpt / 远程…），与机器人业务名一致
    parts = ["清空记单成功", REPLY_SEP, f"员工：{target_name}"]
    for label, n in sorted((result.get("by_table") or {}).items()):
        parts.append(f"{label}：{int(n)}条")
    parts += [
        f"清空条数：{count}条",
        REPLY_SEP,
        f"操作人：{(who or '').strip() or '—'}",
        f"清空时间：{fmt_ts(when)}",
    ]
    return "\n".join(parts)
