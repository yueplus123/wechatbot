"""把机器人本地员工记单 / USDT 补推到薪火「微信BOT数据」。

默认只推「尚未成功推送」的记录。
闪退重启后不要用本命令批量回补丢失推送——员工会重新发指令。
若确需人工抢救，请用 --id 指定单条，或加 --include-pushed。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.integrations.xinhuo_bridge import get_bridge
from apps.integrations.xinhuo_push import (
    BOT_PAGE_BOOKS,
    build_bot_record_payload,
    push_bot_record_to_xinhuo,
)
from apps.ledger.models import LedgerEntry


class Command(BaseCommand):
    help = "补推 staff_order / usdt 到薪火（默认跳过已推送；不在启动时自动执行）"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="只推送指定 LedgerEntry.id")
        parser.add_argument("--recent", type=int, default=0, help="推送最近 N 条（未推送的）")
        parser.add_argument("--hours", type=int, default=0, help="推送最近 N 小时内（未推送的）")
        parser.add_argument(
            "--include-pushed",
            action="store_true",
            help="包含已成功推送过的记录（一般不要用；可能造成薪火侧混乱）",
        )
        parser.add_argument("--dry-run", action="store_true", help="只打印，不 POST")

    def handle(self, *args, **options):
        bridge = get_bridge(refresh=True)
        url = bridge.url_for("bot_record_push")
        if not url or not bridge.api_key:
            self.stderr.write(self.style.ERROR("未配置 XINHUO_API_BASE / BOT_PUSH_URL 与 API Key"))
            return

        self.stdout.write(f"bot push url: {url}")
        self.stdout.write(
            "注意：闪退重启后请勿批量补推未推送旧账；员工会重新提交。"
        )

        qs = (
            LedgerEntry.objects.filter(book__in=sorted(BOT_PAGE_BOOKS))
            .select_related("event", "employee", "chat_room")
            .order_by("-id")
        )
        if not options.get("include_pushed"):
            qs = qs.filter(xinhuo_pushed_at__isnull=True)

        entry_id = options.get("id")
        recent = int(options.get("recent") or 0)
        hours = int(options.get("hours") or 0)
        dry = bool(options.get("dry_run"))

        if entry_id:
            qs = LedgerEntry.objects.filter(pk=entry_id).select_related(
                "event", "employee", "chat_room"
            )
        elif hours > 0:
            since = timezone.now() - timedelta(hours=hours)
            qs = qs.filter(occurred_at__gte=since)
        elif recent > 0:
            qs = qs[:recent]
        else:
            self.stderr.write(self.style.ERROR("请指定 --id / --recent / --hours"))
            return

        rows = list(qs)
        if not rows:
            self.stdout.write("没有匹配的未推送记单/USDT（或条件为空）")
            return

        ok = 0
        skipped = 0
        for entry in rows:
            if entry.xinhuo_pushed_at and not options.get("include_pushed") and not entry_id:
                skipped += 1
                continue
            # 单条 --id：若已推送且未加 --include-pushed，仍跳过，避免误操作
            if entry.xinhuo_pushed_at and not options.get("include_pushed"):
                self.stdout.write(
                    self.style.WARNING(
                        f"skip already pushed id={entry.id} at {entry.xinhuo_pushed_at}"
                    )
                )
                skipped += 1
                continue
            payload = build_bot_record_payload(entry)
            label = (
                f"id={entry.id} book={entry.book} biz={entry.business_name!r} "
                f"table={(payload or {}).get('table_type')} "
                f"event_id={(payload or {}).get('event_id')}"
            )
            if not payload:
                self.stderr.write(self.style.WARNING(f"skip {label}"))
                continue
            if dry:
                self.stdout.write(f"[dry] {label}")
                continue
            try:
                # --id + 已推送且 --include-pushed：临时清标记以允许再推
                if entry.xinhuo_pushed_at and options.get("include_pushed"):
                    LedgerEntry.objects.filter(pk=entry.pk).update(xinhuo_pushed_at=None)
                push_bot_record_to_xinhuo(int(entry.pk))
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"pushed {label}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"fail {label}: {e}"))

        if not dry:
            self.stdout.write(
                self.style.SUCCESS(f"done: {ok}/{len(rows)} (skipped_pushed={skipped})")
            )
