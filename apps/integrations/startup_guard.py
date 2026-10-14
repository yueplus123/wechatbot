"""闪退/重启后的防重保护：不补推旧账、不清空积压群回执以外的业务。"""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def discard_stale_reply_jobs(*, reason: str = "restart") -> int:
    """
    取消启动前积压的待发/租约中群回执。

    闪退后员工会重新发指令；若把旧的「记单成功」再发出去，会造成混乱。
    """
    from apps.ledger.models import ReplyJob

    qs = ReplyJob.objects.filter(
        status__in=[ReplyJob.Status.PENDING, ReplyJob.Status.LEASED]
    )
    n = qs.update(
        status=ReplyJob.Status.CANCELLED,
        last_error=f"discarded_on_{reason}"[:255],
        updated_at=timezone.now(),
    )
    if n:
        logger.info("discarded %s stale reply jobs on %s", n, reason)
    return int(n)


def mark_xinhuo_pushed(entry_id: int) -> None:
    from apps.ledger.models import LedgerEntry

    LedgerEntry.objects.filter(pk=entry_id).update(xinhuo_pushed_at=timezone.now())
