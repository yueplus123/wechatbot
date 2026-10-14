"""薪火 API Key 占位文字检测。"""


def test_xinhuo_placeholder_api_key_treated_as_unconfigured():
    from apps.integrations.xinhuo_bridge import XinhuoBridge

    b = XinhuoBridge(
        api_base="http://example.com",
        api_key="请填写薪火后端 EXTERNAL_PUSH_API_KEY 的值",
    )
    assert b.api_key == ""
    assert b.api_key_invalid
    assert not b.is_configured()
    ok = XinhuoBridge(api_base="http://example.com", api_key="abc123")
    assert ok.is_configured() and not ok.api_key_invalid
