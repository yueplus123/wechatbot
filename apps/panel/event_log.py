"""网页「日志」页：统一读写 data/runtime.log。"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = _ROOT / "data" / "runtime.log"
_LOCK = threading.Lock()


def log_path() -> Path:
    return LOG_PATH


def append(line: str, *, source: str = "panel") -> None:
    text = (line or "").rstrip()
    if not text:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = f"{ts} [{source}] {text}\n"
    with _LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(row)


_TAIL_BYTES = 128 * 1024  # 日志页只需最后几百行，不读整个文件


def read_tail(n: int = 400) -> list[str]:
    if n < 1:
        n = 1
    if not LOG_PATH.exists():
        return ["（暂无日志。启动网关服务后，Worker / 面板操作会写到这里。）"]
    try:
        with LOG_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return [f"（读取日志失败：{exc}）"]
    lines = raw.splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # 首行可能被截断
    if not lines:
        return ["（日志文件为空）"]
    return lines[-n:]


def clear() -> None:
    with _LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            LOG_PATH.write_text("", encoding="utf-8")
        except PermissionError:
            # Windows：Worker/Django 正占用文件时改用截断
            try:
                with LOG_PATH.open("r+", encoding="utf-8") as f:
                    f.truncate(0)
            except Exception:
                pass
