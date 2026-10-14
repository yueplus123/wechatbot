"""
Worker 性能与稳定性辅助：

- 进程优先级 / CPU 亲和性（防止整机卡死）
- 系统 CPU 采样 + 负载退避（系统过载时自动放慢轮询）
- 轮询计时统计（定期写日志，便于观察耗时是否随时间增长）
- 每日定时重置群窗口的调度判断

除 Windows API 调用外均为纯逻辑，可在任意平台单测。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "low": 0x00000040,
    "below_normal": 0x00004000,
    "belownormal": 0x00004000,
    "normal": 0x00000020,
}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- 优先级 / 亲和性


def affinity_mask(cores: int, total: int) -> int:
    """前 cores 个逻辑核的掩码；cores<=0 或 >=total 表示不限制（返回 0）。"""
    if cores <= 0 or total <= 0 or cores >= total:
        return 0
    return (1 << cores) - 1


def apply_process_limits() -> None:
    """
    按环境变量设置本进程优先级和 CPU 亲和性（仅 Windows 生效）。
    WORKER_PRIORITY: idle(默认)/below_normal/normal
    WORKER_CPU_CORES: 只用前 N 个逻辑核，0=不限制（默认）
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    # 64 位下句柄是指针宽度：必须声明参数/返回类型，否则伪句柄 -1 会被截断成无效句柄
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    proc = kernel32.GetCurrentProcess()

    level = (os.getenv("WORKER_PRIORITY", "idle") or "idle").strip().lower()
    cls = _PRIORITY_CLASSES.get(level)
    if cls is None:
        logger.warning("未知 WORKER_PRIORITY=%r，保持当前优先级", level)
    elif kernel32.SetPriorityClass(proc, cls):
        logger.info("worker priority=%s", level)
    else:
        logger.warning(
            "设置进程优先级失败 level=%s err=%s", level, ctypes.get_last_error()
        )

    cores = env_int("WORKER_CPU_CORES", 0)
    mask = affinity_mask(cores, os.cpu_count() or 0)
    if mask:
        if kernel32.SetProcessAffinityMask(proc, mask):
            logger.info("worker cpu affinity: first %s core(s)", cores)
        else:
            logger.warning(
                "设置 CPU 亲和性失败 cores=%s err=%s", cores, ctypes.get_last_error()
            )


# ---------------------------------------------------------------- 系统 CPU 采样


class SystemCpuSampler:
    """用 GetSystemTimes 计算两次采样间的系统总 CPU 占用（%）；非 Windows 返回 None。"""

    def __init__(self) -> None:
        self._last: tuple[int, int, int] | None = None

    @staticmethod
    def _read() -> tuple[int, int, int] | None:
        if sys.platform != "win32":
            return None
        import ctypes
        from ctypes import wintypes

        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
        )
        if not ok:
            return None

        def _v(ft: wintypes.FILETIME) -> int:
            return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

        return _v(idle), _v(kern), _v(user)

    @staticmethod
    def usage_between(
        prev: tuple[int, int, int], curr: tuple[int, int, int]
    ) -> float | None:
        d_idle = curr[0] - prev[0]
        d_kern = curr[1] - prev[1]  # kernel 时间含 idle
        d_user = curr[2] - prev[2]
        total = d_kern + d_user
        if total <= 0:
            return None
        busy = total - d_idle
        return max(0.0, min(100.0, busy * 100.0 / total))

    def sample(self) -> float | None:
        curr = self._read()
        if curr is None:
            return None
        prev, self._last = self._last, curr
        if prev is None:
            return None
        return self.usage_between(prev, curr)


# ---------------------------------------------------------------- 负载退避


@dataclass
class LoadBackoff:
    """
    系统 CPU 连续 high_hits 次 >= high 时，轮询间隔倍数翻倍（上限 max_factor）；
    CPU < low 时每次减半，回到 1。
    """

    high: float = 90.0
    low: float = 60.0
    high_hits: int = 3
    max_factor: float = 8.0
    factor: float = 1.0
    _streak: int = 0

    @classmethod
    def from_env(cls) -> "LoadBackoff":
        return cls(
            high=env_float("WORKER_CPU_HIGH", 90.0),
            low=env_float("WORKER_CPU_LOW", 60.0),
            high_hits=max(1, env_int("WORKER_CPU_HIGH_HITS", 3)),
            max_factor=max(1.0, env_float("WORKER_BACKOFF_MAX", 8.0)),
        )

    def update(self, cpu: float | None) -> float:
        if cpu is None:
            return self.factor
        if cpu >= self.high:
            self._streak += 1
            if self._streak >= self.high_hits and self.factor < self.max_factor:
                self.factor = min(self.max_factor, self.factor * 2)
                self._streak = 0
        else:
            self._streak = 0
            if cpu < self.low and self.factor > 1.0:
                self.factor = max(1.0, self.factor / 2)
        return self.factor


# ---------------------------------------------------------------- 轮询计时统计


@dataclass
class _ChatStat:
    polls: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    items: int = 0
    full_scans: int = 0


@dataclass
class PollStats:
    """累计一段时间内的轮询耗时，summary() 输出后清零。"""

    chats: dict[str, _ChatStat] = field(default_factory=dict)
    rounds: int = 0
    round_total_ms: float = 0.0
    round_max_ms: float = 0.0
    started: float = field(default_factory=time.monotonic)

    def record_chat(
        self, chat: str, ms: float, items: int = 0, full_scan: bool = False
    ) -> None:
        st = self.chats.setdefault(chat, _ChatStat())
        st.polls += 1
        st.total_ms += ms
        st.max_ms = max(st.max_ms, ms)
        st.items += items
        if full_scan:
            st.full_scans += 1

    def record_round(self, ms: float) -> None:
        self.rounds += 1
        self.round_total_ms += ms
        self.round_max_ms = max(self.round_max_ms, ms)

    def summary(self) -> str:
        mins = (time.monotonic() - self.started) / 60
        avg = self.round_total_ms / self.rounds if self.rounds else 0.0
        parts = [
            f"{mins:.0f}min rounds={self.rounds} avg={avg:.0f}ms max={self.round_max_ms:.0f}ms"
        ]
        for name, st in sorted(self.chats.items()):
            c_avg = st.total_ms / st.polls if st.polls else 0.0
            c_items = st.items / st.polls if st.polls else 0.0
            parts.append(
                f"[{name}] avg={c_avg:.0f}ms max={st.max_ms:.0f}ms "
                f"items/poll={c_items:.1f} full_scans={st.full_scans}"
            )
        return " | ".join(parts)

    def reset(self) -> None:
        self.chats.clear()
        self.rounds = 0
        self.round_total_ms = 0.0
        self.round_max_ms = 0.0
        self.started = time.monotonic()


# ---------------------------------------------------------------- 每日窗口重置调度


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    """'04:00' -> (4, 0)；空 / off / 非法 -> None（关闭）。"""
    s = (value or "").strip().lower()
    if not s or s in {"off", "0", "none", "false", "no"}:
        return None
    try:
        h, m = s.split(":", 1)
        hh, mm = int(h), int(m)
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh, mm


def reset_due(
    now: datetime,
    at: tuple[int, int] | None,
    last_done: date | None,
    window_minutes: int = 120,
) -> bool:
    """
    今天到点（at 之后 window_minutes 分钟内）且今天还没做过 → True。
    超出窗口不补做，避免白天 Worker 重启时突然关窗。
    """
    if at is None:
        return False
    if last_done == now.date():
        return False
    start = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    delta = (now - start).total_seconds()
    return 0 <= delta < window_minutes * 60


class DailyWindowReset:
    """
    每天定时重置群窗口的调度：
    - 到点且当天未做 → 返回全部监听群
    - 失败的群每 retry_seconds 重试一次，仅限当天
    - 执行日期写入 state_path，Worker 当天重启不会重复关窗
    """

    def __init__(
        self,
        at: tuple[int, int] | None,
        state_path: Path | None = None,
        retry_seconds: float = 600.0,
        window_minutes: int = 120,
    ) -> None:
        self.at = at
        self.state_path = state_path
        self.retry_seconds = retry_seconds
        self.window_minutes = window_minutes
        self.last_done: date | None = None
        self.pending: list[str] = []
        self.next_retry: datetime | None = None
        self._load()

    @classmethod
    def from_env(cls, state_path: Path | None = None) -> "DailyWindowReset":
        return cls(
            parse_hhmm(os.getenv("WECHAT_WINDOW_RESET_AT", "04:00")),
            state_path=state_path,
            retry_seconds=env_float("WECHAT_WINDOW_RESET_RETRY_SECONDS", 600.0),
        )

    @property
    def enabled(self) -> bool:
        return self.at is not None

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.last_done = date.fromisoformat(data["last_done"])
        except Exception:  # noqa: BLE001
            self.last_done = None

    def _save(self) -> None:
        if not self.state_path or self.last_done is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"last_done": self.last_done.isoformat()}), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("保存窗口重置状态失败: %s", exc)

    def due_chats(self, now: datetime, chats: list[str]) -> list[str]:
        if not self.enabled or not chats:
            return []
        if self.pending:
            if self.last_done != now.date():
                # 跨天了，放弃昨天没成功的重试
                self.pending = []
                self.next_retry = None
            elif self.next_retry is not None and now >= self.next_retry:
                return [c for c in self.pending if c in chats]
            else:
                return []
        if reset_due(now, self.at, self.last_done, self.window_minutes):
            return list(chats)
        return []

    def record(self, now: datetime, results: dict[str, bool]) -> None:
        self.last_done = now.date()
        self._save()
        self.pending = [chat for chat, ok in results.items() if not ok]
        self.next_retry = (
            now + timedelta(seconds=self.retry_seconds) if self.pending else None
        )
