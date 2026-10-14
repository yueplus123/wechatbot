"""
记账 / 员工记单指令解析（严格模式，不用 AI）。

记账入账：业务名 金额N 给你N 给我N 利润N（至少写一个，互不计算）
员工记单：按群绑定的业务关键词增减 / 查询 / 清空
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class Direction(str, Enum):
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class ParseStatus(str, Enum):
    IGNORED = "ignored"
    OK = "ok"
    ERROR = "error"


BARE_QUERY_HINT = (
    "不能只发「查询」。请使用「查询记账」；记单请管理员发「查询-姓名」（如：查询-李振宇）。"
)


@dataclass(frozen=True)
class ParseResult:
    status: ParseStatus
    error_code: str = ""
    error_message: str = ""
    command: str = ""
    direction: Direction | None = None
    amount: Decimal | None = None
    currency: str = "CNY"
    category: str = ""
    remark: str = ""
    book: str = ""
    rule_version: str = "1"
    normalized_text: str = ""
    business_name: str = ""
    receive_amount: Decimal | None = None  # 金额
    amount_to_you: Decimal | None = None  # 给你
    amount_to_me: Decimal | None = None  # 给我
    profit_amount: Decimal | None = None  # 利润（手填，可负）


def normalize_text(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = s.replace("\u3000", " ")
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def parse_command(
    text: str,
    *,
    trigger_prefix: str = "记账",
    default_currency: str = "CNY",
    categories=None,
    keyword_books=None,
    staff_biz_map=None,
    rule_version: str = "1",
    **_,
) -> ParseResult:
    """解析群内指令。staff_biz_map: 触发词 → 规范业务名（仅本群已绑定）。"""
    _ = categories, keyword_books
    normalized = normalize_text(text)
    prefix = (trigger_prefix or "记账").strip()
    default_ccy = (default_currency or "CNY").upper()

    if normalized in {"查询", "查詢"}:
        return _err("E001", BARE_QUERY_HINT, normalized, rule_version)

    # 查商品（薪火平台商品目录，仅平台名+商品名）
    prod_hit = _parse_product_query(normalized, rule_version=rule_version)
    if prod_hit is not None:
        return prod_hit

    # USDT 优先，避免「查询usdt」被员工记单拦截
    usdt_hit = _parse_ledger_usdt(normalized, rule_version=rule_version)
    if usdt_hit is not None:
        return usdt_hit

    # 管理员按员工查记单：查询-李振宇
    emp_hit = _parse_staff_employee_query(normalized, rule_version=rule_version)
    if emp_hit is not None:
        return emp_hit

    if staff_biz_map:
        staff_hit = _parse_staff_order(
            normalized,
            staff_biz_map=staff_biz_map,
            default_currency=default_ccy,
            rule_version=rule_version,
        )
        if staff_hit is not None:
            return staff_hit

    # 整句别名优先（含「查询记账」「记账查询」），避免被前缀「记账」拆开
    hit = _parse_ledger_special(normalized, rule_version=rule_version)
    if hit is not None:
        return hit

    if not normalized.startswith(prefix):
        entry = _parse_ledger_entry(
            normalized, default_currency=default_ccy, rule_version=rule_version
        )
        if entry is not None:
            return entry
        return ParseResult(status=ParseStatus.IGNORED, normalized_text=normalized)

    body = normalized[len(prefix) :].strip()
    # 机器人回执「记账成功…」被听群再次扫到时，不要当成指令
    if body.startswith("成功"):
        return ParseResult(status=ParseStatus.IGNORED, normalized_text=normalized)
    if not body or body in {"帮助", "help", "HELP"}:
        return ParseResult(
            status=ParseStatus.OK,
            command="ledger_help",
            book="ledger",
            rule_version=rule_version,
            normalized_text=normalized,
        )

    if body in {"查询", "查詢"}:
        return _err("E001", BARE_QUERY_HINT, normalized, rule_version)

    if body in {"清账", "清帳"}:
        return ParseResult(
            status=ParseStatus.OK,
            command="settlement_clear",
            book="ledger",
            rule_version=rule_version,
            normalized_text=normalized,
        )

    hit = _parse_ledger_special(
        body, rule_version=rule_version, normalized_override=normalized
    )
    if hit is not None:
        return hit

    usdt_hit = _parse_ledger_usdt(
        body, rule_version=rule_version, normalized_override=normalized
    )
    if usdt_hit is not None:
        return usdt_hit

    entry = _parse_ledger_entry(
        body,
        default_currency=default_ccy,
        rule_version=rule_version,
        normalized_override=normalized,
    )
    if entry is not None:
        return entry

    return _err(
        "E001",
        "格式不正确。发「记账」查看指令说明。",
        normalized,
        rule_version,
    )


def _parse_product_query(text: str, *, rule_version: str) -> ParseResult | None:
    """查商品 / 查商品 关键词 / 查询商品 xxx。"""
    m = re.fullmatch(r"(?:查商品|查询商品|商品查询)\s*(.*)", text, flags=re.IGNORECASE)
    if not m:
        return None
    q = (m.group(1) or "").strip()
    return ParseResult(
        status=ParseStatus.OK,
        command="product_query",
        remark=q,
        book="",
        rule_version=rule_version,
        normalized_text=text,
    )


def _parse_staff_employee_query(
    text: str,
    *,
    rule_version: str,
) -> ParseResult | None:
    """查询-李振宇 → 管理员按统一备注名查该员工全部记单。"""
    m = re.fullmatch(r"查询\s*[-－—]\s*(.+)", text)
    if not m:
        return None
    name = (m.group(1) or "").strip()
    if not name:
        return _err(
            "E001",
            "请写员工备注名。例：查询-李振宇",
            text,
            rule_version,
        )
    return ParseResult(
        status=ParseStatus.OK,
        command="staff_order_query_employee",
        book="staff_order",
        remark=name[:64],
        rule_version=rule_version,
        normalized_text=text,
    )


def _parse_staff_order(
    text: str,
    *,
    staff_biz_map: dict[str, str],
    default_currency: str,
    rule_version: str,
) -> ParseResult | None:
    """按最长触发词匹配：清空 / 查询 / 增减。"""
    if not text or not staff_biz_map:
        return None

    from apps.ledger.staff_order_catalog import (
        is_premium_implicit_plus,
        normalize_sku_remark,
    )

    triggers = sorted(staff_biz_map.keys(), key=len, reverse=True)
    for trig in triggers:
        if not trig:
            continue
        canonical = staff_biz_map[trig]
        esc = re.escape(trig)
        sku_remark = normalize_sku_remark(canonical, trig)

        # 清空业务-姓名（必填员工，避免误清全员）
        m_clear = re.fullmatch(
            rf"(?:清空\s*{esc}|{esc}\s*清空)\s*[-－—]\s*(.+)",
            text,
            flags=re.IGNORECASE,
        )
        if m_clear:
            target = (m_clear.group(1) or "").strip()
            if not target:
                return _err(
                    "E001",
                    f"请指定员工备注名。例：清空{canonical}-张越",
                    text,
                    rule_version,
                )
            return ParseResult(
                status=ParseStatus.OK,
                command="staff_order_clear",
                book="staff_order",
                business_name=canonical,
                remark=target[:64],
                rule_version=rule_version,
                normalized_text=text,
            )

        # 仅「清空业务」→ 提示必须带员工
        if re.fullmatch(rf"清空\s*{esc}", text, flags=re.IGNORECASE) or re.fullmatch(
            rf"{esc}\s*清空", text, flags=re.IGNORECASE
        ):
            return _err(
                "E001",
                f"清空需指定员工，避免清掉全员。例：清空{canonical}-张越",
                text,
                rule_version,
            )

        # 查询业务-姓名（必填员工）
        m_query = re.fullmatch(
            rf"(?:查询\s*{esc}|{esc}\s*查询)\s*[-－—]\s*(.+)",
            text,
            flags=re.IGNORECASE,
        )
        if m_query:
            target = (m_query.group(1) or "").strip()
            if not target:
                return _err(
                    "E001",
                    f"请指定员工备注名。例：查询{canonical}-张越",
                    text,
                    rule_version,
                )
            return ParseResult(
                status=ParseStatus.OK,
                command="staff_order_query",
                book="staff_order",
                business_name=canonical,
                remark=target[:64],
                rule_version=rule_version,
                normalized_text=text,
            )

        # 仅「查询业务」→ 提示必须带员工
        if re.fullmatch(rf"查询\s*{esc}", text, flags=re.IGNORECASE) or re.fullmatch(
            rf"{esc}\s*查询", text, flags=re.IGNORECASE
        ):
            return _err(
                "E001",
                f"查询需指定员工。例：查询{canonical}-张越；查全部业务请发：查询-张越",
                text,
                rule_version,
            )

        # 中文增减
        m = re.fullmatch(
            rf"{esc}\s*(增加|减少)\s*(\d+(?:\.\d{{1,2}})?)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            amt = _parse_staff_amount(m.group(2))
            if amt is None:
                return _err(
                    "E002",
                    "数量须为大于 0 的数字，最多两位小数。",
                    text,
                    rule_version,
                )
            direction = (
                Direction.INCOME if m.group(1) == "增加" else Direction.EXPENSE
            )
            return ParseResult(
                status=ParseStatus.OK,
                command="staff_order_add",
                direction=direction,
                amount=amt,
                currency=default_currency,
                category="其他",
                book="staff_order",
                business_name=canonical,
                remark=sku_remark[:64],
                rule_version=rule_version,
                normalized_text=text,
            )

        # 空格 / 正负号数量：充币 10 / 充币+10 / 充币-5
        m = re.fullmatch(
            rf"{esc}\s*([+-]?)(\d+(?:\.\d{{1,2}})?)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            amt = _parse_staff_amount(m.group(2))
            if amt is None:
                return _err(
                    "E002",
                    "数量须为大于 0 的数字，最多两位小数。",
                    text,
                    rule_version,
                )
            sign = m.group(1) or "+"
            direction = Direction.INCOME if sign != "-" else Direction.EXPENSE
            return ParseResult(
                status=ParseStatus.OK,
                command="staff_order_add",
                direction=direction,
                amount=amt,
                currency=default_currency,
                category="其他",
                book="staff_order",
                business_name=canonical,
                remark=sku_remark[:64],
                rule_version=rule_version,
                normalized_text=text,
            )

        # 高级会员简写：单独发 1y/3y/6y/12y 即 +1
        if is_premium_implicit_plus(trig) and re.fullmatch(
            esc, text, flags=re.IGNORECASE
        ):
            return ParseResult(
                status=ParseStatus.OK,
                command="staff_order_add",
                direction=Direction.INCOME,
                amount=Decimal("1.00"),
                currency=default_currency,
                category="其他",
                book="staff_order",
                business_name=canonical,
                remark=sku_remark[:64],
                rule_version=rule_version,
                normalized_text=text,
            )

    return None


def _parse_staff_amount(token: str) -> Decimal | None:
    if not token or "," in token or "e" in token.lower():
        return None
    if not re.fullmatch(r"\d+(\.\d{1,2})?", token):
        return None
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_ledger_special(
    text: str,
    *,
    rule_version: str,
    normalized_override: str | None = None,
) -> ParseResult | None:
    from apps.ledger.ledger_ops import ledger_command_aliases

    normalized = normalize_text(text)
    display = normalized_override or normalized
    hit = ledger_command_aliases().get(normalized)
    if hit in {
        "ledger_query",
        "settlement_clear",
        "ledger_help",
        "ledger_query_month",
        "ledger_query_last_month",
        "ledger_query_week",
    }:
        return ParseResult(
            status=ParseStatus.OK,
            command=hit,
            book="ledger",
            rule_version=rule_version,
            normalized_text=display,
        )
    return None


# 主标签 + 兼容别名；金额/给你/给我/利润各自独立保存
_TAG_ALIASES = {
    "金额": "金额",
    "收入": "金额",
    "给你": "给你",
    "支出": "给你",
    "给我": "给我",
    "利润": "利润",
}


def _parse_ledger_usdt(
    text: str,
    *,
    rule_version: str,
    normalized_override: str | None = None,
) -> ParseResult | None:
    """USDT：给我欠款，刀数 ／ 查询usdt ／ 已转账。"""
    normalized = normalize_text(text)
    display = normalized_override or normalized
    if not normalized:
        return None

    if normalized in {"已转账", "已轉賬"}:
        return ParseResult(
            status=ParseStatus.OK,
            command="usdt_settlement_clear",
            book="usdt",
            business_name="USDT",
            remark="USDT",
            rule_version=rule_version,
            normalized_text=display,
        )

    if re.fullmatch(r"(查询|查詢)\s*usdt", normalized, flags=re.IGNORECASE) or re.fullmatch(
        r"usdt\s*(查询|查詢)", normalized, flags=re.IGNORECASE
    ):
        return ParseResult(
            status=ParseStatus.OK,
            command="usdt_query",
            book="usdt",
            business_name="USDT",
            remark="USDT",
            rule_version=rule_version,
            normalized_text=display,
        )

    # 兼容旧清账口令
    if re.fullmatch(r"(清账|清帳|清空)\s*usdt", normalized, flags=re.IGNORECASE) or re.fullmatch(
        r"usdt\s*(清账|清帳|清空)", normalized, flags=re.IGNORECASE
    ):
        return ParseResult(
            status=ParseStatus.OK,
            command="usdt_settlement_clear",
            book="usdt",
            business_name="USDT",
            remark="USDT",
            rule_version=rule_version,
            normalized_text=display,
        )

    # 给我6740，2000刀 → 欠款 6740 元，充值 2000 美金
    m = re.fullmatch(
        r"(给我|usdt)\s*(\d+(?:\.\d{1,2})?)\s*[，,]\s*([+-]?)(\d+(?:\.\d{1,2})?)\s*刀",
        normalized,
        flags=re.IGNORECASE,
    )
    if m:
        debt = _parse_staff_amount(m.group(2))
        amt = _parse_staff_amount(m.group(4))
        if debt is None or amt is None:
            return _err(
                "E002",
                "格式：给我6740，2000刀（前面是欠款元，后面是美金刀数）",
                display,
                rule_version,
            )
        sign = m.group(3) or "+"
        direction = Direction.INCOME if sign != "-" else Direction.EXPENSE
        usd = amt.quantize(Decimal("0.01"))
        return ParseResult(
            status=ParseStatus.OK,
            command="ledger",
            direction=direction,
            amount=usd,
            currency="USD",
            category="其他",
            remark=f"欠款{debt}",
            book="usdt",
            rule_version=rule_version,
            normalized_text=display,
            business_name="USDT",
            receive_amount=usd if direction == Direction.INCOME else Decimal("0.00"),
            amount_to_you=usd if direction == Direction.EXPENSE else Decimal("0.00"),
            amount_to_me=debt.quantize(Decimal("0.01")),
            profit_amount=Decimal("0.00"),
        )

    # 给我2000刀 → 仅美金，欠款 0
    m = re.fullmatch(
        r"(给我|usdt)\s*([+-]?)(\d+(?:\.\d{1,2})?)\s*刀",
        normalized,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    amt = _parse_staff_amount(m.group(3))
    if amt is None:
        return _err(
            "E002",
            "格式：给我6740，2000刀（前面是欠款元，后面是美金刀数）",
            display,
            rule_version,
        )
    sign = m.group(2) or "+"
    direction = Direction.INCOME if sign != "-" else Direction.EXPENSE
    usd = amt.quantize(Decimal("0.01"))
    return ParseResult(
        status=ParseStatus.OK,
        command="ledger",
        direction=direction,
        amount=usd,
        currency="USD",
        category="其他",
        remark="USDT",
        book="usdt",
        rule_version=rule_version,
        normalized_text=display,
        business_name="USDT",
        receive_amount=usd if direction == Direction.INCOME else Decimal("0.00"),
        amount_to_you=usd if direction == Direction.EXPENSE else Decimal("0.00"),
        amount_to_me=Decimal("0.00"),
        profit_amount=Decimal("0.00"),
    )


def _parse_ledger_entry(
    text: str,
    *,
    default_currency: str,
    rule_version: str,
    normalized_override: str | None = None,
) -> ParseResult | None:
    """
    业务名 金额N 给你N 给我N 利润N
    - 金额 / 给你 / 给我 / 利润 各自独立，互不计算
    - 至少写一个标签；未写的字段记为 0
    - 兼容：收入→金额，支出→给你
    """
    normalized = normalize_text(text)
    display = normalized_override or normalized
    if not normalized:
        return None

    tag_re = re.compile(
        r"(金额|收入|给你|支出|给我|利润)\s*(-?\d+(?:\.\d{1,2})?)"
    )
    matches = list(tag_re.finditer(normalized))
    if not matches:
        return None

    business = normalized[: matches[0].start()].strip()
    trailing = normalized[matches[-1].end() :].strip()
    fmt_hint = (
        "记账格式：业务名 金额N 给你N 给我N 利润N（至少写一个）。"
        "例：充币金额214给你147利润67"
    )
    if trailing:
        return _err("E001", fmt_hint, display, rule_version)
    between = normalized[matches[0].start() : matches[-1].end()]
    if tag_re.sub(" ", between).strip():
        return _err("E001", fmt_hint, display, rule_version)
    if not business:
        return _err(
            "E001",
            "请写业务名，例如：充币金额214给你147利润67",
            display,
            rule_version,
        )

    fields: dict[str, Decimal] = {}
    for m in matches:
        raw_key = m.group(1)
        key = _TAG_ALIASES.get(raw_key, raw_key)
        token = m.group(2)
        if key == "利润":
            val = _parse_profit(token)
            if val is None:
                return _err(
                    "E002",
                    "利润须为数字（可为零或负数），最多两位小数。",
                    display,
                    rule_version,
                )
        else:
            val = _parse_nonneg_amount(token)
            if val is None:
                return _err(
                    "E002",
                    "金额/给你/给我须为 ≥0 的数字，最多两位小数。",
                    display,
                    rule_version,
                )
        fields[key] = (fields.get(key) or Decimal("0.00")) + val

    receive = fields.get("金额", Decimal("0.00"))
    to_you = fields.get("给你", Decimal("0.00"))
    to_me = fields.get("给我", Decimal("0.00"))
    profit = fields.get("利润", Decimal("0.00"))

    if receive == 0 and to_you == 0 and to_me == 0 and "利润" not in fields:
        return _err("E001", fmt_hint, display, rule_version)

    primary = receive if receive != 0 else (
        to_me if to_me != 0 else (to_you if to_you != 0 else abs(profit))
    )
    direction = (
        Direction.INCOME
        if (receive + to_me) >= to_you
        else Direction.EXPENSE
    )

    return ParseResult(
        status=ParseStatus.OK,
        command="ledger",
        direction=direction,
        amount=primary.quantize(Decimal("0.01")),
        currency=default_currency or "CNY",
        category="其他",
        remark=business,
        book="ledger",
        rule_version=rule_version,
        normalized_text=display,
        business_name=business[:128],
        receive_amount=receive.quantize(Decimal("0.01")),
        amount_to_you=to_you.quantize(Decimal("0.01")),
        amount_to_me=to_me.quantize(Decimal("0.01")),
        profit_amount=profit.quantize(Decimal("0.01")),
    )


def _parse_nonneg_amount(token: str) -> Decimal | None:
    """金额/给你/给我：≥ 0。"""
    if not token or "," in token or "e" in token.lower():
        return None
    if token.startswith("-"):
        return None
    if not re.fullmatch(r"\d+(\.\d{1,2})?", token):
        return None
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_amount(token: str) -> Decimal | None:
    """兼容旧调用：必须 > 0。"""
    value = _parse_nonneg_amount(token)
    if value is None or value <= 0:
        return None
    return value


def _parse_profit(token: str) -> Decimal | None:
    """利润：可为零或负数。"""
    if not token or "," in token or "e" in token.lower():
        return None
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", token):
        return None
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


def _err(code: str, message: str, normalized: str, rule_version: str) -> ParseResult:
    return ParseResult(
        status=ParseStatus.ERROR,
        error_code=code,
        error_message=message,
        rule_version=rule_version,
        normalized_text=normalized,
    )
