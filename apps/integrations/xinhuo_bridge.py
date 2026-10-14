"""
薪火统一对接层（xinhuo-bridge）

架构：
  微信 ←→ 本机听群/网关
           ↓
     【xinhuo-bridge】本模块
           ↓
     薪火固定能力路径（不再做 URL 字符串替换）

能力表：
  product_query   GET  /api/external/wechat-bot/products/
  bot_record_push POST /api/external/wechat-bot/push/
  ledger_push     POST /api/external/ledger/push/
  bridge_health   GET  /api/external/bridge/health/
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# 固定能力路径（相对 api base），禁止再用 replace 拼 URL
CAPABILITY_PATHS: dict[str, tuple[str, str]] = {
    "product_query": ("GET", "/api/external/wechat-bot/products/"),
    "bot_record_push": ("POST", "/api/external/wechat-bot/push/"),
    "ledger_push": ("POST", "/api/external/ledger/push/"),
    "bridge_health": ("GET", "/api/external/bridge/health/"),
}


@dataclass
class BridgeResult:
    ok: bool
    status_code: int = 0
    data: Any = None
    text: str = ""
    error: str = ""
    capability: str = ""
    url: str = ""
    attempts: int = 0


@dataclass
class XinhuoBridge:
    """统一鉴权、超时、重试、日志的薪火客户端。"""

    api_base: str = ""
    api_key: str = ""
    timeout: float = 8.0
    max_retries: int = 2
    # 兼容旧配置的显式覆盖（优先于能力表拼接）
    overrides: dict[str, str] = field(default_factory=dict)
    # api_key 含中文等非 ASCII 字符（多半没把 .env 模板里的占位文字换掉）
    api_key_invalid: bool = False

    def __post_init__(self) -> None:
        self.api_base = (self.api_base or "").strip().rstrip("/")
        self.api_key = (self.api_key or "").strip()
        if self.api_key and not self.api_key.isascii():
            # HTTP 请求头只能是 ASCII，带中文会抛 'ascii' codec 错误；按未配置处理
            logger.error(
                "XINHUO_BOT_PUSH_API_KEY 含中文/非 ASCII 字符，多半还是模板占位文字，"
                "请在 config/.env 填入真实密钥"
            )
            self.api_key = ""
            self.api_key_invalid = True
        self.timeout = float(self.timeout or 8)
        self.max_retries = max(0, int(self.max_retries or 0))

    # ---------- 配置解析 ----------

    @classmethod
    def from_settings(cls) -> "XinhuoBridge":
        api_base = (getattr(settings, "XINHUO_API_BASE", "") or "").strip()
        if not api_base:
            # 从任一已知 URL 推导 origin
            for key in (
                "XINHUO_LEDGER_PUSH_URL",
                "XINHUO_BOT_PUSH_URL",
                "XINHUO_PRODUCT_QUERY_URL",
            ):
                url = (getattr(settings, key, "") or "").strip()
                if url:
                    api_base = _origin_of(url)
                    if api_base:
                        break

        overrides: dict[str, str] = {}
        product = (getattr(settings, "XINHUO_PRODUCT_QUERY_URL", "") or "").strip()
        bot = (getattr(settings, "XINHUO_BOT_PUSH_URL", "") or "").strip()
        ledger = (getattr(settings, "XINHUO_LEDGER_PUSH_URL", "") or "").strip()
        if product:
            overrides["product_query"] = product
        if bot:
            overrides["bot_record_push"] = bot
        if ledger:
            overrides["ledger_push"] = ledger

        return cls(
            api_base=api_base,
            api_key=(getattr(settings, "XINHUO_BOT_PUSH_API_KEY", "") or "").strip(),
            timeout=float(getattr(settings, "XINHUO_BOT_PUSH_TIMEOUT", 8) or 8),
            max_retries=int(getattr(settings, "XINHUO_BRIDGE_MAX_RETRIES", 2) or 2),
            overrides=overrides,
        )

    def is_configured(self) -> bool:
        return bool(self.api_key and (self.api_base or self.overrides))

    def url_for(self, capability: str) -> str:
        if capability not in CAPABILITY_PATHS:
            raise KeyError(f"unknown capability: {capability}")
        override = (self.overrides.get(capability) or "").strip()
        if override:
            return override
        if not self.api_base:
            return ""
        _, path = CAPABILITY_PATHS[capability]
        return f"{self.api_base}{path}"

    def capabilities_status(self) -> list[dict[str, Any]]:
        rows = []
        for name, (method, path) in CAPABILITY_PATHS.items():
            url = self.url_for(name)
            rows.append(
                {
                    "name": name,
                    "method": method,
                    "path": path,
                    "url": url,
                    "ready": bool(url and self.api_key),
                }
            )
        return rows

    def connection_info(self) -> dict[str, Any]:
        caps = self.capabilities_status()
        ledger = next((c for c in caps if c["name"] == "ledger_push"), {})
        bot = next((c for c in caps if c["name"] == "bot_record_push"), {})
        product = next((c for c in caps if c["name"] == "product_query"), {})
        key = self.api_key
        return {
            "bridge": "xinhuo-bridge",
            "api_base": self.api_base,
            "ledger_push_url": ledger.get("url") or "",
            "bot_push_url": bot.get("url") or "",
            "product_query_url": product.get("url") or "",
            "effective_push_url": ledger.get("url") or "",
            "capabilities": caps,
            "api_key_set": bool(key),
            "api_key_invalid": self.api_key_invalid,
            "api_key_masked": (("*" * max(0, len(key) - 4)) + key[-4:]) if key else "",
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "ready": bool(self.api_key and (ledger.get("url") or bot.get("url") or product.get("url"))),
        }

    # ---------- HTTP 核心 ----------

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h = {"X-Api-Key": self.api_key}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def request(
        self,
        capability: str,
        *,
        method: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        log_label: str = "",
        retry: bool | None = None,
    ) -> BridgeResult:
        if capability not in CAPABILITY_PATHS:
            return BridgeResult(ok=False, error=f"unknown capability: {capability}", capability=capability)

        default_method, _ = CAPABILITY_PATHS[capability]
        method = (method or default_method).upper()
        url = self.url_for(capability)
        label = log_label or capability

        if not url or not self.api_key:
            logger.error(
                "薪火未推送 %s：缺少 API 地址或密钥 url=%r api_key_set=%s",
                label,
                url,
                bool(self.api_key),
            )
            return BridgeResult(
                ok=False,
                error="未配置薪火 API Base / 能力 URL 或 API Key",
                capability=capability,
                url=url,
            )

        # 幂等：若有 event_id，写进日志便于排查
        event_id = ""
        if isinstance(json_body, dict):
            event_id = str(json_body.get("event_id") or "")[:128]

        attempts_allowed = (self.max_retries + 1) if (retry if retry is not None else True) else 1
        last_err = ""
        last_status = 0
        last_text = ""

        for attempt in range(1, attempts_allowed + 1):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                    resp = client.request(
                        method,
                        url,
                        params=params,
                        json=json_body if json_body is not None else None,
                        headers=self._headers(json_body=json_body is not None),
                    )
                last_status = resp.status_code
                last_text = (resp.text or "")[:500]
                data: Any = None
                try:
                    data = resp.json() if resp.content else None
                except Exception:
                    data = None

                if resp.status_code >= 500 and attempt < attempts_allowed:
                    logger.warning(
                        "xinhuo-bridge %s retryable status=%s attempt=%s/%s event_id=%s",
                        label,
                        resp.status_code,
                        attempt,
                        attempts_allowed,
                        event_id,
                    )
                    time.sleep(min(0.4 * attempt, 1.5))
                    continue

                if resp.status_code >= 400:
                    logger.warning(
                        "xinhuo-bridge %s failed status=%s attempt=%s event_id=%s body=%s",
                        label,
                        resp.status_code,
                        attempt,
                        event_id,
                        last_text[:300],
                    )
                    return BridgeResult(
                        ok=False,
                        status_code=resp.status_code,
                        data=data,
                        text=last_text,
                        error=f"HTTP {resp.status_code}",
                        capability=capability,
                        url=url,
                        attempts=attempt,
                    )

                logger.info(
                    "xinhuo-bridge %s ok status=%s attempt=%s event_id=%s",
                    label,
                    resp.status_code,
                    attempt,
                    event_id or "-",
                )
                return BridgeResult(
                    ok=True,
                    status_code=resp.status_code,
                    data=data,
                    text=last_text,
                    capability=capability,
                    url=url,
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                logger.warning(
                    "xinhuo-bridge %s error attempt=%s/%s event_id=%s err=%s",
                    label,
                    attempt,
                    attempts_allowed,
                    event_id,
                    exc,
                )
                if attempt < attempts_allowed:
                    time.sleep(min(0.4 * attempt, 1.5))
                    continue
                logger.exception("xinhuo-bridge %s exhausted url=%s", label, url)

        return BridgeResult(
            ok=False,
            status_code=last_status,
            text=last_text,
            error=last_err or "request failed",
            capability=capability,
            url=url,
            attempts=attempts_allowed,
        )

    # ---------- 能力封装 ----------

    def health_check(self) -> dict[str, Any]:
        """探测 bridge_health；若薪火未部署该接口，回退用 ledger pull ping。"""
        info = self.connection_info()
        if not info["ready"]:
            message = (
                "API Key 还是模板占位文字：请在 config\\.env 把 XINHUO_BOT_PUSH_API_KEY 换成真实密钥后重启服务"
                if self.api_key_invalid
                else "未配置 API Base（或能力 URL）与 API Key"
            )
            return {
                "ok": False,
                "message": message,
                **info,
            }

        # 1) 优先打薪火 bridge/health
        health_url = self.url_for("bridge_health")
        if health_url:
            r = self.request("bridge_health", retry=False, log_label="health")
            if r.ok and isinstance(r.data, dict):
                return {
                    "ok": True,
                    "message": "连接成功（bridge/health）",
                    "http_status": r.status_code,
                    "remote": r.data,
                    **info,
                }

        # 2) 回退：ledger pull ping（不改数据）
        r = self.request(
            "ledger_push",
            json_body={
                "action": "pull",
                "chat_name": "__wxbot_ping__",
                "status": "ACTIVE",
            },
            retry=False,
            log_label="health-ping",
        )
        if r.ok:
            return {
                "ok": True,
                "message": f"连接成功（ledger ping HTTP {r.status_code}）",
                "http_status": r.status_code,
                "body_preview": r.text[:240],
                **info,
            }
        return {
            "ok": False,
            "message": r.error or f"薪火返回 HTTP {r.status_code}",
            "http_status": r.status_code,
            "body_preview": r.text[:240],
            **info,
        }

    def product_query(self, *, q: str = "", limit: int = 50) -> dict[str, Any]:
        params: dict[str, str] = {"as": "text", "limit": str(limit)}
        q = (q or "").strip()
        if q:
            params["q"] = q
        r = self.request("product_query", params=params, log_label="product_query")
        if not r.ok:
            msg = r.error or "查商品失败"
            if "未配置" in msg:
                text = "❌ 未配置薪火 API Base / API Key（XINHUO_API_BASE + XINHUO_BOT_PUSH_API_KEY）"
            else:
                text = f"❌ 查商品失败（{msg}）"
            return {"ok": False, "text": text, "count": 0, "results": []}

        data = r.data if isinstance(r.data, dict) else {}
        text = str(data.get("text") or "").strip()
        results = data.get("results") or []
        if not text and isinstance(results, list):
            lines = [f"商品目录（共 {len(results)} 条）", "--------------"]
            for row in results:
                if not isinstance(row, dict):
                    continue
                pn = str(row.get("platform_name") or "").strip()
                pd = str(row.get("product_name") or "").strip()
                if pn and pd:
                    lines.append(f"{pn} · {pd}")
            lines.append("--------------")
            text = "\n".join(lines) if len(lines) > 3 else "未找到相关商品。可发：查商品 或 查商品 关键词"
        return {
            "ok": True,
            "text": text or "未找到相关商品。可发：查商品 或 查商品 关键词",
            "count": int(data.get("count") or (len(results) if isinstance(results, list) else 0) or 0),
            "results": results if isinstance(results, list) else [],
        }

    def ledger_post(self, payload: dict[str, Any], *, log_label: str = "ledger") -> BridgeResult:
        return self.request("ledger_push", json_body=payload, log_label=log_label)

    def bot_record_post(self, payload: dict[str, Any], *, log_label: str = "bot_record") -> BridgeResult:
        return self.request("bot_record_push", json_body=payload, log_label=log_label)


def _origin_of(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        pass
    return ""


_bridge: XinhuoBridge | None = None


def get_bridge(*, refresh: bool = False) -> XinhuoBridge:
    global _bridge
    if _bridge is None or refresh:
        _bridge = XinhuoBridge.from_settings()
    return _bridge
