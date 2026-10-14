"""微信适配层协议：业务代码只依赖本模块，不直接调用 pyweixin。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Protocol


class MessageType(str, Enum):
    TEXT = "text"
    OTHER = "other"


@dataclass(frozen=True)
class RawMessage:
    chat_name: str
    sender_display_name: str
    message_type: MessageType
    raw_text: str
    client_observed_at: datetime
    local_sequence: int = 0
    ui_timestamp: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str = "ok"
    uncertain: bool = False  # 发送结果不确定 → 标记 unknown，禁止盲目重试


@dataclass(frozen=True)
class AdapterHealth:
    running: bool
    wechat_login_state: str  # logged_in / logged_out / unknown
    wechat_version: str = ""
    detail: str = ""


class WeChatAdapter(Protocol):
    def start(self) -> None: ...

    def listen(self, chat_names: list[str]) -> Iterable[RawMessage]: ...

    def poll(self, chat_names: list[str]) -> list[RawMessage]:
        """单轮拉取（便于动态更新监听群列表）。"""
        ...

    def list_groups(self) -> list[dict]:
        """从微信读取群/会话名称，供面板勾选绑定。项含 name，可选 member_count/source。"""
        ...

    def send_text(self, chat_name: str, text: str) -> SendResult: ...

    def health(self) -> AdapterHealth: ...

    def client_version(self) -> str: ...

    def close(self) -> None: ...
