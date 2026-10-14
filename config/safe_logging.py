"""Windows 下多进程共用同一日志文件时的安全轮转。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    """
    Django 网关与 Worker 同时写 data/runtime.log 时，
    标准 RotatingFileHandler 在 os.rename 会触发 WinError 32。
    轮转失败时跳过本次 rollover，继续追加，避免刷屏 Logging error。
    """

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except PermissionError:
            return
        except OSError as exc:
            # WinError 32: 另一个程序正在使用此文件
            if getattr(exc, "winerror", None) == 32 or exc.errno in {13, 11}:
                return
            raise

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # 避免轮转失败时向 stderr 刷长堆栈（业务仍正常）
        try:
            super().handleError(record)
        except Exception:
            pass
