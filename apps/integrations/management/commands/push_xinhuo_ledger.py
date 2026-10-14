"""把机器人本地记账补推到薪火 LedgerRecord。

默认只推尚未成功推送的记录。闪退重启后不要批量回补——员工会重新提交。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.integrations.xinhuo_push import _push_url, build_ledger_payload, push_ledger_entry_to_xinhuo
from apps.ledger.models import LedgerEntry


class Command(BaseCommand):
    help = "补推 ledger 到薪火（默认跳过已推送；不在启动时自动执行）"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="只推送指定 LedgerEntry.id")
        parser.add_argument("--recent", type=int, default=0, help="推送最近 N 条（未推送的）")
        parser.add_argument("--hours", type=int, default=0, help="推送最近 N 小时内（未推送的）")
        parser.add_argument(
            "--include-pushed",
            action="store_true",
            help="包含已成功推送过的记录（一般不要用）",
        )
        parser.add_argument("--dry-run", action="store_true", help="只打印将要推送的条目，不实际 POST")

    def handle(self, *args, **options):
        url = _push_url()
        if not url:
            self.stderr.write(self.style.ERROR("未配置 XINHUO_API_BASE / 能力 URL 与 API Key"))
            return

        self.stdout.write(f"push url: {url}")
        self.stdout.write("注意：闪退重启后请勿批量补推未推送旧账。")

        qs = (
            LedgerEntry.objects.filter(book=LedgerEntry.Book.LEDGER)
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
            qs = LedgerEntry.objects.filter(pk=entry_id, book=LedgerEntry.Book.LEDGER).select_related(
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
            self.stdout.write("没有匹配的未推送记录")
            return

        ok = 0
        for entry in rows:
            if entry.xinhuo_pushed_at and not options.get("include_pushed"):
                self.stdout.write(
                    self.style.WARNING(
                        f"skip already pushed id={entry.id} at {entry.xinhuo_pushed_at}"
                    )
                )
                continue
            payload = build_ledger_payload(entry)
            label = (
                f"id={entry.id} project={entry.business_name!r} "
                f"amount={entry.receive_amount or entry.amount} "
                f"event_id={(payload or {}).get('event_id')}"
            )
            if dry:
                self.stdout.write(f"[dry] {label}")
                continue
            try:
                if entry.xinhuo_pushed_at and options.get("include_pushed"):
                    LedgerEntry.objects.filter(pk=entry.pk).update(xinhuo_pushed_at=None)
                push_ledger_entry_to_xinhuo(int(entry.pk))
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"pushed {label}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"fail {label}: {e}"))

        if not dry:
            self.stdout.write(self.style.SUCCESS(f"done: {ok}/{len(rows)}"))
