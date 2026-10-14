"""员工记单：内置业务目录（单 SKU / 多 SKU）。

单 SKU：触发词计入规范业务名，查询只显示业务名合计。
多 SKU：多个规格词归入同一业务，入库 remark=规格，查询按规格拆开。

记账 / USDT 不在本目录（走记账本）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class StaffBizDef:
    key: str
    """规范业务名（入库 business_name）。"""

    aliases: tuple[str, ...] = ()
    """单 SKU 额外触发词（仍计入本业务，查询不拆开）。"""

    skus: tuple[str, ...] = ()
    """多 SKU 规格触发词（入库 remark=规格，查询拆开）。"""

    @property
    def multi_sku(self) -> bool:
        return bool(self.skus)

    @property
    def label(self) -> str:
        if self.skus:
            preview = "、".join(self.skus[:4])
            more = f"…共{len(self.skus)}项" if len(self.skus) > 4 else ""
            return f"{self.key}｜多规格 {preview}{more}"
        if self.aliases:
            return f"{self.key}（{'、'.join(self.aliases)}）"
        return self.key

    @property
    def all_triggers(self) -> tuple[str, ...]:
        """解析用触发词。多 SKU 含规范名（供查询/清空）。"""
        if self.skus:
            return (self.key, *self.skus)
        return (self.key, *self.aliases)


_MEGA_DURATIONS = ("一个月", "三个月", "六个月", "一年")
_MEGA_SKUS: tuple[str, ...] = tuple(
    f"pro{tier}{dur}" for tier in (1, 2, 3) for dur in _MEGA_DURATIONS
)

# 高级会员时长简写：单独发 1y = 一个月 +1；1y-1 = 一个月 -1。3y/6y/12y 同理。
_PREMIUM_Y_SKU: dict[str, str] = {
    "1y": "一个月",
    "3y": "三个月",
    "6y": "六个月",
    "12y": "一年",
}


STAFF_BIZ_CATALOG: tuple[StaffBizDef, ...] = (
    # —— 单 SKU ——
    StaffBizDef("充币"),
    StaffBizDef("一次卡"),
    StaffBizDef("共享"),
    StaffBizDef("打包"),
    StaffBizDef("openrouter", ("op",)),
    StaffBizDef("patreon", ("pat",)),
    StaffBizDef("约稿"),
    StaffBizDef("booth"),
    StaffBizDef("pictspace", ("pict",)),
    # —— 多 SKU ——
    StaffBizDef(
        "高级会员",
        skus=("一个月", "三个月", "六个月", "一年", "会员"),
    ),
    StaffBizDef("mega", skus=_MEGA_SKUS),
    StaffBizDef("gpt", skus=("pro100刀", "pro200刀", "plus")),
)

STAFF_BIZ_KEYS: frozenset[str] = frozenset(b.key for b in STAFF_BIZ_CATALOG)

# 旧 biz_key / 独立业务名 → 新规范名
LEGACY_BIZ_KEY_MAP: dict[str, str] = {
    "plus": "gpt",
    "pro100刀": "gpt",
    "pro200刀": "gpt",
    "会员": "高级会员",
}


def premium_y_sku(trigger: str) -> str | None:
    """1y/3y/6y/12y → 一个月/三个月/六个月/一年。大小写不敏感。"""
    return _PREMIUM_Y_SKU.get((trigger or "").strip().casefold())


def is_premium_implicit_plus(trigger: str) -> bool:
    """单独发该词即表示 +1。"""
    return premium_y_sku(trigger) is not None


def catalog_by_key() -> dict[str, StaffBizDef]:
    return {b.key: b for b in STAFF_BIZ_CATALOG}


def is_multi_sku(business_name: str) -> bool:
    biz = catalog_by_key().get((business_name or "").strip())
    return bool(biz and biz.multi_sku)


def sku_order(business_name: str) -> tuple[str, ...]:
    biz = catalog_by_key().get((business_name or "").strip())
    if biz and biz.skus:
        return biz.skus
    return ()


def normalize_sku_remark(business_name: str, trigger: str) -> str:
    """入库 remark：多 SKU 用规格词；单 SKU 用规范业务名（查询不拆开）。"""
    biz_name = (business_name or "").strip()
    trig = (trigger or "").strip()
    biz = catalog_by_key().get(biz_name)
    if not biz:
        return (trig or biz_name)[:64]

    if not biz.multi_sku:
        return biz.key[:64]

    # mega：去掉空格，统一 pro1一个月
    if biz_name == "mega":
        compact = re.sub(r"\s+", "", trig)
        if compact.lower().startswith("pro"):
            # 保留 pro 后数字大小写：统一 pro
            m = re.fullmatch(
                r"(?i)pro([123])(一个月|三个月|六个月|一年)",
                compact,
            )
            if m:
                return f"pro{m.group(1)}{m.group(2)}"
        if compact == "mega" or not compact:
            return "mega"
        return compact[:64]

    # gpt：规范规格大小写；plus 为独立规格
    if biz_name == "gpt":
        low = trig.lower().replace(" ", "")
        if low == "plus":
            return "plus"
        if "100" in low:
            return "pro100刀"
        if "200" in low:
            return "pro200刀"
        if trig in biz.skus:
            return trig
        return (trig or "gpt")[:64]

    # 高级会员：1y/3y/6y/12y 记入对应时长；用规范名记单则 remark=高级会员
    if biz_name == "高级会员":
        mapped = premium_y_sku(trig)
        if mapped:
            return mapped
    if trig == biz.key or not trig:
        return biz.key
    if trig in biz.skus:
        return trig
    return trig[:64]


def expand_triggers(enabled_keys: set[str] | list[str] | frozenset[str]) -> dict[str, str]:
    """启用的规范名 → {触发词: 规范业务名}。含 mega 空格写法、ASCII 小写。"""
    wanted = {str(k).strip() for k in enabled_keys if str(k).strip()}
    mapped: set[str] = set()
    for k in wanted:
        mapped.add(LEGACY_BIZ_KEY_MAP.get(k, k))

    out: dict[str, str] = {}

    def _put(trigger: str, canonical: str) -> None:
        if not trigger:
            return
        out[trigger] = canonical
        low = trigger.lower()
        if low != trigger and any(ch.isascii() and ch.isalpha() for ch in trigger):
            out[low] = canonical

    for biz in STAFF_BIZ_CATALOG:
        if biz.key not in mapped:
            continue
        for t in biz.all_triggers:
            _put(t, biz.key)
        if biz.key == "高级会员":
            for alias in _PREMIUM_Y_SKU:
                _put(alias, biz.key)
        # mega：允许 pro1 一个月（中间空格）
        if biz.key == "mega":
            for tier in (1, 2, 3):
                for dur in _MEGA_DURATIONS:
                    _put(f"pro{tier} {dur}", biz.key)
        # gpt：兼容大小写混写
        if biz.key == "gpt":
            for sku in biz.skus:
                _put(sku.lower(), biz.key)
                _put(sku.replace("刀", "刀"), biz.key)

    return out


def resolve_canonical(trigger: str, trigger_map: dict[str, str]) -> str | None:
    t = (trigger or "").strip()
    if not t:
        return None
    if t in trigger_map:
        return trigger_map[t]
    low = t.lower()
    if low in trigger_map:
        return trigger_map[low]
    # mega 去空格再试
    compact = re.sub(r"\s+", "", t)
    if compact != t and compact in trigger_map:
        return trigger_map[compact]
    return None
