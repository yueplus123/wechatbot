from django.db import models


class AuditLog(models.Model):
    actor = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    before_json = models.JSONField(null=True, blank=True)
    after_json = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "审计日志"
        verbose_name_plural = verbose_name
        indexes = [models.Index(fields=["created_at"])]


class GatewayHeartbeat(models.Model):
    gateway_id = models.CharField(max_length=64, unique=True)
    adapter_state = models.CharField(max_length=32, blank=True, default="")
    wechat_login_state = models.CharField(max_length=32, blank=True, default="")
    wechat_version = models.CharField(max_length=64, blank=True, default="")
    listening_chats = models.JSONField(default=list, blank=True)
    discovered_chats = models.JSONField(
        default=list,
        blank=True,
        help_text="Worker 从微信同步的群/会话名列表",
    )
    last_rx_at = models.DateTimeField(null=True, blank=True)
    last_tx_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "网关心跳"
        verbose_name_plural = verbose_name
