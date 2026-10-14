# -*- coding: utf-8 -*-
"""微信 4.x 群聊发送人辅助。

社区共识（pywechat / wx4py）：群昵称是图片，UIA 读不到。
成熟做法：截取气泡上方昵称条 → OCR → 对照已知群昵称/绑定。
本模块用 Windows 自带 OCR（winsdk），不点头像、不抢鼠标。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from gateway.chat_names import (
    is_email_like_nickname,
    nickname_match_key,
    normalize_chat_name,
    normalize_group_nickname,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_NICK_CACHE = _ROOT / "data" / "room_nicks.json"
_MY_ALIAS_CACHE = _ROOT / "data" / "room_my_alias.json"
_AVATAR_MAP_CACHE = _ROOT / "data" / "room_avatar_map.json"
_OCR_ENABLED = os.getenv("SENDER_OCR", "1").strip().lower() not in {"0", "false", "no", "off"}
_OCR_DEBUG = os.getenv("SENDER_OCR_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}


def room_nicks_path() -> Path:
    return _NICK_CACHE


def load_my_alias(chat_name: str) -> str:
    key = normalize_chat_name(chat_name)
    if not key:
        return ""
    data = _read_my_alias_cache()
    if key in data:
        return str(data[key] or "").strip()
    for k, v in data.items():
        if key in k or k in key:
            return str(v or "").strip()
    return ""


def remember_my_alias(chat_name: str, alias: str) -> None:
    key = normalize_chat_name(chat_name)
    val = normalize_group_nickname(alias) or (alias or "").strip()
    if not key or not val:
        return
    data = _read_my_alias_cache()
    data[key] = val
    try:
        _MY_ALIAS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _MY_ALIAS_CACHE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("write room_my_alias failed: %s", exc)


def _read_my_alias_cache() -> dict[str, str]:
    try:
        if not _MY_ALIAS_CACHE.exists():
            return {}
        raw = json.loads(_MY_ALIAS_CACHE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("read room_my_alias failed: %s", exc)
        return {}


def load_room_nicks(chat_name: str = "") -> list[str] | dict[str, list[str]]:
    """chat_name 为空时返回全表；否则返回该群昵称列表。"""
    data = _read_cache()
    if not chat_name:
        return data
    key = normalize_chat_name(chat_name)
    if not key:
        return []
    if key in data:
        return list(data[key])
    for k, v in data.items():
        if key in k or k in key:
            return list(v)
    return []


def remember_room_nicks(chat_name: str, names: list[str]) -> None:
    """合并写入（兼容旧调用）。改名场景请用 set_room_nicks 全量覆盖。"""
    key = normalize_chat_name(chat_name)
    if not key:
        return
    data = _read_cache()
    merged: list[str] = list(data.get(key) or [])
    for n in names:
        s = normalize_group_nickname(str(n or "")) or str(n or "").strip()
        if not s:
            continue
        if any(x.casefold() == s.casefold() for x in merged):
            continue
        merged.append(s)
    data[key] = merged
    _write_cache(data)


def set_room_nicks(chat_name: str, names: list[str]) -> list[str]:
    """全量覆盖某群 OCR/匹配用昵称缓存（改名后必须用这个，否则旧名残留）。"""
    key = normalize_chat_name(chat_name)
    if not key:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for n in names:
        s = normalize_group_nickname(str(n or "")) or str(n or "").strip()
        if not s:
            continue
        cf = s.casefold()
        if cf in seen:
            continue
        seen.add(cf)
        cleaned.append(s)
    data = _read_cache()
    data[key] = cleaned
    _write_cache(data)
    return cleaned


def _read_cache() -> dict[str, list[str]]:
    try:
        if not _NICK_CACHE.exists():
            return {}
        raw = json.loads(_NICK_CACHE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[str]] = {}
        for k, v in raw.items():
            if isinstance(v, list):
                out[str(k)] = [str(x) for x in v if str(x).strip()]
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("read room_nicks failed: %s", exc)
        return {}


def _write_cache(data: dict[str, list[str]]) -> None:
    try:
        _NICK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _NICK_CACHE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("write room_nicks failed: %s", exc)


def normalize_ocr_nick(text: str) -> str:
    """OCR 结果规范化：去空格、常见误识。"""
    s = re.sub(r"\s+", "", (text or "").strip())
    if not s:
        return ""
    # 企微后缀常见误识：优准→优推、优稚→优推
    s = s.replace("薪火优准", "薪火优推").replace("薪火优稚", "薪火优推")
    s = s.replace("薪火优椎", "薪火优推").replace("薪火优锥", "薪火优推")
    return s


def _ocr_text_variants(compact: str) -> list[str]:
    """
    OCR 裁切偶发吃进头像边缘噪点（如「叫叽里呱啦」）。
    保留原文，并额外尝试去掉左侧 1～2 字。
    """
    s = (compact or "").strip()
    if not s:
        return []
    out = [s]
    for n in (1, 2):
        if len(s) > n + 2:
            out.append(s[n:])
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        k = x.casefold()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def match_text_to_candidates(text: str, candidates: list[str]) -> str:
    """
    将 OCR/UIA 文本匹配到已知昵称。
    规则：去空格后比对；长名优先；带 @ 的 OCR 优先配带 @ 的绑定，避免「叽里呱啦」抢走「叽里呱啦@企业」。
    """
    raw = (text or "").strip()
    if not raw or not candidates:
        return ""
    compact0 = normalize_ocr_nick(raw)
    if not compact0:
        return ""
    variants = _ocr_text_variants(compact0)

    scored: list[tuple[float, int, str]] = []
    for cand in candidates:
        c = normalize_group_nickname(cand) or cand.strip()
        if not c:
            continue
        c_compact = re.sub(r"\s+", "", c)
        c_cf = c_compact.casefold()
        cand_has_at = "@" in c_compact
        # 绑定「优雅」时同时接受 OCR「优推」
        cand_alts = [c_compact]
        if "薪火优雅" in c_compact:
            cand_alts.append(c_compact.replace("薪火优雅", "薪火优推"))
        if "薪火优推" in c_compact:
            cand_alts.append(c_compact.replace("薪火优推", "薪火优雅"))

        best_for_cand = 0.0
        for compact in variants:
            compact_cf = compact.casefold()
            ocr_has_at = "@" in compact
            stripped = compact.casefold() != compact0.casefold()
            for c_alt in cand_alts:
                c_alt_cf = c_alt.casefold()
                score = 0.0
                if compact_cf == c_alt_cf:
                    # 去前缀后的精确命中略低于原文精确，但仍很高
                    score = (295 if stripped else 300) + len(c_alt)
                else:
                    ratio = _similarity(compact, c_alt)
                    if ratio >= 0.88:
                        score = 200 * ratio + len(c_alt)
                    elif compact_cf.startswith(c_alt_cf) and len(c_alt_cf) >= 2:
                        rest = compact_cf[len(c_alt_cf) :]
                        if not rest:
                            score = 180 + len(c_alt)
                        elif rest.startswith("@") and cand_has_at:
                            score = 50
                        elif not rest[:1].isalnum() and not rest.startswith("@"):
                            score = 120 + len(c_alt)
                    elif c_alt_cf.startswith(compact_cf) and len(compact_cf) >= 3:
                        coverage = len(compact_cf) / max(1, len(c_alt_cf))
                        if coverage >= 0.5:
                            score = 100 * coverage + len(compact_cf)
                    elif ratio >= 0.75 and len(compact_cf) >= 3:
                        score = 80 * ratio + len(compact_cf) * 0.5

                if score <= 0:
                    continue
                if ocr_has_at and cand_has_at:
                    score += 40
                elif ocr_has_at and not cand_has_at:
                    score *= 0.25
                elif (not ocr_has_at) and cand_has_at:
                    if compact_cf != nickname_match_key(c) and _similarity(
                        compact, c_alt.split("@", 1)[0]
                    ) < 0.95:
                        score *= 0.5
                # 原文带无关前缀、且未走到「去前缀精确」时：降权模糊分
                if (
                    not stripped
                    and len(compact0) > len(c_alt)
                    and c_alt_cf in compact0.casefold()
                ):
                    idx = compact0.casefold().find(c_alt_cf)
                    if idx > 0 and compact0.casefold()[idx - 1] not in {
                        "@",
                        "（",
                        "(",
                        "/",
                    }:
                        # 仅保留去前缀变体上的高分；原文模糊分扔掉
                        if compact_cf == compact0.casefold():
                            score = 0.0
                best_for_cand = max(best_for_cand, score)

        if best_for_cand > 0:
            scored.append((best_for_cand, len(c), c))

    if not scored:
        return ""
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, _, best = scored[0]
    if best_score < 40:
        return ""
    return best


def capture_window_image(win: Any) -> tuple[Any, Any] | None:
    """PrintWindow 截取窗口，返回 (PIL.Image, GetWindowRect)。用于头像侧判定/缓存。"""
    import ctypes
    from ctypes import wintypes

    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        hwnd = int(win.handle)
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    if w < 40 or h < 40:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBX", (w, h), buf, "raw", "BGRX", 0, 1).convert("RGB")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return img, rect


def item_image_box(item: Any, win_rect: Any) -> tuple[int, int, int, int] | None:
    """返回相对窗口的 item 框；若无 win_rect 则返回屏幕绝对坐标。"""
    try:
        ir = item.rectangle()
        if win_rect is None:
            return (int(ir.left), int(ir.top), int(ir.right), int(ir.bottom))
        return (
            int(ir.left - win_rect.left),
            int(ir.top - win_rect.top),
            int(ir.right - win_rect.left),
            int(ir.bottom - win_rect.top),
        )
    except Exception:  # noqa: BLE001
        return None


def avatar_side_and_hash(
    window_img: Any, win_rect: Any, item: Any
) -> tuple[str, str]:
    """返回 (LEFT|RIGHT|UNCLEAR, sha1)。优先窗口截图；失败则屏幕 BitBlt。"""
    try:
        from PIL import ImageStat
    except ImportError:
        return "UNCLEAR", ""
    box = item_image_box(item, win_rect)
    if box is None:
        return "UNCLEAR", ""
    left, top, right, bottom = box
    item_img = None
    if window_img is not None and win_rect is not None:
        left_c = max(0, left)
        top_c = max(0, top)
        right_c = min(window_img.width, right)
        bottom_c = min(window_img.height, bottom)
        if right_c - left_c >= 40 and bottom_c - top_c >= 20:
            try:
                item_img = window_img.crop((left_c, top_c, right_c, bottom_c))
            except Exception:  # noqa: BLE001
                item_img = None
    if item_img is None:
        # 屏幕绝对坐标 BitBlt（PrintWindow 黑屏时仍可用）
        if win_rect is not None:
            sx0 = int(win_rect.left) + int(left)
            sy0 = int(win_rect.top) + int(top)
            sx1 = int(win_rect.left) + int(right)
            sy1 = int(win_rect.top) + int(bottom)
        else:
            sx0, sy0, sx1, sy1 = left, top, right, bottom
        item_img = _bitblt_region(sx0, sy0, sx1, sy1)
    if item_img is None:
        return "UNCLEAR", ""
    aw = min(52, item_img.width // 4)
    left_av = item_img.crop((0, 0, aw, item_img.height))
    right_av = item_img.crop(
        (item_img.width - aw, 0, item_img.width, item_img.height)
    )
    lv = sum(ImageStat.Stat(left_av).var)
    rv = sum(ImageStat.Stat(right_av).var)
    if lv > rv * 1.25:
        side, av = "LEFT", left_av
    elif rv > lv * 1.25:
        side, av = "RIGHT", right_av
    else:
        side, av = "UNCLEAR", left_av
    try:
        digest = hashlib.sha1(av.resize((32, 32)).tobytes()).hexdigest()
    except Exception:  # noqa: BLE001
        digest = ""
    return side, digest


def _partition_at_bindings(candidates: list[str]) -> tuple[list[str], list[str]]:
    with_at: list[str] = []
    without: list[str] = []
    for cand in candidates:
        c = normalize_group_nickname(cand) or (cand or "").strip()
        if not c:
            continue
        if "@" in c and not is_email_like_nickname(c):
            with_at.append(c)
        else:
            without.append(c)
    return with_at, without


def _is_wecom_orange_pixel(r: int, g: int, b: int) -> bool:
    """企微群昵称后缀常见橙/琥珀色。"""
    return (
        r >= 175
        and 55 <= g <= 210
        and b <= 150
        and (r - b) >= 55
        and (r - g) >= 15
    )


def nick_strip_has_wecom_orange(
    window_img: Any, win_rect: Any, item: Any
) -> bool | None:
    """
    检测气泡上方昵称条是否含企微橙色后缀。
    返回 True/False；无法取样时返回 None。
    """
    crops = list_nickname_crops(window_img, win_rect, item, side="LEFT")
    if not crops:
        return None
    for strip in crops:
        try:
            rgb = strip.convert("RGB")
            # 缩小加速
            small = rgb.resize((max(40, rgb.width // 2), max(12, rgb.height // 2)))
            pix = small.load()
            w, h = small.size
            orange = 0
            total = w * h
            for y in range(h):
                for x in range(w):
                    r, g, b = pix[x, y]
                    if _is_wecom_orange_pixel(r, g, b):
                        orange += 1
            ratio = orange / max(1, total)
            logger.info(
                "wecom-orange orange=%s total=%s ratio=%.4f",
                orange,
                total,
                ratio,
            )
            # 后缀色块不大，按绝对数量 + 比例双门槛
            if orange >= 12 or ratio >= 0.012:
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("wecom orange detect failed: %s", exc)
            continue
    return False


def resolve_sender_by_wecom_tag(
    window_img: Any,
    win_rect: Any,
    item: Any,
    candidates: list[str],
) -> str:
    """
    不 OCR：用「有无企微橙色 @企业」区分短名 vs 名@企业。
    仅当绑定正好拆成「1 个带 @ + 1 个不带 @」时可用。
    """
    with_at, without = _partition_at_bindings(candidates)
    if len(with_at) != 1 or len(without) != 1:
        return ""
    flag = nick_strip_has_wecom_orange(window_img, win_rect, item)
    if flag is None:
        return ""
    hit = with_at[0] if flag else without[0]
    logger.info(
        "wecom-tag resolve orange=%s -> %s",
        flag,
        hit,
    )
    return hit


def load_avatar_map(chat_name: str) -> dict[str, str]:
    key = normalize_chat_name(chat_name)
    if not key:
        return {}
    try:
        if not _AVATAR_MAP_CACHE.exists():
            return {}
        raw = json.loads(_AVATAR_MAP_CACHE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        room = raw.get(key) or {}
        if not isinstance(room, dict):
            return {}
        return {str(k): str(v) for k, v in room.items() if str(v).strip()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("read avatar map failed: %s", exc)
        return {}


def remember_avatar_sender(chat_name: str, av_hash: str, sender: str) -> None:
    key = normalize_chat_name(chat_name)
    h = (av_hash or "").strip()
    nick = normalize_group_nickname(sender) or (sender or "").strip()
    if not key or not h or not nick or nick == "未知发送人":
        return
    try:
        data: dict[str, Any] = {}
        if _AVATAR_MAP_CACHE.exists():
            raw = json.loads(_AVATAR_MAP_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        room = data.get(key)
        if not isinstance(room, dict):
            room = {}
        room[h] = nick
        # 限制每群缓存规模
        if len(room) > 40:
            room = dict(list(room.items())[-40:])
        data[key] = room
        _AVATAR_MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _AVATAR_MAP_CACHE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("write avatar map failed: %s", exc)


def get_window_rect(win: Any) -> Any | None:
    """仅取窗口矩形，不截图。"""
    import ctypes
    from ctypes import wintypes

    try:
        hwnd = int(win.handle)
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    rect = RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect


def bubble_looks_self(window_img: Any, win_rect: Any, item: Any) -> bool:
    """
    用气泡颜色判断是否自己发的（pywechat 作者建议：己方偏绿，对方偏白）。
    不点击、不 OCR。
    """
    try:
        from PIL import ImageStat
    except ImportError:
        return False
    box = item_image_box(item, win_rect)
    if box is None or window_img is None:
        return False
    left, top, right, bottom = box
    left = max(0, left)
    top = max(0, top)
    right = min(window_img.width, right)
    bottom = min(window_img.height, bottom)
    if right - left < 60 or bottom - top < 24:
        return False
    try:
        # 取行中部偏右的一条带，避开左侧头像
        x0 = left + int((right - left) * 0.35)
        x1 = right - 8
        y0 = top + 4
        y1 = bottom - 4
        if x1 - x0 < 20 or y1 - y0 < 10:
            return False
        crop = window_img.crop((x0, y0, x1, y1))
        stat = ImageStat.Stat(crop)
        r, g, b = stat.mean[0], stat.mean[1], stat.mean[2]
    except Exception:  # noqa: BLE001
        return False
    # 微信己方气泡常见：绿感（G 明显高于 R/B），对方近白灰
    if g > r + 12 and g > b + 8 and g > 140:
        return True
    if r > 200 and g > 200 and b > 200:
        return False
    return False


def ocr_enabled() -> bool:
    return _OCR_ENABLED


def crop_nickname_strip(
    window_img: Any, win_rect: Any, item: Any, *, side: str = "LEFT"
) -> Any | None:
    """兼容旧接口：返回第一块可用昵称条。"""
    crops = list_nickname_crops(window_img, win_rect, item, side=side)
    return crops[0] if crops else None


def list_nickname_crops(
    window_img: Any, win_rect: Any, item: Any, *, side: str = "LEFT"
) -> list[Any]:
    """
    多区域裁切昵称条（微信昵称常在气泡正上方，可能略超出 ListItem）。
    优先屏幕 BitBlt，避免 PrintWindow 黑屏。
    """
    if side == "RIGHT":
        return []
    box = item_image_box(item, win_rect)
    if box is None:
        return []
    left, top, right, bottom = box
    # box 已是相对窗口；BitBlt 需要屏幕坐标
    if win_rect is not None:
        offset_x = int(win_rect.left)
        offset_y = int(win_rect.top)
    else:
        # box 即为屏幕绝对坐标
        offset_x = 0
        offset_y = 0
    w = max(1, right - left)
    h = max(1, bottom - top)
    if w < 60 or h < 20:
        return []

    # 相对 ListItem 的若干候选框（屏幕坐标）
    regions: list[tuple[int, int, int, int]] = []
    # 1) 气泡上方（最常见：昵称在 item 顶边之上）
    # 左侧多留空，避开头像边缘噪点（曾误识出「叫叽里呱啦」）
    regions.append(
        (
            offset_x + left + 52,
            offset_y + top - 28,
            offset_x + left + min(w - 8, max(220, int(w * 0.7))),
            offset_y + top + 6,
        )
    )
    # 2) item 顶部条
    regions.append(
        (
            offset_x + left + 56,
            offset_y + top + 1,
            offset_x + left + min(w - 8, max(200, int(w * 0.75))),
            offset_y + top + min(36, max(22, h // 3)),
        )
    )

    out: list[Any] = []
    seen: set[tuple[int, int, int, int]] = set()
    for x0, y0, x1, y1 in regions:
        if x1 - x0 < 40 or y1 - y0 < 12:
            continue
        key = (x0, y0, x1, y1)
        if key in seen:
            continue
        seen.add(key)
        strip = _bitblt_region(x0, y0, x1, y1)
        if strip is None and window_img is not None and win_rect is not None:
            # 回退到窗口截图相对坐标
            lx0 = max(0, x0 - offset_x)
            ly0 = max(0, y0 - offset_y)
            lx1 = min(window_img.width, x1 - offset_x)
            ly1 = min(window_img.height, y1 - offset_y)
            if lx1 - lx0 >= 40 and ly1 - ly0 >= 12:
                try:
                    strip = window_img.crop((lx0, ly0, lx1, ly1))
                except Exception:  # noqa: BLE001
                    strip = None
        if strip is None:
            continue
        try:
            from PIL import ImageStat

            if sum(ImageStat.Stat(strip).mean) < 12:
                continue
            out.append(strip.resize((strip.width * 2, strip.height * 2)))
        except Exception:  # noqa: BLE001
            continue
    return out


def _bitblt_region(left: int, top: int, right: int, bottom: int) -> Any | None:
    import ctypes

    try:
        from PIL import Image
    except ImportError:
        return None
    w = max(1, int(right - left))
    h = max(1, int(bottom - top))
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, int(left), int(top), 0x00CC0020)
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBX", (w, h), buf, "raw", "BGRX", 0, 1).convert("RGB")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)
    return img


def ocr_image_text(img: Any) -> str:
    """Windows.Media.Ocr 识别图片文字（需 winsdk）。"""
    if img is None:
        return ""
    try:
        from PIL import Image
    except ImportError:
        return ""
    if not isinstance(img, Image.Image):
        return ""

    async def _run() -> str:
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import FileAccessMode, StorageFile

        engine = OcrEngine.try_create_from_language(Language("zh-Hans"))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return ""
        tmp = Path(tempfile.gettempdir()) / f"xh_nick_ocr_{os.getpid()}.png"
        img.convert("RGB").save(tmp, format="PNG")
        try:
            file = await StorageFile.get_file_from_path_async(str(tmp))
            stream = await file.open_async(FileAccessMode.READ)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await engine.recognize_async(bitmap)
            return (result.text or "").strip()
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    try:
        return asyncio.run(_run()) or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("winsdk ocr failed: %s", exc)
        return ""


def resolve_sender_by_ocr(
    window_img: Any,
    win_rect: Any,
    item: Any,
    candidates: list[str],
    *,
    side: str = "LEFT",
) -> str:
    """
    wx4py 同款思路：多区域 OCR 昵称条 → 对照已知昵称。
    """
    if not _OCR_ENABLED or not candidates:
        return ""
    crops = list_nickname_crops(window_img, win_rect, item, side=side)
    if not crops:
        logger.info("ocr nick: no crop side=%s", side)
        return ""
    for i, strip in enumerate(crops):
        if _OCR_DEBUG:
            try:
                debug_dir = _ROOT / "data" / "ocr_debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                strip.save(debug_dir / f"nick_{os.getpid()}_{i}.png")
            except Exception:  # noqa: BLE001
                pass
        raw = ocr_image_text(strip)
        if not raw:
            continue
        normalized = normalize_ocr_nick(raw)
        # 明显不是昵称条（窗口标题/时间/杂讯）
        junk_markers = ("（", "(", "）", ")", "bot", "Bot", ":", "：")
        probe = normalized or re.sub(r"\s+", "", raw)
        if probe and (
            any(m in probe for m in ("Etbbot", "ttestbbot", "不急了"))
            or (any(m in probe for m in junk_markers) and "@" not in probe and len(probe) <= 12)
            or re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", probe)
        ):
            logger.info(
                "ocr nick#%s raw=%r norm=%r hit='' (junk)",
                i,
                raw[:80],
                (normalized or "")[:80],
            )
            continue
        hit = match_text_to_candidates(normalized or raw, candidates)
        logger.info(
            "ocr nick#%s raw=%r norm=%r hit=%r",
            i,
            raw[:80],
            (normalized or "")[:80],
            hit,
        )
        if hit:
            return hit
    return ""
