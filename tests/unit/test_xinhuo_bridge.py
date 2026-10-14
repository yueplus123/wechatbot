"""xinhuo-bridge 能力表与 URL 解析。"""

from __future__ import annotations

from apps.integrations.xinhuo_bridge import CAPABILITY_PATHS, XinhuoBridge


def test_capability_paths_fixed():
    assert CAPABILITY_PATHS["product_query"] == ("GET", "/api/external/wechat-bot/products/")
    assert CAPABILITY_PATHS["bot_record_push"] == ("POST", "/api/external/wechat-bot/push/")
    assert CAPABILITY_PATHS["ledger_push"] == ("POST", "/api/external/ledger/push/")
    assert CAPABILITY_PATHS["bridge_health"] == ("GET", "/api/external/bridge/health/")


def test_url_from_api_base():
    b = XinhuoBridge(api_base="http://127.0.0.1:8001", api_key="k")
    assert b.url_for("product_query") == "http://127.0.0.1:8001/api/external/wechat-bot/products/"
    assert b.url_for("ledger_push") == "http://127.0.0.1:8001/api/external/ledger/push/"


def test_override_wins():
    b = XinhuoBridge(
        api_base="http://127.0.0.1:8001",
        api_key="k",
        overrides={"ledger_push": "http://other/ledger/"},
    )
    assert b.url_for("ledger_push") == "http://other/ledger/"
    assert b.url_for("product_query").endswith("/wechat-bot/products/")


def test_connection_info_ready():
    b = XinhuoBridge(api_base="http://127.0.0.1:8001", api_key="secret-key")
    info = b.connection_info()
    assert info["ready"] is True
    assert info["bridge"] == "xinhuo-bridge"
    assert len(info["capabilities"]) == 4
