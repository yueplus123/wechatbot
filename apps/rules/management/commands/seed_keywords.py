from django.core.management.base import BaseCommand

from apps.ledger.models import LedgerEntry
from apps.ledger.staff_order_catalog import STAFF_BIZ_CATALOG
from apps.rules.models import Keyword

DEFAULT_KEYWORDS: list[tuple[str, str, list[str]]] = [
    (
        "记账",
        LedgerEntry.Book.LEDGER,
        [
            "记账",
            "充币金额214给你147利润67",
            "飞机号 金额50 利润10",
            "gpt代充 金额200 给你120 利润50",
        ],
    ),
    (
        "查询记账",
        LedgerEntry.Book.LEDGER,
        ["查询记账", "记账查询"],
    ),
    (
        "查询本月",
        LedgerEntry.Book.LEDGER,
        ["查询本月"],
    ),
    (
        "查询上月",
        LedgerEntry.Book.LEDGER,
        ["查询上月"],
    ),
    (
        "查询本周",
        LedgerEntry.Book.LEDGER,
        ["查询本周"],
    ),
    (
        "清账",
        LedgerEntry.Book.LEDGER,
        ["清账"],
    ),
    (
        "收入",
        LedgerEntry.Book.LEDGER,
        [
            "飞机号 收入50 利润10",
            "gpt代充 收入200 支出120 利润50",
        ],
    ),
    (
        "支出",
        LedgerEntry.Book.LEDGER,
        [
            "gpt代充 收入200 支出120 利润50",
        ],
    ),
    (
        "利润",
        LedgerEntry.Book.LEDGER,
        [
            "gpt普号 金额100 利润10",
            "利润25",
        ],
    ),
    (
        "金额",
        LedgerEntry.Book.LEDGER,
        [
            "飞机号 金额50 利润10",
            "gpt代充 金额200 给你120 利润50",
        ],
    ),
    (
        "给你",
        LedgerEntry.Book.LEDGER,
        [
            "gpt代充 金额200 给你120 利润50",
            "给你120",
        ],
    ),
    (
        "给我",
        LedgerEntry.Book.LEDGER,
        [
            "飞机号 给我50",
        ],
    ),
    (
        "usdt",
        LedgerEntry.Book.USDT,
        [
            "给我6740，2000刀",
            "给我2000刀",
            "查询usdt",
            "已转账",
            "清账usdt",
        ],
    ),
]


def _staff_usages_for_biz(biz) -> list[str]:
    """根据单/多 SKU 生成用法示例。"""
    samples: list[str] = [
        f"查询{biz.key}-张越",
        f"清空{biz.key}-张越",
    ]
    if biz.multi_sku:
        if biz.key == "高级会员":
            samples.extend(
                ["1y", "3y", "6y", "12y", "1y-1", "3y-1", "6y-1", "12y-1"]
            )
        for sku in biz.skus:
            samples.extend([f"{sku}+1", f"{sku}-1", f"{sku} 1"])
        if biz.skus:
            samples.append(f"查询{biz.skus[0]}-张越")
    else:
        samples.extend(
            [
                f"{biz.key}+1",
                f"{biz.key} 10",
                f"{biz.key}-5",
                f"{biz.key}增加10",
                f"{biz.key}减少5",
            ]
        )
        for a in biz.aliases:
            samples.extend([f"{a}+1", f"{a} 1", f"{a}-1"])
    return samples


for _biz in STAFF_BIZ_CATALOG:
    DEFAULT_KEYWORDS.append(
        (_biz.key, LedgerEntry.Book.STAFF_ORDER, _staff_usages_for_biz(_biz))
    )


def ensure_default_keywords(*, refresh: bool = False) -> tuple[int, int]:
    """写入默认关键词到关键词池。返回 (新增数, 更新数)。"""
    created = 0
    updated = 0
    # 旧裸「查询」关键词改为「查询记账」
    try:
        old = Keyword.objects.filter(keyword="查询", book=LedgerEntry.Book.LEDGER).first()
        if old and not Keyword.objects.filter(keyword="查询记账").exists():
            old.keyword = "查询记账"
            old.set_usage_list(["查询记账", "记账查询"])
            old.save()
            updated += 1
    except Exception:  # noqa: BLE001
        pass

    # 员工记单目录迁移 + usdt 迁记账本
    try:
        from apps.ledger.staff_order_catalog import LEGACY_BIZ_KEY_MAP
        from apps.rules.models import StaffBizRoomScope

        # 旧勾选 key → 新 key
        for old_key, new_key in LEGACY_BIZ_KEY_MAP.items():
            for scope in StaffBizRoomScope.objects.filter(biz_key=old_key):
                target, created = StaffBizRoomScope.objects.get_or_create(
                    chat_room_id=scope.chat_room_id,
                    biz_key=new_key,
                    defaults={"enabled": scope.enabled},
                )
                if not created and scope.enabled and not target.enabled:
                    target.enabled = True
                    target.save(update_fields=["enabled", "updated_at"])
                scope.delete()
                updated += 1

        # 历史记单：plus → gpt（规格 plus）；pro100刀/pro200刀 → gpt
        for e in LedgerEntry.objects.filter(
            book=LedgerEntry.Book.STAFF_ORDER, business_name="plus"
        ):
            e.business_name = "gpt"
            e.remark = "plus"
            e.save(update_fields=["business_name", "remark", "updated_at"])
            updated += 1
        # 曾误迁到 openrouter 且 remark=plus 的，改回 gpt
        for e in LedgerEntry.objects.filter(
            book=LedgerEntry.Book.STAFF_ORDER,
            business_name="openrouter",
            remark__iexact="plus",
        ):
            e.business_name = "gpt"
            e.remark = "plus"
            e.save(update_fields=["business_name", "remark", "updated_at"])
            updated += 1

        for sku in ("pro100刀", "pro200刀"):
            for e in LedgerEntry.objects.filter(
                book=LedgerEntry.Book.STAFF_ORDER, business_name=sku
            ):
                e.remark = (e.remark or "").strip() or sku
                e.business_name = "gpt"
                e.save(update_fields=["business_name", "remark", "updated_at"])
                updated += 1

        # 旧独立关键词清理/改名
        for old_kw, new_kw in (
            ("plus", "gpt"),
            ("pro100刀", "gpt"),
            ("pro200刀", "gpt"),
        ):
            old = Keyword.objects.filter(
                keyword=old_kw, book=LedgerEntry.Book.STAFF_ORDER
            ).first()
            if not old:
                continue
            if Keyword.objects.filter(
                keyword=new_kw, book=LedgerEntry.Book.STAFF_ORDER
            ).exists():
                old.delete()
            else:
                old.keyword = new_kw
                old.save(update_fields=["keyword", "updated_at"])
            updated += 1

        # 群勾选：旧 plus 独立 key → gpt
        for scope in StaffBizRoomScope.objects.filter(biz_key="plus"):
            target, created = StaffBizRoomScope.objects.get_or_create(
                chat_room=scope.chat_room,
                biz_key="gpt",
                defaults={"enabled": scope.enabled},
            )
            if not created and scope.enabled and not target.enabled:
                target.enabled = True
                target.save(update_fields=["enabled", "updated_at"])
            scope.delete()
            updated += 1

        StaffBizRoomScope.objects.filter(biz_key="pro").delete()
        Keyword.objects.filter(keyword="pro", book=LedgerEntry.Book.STAFF_ORDER).delete()

        # usdt：从员工记单迁到记账本
        StaffBizRoomScope.objects.filter(biz_key="usdt").delete()
        moved = LedgerEntry.objects.filter(
            book=LedgerEntry.Book.STAFF_ORDER, business_name__iexact="usdt"
        ).update(book=LedgerEntry.Book.USDT, business_name="USDT")
        if moved:
            updated += 1
        staff_usdt_kw = Keyword.objects.filter(
            keyword="usdt", book=LedgerEntry.Book.STAFF_ORDER
        ).first()
        if staff_usdt_kw:
            ledger_usdt = Keyword.objects.filter(
                keyword="usdt", book=LedgerEntry.Book.USDT
            ).first()
            if ledger_usdt and ledger_usdt.pk != staff_usdt_kw.pk:
                staff_usdt_kw.delete()
            else:
                staff_usdt_kw.book = LedgerEntry.Book.USDT
                staff_usdt_kw.set_usage_list(
                    [
                        "给我6740，2000刀",
                        "给我2000刀",
                        "查询usdt",
                        "已转账",
                        "清账usdt",
                    ]
                )
                staff_usdt_kw.save()
                updated += 1
    except Exception:  # noqa: BLE001
        pass

    for i, (kw, book, usages) in enumerate(DEFAULT_KEYWORDS):
        obj, was_created = Keyword.objects.get_or_create(
            keyword=kw,
            defaults={
                "book": book,
                "enabled": True,
                "sort_order": i * 10,
                "remark": usages[0] if usages else "",
                "usages": "\n".join(usages),
            },
        )
        if was_created:
            created += 1
            continue
        need = refresh or not (obj.usages or "").strip() or obj.book != book
        if need:
            obj.book = book
            obj.enabled = True
            obj.sort_order = i * 10
            obj.set_usage_list(usages)
            obj.save(
                update_fields=[
                    "book",
                    "enabled",
                    "sort_order",
                    "remark",
                    "usages",
                    "updated_at",
                ]
            )
            updated += 1
    return created, updated


class Command(BaseCommand):
    help = "初始化/刷新默认关键词池（含用法示例）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="覆盖已有关键词的账本与用法示例",
        )

    def handle(self, *args, **options):
        created, updated = ensure_default_keywords(refresh=options["refresh"])
        self.stdout.write(
            self.style.SUCCESS(f"关键词池就绪：新增 {created}，刷新用法 {updated}")
        )
