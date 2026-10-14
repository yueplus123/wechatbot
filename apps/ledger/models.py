from django.db import models

from apps.chatrooms.models import ChatRoom, Employee
from apps.rules.models import Category


class MessageEvent(models.Model):
    class ParseStatus(models.TextChoices):
        PENDING = "pending", "待处理"
        IGNORED = "ignored", "忽略"
        ACCEPTED = "accepted", "已接受"
        DUPLICATE = "duplicate", "重复"
        REJECTED = "rejected", "拒绝"
        ERROR = "error", "错误"

    event_uuid = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    chat_room = models.ForeignKey(
        ChatRoom, null=True, blank=True, on_delete=models.SET_NULL
    )
    chat_name = models.CharField(max_length=128)
    sender_display_name = models.CharField(max_length=128)
    message_type = models.CharField(max_length=32, default="text")
    raw_text = models.TextField()
    normalized_text = models.TextField(blank=True, default="")
    received_at = models.DateTimeField()
    client_observed_at = models.DateTimeField(null=True, blank=True)
    adapter = models.CharField(max_length=32, blank=True, default="")
    wechat_version = models.CharField(max_length=64, blank=True, default="")
    parse_status = models.CharField(
        max_length=16, choices=ParseStatus.choices, default=ParseStatus.PENDING
    )
    error_code = models.CharField(max_length=16, blank=True, default="")
    error_message = models.CharField(max_length=255, blank=True, default="")
    rule_version = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "消息事件"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["received_at"]),
            models.Index(fields=["chat_name", "sender_display_name"]),
        ]


class LedgerEntry(models.Model):
    class Direction(models.TextChoices):
        INCOME = "INCOME", "收入"
        EXPENSE = "EXPENSE", "支出"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "有效"
        REVERSED = "REVERSED", "已撤销"
        PROCESSED = "PROCESSED", "已清账"

    class Book(models.TextChoices):
        LEDGER = "ledger", "记账表"
        USDT = "usdt", "USDT"
        STAFF_ORDER = "staff_order", "员工记单"

    record_no = models.CharField(max_length=48, unique=True)
    event = models.OneToOneField(
        MessageEvent, on_delete=models.PROTECT, related_name="ledger_entry"
    )
    book = models.CharField(
        "账本",
        max_length=32,
        choices=Book.choices,
        default=Book.LEDGER,
        db_index=True,
    )
    direction = models.CharField(max_length=16, choices=Direction.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=4, default="CNY")
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="entries"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="entries"
    )
    chat_room = models.ForeignKey(
        ChatRoom, on_delete=models.PROTECT, related_name="entries"
    )
    remark = models.CharField(max_length=200, blank=True, default="")
    # 记账表专用：项目名 / 金额 / 给你 / 给我 / 利润（消息里未写的填 0）
    business_name = models.CharField(
        "项目", max_length=128, blank=True, default=""
    )
    receive_amount = models.DecimalField(
        "金额", max_digits=18, decimal_places=2, default=0
    )
    amount_to_you = models.DecimalField(
        "给你", max_digits=18, decimal_places=2, default=0
    )
    amount_to_me = models.DecimalField(
        "给我", max_digits=18, decimal_places=2, default=0
    )
    profit_amount = models.DecimalField(
        "利润", max_digits=18, decimal_places=2, default=0
    )
    occurred_at = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversed_entries",
    )
    reverse_reason = models.CharField(max_length=255, blank=True, default="")
    # 薪火推送成功时间；空=尚未成功推过。闪退重启后不得自动补推未推记录。
    xinhuo_pushed_at = models.DateTimeField(
        "薪火已推送于", null=True, blank=True, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "账目"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["occurred_at"]),
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["chat_room", "direction", "currency"]),
            models.Index(fields=["book", "status", "-id"]),
        ]

    @property
    def book_label(self) -> str:
        return self.get_book_display()

    @property
    def profit(self):
        """记账表利润（消息/面板写入的利润字段）。"""
        return self.profit_amount or 0


class ReplyJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待发送"
        LEASED = "leased", "已租约"
        SENT = "sent", "已发送"
        FAILED = "failed", "失败"
        UNKNOWN = "unknown", "结果未知"
        CANCELLED = "cancelled", "已取消"

    event = models.ForeignKey(
        MessageEvent, on_delete=models.CASCADE, related_name="reply_jobs"
    )
    chat_name = models.CharField(max_length=128)
    body = models.TextField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    leased_by = models.CharField(max_length=64, blank=True, default="")
    leased_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "回复任务"
        verbose_name_plural = verbose_name
        indexes = [models.Index(fields=["status", "next_retry_at"])]
