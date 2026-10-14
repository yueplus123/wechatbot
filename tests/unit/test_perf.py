"""Worker 性能辅助：调度、退避、统计（纯逻辑，不依赖 Windows / Django）。"""

from datetime import date, datetime

from gateway.perf import (
    DailyWindowReset,
    LoadBackoff,
    PollStats,
    SystemCpuSampler,
    affinity_mask,
    parse_hhmm,
    reset_due,
)


def test_parse_hhmm():
    assert parse_hhmm("04:00") == (4, 0)
    assert parse_hhmm(" 23:59 ") == (23, 59)
    for off in ("", "off", "OFF", "0", None, "25:00", "4", "ab:cd"):
        assert parse_hhmm(off) is None


def test_reset_due_window():
    at = (4, 0)
    assert not reset_due(datetime(2026, 10, 14, 3, 59), at, None)
    assert reset_due(datetime(2026, 10, 14, 4, 0), at, None)
    assert reset_due(datetime(2026, 10, 14, 5, 30), at, None)
    # 超出 2 小时窗口不补做（白天重启 Worker 不会突然关窗）
    assert not reset_due(datetime(2026, 10, 14, 6, 0), at, None)
    # 今天已经做过
    assert not reset_due(datetime(2026, 10, 14, 4, 10), at, date(2026, 10, 14))
    assert not reset_due(datetime(2026, 10, 14, 4, 10), None, None)


def test_daily_reset_runs_once_and_persists(tmp_path):
    state = tmp_path / "window_reset.json"
    r = DailyWindowReset((4, 0), state_path=state)
    now = datetime(2026, 10, 14, 4, 1)
    assert r.due_chats(now, ["A", "B"]) == ["A", "B"]
    r.record(now, {"A": True, "B": True})
    assert r.due_chats(datetime(2026, 10, 14, 4, 30), ["A", "B"]) == []

    # Worker 当天重启：读回状态，不再重复关窗
    r2 = DailyWindowReset((4, 0), state_path=state)
    assert r2.due_chats(datetime(2026, 10, 14, 4, 40), ["A", "B"]) == []
    # 第二天照常
    assert r2.due_chats(datetime(2026, 10, 15, 4, 0), ["A", "B"]) == ["A", "B"]


def test_daily_reset_retries_failed_same_day_only():
    r = DailyWindowReset((4, 0), retry_seconds=600)
    t0 = datetime(2026, 10, 14, 4, 0)
    r.record(t0, {"A": True, "B": False})
    assert r.due_chats(datetime(2026, 10, 14, 4, 5), ["A", "B"]) == []
    assert r.due_chats(datetime(2026, 10, 14, 4, 10), ["A", "B"]) == ["B"]
    # 群已不在监听列表就不重试它
    assert r.due_chats(datetime(2026, 10, 14, 4, 10), ["A"]) == []
    r.record(datetime(2026, 10, 14, 4, 10), {"B": True})
    assert r.pending == []
    # 跨天放弃昨天的重试
    r.record(t0, {"B": False})
    assert r.due_chats(datetime(2026, 10, 15, 3, 0), ["B"]) == []
    assert r.pending == []


def test_daily_reset_disabled():
    r = DailyWindowReset(None)
    assert not r.enabled
    assert r.due_chats(datetime(2026, 10, 14, 4, 0), ["A"]) == []


def test_load_backoff_doubles_and_recovers():
    b = LoadBackoff(high=90, low=60, high_hits=3, max_factor=8)
    assert b.update(95) == 1
    assert b.update(95) == 1
    assert b.update(95) == 2
    for _ in range(3):
        b.update(99)
    assert b.factor == 4
    for _ in range(30):
        b.update(99)
    assert b.factor == 8  # 封顶
    assert b.update(75) == 8  # 60~90 之间保持
    assert b.update(30) == 4
    assert b.update(30) == 2
    assert b.update(30) == 1
    assert b.update(None) == 1


def test_load_backoff_streak_resets_on_normal_sample():
    b = LoadBackoff(high=90, low=60, high_hits=3)
    b.update(95)
    b.update(95)
    b.update(70)  # 中断连续高负载
    assert b.update(95) == 1


def test_affinity_mask():
    assert affinity_mask(0, 8) == 0
    assert affinity_mask(8, 8) == 0
    assert affinity_mask(1, 8) == 0b1
    assert affinity_mask(2, 8) == 0b11


def test_cpu_usage_between():
    # idle 增加 25，kernel(含 idle) 增加 50，user 增加 50 → 总 100，忙 75
    assert SystemCpuSampler.usage_between((0, 0, 0), (25, 50, 50)) == 75.0
    assert SystemCpuSampler.usage_between((0, 0, 0), (0, 0, 0)) is None


def test_poll_stats_summary_and_reset():
    s = PollStats()
    s.record_chat("群A", 120, items=30, full_scan=True)
    s.record_chat("群A", 40, items=30)
    s.record_round(160)
    s.record_round(40)
    text = s.summary()
    assert "rounds=2" in text
    assert "avg=100ms" in text
    assert "[群A] avg=80ms max=120ms items/poll=30.0 full_scans=1" in text
    s.reset()
    assert s.rounds == 0 and not s.chats
