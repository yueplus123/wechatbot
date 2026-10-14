"""FakeAdapter：单元/集成测试与无微信环境联调，不依赖真实客户端。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import List

from gateway.adapters.base import (
    AdapterHealth,
    MessageType,
    RawMessage,
    SendResult,
    WeChatAdapter,
)


class FakeAdapter:
    """内存消息队列模拟微信。"""

    def __init__(self, messages: List[RawMessage] | None = None) -> None:
        self._queue: list[RawMessage] = list(messages or [])
        self.sent: list[tuple[str, str]] = []
        self._started = False
        self._seq = 0

    def start(self) -> None:
        self._started = True

    def push(
        self,
        chat_name: str,
        sender: str,
        text: str,
        *,
        message_type: MessageType = MessageType.TEXT,
        at: datetime | None = None,
    ) -> RawMessage:
        self._seq += 1
        msg = RawMessage(
            chat_name=chat_name,
            sender_display_name=sender,
            message_type=message_type,
            raw_text=text,
            client_observed_at=at or datetime.now(timezone.utc).astimezone(),
            local_sequence=self._seq,
            ui_timestamp="",
        )
        self._queue.append(msg)
        return msg

    def poll(self, chat_names: list[str]) -> list[RawMessage]:
        allow = set(chat_names)
        out: list[RawMessage] = []
        remain: list[RawMessage] = []
        for msg in self._queue:
            if msg.chat_name in allow:
                out.append(msg)
            else:
                remain.append(msg)
        self._queue = remain
        return out

    def listen(self, chat_names: list[str]) -> Iterable[RawMessage]:
        def _gen() -> Iterator[RawMessage]:
            while True:
                batch = self.poll(chat_names)
                if not batch:
                    break
                yield from batch

        return _gen()

    def list_groups(self) -> list[dict]:
        names = {m.chat_name for m in self._queue}
        names.update(c for c, _ in self.sent)
        return [{"name": n, "member_count": None, "source": "fake", "kind": "group"} for n in sorted(names)]

    def list_pinned_chats(self) -> list[dict]:
        return self.list_groups()

    def send_text(self, chat_name: str, text: str) -> SendResult:
        if not self._started:
            return SendResult(ok=False, detail="adapter_not_started")
        self.sent.append((chat_name, text))
        return SendResult(ok=True, detail="ok")

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            running=self._started,
            wechat_login_state="logged_in" if self._started else "logged_out",
            wechat_version="fake-0.0",
            detail="FakeAdapter",
        )

    def client_version(self) -> str:
        return "fake-0.0"

    def close(self) -> None:
        self._started = False


# 类型检查友好
_: WeChatAdapter = FakeAdapter()  # type: ignore[assignment]
