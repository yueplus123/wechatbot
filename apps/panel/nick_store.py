"""网页控制台专用：群成员昵称缓存（Worker / room_nicks.json 不得改写本文件）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CACHE = _ROOT / "data" / "panel_member_nicks.json"
_LOCK = threading.Lock()


def _read() -> dict[str, list[str]]:
    try:
        if not _CACHE.exists():
            return {}
        raw = json.loads(_CACHE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[str]] = {}
        for k, v in raw.items():
            if isinstance(v, list):
                out[str(k)] = [str(x).strip() for x in v if str(x).strip()]
        return out
    except Exception:
        return {}


def _write(data: dict[str, list[str]]) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _clean(nicks: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in nicks:
        s = str(n or "").strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def save_room_members(room_id: int, nicks: list[str]) -> list[str]:
    cleaned = _clean(nicks)
    with _LOCK:
        data = _read()
        data[str(int(room_id))] = cleaned
        _write(data)
    return cleaned


def load_room_members(room_id: int) -> list[str]:
    with _LOCK:
        data = _read()
    return list(data.get(str(int(room_id))) or [])


def unbound_members(room_id: int, bound_nicks: list[str]) -> list[str]:
    bound = {str(x or "").strip().casefold() for x in bound_nicks if str(x or "").strip()}
    out: list[str] = []
    for n in load_room_members(room_id):
        if n.casefold() in bound:
            continue
        out.append(n)
    return out
