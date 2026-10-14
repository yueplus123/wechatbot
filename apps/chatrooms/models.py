from django.db import models


class ChatRoom(models.Model):
    name = models.CharField("群名称", max_length=128, unique=True)
    alias = models.CharField("别名", max_length=128, blank=True, default="")
    enabled = models.BooleanField("启用监听", default=True)
    strict_prefix = models.BooleanField("严格前缀", default=True)
    default_currency = models.CharField(max_length=4, default="CNY")
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "聊天群"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.name


class Employee(models.Model):
    class Role(models.TextChoices):
        STAFF = "staff", "员工"
        ADMIN = "admin", "管理员"
        # 兼容旧数据
        FINANCE = "finance", "财务/组长"

    employee_code = models.CharField("员工编号", max_length=32, unique=True)
    display_name = models.CharField("显示名", max_length=64)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STAFF)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "员工"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.employee_code}:{self.display_name}"

    @property
    def is_admin(self) -> bool:
        return self.role in {self.Role.ADMIN, self.Role.FINANCE}

    @property
    def role_label(self) -> str:
        return self.get_role_display()


class SenderBinding(models.Model):
    class Role(models.TextChoices):
        STAFF = "staff", "员工"
        ADMIN = "admin", "管理员"
        FINANCE = "finance", "财务/组长"

    chat_room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="bindings"
    )
    sender_display_name = models.CharField("群内显示名", max_length=128)
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="bindings"
    )
    # 按群授权：同一人在 A 群可管理员、B 群可仅员工
    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.STAFF, verbose_name="本群角色"
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "发送人绑定"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["chat_room", "sender_display_name"],
                condition=models.Q(active=True),
                name="uniq_active_sender_binding",
            )
        ]

    def __str__(self) -> str:
        return f"{self.chat_room_id}:{self.sender_display_name}"

    @property
    def is_admin(self) -> bool:
        return self.role in {self.Role.ADMIN, self.Role.FINANCE}

    @property
    def role_label(self) -> str:
        return self.get_role_display()


def is_admin_in_room(room, employee) -> bool:
    """本群是否管理员（按绑定角色；同备注名在本群任一昵称为管理员即算）。"""
    if room is None or employee is None:
        return False
    room_id = getattr(room, "id", None) or room
    emp_id = getattr(employee, "id", None)
    if not room_id or not emp_id:
        return False
    return SenderBinding.objects.filter(
        chat_room_id=room_id,
        employee_id=emp_id,
        active=True,
        role__in=[SenderBinding.Role.ADMIN, SenderBinding.Role.FINANCE],
    ).exists()
