from django.db import models


class Category(models.Model):
    class Type(models.TextChoices):
        INCOME = "INCOME", "收入"
        EXPENSE = "EXPENSE", "支出"

    type = models.CharField(max_length=16, choices=Type.choices)
    name = models.CharField(max_length=64)
    aliases = models.CharField(
        "别名(逗号分隔)", max_length=255, blank=True, default=""
    )
    enabled = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "分类"
        verbose_name_plural = verbose_name
        unique_together = [("type", "name")]

    def __str__(self) -> str:
        return f"{self.type}:{self.name}"

    def alias_list(self) -> list[str]:
        return [a.strip() for a in self.aliases.split(",") if a.strip()]


class KeywordRule(models.Model):
    """旧版规则表（兼容 Admin）；面板关键词池请用 Keyword。"""

    class RuleType(models.TextChoices):
        TRIGGER = "trigger", "触发前缀"
        DIRECTION = "direction", "收支别名"
        COMMAND = "command", "指令别名"

    rule_type = models.CharField(max_length=16, choices=RuleType.choices)
    keyword = models.CharField(max_length=64)
    target = models.CharField(max_length=64, blank=True, default="")
    priority = models.IntegerField(default=100)
    enabled = models.BooleanField(default=True)
    version = models.CharField(max_length=16, default="1")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "关键词规则"
        verbose_name_plural = verbose_name
        ordering = ["priority", "id"]


class Keyword(models.Model):
    """面板可维护的关键词池：命中后可路由到指定账本。"""

    keyword = models.CharField("关键词", max_length=64, unique=True)
    remark = models.CharField("备注", max_length=128, blank=True, default="")
    # 多种用法，一行一条，也可用 ／ 分隔。例：
    # 充币5000
    # 充币 5000
    # 查询充币
    # 充币查询
    usages = models.TextField("用法列表", blank=True, default="")
    # 空=不改路由；填写 LedgerEntry.Book 值
    book = models.CharField("归属账本", max_length=32, blank=True, default="")
    enabled = models.BooleanField("启用", default=True)
    sort_order = models.IntegerField("排序", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "关键词"
        verbose_name_plural = verbose_name
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.keyword

    @property
    def book_label(self) -> str:
        if not self.book:
            return "未指定"
        from apps.ledger.books import BOOK_LABELS

        return BOOK_LABELS.get(self.book, self.book)

    def usage_list(self) -> list[str]:
        raw = (self.usages or "").strip() or (self.remark or "").strip()
        if not raw:
            return []
        parts: list[str] = []
        for chunk in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            for piece in chunk.replace("／", "|").replace("、", "|").split("|"):
                s = piece.strip()
                if s and s not in parts:
                    parts.append(s)
        return parts

    def usages_display(self) -> str:
        return " ／ ".join(self.usage_list())

    def set_usage_list(self, items: list[str]) -> None:
        cleaned: list[str] = []
        for it in items:
            s = (it or "").strip()
            if s and s not in cleaned:
                cleaned.append(s)
        self.usages = "\n".join(cleaned)
        if cleaned and not self.remark:
            self.remark = cleaned[0]


class BookRoomScope(models.Model):
    """听群 ↔ 账本：在听群页为每个群勾选要用的记录表。

    某群若尚无任何作用域行，则全部账本可用（兼容旧数据）。
    一旦为该群保存过，仅 enabled=True 的账本关键词对该群生效。
    """

    book = models.CharField("账本", max_length=32, db_index=True)
    chat_room = models.ForeignKey(
        "chatrooms.ChatRoom",
        on_delete=models.CASCADE,
        related_name="book_scopes",
        verbose_name="群",
    )
    enabled = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "账本群作用域"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["book", "chat_room"], name="uniq_book_room_scope"
            )
        ]
        ordering = ["book", "chat_room_id"]

    def __str__(self) -> str:
        return f"{self.book}:{self.chat_room_id}"


class StaffBizRoomScope(models.Model):
    """听群 ↔ 员工记单业务关键词：每个群单独勾选启用哪些业务。"""

    chat_room = models.ForeignKey(
        "chatrooms.ChatRoom",
        on_delete=models.CASCADE,
        related_name="staff_biz_scopes",
        verbose_name="群",
    )
    biz_key = models.CharField("业务规范名", max_length=64, db_index=True)
    enabled = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "员工记单业务群绑定"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["chat_room", "biz_key"], name="uniq_staff_biz_room_scope"
            )
        ]
        ordering = ["chat_room_id", "biz_key"]

    def __str__(self) -> str:
        return f"{self.chat_room_id}:{self.biz_key}"
