"""
网关 Worker：
- 默认 pinned：用户「单独打开群 + 左上角图钉置顶」= 监听该群（不搜群）
- 从桌面控制台「听群」页勾选要监听的群
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / "config" / ".env")
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from gateway.adapters import create_adapter
from gateway.adapters.base import MessageType
from gateway.dedupe import SlidingWindowDedupe, make_fingerprint
from gateway.perf import (
    DailyWindowReset,
    LoadBackoff,
    SystemCpuSampler,
    apply_process_limits,
    env_float,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gateway.worker")

# 同时写入网页「日志」页读取的文件（不轮转，避免与 Django 抢 rename）
try:
    _log_dir = ROOT / "data"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(
        _log_dir / "runtime.log", encoding="utf-8", delay=True
    )
    _fh.setFormatter(
        logging.Formatter("%(asctime)s [worker] %(levelname)s %(message)s")
    )
    # 轮转冲突时不向 stderr 刷堆栈
    _fh.handleError = lambda record: None  # type: ignore[method-assign]
    logging.getLogger().addHandler(_fh)
except Exception:
    pass


def _sync_binding_nicks_from_db() -> None:
    """把已授权昵称合并进 room_nicks（不得全量覆盖，否则网页下拉只剩已绑定的人）。"""
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from apps.chatrooms.models import SenderBinding
        from gateway.sender_resolve import remember_room_nicks

        by_room: dict[str, list[str]] = {}
        for b in SenderBinding.objects.filter(active=True).select_related("chat_room"):
            by_room.setdefault(b.chat_room.name, []).append(b.sender_display_name)
        for room_name, nicks in by_room.items():
            remember_room_nicks(room_name, nicks)
        if by_room:
            logger.info(
                "merged binding nicks rooms=%s",
                {k: len(v) for k, v in by_room.items()},
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync binding nicks skipped: %s", exc)


class GatewayClient:
    def __init__(self, base: str, gateway_id: str, secret: str) -> None:
        self.base = base.rstrip("/")
        self.gateway_id = gateway_id
        self.secret = secret.encode("utf-8")
        self.http = httpx.Client(timeout=15.0, trust_env=False)

    def _headers(self, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        payload = f"{self.gateway_id}.{ts}.{nonce}.".encode("utf-8") + body
        sig = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Gateway-Id": self.gateway_id,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sig,
            "X-Request-Id": uuid.uuid4().hex,
        }

    def get_enabled_chats(self) -> tuple[list[str], bool]:
        body = b""
        r = self.http.get(
            f"{self.base}/api/v1/gateway/chats",
            headers=self._headers(body),
        )
        r.raise_for_status()
        data = r.json()
        chats = [item["name"] for item in data.get("chats", [])]
        return chats, bool(data.get("discover_chats"))

    def sync_pinned_chats(self, chats: list) -> dict:
        body = json.dumps({"chats": chats}, ensure_ascii=False).encode("utf-8")
        r = self.http.post(
            f"{self.base}/api/v1/gateway/chats/sync-pinned",
            content=body,
            headers=self._headers(body),
        )
        r.raise_for_status()
        return r.json()

    def post_message(self, data: dict) -> dict:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        r = self.http.post(
            f"{self.base}/api/v1/gateway/messages",
            content=body,
            headers=self._headers(body),
        )
        r.raise_for_status()
        return r.json()

    def next_reply(self) -> dict | None:
        body = b""
        r = self.http.get(
            f"{self.base}/api/v1/gateway/replies/next",
            params={"gateway_id": self.gateway_id},
            headers=self._headers(body),
        )
        r.raise_for_status()
        return r.json().get("job")

    def reply_result(self, job_id: int, status: str, detail: str = "ok") -> None:
        data = {
            "status": status,
            "sent_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "adapter_result": detail,
        }
        body = json.dumps(data).encode("utf-8")
        r = self.http.post(
            f"{self.base}/api/v1/gateway/replies/{job_id}/result",
            content=body,
            headers=self._headers(body),
        )
        r.raise_for_status()

    def heartbeat(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = self.http.post(
            f"{self.base}/api/v1/gateway/heartbeat",
            content=body,
            headers=self._headers(body),
        )
        r.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--chats",
        default="",
        help="可选：临时覆盖面板配置（逗号分隔）。默认从面板读取启用群。",
    )
    parser.add_argument("--adapter", default=os.getenv("WECHAT_ADAPTER", "pyweixin"))
    parser.add_argument(
        "--api", default=os.getenv("GATEWAY_API_BASE", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=int(os.getenv("CHAT_LIST_REFRESH_SECONDS", "30")),
        help="从面板同步群列表的间隔秒数",
    )
    args = parser.parse_args()
    override = [c.strip() for c in args.chats.split(",") if c.strip()]

    gateway_id = os.getenv("GATEWAY_ID", "wx-gateway-01")
    secret = os.getenv("GATEWAY_HMAC_SECRET", "")
    client = GatewayClient(args.api, gateway_id, secret)
    apply_process_limits()
    adapter = create_adapter(args.adapter)
    adapter.start()
    # 性能：系统过载时放慢轮询；定期输出耗时统计；每天定时重置群窗口
    cpu_sampler = SystemCpuSampler()
    cpu_sampler.sample()
    backoff = LoadBackoff.from_env()
    last_cpu: float | None = None
    stats = getattr(adapter, "stats", None)
    stats_every = env_float("WORKER_STATS_SECONDS", 600.0)
    last_stats = time.time()
    window_reset = DailyWindowReset.from_env(ROOT / "data" / "window_reset.json")
    can_reset = callable(getattr(adapter, "reset_chat_windows", None))
    if window_reset.enabled and can_reset:
        logger.info(
            "daily window reset at %02d:%02d (WECHAT_WINDOW_RESET_AT)", *window_reset.at
        )
    local_dedupe = SlidingWindowDedupe(
        window_seconds=int(os.getenv("DEDUP_WINDOW_SECONDS", "15"))
    )
    min_interval = float(os.getenv("REPLY_MIN_INTERVAL_SECONDS", "0.6"))
    last_send = 0.0
    last_hb = 0.0
    last_refresh = 0.0
    last_discover = 0.0
    chats: list[str] = list(override)
    discovered: list[dict] = []
    pending_discover = False
    poll_sleep = float(os.getenv("WECHAT_POLL_SECONDS", "2.5"))
    pause_flag = ROOT / "data" / "pause.flag"
    force_flag = ROOT / "data" / "force_refresh_pinned.flag"
    listen_source = os.getenv("WECHAT_LISTEN_SOURCE", "pinned").lower()
    # 只读测试：收消息、入库，但不向微信发任何回复
    no_send = os.getenv("WORKER_NO_SEND", "0").lower() in {"1", "true", "yes"}
    if no_send:
        logger.warning("WORKER_NO_SEND=1：只收不发，不会向微信发送任何回复")

    logger.info(
        "worker started api=%s source=%s refresh=%ss poll=%ss "
        "(warm-skip history; no xinhuo backfill on restart)",
        args.api,
        listen_source,
        args.refresh,
        poll_sleep,
    )
    # 丢弃闪退前积压的群回执；不补推薪火旧账
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from apps.integrations.startup_guard import discard_stale_reply_jobs

        n = discard_stale_reply_jobs(reason="worker_start")
        if n:
            logger.info("cancelled %s stale reply jobs on worker start", n)
    except Exception as exc:  # noqa: BLE001
        logger.debug("startup reply discard skipped: %s", exc)
    _sync_binding_nicks_from_db()
    try:
        while True:
            now = time.time()

            if pause_flag.exists():
                if int(now) % 20 == 0:
                    logger.info("已暂停听群（存在 data/pause.flag）。点「继续听群」后恢复。")
                time.sleep(1.0)
                continue

            force_refresh = force_flag.exists()
            if force_refresh:
                try:
                    force_flag.unlink()
                except OSError:
                    pass
                last_refresh = 0.0
                logger.info("收到强制刷新置顶群请求")

            # 刷新监听列表
            if not override and (force_refresh or now - last_refresh >= args.refresh):
                try:
                    _sync_binding_nicks_from_db()
                    if listen_source == "pinned":
                        pinned = adapter.list_pinned_chats()
                        chats = [p["name"] for p in pinned if p.get("name")]
                        discovered = pinned
                        if chats:
                            client.sync_pinned_chats(pinned)
                        logger.info("listening pinned chats: %s", chats or "(none)")
                    else:
                        chats, want_discover = client.get_enabled_chats()
                        if want_discover:
                            pending_discover = True
                        logger.info("synced listening chats: %s", chats or "(empty)")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("refresh chats failed: %s", exc)
                last_refresh = now

            if pending_discover and listen_source != "pinned":
                try:
                    discovered = adapter.list_groups()
                    pending_discover = False
                    last_discover = now
                    h = adapter.health()
                    payload = {
                        "gateway_id": gateway_id,
                        "adapter_state": "running",
                        "wechat_login_state": h.wechat_login_state,
                        "wechat_version": h.wechat_version,
                        "listening_chats": chats,
                        "last_received_at": datetime.now(timezone.utc)
                        .astimezone()
                        .isoformat(),
                    }
                    if discovered:
                        payload["discovered_chats"] = discovered
                    client.heartbeat(payload)
                    last_hb = now
                except Exception as exc:  # noqa: BLE001
                    logger.warning("discover chats failed: %s", exc)
                    pending_discover = False

            if now - last_hb > 30:
                h = adapter.health()
                try:
                    payload_hb = {
                        "gateway_id": gateway_id,
                        "adapter_state": "running",
                        "wechat_login_state": h.wechat_login_state,
                        "wechat_version": h.wechat_version,
                        "listening_chats": chats,
                        "last_received_at": datetime.now(timezone.utc)
                        .astimezone()
                        .isoformat(),
                    }
                    if discovered:
                        payload_hb["discovered_chats"] = discovered
                    client.heartbeat(payload_hb)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("heartbeat failed: %s", exc)
                last_hb = now

            if not chats:
                if int(now) % 15 < 2:
                    logger.info(
                        "当前无监听群。请把要记账的群「单独打开」并点左上角绿色图钉置顶。"
                    )
                time.sleep(2)
                continue

            if window_reset.enabled and can_reset:
                local_now = datetime.now()
                due = window_reset.due_chats(local_now, chats)
                if due:
                    logger.info("开始定时重置群窗口: %s", due)
                    try:
                        results = adapter.reset_chat_windows(due)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("定时重置群窗口失败: %s", exc)
                        results = {c: False for c in due}
                    window_reset.record(local_now, results)
                    failed = [c for c, ok in results.items() if not ok]
                    if failed:
                        logger.error(
                            "以下群窗口重置失败，%.0f 分钟后重试；如仍失败请手动单独打开并置顶: %s",
                            window_reset.retry_seconds / 60,
                            failed,
                        )
                    # 窗口句柄变了：下一轮立即重新扫描置顶窗
                    last_refresh = 0.0
                    continue

            t_poll = time.perf_counter()
            try:
                messages = adapter.poll(chats)
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll failed: %s", exc)
                time.sleep(2)
                continue
            if stats is not None:
                stats.record_round((time.perf_counter() - t_poll) * 1000)

            for msg in messages:
                if msg.message_type != MessageType.TEXT:
                    continue
                text = (msg.raw_text or "").strip()
                # 入账指令：按正文短窗去重（仅成功/重复后标记，失败可立即重试）
                try:
                    from apps.ledger.books import is_book_command_text

                    is_cmd = is_book_command_text(text, msg.chat_name)
                except Exception:  # noqa: BLE001
                    is_cmd = text.startswith("记账") or text.startswith("記賬")
                if is_cmd:
                    from gateway.chat_names import normalize_group_nickname

                    sk = normalize_group_nickname(msg.sender_display_name) or (
                        msg.sender_display_name or ""
                    )
                    if sk in {"", "未知发送人"}:
                        sk = ""
                    ui_ts = str(msg.ui_timestamp or "").strip()
                    time_part = ui_ts or str(int(time.time() // 5))
                    fp = make_fingerprint(
                        gateway_id,
                        msg.chat_name,
                        sk,
                        text,
                        time_part,
                        "",
                    )
                else:
                    fp = make_fingerprint(
                        gateway_id,
                        msg.chat_name,
                        msg.sender_display_name,
                        text,
                        msg.ui_timestamp,
                        msg.local_sequence,
                    )
                if local_dedupe.contains(fp):
                    continue

                payload = {
                    "event_uuid": str(uuid.uuid4()),
                    "chat_name": msg.chat_name,
                    "sender_display_name": msg.sender_display_name,
                    "message_type": "text",
                    "raw_text": msg.raw_text,
                    "client_observed_at": msg.client_observed_at.isoformat(),
                    "adapter": args.adapter,
                    "wechat_version": adapter.client_version(),
                    "ui_timestamp": msg.ui_timestamp,
                    "local_sequence": msg.local_sequence,
                }
                if isinstance(msg.extra, dict) and msg.extra.get("likely_self"):
                    payload["likely_self"] = True
                try:
                    result = client.post_message(payload)
                    status = result.get("status")
                    # 仅成功/确认重复后计入本地去重；E004 等失败不封锁重试
                    if status in {"accepted", "duplicate"}:
                        local_dedupe.mark(fp)
                    logger.info(
                        "message result=%s reason=%s chat=%s text=%s",
                        status,
                        result.get("reason") or result.get("error_code") or "",
                        msg.chat_name,
                        (msg.raw_text or "")[:60],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("upload failed: %s", exc)

            try:
                while not no_send:
                    job = client.next_reply()
                    if not job:
                        break
                    wait = min_interval - (time.time() - last_send)
                    if wait > 0:
                        time.sleep(wait)
                    if job["chat_name"] not in chats:
                        logger.error("拒绝切错群发送: %s", job["chat_name"])
                        client.reply_result(job["id"], "failed", "chat_not_allowed")
                        continue
                    send_res = adapter.send_text(job["chat_name"], job["body"])
                    last_send = time.time()
                    if send_res.uncertain:
                        client.reply_result(job["id"], "unknown", send_res.detail)
                    elif send_res.ok:
                        client.reply_result(job["id"], "sent", send_res.detail)
                    else:
                        client.reply_result(job["id"], "failed", send_res.detail)
            except Exception as exc:  # noqa: BLE001
                logger.exception("reply loop failed: %s", exc)

            # 系统 CPU 持续过高时放慢轮询，给系统留余量，防止整机卡死
            cpu = cpu_sampler.sample()
            if cpu is not None:
                last_cpu = cpu
            old_factor = backoff.factor
            factor = backoff.update(cpu)
            if factor != old_factor:
                if factor > old_factor:
                    logger.warning(
                        "系统 CPU %.0f%% 持续过高，轮询间隔放慢到 %.1fs（x%.0f）",
                        cpu or 0.0,
                        poll_sleep * factor,
                        factor,
                    )
                else:
                    logger.info(
                        "系统 CPU 回落到 %.0f%%，轮询间隔恢复到 %.1fs",
                        cpu or 0.0,
                        poll_sleep * factor,
                    )

            if stats is not None and stats_every > 0 and now - last_stats >= stats_every:
                logger.info(
                    "perf %s | cpu=%s backoff=x%.0f",
                    stats.summary(),
                    f"{last_cpu:.0f}%" if last_cpu is not None else "n/a",
                    backoff.factor,
                )
                stats.reset()
                last_stats = now

            time.sleep(poll_sleep * factor)
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
