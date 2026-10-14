import json

from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.audit.models import GatewayHeartbeat
from apps.chatrooms.models import ChatRoom
from apps.gateway_api.services import process_gateway_message, verify_gateway_signature
from apps.ledger.models import ReplyJob


def _read_headers(request):
    return {
        "gateway_id": request.headers.get("X-Gateway-Id", ""),
        "timestamp": request.headers.get("X-Timestamp", ""),
        "nonce": request.headers.get("X-Nonce", ""),
        "signature": request.headers.get("X-Signature", ""),
        "request_id": request.headers.get("X-Request-Id", ""),
    }


def _unauthorized():
    return JsonResponse({"detail": "unauthorized"}, status=401)


@method_decorator(csrf_exempt, name="dispatch")
class GatewayMessagesView(View):
    def post(self, request):
        h = _read_headers(request)
        body = request.body or b"{}"
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=body,
            signature=h["signature"],
        ):
            return _unauthorized()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"detail": "invalid json"}, status=400)
        result = process_gateway_message(payload, h["gateway_id"] or "unknown")
        return JsonResponse(result)


@method_decorator(csrf_exempt, name="dispatch")
class GatewayReplyNextView(View):
    def get(self, request):
        h = _read_headers(request)
        # GET 签名：空 body
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=b"",
            signature=h["signature"],
        ):
            return _unauthorized()
        gateway_id = request.GET.get("gateway_id") or h["gateway_id"]
        now = timezone.now()
        from django.db import connection, transaction

        with transaction.atomic():
            qs = ReplyJob.objects.filter(status=ReplyJob.Status.PENDING).order_by("id")
            # MySQL/PostgreSQL 可用 skip_locked；SQLite PoC 降级为普通锁
            if connection.vendor in {"mysql", "postgresql"}:
                qs = qs.select_for_update(skip_locked=True)
            else:
                qs = qs.select_for_update()
            job = qs.first()
            if job is None:
                return JsonResponse({"job": None})
            job.status = ReplyJob.Status.LEASED
            job.leased_by = gateway_id
            job.leased_until = now + timezone.timedelta(seconds=60)
            job.attempts += 1
            job.save(
                update_fields=[
                    "status",
                    "leased_by",
                    "leased_until",
                    "attempts",
                    "updated_at",
                ]
            )
        return JsonResponse(
            {
                "job": {
                    "id": job.id,
                    "chat_name": job.chat_name,
                    "body": job.body,
                    "attempts": job.attempts,
                }
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class GatewayReplyResultView(View):
    def post(self, request, job_id: int):
        h = _read_headers(request)
        body = request.body or b"{}"
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=body,
            signature=h["signature"],
        ):
            return _unauthorized()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"detail": "invalid json"}, status=400)
        try:
            job = ReplyJob.objects.get(pk=job_id)
        except ReplyJob.DoesNotExist:
            return JsonResponse({"detail": "not_found"}, status=404)
        status = payload.get("status")
        if status == "sent":
            job.status = ReplyJob.Status.SENT
            job.sent_at = timezone.now()
            job.last_error = ""
        elif status == "unknown":
            job.status = ReplyJob.Status.UNKNOWN
            job.last_error = payload.get("adapter_result") or "unknown"
        else:
            job.status = ReplyJob.Status.FAILED
            job.last_error = payload.get("adapter_result") or "failed"
            # 简单退避：最多 3 次
            if job.attempts < 3:
                job.status = ReplyJob.Status.PENDING
                job.next_retry_at = timezone.now() + timezone.timedelta(
                    seconds=5 * job.attempts
                )
        job.save()
        return JsonResponse({"ok": True, "status": job.status})


@method_decorator(csrf_exempt, name="dispatch")
class GatewayHeartbeatView(View):
    def post(self, request):
        h = _read_headers(request)
        body = request.body or b"{}"
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=body,
            signature=h["signature"],
        ):
            return _unauthorized()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"detail": "invalid json"}, status=400)
        gateway_id = payload.get("gateway_id") or h["gateway_id"]

        def _dt(value):
            if not value:
                return None
            from django.utils.dateparse import parse_datetime
            from django.utils import timezone as tz

            dt = parse_datetime(str(value))
            if dt is None:
                return None
            if tz.is_naive(dt):
                dt = tz.make_aware(dt, tz.get_current_timezone())
            return dt

        # 未携带 discovered_chats 时不覆盖已有缓存
        defaults = {
            "adapter_state": payload.get("adapter_state") or "",
            "wechat_login_state": payload.get("wechat_login_state") or "",
            "wechat_version": payload.get("wechat_version") or "",
            "listening_chats": payload.get("listening_chats") or [],
            "last_rx_at": _dt(payload.get("last_received_at")),
            "last_tx_at": _dt(payload.get("last_sent_at")),
            "error": payload.get("error") or "",
        }
        if "discovered_chats" in payload:
            defaults["discovered_chats"] = payload.get("discovered_chats") or []
        obj, _ = GatewayHeartbeat.objects.update_or_create(
            gateway_id=gateway_id,
            defaults=defaults,
        )
        return JsonResponse({"ok": True, "updated_at": obj.updated_at.isoformat()})


@method_decorator(csrf_exempt, name="dispatch")
class GatewayChatsView(View):
    """返回面板中启用的监听群，供 Worker 动态同步。"""

    def get(self, request):
        h = _read_headers(request)
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=b"",
            signature=h["signature"],
        ):
            return _unauthorized()
        rooms = ChatRoom.objects.filter(enabled=True).order_by("name")
        from pathlib import Path

        from django.conf import settings

        flag = Path(settings.BASE_DIR) / "data" / "request_discover.flag"
        discover = False
        if flag.exists():
            discover = True
            try:
                flag.unlink()
            except OSError:
                pass
        return JsonResponse(
            {
                "chats": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "alias": r.alias,
                        "strict_prefix": r.strict_prefix,
                    }
                    for r in rooms
                ],
                "discover_chats": discover,
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class GatewayChatsSyncPinnedView(View):
    """Worker 上报：用户置顶的独立聊天窗 = 要监听的群。"""

    PINNED_ALIAS = "置顶窗口自动监听"

    def post(self, request):
        h = _read_headers(request)
        body = request.body or b"{}"
        if not verify_gateway_signature(
            gateway_id=h["gateway_id"],
            timestamp=h["timestamp"],
            nonce=h["nonce"],
            body=body,
            signature=h["signature"],
        ):
            return _unauthorized()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"detail": "invalid json"}, status=400)

        names = []
        for item in payload.get("chats") or []:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = (item.get("name") or "").strip()
            else:
                continue
            if name:
                names.append(name)
        # 去重保序
        seen = set()
        uniq = []
        for n in names:
            if n not in seen:
                seen.add(n)
                uniq.append(n)

        for name in uniq:
            room, created = ChatRoom.objects.get_or_create(
                name=name,
                defaults={
                    "enabled": True,
                    "strict_prefix": True,
                    "alias": self.PINNED_ALIAS,
                },
            )
            if not created:
                room.enabled = True
                if not room.alias:
                    room.alias = self.PINNED_ALIAS
                room.save(update_fields=["enabled", "alias", "updated_at"])

        # 仅停用「自动置顶」产生、且当前未置顶的群；手动配置的群不动
        disabled = (
            ChatRoom.objects.filter(alias=self.PINNED_ALIAS)
            .exclude(name__in=uniq)
            .update(enabled=False)
        )
        return JsonResponse(
            {
                "ok": True,
                "listening": uniq,
                "disabled_auto": disabled,
            }
        )
