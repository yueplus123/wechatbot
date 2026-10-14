from gateway.adapters.base import WeChatAdapter
from gateway.adapters.fake import FakeAdapter
from gateway.adapters.pyweixin import PyWeixinAdapter

__all__ = ["WeChatAdapter", "FakeAdapter", "PyWeixinAdapter", "create_adapter"]


def create_adapter(name: str = "pyweixin", **kwargs) -> WeChatAdapter:
    name = (name or "pyweixin").lower()
    if name in {"fake", "test"}:
        return FakeAdapter(**kwargs)
    if name in {"pyweixin", "pywechat", "default"}:
        return PyWeixinAdapter(**kwargs)
    raise ValueError(f"未知适配器: {name}")
