"""记账表专用：查询 / 清账 / 统计 / 群内回复模板。

记账入账成功模板（与群内截图一致）：
--------------------
时间
✅ 订单记录成功
业务名 / 金额 / 给你 / 给我 / 利润
--------------------
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import QuerySet
from django.utils import timezone

from apps.ledger.models import LedgerEntry

LEDGER_BOOK = LedgerEntry.Book.LEDGER
REPLY_SEP = "--------------------"
USDT_REPLY_SEP = "--------------"

REPLY_COMMAND_FOOTER = """📒 指令说明
【入账】业务名 金额N 给你N 给我N 利润N（至少写一个，互不计算）
• 例：充币金额214给你147利润67
• 例：飞机号 给我50
【查询】未清账按业务汇总：查询记账
【清账】未清账全部标记已清账：清账
（USDT：给我12467，1000刀 → 美金+1000、人民币12467 ／ 查询usdt；USDT 不清空）
【统计】查询本月 / 查询上月 / 查询本周
【记单】业务名+数量 / 业务名-数量（例：充币+1）
• 查询单项：查询业务名-姓名（例：查询充币-李振宇）
• 查询全部：查询-姓名（该员工所有未清空记单）
• 清空单项（管理员）：清空业务名-姓名（例：清空充币-李振宇）
• 清空全部（管理员）：清空-姓名
• 业务：充币 共享 打包 openrouter patreon booth pictspace mega gpt 一次卡 远程
【帮助】发「记账」查看本说明"""


def fmt_amount(amount) -> str:
    """整数不带小数；小数最多两位。不加「元」。"""
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    if a == a.to_integral_value():
        return str(int(a))
    return f"{a:.2f}"


def fmt_yuan(amount) -> str:
    """两位小数 + 元（兼容旧调用）。"""
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    return f"{a:.2f}元"


def fmt_money(amount, currency: str = "CNY") -> str:
    """按币种展示；USD 用「美金」。"""
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    if (currency or "").upper() == "USD":
        return f"{a:.2f}美金"
    return f"{a:.2f}元"


def fmt_debt_yuan(amount) -> str:
    """人民币展示：12467元（整数不带小数）。"""
    a = Decimal(amount or 0).quantize(Decimal("0.01"))
    if a == a.to_integral_value():
        return f"{int(a)}元"
    return f"{a:.2f}元"


def fmt_ts(when=None) -> str:
    when = when or timezone.localtime()
    if isinstance(when, datetime) and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return timezone.localtime(when).strftime("%Y-%m-%d %H:%M:%S")


def fmt_date(when) -> str:
    if isinstance(when, datetime) and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return timezone.localtime(when).strftime("%Y-%m-%d")


def parse_remote_datetime(raw) -> datetime | None:
    """解析薪火/远程 ISO 时间（含 +00:00），转为带时区的 datetime，供 fmt_ts 转北京时间。"""
    from django.utils.dateparse import parse_datetime

    s = str(raw or "").strip()
    if not s:
        return None
    dt = parse_datetime(s)
    if dt is None:
        try:
            raw19 = s.replace("T", " ")[:19]
            dt = datetime.strptime(raw19, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if timezone.is_naive(dt):
        # 无时区信息时按业务时区（Asia/Shanghai）理解
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def wrap_reply(main: str, *, trailing_ts: datetime | None = None) -> str:
    """兼容旧调用：主内容原样返回（模板已自带分隔线）。"""
    text = main.strip()
    if trailing_ts is not None and fmt_ts(trailing_ts) not in text:
        text = f"{text}\n{fmt_ts(trailing_ts)}"
    return text


def ledger_error_reply(message: str, *, trailing_ts: datetime | None = None) -> str:
    """错误/警告回复：只回错误信息，不附带指令说明。"""
    lines = [REPLY_SEP, message.strip(), REPLY_SEP]
    if trailing_ts is not None:
        lines.append(fmt_ts(trailing_ts))
    return "\n".join(lines)


def _project_block(
    name: str,
    income: Decimal,
    expense: Decimal,
    profit_text: str,
    *,
    currency: str = "CNY",
) -> str:
    """兼容旧调用（已不再用于记账成功）。"""
    return (
        f"┌─ 项目名：{name}\n"
        f"│ 收入：{fmt_money(income, currency)}\n"
        f"│ 支出：{fmt_money(expense, currency)}\n"
        f"└─ 利润：{profit_text}"
    )


def _biz_query_block(
    name: str,
    amount: Decimal,
    to_you: Decimal,
    to_me: Decimal,
    profit_text: str,
) -> str:
    """查询用：业务名 / 金额 / 给你 / 给我 / 利润。"""
    return (
        f"业务名：{name}\n"
        f"金额：{fmt_amount(amount)}\n"
        f"给你：{fmt_amount(to_you)}\n"
        f"给我：{fmt_amount(to_me)}\n"
        f"利润：{profit_text}"
    )


def entry_income(entry: LedgerEntry) -> Decimal:
    """兼容：仅 receive_amount（金额），不再把「给我」并入。"""
    return entry.receive_amount or Decimal("0")


def entry_expense(entry: LedgerEntry) -> Decimal:
    return entry.amount_to_you or Decimal("0")


def entry_amount(entry: LedgerEntry) -> Decimal:
    return entry.receive_amount or Decimal("0")


def entry_to_you(entry: LedgerEntry) -> Decimal:
    return entry.amount_to_you or Decimal("0")


def entry_to_me(entry: LedgerEntry) -> Decimal:
    return entry.amount_to_me or Decimal("0")


def entry_profit_text(entry: LedgerEntry) -> str:
    """单条利润展示。"""
    p = entry.profit_amount
    if p is None:
        return "未填写"
    return fmt_amount(p)


def _totals_block(amount, to_you, to_me, profit) -> str:
    return (
        "合计：\n"
        f"金额：{fmt_amount(amount)}\n"
        f"给你：{fmt_amount(to_you)}\n"
        f"给我：{fmt_amount(to_me)}\n"
        f"利润：{fmt_amount(profit)}"
    )


def _uncleared_ledger_totals(room, extra: LedgerEntry | None = None):
    """本群未清账记账合计（不含 USDT）。"""
    amount = to_you = to_me = profit = Decimal("0")
    seen_id = None
    if extra is not None:
        amount += extra.receive_amount or Decimal("0")
        to_you += extra.amount_to_you or Decimal("0")
        to_me += extra.amount_to_me or Decimal("0")
        profit += extra.profit_amount or Decimal("0")
        seen_id = getattr(extra, "pk", None)
    if room is None:
        return amount, to_you, to_me, profit
    try:
        qs = LedgerEntry.objects.filter(
            chat_room=room,
            book=LEDGER_BOOK,
            status=LedgerEntry.Status.ACTIVE,
        ).exclude(business_name__iexact="USDT")
        if seen_id:
            qs = qs.exclude(pk=seen_id)
        for e in qs:
            amount += e.receive_amount or Decimal("0")
            to_you += e.amount_to_you or Decimal("0")
            to_me += e.amount_to_me or Decimal("0")
            profit += e.profit_amount or Decimal("0")
    except Exception:
        pass
    return amount, to_you, to_me, profit


def ledger_entry_success_reply(
    entry: LedgerEntry | None = None,
    *,
    operator_name: str = "",
    when=None,
) -> str:
    """记账成功模板（与截图一致，只换数字；末尾带未清账合计）。"""
    _ = operator_name
    when = when or (getattr(entry, "occurred_at", None) if entry else None) or timezone.now()
    biz = "—"
    receive = Decimal("0")
    to_you = Decimal("0")
    to_me = Decimal("0")
    profit = Decimal("0")
    room = None
    if entry is not None:
        biz = (entry.business_name or entry.remark or "").strip() or "—"
        receive = entry.receive_amount or Decimal("0")
        to_you = entry.amount_to_you or Decimal("0")
        to_me = entry.amount_to_me or Decimal("0")
        profit = entry.profit_amount or Decimal("0")
        room = getattr(entry, "chat_room", None)
    tot_a, tot_y, tot_m, tot_p = _uncleared_ledger_totals(room, extra=entry)
    return (
        f"{REPLY_SEP}\n"
        f"{fmt_ts(when)}\n"
        f"✅ 订单记录成功\n"
        f"业务名：{biz}\n"
        f"金额：{fmt_amount(receive)}\n"
        f"给你：{fmt_amount(to_you)}\n"
        f"给我：{fmt_amount(to_me)}\n"
        f"利润：{fmt_amount(profit)}\n"
        f"{REPLY_SEP}\n"
        f"{_totals_block(tot_a, tot_y, tot_m, tot_p)}\n"
        f"{REPLY_SEP}"
    )


def _usdt_project_block(usd_amount, cny_amount) -> str:
    return (
        f"┌─ 项目名：USDT\n"
        f"│ 美金：{fmt_money(usd_amount, 'USD')}\n"
        f"└─ 人民币：{fmt_debt_yuan(cny_amount)}"
    )


def usdt_entry_success_reply(
    entry: LedgerEntry | None = None,
    *,
    operator_name: str = "",
    when=None,
) -> str:
    """USDT 入账成功模板（只改数字，不改结构）。"""
    when = when or (getattr(entry, "occurred_at", None) if entry else None) or timezone.now()
    who = (operator_name or "").strip()
    usd = Decimal("0")
    debt = Decimal("0")
    if entry is not None:
        usd = Decimal(entry.amount or 0)
        if entry.direction == LedgerEntry.Direction.EXPENSE:
            usd = -abs(usd)
        debt = entry.amount_to_me or Decimal("0")
        if not who:
            who = (getattr(getattr(entry, "employee", None), "display_name", None) or "") or ""
    return (
        f"记录成功\n"
        f"{USDT_REPLY_SEP}\n"
        f"{_usdt_project_block(usd, debt)}\n"
        f"{USDT_REPLY_SEP}\n"
        f"记录人：{who or '—'}\n"
        f"记录时间：{fmt_ts(when)}"
    )


USDT_QUERY_MAX_ROWS = 30


def format_usdt_query(room) -> str:
    """查询usdt：以薪火「微信BOT数据·USDT」为准（全部记录，USDT 不清空）。"""
    _ = room
    now = timezone.now()
    try:
        from apps.integrations.xinhuo_push import pull_bot_records_from_xinhuo

        data = pull_bot_records_from_xinhuo(business_name="USDT", status="*")
    except Exception:
        data = None
    if data is None:
        return ledger_error_reply("⚠️ 薪火暂时连不上，查询失败，请稍后再试。")

    rows = [
        r
        for r in (data.get("records") or [])
        if str(r.get("table_type") or "").lower() == "usdt"
    ]
    parts = ["USDT查询", USDT_REPLY_SEP]
    if not rows:
        parts.append("暂无记录")
        parts.append(USDT_REPLY_SEP)
        parts.append(f"查询时间：{fmt_ts(now)}")
        return "\n".join(parts)

    def _dec(v) -> Decimal:
        try:
            return Decimal(str(v if v not in (None, "") else 0))
        except Exception:
            return Decimal("0")

    total_usd = sum((_dec(r.get("amount_usd")) for r in rows), Decimal("0"))
    total_cny = sum(
        (_dec(r.get("amount_cny", r.get("amount"))) for r in rows), Decimal("0")
    )
    shown = rows[-USDT_QUERY_MAX_ROWS:]
    if len(rows) > len(shown):
        parts.append(f"共{len(rows)}条，仅显示最近{len(shown)}条")
        parts.append(USDT_REPLY_SEP)
    for r in shown:
        parts.append(
            _usdt_project_block(
                _dec(r.get("amount_usd")), _dec(r.get("amount_cny", r.get("amount")))
            )
        )
        parts.append(f"记录人：{(str(r.get('employee_name') or '')).strip() or '—'}")
        dt = parse_remote_datetime(r.get("occurred_at"))
        parts.append(f"记录时间：{fmt_ts(dt) if dt else '—'}")
        parts.append(USDT_REPLY_SEP)
    parts.append(f"合计美金：{fmt_money(total_usd, 'USD')}")
    parts.append(f"合计人民币：{fmt_debt_yuan(total_cny)}")
    parts.append(USDT_REPLY_SEP)
    parts.append(f"查询时间：{fmt_ts(now)}")
    return "\n".join(parts)


def ledger_clear_reply(
    *,
    operator_name: str = "",
    count: int = 0,
    range_text: str = "全部项目",
    when=None,
) -> str:
    """清账成功模板。"""
    when = when or timezone.now()
    return (
        f"清空账目成功\n"
        f"{REPLY_SEP}\n"
        f"清空范围：{range_text}\n"
        f"清空条数：{count}条\n"
        f"{REPLY_SEP}\n"
        f"操作人：{(operator_name or '').strip() or '—'}\n"
        f"清空时间：{fmt_ts(when)}"
    )


def ledger_primary_amount(
    *,
    receive_amount: Decimal,
    amount_to_you: Decimal,
    amount_to_me: Decimal,
    profit_amount: Decimal,
) -> Decimal:
    """legacy amount：优先收入，其次支出，再利润绝对值。"""
    income = (receive_amount or 0) + (amount_to_me or 0)
    expense = amount_to_you or 0
    if income:
        return Decimal(income)
    if expense:
        return Decimal(expense)
    return abs(Decimal(profit_amount or 0))


LEDGER_HELP_TEXT = (
    f"📒 记账帮助\n"
    f"{REPLY_SEP}\n"
    f"入账：业务名 金额N 给你N 给我N 利润N（至少写一个）\n"
    f"例：充币金额214给你147利润67\n"
    f"USDT：给我12467，1000刀（美金+1000，人民币12467）／ 查询usdt（USDT 不清空）\n"
    f"查询记账 · 清账 · 查询本月/上月/本周\n"
    f"{REPLY_SEP}\n"
    f"记单：业务名+1 ／ 查询业务名-姓名 ／ 清空业务名-姓名\n"
    f"全部：查询-姓名 ／ 清空-姓名（清空仅管理员）\n"
    f"{REPLY_SEP}\n"
    f"{fmt_ts()}"
)


def _ledger_qs(room, *, start: datetime, end: datetime) -> QuerySet:
    # 周期/区间：跨群；不计已撤销；已清账仍计入流水；不含 USDT（独立账本）
    _ = room
    return (
        LedgerEntry.objects.filter(
            book=LEDGER_BOOK,
            occurred_at__gte=start,
            occurred_at__lt=end,
        )
        .exclude(status=LedgerEntry.Status.REVERSED)
        .exclude(business_name__iexact="USDT")
    )


def _period_bounds(period: str) -> tuple[datetime, datetime, str]:
    """返回 [start, end) 与展示文案（含开始日、含结束日）。"""
    now = timezone.localtime()

    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now + timedelta(seconds=1)
        label_end = now
        return start, end, f"{fmt_date(start)} 至 {fmt_date(label_end)}"

    if period == "last_month":
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_this
        start = (first_this - timedelta(days=1)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        last_day = end - timedelta(seconds=1)
        return start, end, f"{fmt_date(start)} 至 {fmt_date(last_day)}"

    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = now + timedelta(seconds=1)
        return start, end, f"{fmt_date(start)} 至 {fmt_date(now)}"

    raise ValueError(f"unknown period: {period}")


def _aggregate_by_project(entries: list[LedgerEntry]) -> list[dict]:
    """
    按业务名汇总。
    返回 [{name, amount, to_you, to_me, profit, profit_text, missing}, ...]
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "amount": Decimal("0"),
            "to_you": Decimal("0"),
            "to_me": Decimal("0"),
            "profit": Decimal("0"),
            "profit_n": 0,
            "missing": 0,
            "order": 0,
        }
    )
    order = 0
    for e in entries:
        name = (e.business_name or e.remark or "").strip() or "未命名"
        b = buckets[name]
        if b["order"] == 0:
            order += 1
            b["order"] = order
        b["amount"] += entry_amount(e)
        b["to_you"] += entry_to_you(e)
        b["to_me"] += entry_to_me(e)
        if e.profit_amount is None:
            b["missing"] += 1
        else:
            b["profit"] += e.profit_amount
            b["profit_n"] += 1

    rows = []
    for name, b in sorted(buckets.items(), key=lambda kv: kv[1]["order"]):
        if b["profit_n"] == 0:
            profit_text = "未填写"
        elif b["missing"] > 0:
            profit_text = f"{fmt_amount(b['profit'])}（另有{b['missing']}条未填写）"
        else:
            profit_text = fmt_amount(b["profit"])
        rows.append(
            {
                "name": name,
                "amount": b["amount"],
                "to_you": b["to_you"],
                "to_me": b["to_me"],
                "profit": b["profit"],
                "profit_text": profit_text,
                "missing": b["missing"],
            }
        )
    return rows


def _range_label_from_entries(entries: list[LedgerEntry], fallback_now=None) -> str:
    fallback_now = fallback_now or timezone.now()
    times = []
    for e in entries:
        occ = getattr(e, "occurred_at", None)
        if isinstance(occ, datetime):
            times.append(occ)
    if not times:
        return f"{fmt_date(fallback_now)} 至 {fmt_date(fallback_now)}"
    return f"{fmt_date(min(times))} 至 {fmt_date(max(times))}"


def _format_query_message(
    *,
    range_label: str,
    entries: list[LedgerEntry],
    query_when=None,
) -> str:
    query_when = query_when or timezone.now()
    if not range_label:
        range_label = _range_label_from_entries(entries, query_when)
    parts = [
        "账目查询",
        f"查询范围：{range_label}",
        REPLY_SEP,
    ]
    groups = _aggregate_by_project(entries)
    sum_amount = sum_you = sum_me = sum_profit = Decimal("0")
    if not groups:
        parts.append(
            _biz_query_block("—", Decimal("0"), Decimal("0"), Decimal("0"), "未填写")
        )
        parts.append(REPLY_SEP)
    else:
        for g in groups:
            parts.append(
                _biz_query_block(
                    g["name"],
                    g["amount"],
                    g["to_you"],
                    g["to_me"],
                    g["profit_text"],
                )
            )
            parts.append(REPLY_SEP)
            sum_amount += g["amount"]
            sum_you += g["to_you"]
            sum_me += g["to_me"]
            sum_profit += g["profit"]
    parts.append(
        _totals_block(sum_amount, sum_you, sum_me, sum_profit)
    )
    parts.append(REPLY_SEP)
    parts.append(f"查询时间：{fmt_ts(query_when)}")
    return "\n".join(parts)


def _entries_from_remote_active(room, remote: dict, *, start: datetime, end: datetime) -> list:
    """把远程 ACTIVE 记录转成轻量对象，供模板聚合（字段名对齐 LedgerEntry）。"""
    from apps.integrations.xinhuo_push import _dec, _norm_chat, entry_event_id

    room_eids = {
        entry_event_id(e)
        for e in LedgerEntry.objects.filter(chat_room=room, book=LEDGER_BOOK).select_related(
            "event"
        )
    }
    chat_want = _norm_chat(getattr(room, "name", "") or "")
    out = []
    for r in remote.get("records") or []:
        st = str(r.get("status") or "").upper()
        if st != "ACTIVE":
            continue
        eid = str(r.get("event_id") or "")
        chat = _norm_chat(str(r.get("chat_name") or ""))
        if eid not in room_eids and chat != chat_want:
            continue
        occ = str(r.get("occurred_at") or "")
        if occ and start and end:
            try:
                dt = parse_remote_datetime(occ)
                if dt is not None and (dt < start or dt >= end):
                    continue
            except Exception:
                pass
        obj = LedgerEntry(
            business_name=str(r.get("business_name") or "")[:128],
            receive_amount=_dec(r.get("receive_amount")),
            amount_to_you=_dec(r.get("amount_to_you")),
            amount_to_me=_dec(r.get("amount_to_me")),
            profit_amount=_dec(r.get("profit_amount")),
        )
        out.append(obj)
    return out


def format_ledger_period_query(room, period: str) -> str:
    """查询本月 / 上月 / 本周：按项目汇总收入/支出/利润（跨群）。"""
    remote = None
    try:
        from apps.chatrooms.models import ChatRoom
        from apps.integrations.xinhuo_push import sync_room_ledger_from_xinhuo

        for r in ChatRoom.objects.filter(enabled=True):
            try:
                sync_room_ledger_from_xinhuo(r)
            except Exception:
                continue
        from apps.integrations.xinhuo_push import pull_ledger_from_xinhuo

        remote = pull_ledger_from_xinhuo("*", status="")
    except Exception:
        pass
    start, end, label = _period_bounds(period)
    local_qs = list(_ledger_qs(room, start=start, end=end).order_by("occurred_at", "id"))

    if remote is not None:
        from apps.integrations.xinhuo_push import _dec

        entries = []
        for r in remote.get("records") or []:
            st = str(r.get("status") or "").upper()
            if st == "REVERSED":
                continue
            biz = str(r.get("business_name") or "").strip()
            if biz.upper() == "USDT":
                continue
            book = str(r.get("book") or "").strip().lower()
            if book == "usdt":
                continue
            occ = str(r.get("occurred_at") or "")
            if occ:
                try:
                    dt = parse_remote_datetime(occ)
                    if dt is not None and (dt < start or dt >= end):
                        continue
                except Exception:
                    pass
            entries.append(
                LedgerEntry(
                    business_name=str(r.get("business_name") or "")[:128],
                    receive_amount=_dec(r.get("receive_amount")),
                    amount_to_you=_dec(r.get("amount_to_you")),
                    amount_to_me=_dec(r.get("amount_to_me")),
                    profit_amount=_dec(r.get("profit_amount")),
                )
            )
        if entries or not local_qs:
            return _format_query_message(range_label=label, entries=entries)

    return _format_query_message(range_label=label, entries=local_qs)


def format_ledger_query(room, employee=None) -> str:
    """查询：全部未清账（跨群），与薪火后台「未清账合计」对齐。"""
    _ = employee
    remote = None
    try:
        from apps.chatrooms.models import ChatRoom
        from apps.integrations.xinhuo_push import (
            pull_ledger_from_xinhuo,
            sync_room_ledger_from_xinhuo,
        )

        for r in ChatRoom.objects.filter(enabled=True):
            try:
                sync_room_ledger_from_xinhuo(r)
            except Exception:
                continue
        remote = pull_ledger_from_xinhuo("*", status="ACTIVE")
    except Exception:
        pass

    now = timezone.now()
    if remote is not None:
        from apps.integrations.xinhuo_push import _dec

        entries: list[LedgerEntry] = []
        times: list[datetime] = []
        for r in remote.get("records") or []:
            st = str(r.get("status") or "").upper()
            if st != "ACTIVE":
                continue
            biz = str(r.get("business_name") or "").strip()
            if biz.upper() == "USDT":
                continue
            book = str(r.get("book") or "").strip().lower()
            if book == "usdt":
                continue
            occ_raw = str(r.get("occurred_at") or "")
            occ_dt = None
            if occ_raw:
                try:
                    occ_dt = parse_remote_datetime(occ_raw)
                    if occ_dt is not None:
                        times.append(occ_dt)
                except Exception:
                    pass
            entries.append(
                LedgerEntry(
                    business_name=str(r.get("business_name") or "")[:128],
                    receive_amount=_dec(r.get("receive_amount")),
                    amount_to_you=_dec(r.get("amount_to_you")),
                    amount_to_me=_dec(r.get("amount_to_me")),
                    profit_amount=_dec(r.get("profit_amount")),
                    occurred_at=occ_dt or now,
                )
            )
        if times:
            label = f"{fmt_date(min(times))} 至 {fmt_date(max(times))}"
        else:
            label = _range_label_from_entries(entries, now)
        return _format_query_message(range_label=label, entries=entries, query_when=now)

    # 本地兜底：全部未清账（不限群、不限当天）；不含 USDT
    qs = list(
        LedgerEntry.objects.filter(
            book=LEDGER_BOOK,
            status=LedgerEntry.Status.ACTIVE,
        )
        .exclude(business_name__iexact="USDT")
        .order_by("occurred_at", "id")
    )
    label = _range_label_from_entries(qs, now)
    return _format_query_message(range_label=label, entries=qs, query_when=now)


def clear_ledger(room, employee=None) -> str:
    """清账：全部未清账记录标记为已清账（跨群）。"""
    return clear_settlements(room, employee)


def clear_settlements(room, employee=None, *, event=None) -> str:
    """清账：全部未清账（ACTIVE）标记为已清账（跨群）。"""
    try:
        from apps.chatrooms.models import ChatRoom
        from apps.integrations.xinhuo_push import sync_room_ledger_from_xinhuo

        for r in ChatRoom.objects.filter(enabled=True):
            try:
                sync_room_ledger_from_xinhuo(r)
            except Exception:
                continue
    except Exception:
        pass
    qs = LedgerEntry.objects.filter(
        book=LEDGER_BOOK,
        status=LedgerEntry.Status.ACTIVE,
    ).exclude(business_name__iexact="USDT").order_by("occurred_at", "id")
    if not qs.exists():
        return ledger_error_reply("ℹ️ 没有可清账的记录")

    first = qs.first()
    last = qs.last()
    count = qs.count()
    projects = sorted(
        {
            (e.business_name or e.remark or "").strip() or "未命名"
            for e in qs
        }
    )
    if len(projects) == 1:
        range_text = (
            f"{projects[0]}（{fmt_date(first.occurred_at)} 至 {fmt_date(last.occurred_at)}）"
        )
    else:
        range_text = (
            f"全部项目（{fmt_date(first.occurred_at)} 至 {fmt_date(last.occurred_at)}）"
        )

    who = (employee.display_name if employee else "") or "操作人"
    when = timezone.now()
    qs.update(
        status=LedgerEntry.Status.PROCESSED,
        reverse_reason=f"清账 by {who}",
        reversed_at=when,
        updated_at=when,
    )
    try:
        from apps.integrations.xinhuo_push import schedule_push_settlement

        schedule_push_settlement(room, employee, event=event)
    except Exception:
        pass
    return ledger_clear_reply(
        operator_name=who,
        count=count,
        range_text=range_text,
        when=when,
    )


def ledger_command_aliases() -> dict[str, str]:
    """整句 → 指令名。裸「查询」已禁用，改用「查询记账」。"""
    from apps.rules.parser import normalize_text

    mapping = {
        "查询记账": "ledger_query",
        "查詢記賬": "ledger_query",
        "记账查询": "ledger_query",
        "記賬查詢": "ledger_query",
        "查询本月": "ledger_query_month",
        "查询上月": "ledger_query_last_month",
        "查询本周": "ledger_query_week",
        "清账": "settlement_clear",
        "清帳": "settlement_clear",
        "记账": "ledger_help",
        "記賬": "ledger_help",
    }
    return {normalize_text(k): v for k, v in mapping.items()}
