"""网页控制台视图：复用 apps.panel.services 业务函数。"""
from __future__ import annotations

import os
from pathlib import Path

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

ROOT = Path(__file__).resolve().parents[2]
PAUSE_FLAG = ROOT / "data" / "pause.flag"
BOOK_LABELS = {
    "staff_order": "员工记单",
    "ledger": "记账",
    "usdt": "USDT",
}


def _paused() -> bool:
    return PAUSE_FLAG.exists()


def _set_paused(paused: bool) -> None:
    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSE_FLAG.write_text("1", encoding="utf-8")
    elif PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()


def _unbound_nicks(all_nicks: list[str], bindings: list[dict]) -> list[str]:
    from apps.panel.nick_store import unbound_members

    # room_id 未知时用列表过滤
    bound = {(b.get("nick") or "").strip().casefold() for b in bindings}
    out: list[str] = []
    seen: set[str] = set()
    for n in all_nicks:
        s = str(n or "").strip()
        if not s:
            continue
        key = s.casefold()
        if key in bound or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _worker_running() -> bool:
    """Worker 每 30 秒上报一次心跳；90 秒内有心跳即视为运行中。"""
    from datetime import timedelta

    from django.utils import timezone

    from apps.audit.models import GatewayHeartbeat

    gid = os.getenv("GATEWAY_ID", "wx-gateway-01")
    ts = (
        GatewayHeartbeat.objects.filter(gateway_id=gid)
        .values_list("updated_at", flat=True)
        .first()
    )
    return bool(ts and timezone.now() - ts < timedelta(seconds=90))


def _nav_ctx(**extra):
    ctx = {
        "paused": _paused(),
        "worker_running": _worker_running(),
        "book_labels": BOOK_LABELS,
    }
    ctx.update(extra)
    return ctx


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("panel:home")
    error = ""
    if request.method == "POST":
        user = authenticate(
            request,
            username=(request.POST.get("username") or "").strip(),
            password=request.POST.get("password") or "",
        )
        if user is not None:
            login(request, user)
            return redirect(request.GET.get("next") or "panel:home")
        error = "用户名或密码错误"
    return render(request, "panel/login.html", {"error": error})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("panel:login")


@login_required
def home(request: HttpRequest) -> HttpResponse:
    """概览 = 状态 + 听群列表 + 薪火连接（原三个页面合并）。"""
    from apps.chatrooms.models import ChatRoom
    from apps.integrations.xinhuo_push import xinhuo_connection_info
    from apps.ledger.models import LedgerEntry

    rooms = list(ChatRoom.objects.order_by("-enabled", "name"))
    return render(
        request,
        "panel/home.html",
        _nav_ctx(
            rooms=rooms,
            room_count=sum(1 for r in rooms if r.enabled),
            entry_count=LedgerEntry.objects.count(),
            gateway_id=os.getenv("GATEWAY_ID", "wx-gateway-01"),
            info=xinhuo_connection_info(),
        ),
    )


@login_required
@require_POST
def xinhuo_test(request: HttpRequest) -> HttpResponse:
    from apps.integrations.xinhuo_push import test_xinhuo_connection

    result = test_xinhuo_connection()
    if result.get("ok"):
        messages.success(request, "薪火连通性测试通过")
    else:
        messages.error(request, result.get("message") or "薪火连通性测试失败")
    return redirect("panel:home")


@login_required
@require_POST
def set_pause(request: HttpRequest) -> HttpResponse:
    from apps.panel import event_log

    action = (request.POST.get("action") or "").strip()
    if action == "pause":
        _set_paused(True)
        event_log.append("已暂停听群", source="panel")
        messages.success(request, "已暂停听群（Worker 仍运行，但不再处理消息）")
    elif action == "resume":
        _set_paused(False)
        event_log.append("已继续听群", source="panel")
        messages.success(request, "已继续听群")
    return redirect(request.POST.get("next") or "panel:home")


@login_required
@require_POST
def room_toggle(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.chatrooms.models import ChatRoom

    room = get_object_or_404(ChatRoom, pk=room_id)
    room.enabled = not room.enabled
    room.save(update_fields=["enabled", "updated_at"])
    messages.success(
        request, f"「{room.name}」已{'启用' if room.enabled else '停用'}监听"
    )
    return redirect("panel:home")


@login_required
def room_detail(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.chatrooms.models import ChatRoom
    from apps.panel import nick_store
    from apps.panel import services as svc

    room = get_object_or_404(ChatRoom, pk=room_id)
    settings_data = svc.load_room_settings(room.name)
    bindings = svc.list_bindings(room.name)
    all_nicks = nick_store.load_room_members(room.id)
    nicks = _unbound_nicks(all_nicks, bindings)
    return render(
        request,
        "panel/room_detail.html",
        _nav_ctx(
            room=room,
            bindings=bindings,
            settings=settings_data,
            nicks=nicks,
            nick_total=len(all_nicks),
        ),
    )


@login_required
@require_POST
def room_settings_save(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.chatrooms.models import ChatRoom
    from apps.panel import services as svc

    room = get_object_or_404(ChatRoom, pk=room_id)
    books = request.POST.getlist("books")
    biz_keys = request.POST.getlist("biz_keys")
    data = svc.load_room_settings(room.name)
    rid = int(data.get("room_id") or room.id)
    try:
        svc.save_room_settings(rid, books, biz_keys)
        messages.success(request, "群设置已保存")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect("panel:room_detail", room_id=room.id)


@login_required
@require_POST
def binding_add(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.chatrooms.models import ChatRoom
    from apps.panel import nick_store
    from apps.panel import services as svc
    from django.http import JsonResponse

    room = get_object_or_404(ChatRoom, pk=room_id)
    want_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )
    try:
        msg = svc.add_binding(
            room.name,
            request.POST.get("nick") or "",
            request.POST.get("display") or "",
            request.POST.get("role") or "staff",
        )
        try:
            from apps.panel import event_log

            event_log.append(msg, source="panel")
        except Exception:
            pass
        if want_json:
            bindings = svc.list_bindings(room.name)
            all_nicks = nick_store.load_room_members(room.id)
            return JsonResponse(
                {
                    "ok": True,
                    "message": msg,
                    "bindings": bindings,
                    "nicks": _unbound_nicks(all_nicks, bindings),
                    "nick_total": len(all_nicks),
                }
            )
        messages.success(request, msg)
    except Exception as exc:  # noqa: BLE001
        if want_json:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)
        messages.error(request, str(exc))
    return redirect("panel:room_detail", room_id=room.id)


@login_required
@require_POST
def room_load_nicks(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.chatrooms.models import ChatRoom
    from apps.panel import nick_store
    from apps.panel import services as svc

    room = get_object_or_404(ChatRoom, pk=room_id)
    try:
        nicks = svc.load_nicks_for_room(room.name)
        nick_store.save_room_members(room.id, nicks)
        messages.success(request, f"已读取 {len(nicks)} 个昵称")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect("panel:room_detail", room_id=room.id)


@login_required
@require_POST
def binding_remove(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.panel import services as svc

    try:
        svc.remove_binding(int(request.POST.get("bind_id") or 0))
        messages.success(request, "已移除绑定")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect("panel:room_detail", room_id=room_id)


@login_required
@require_POST
def binding_role(request: HttpRequest, room_id: int) -> HttpResponse:
    from apps.panel import services as svc

    try:
        svc.set_binding_role(
            int(request.POST.get("bind_id") or 0),
            request.POST.get("role") or "staff",
        )
        messages.success(request, "角色已更新")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect("panel:room_detail", room_id=room_id)


@login_required
def book_page(request: HttpRequest, book: str) -> HttpResponse:
    if book not in BOOK_LABELS:
        return redirect("panel:home")
    from apps.panel import services as svc

    q = (request.GET.get("q") or "").strip()
    tab = "keywords" if request.GET.get("tab") == "keywords" else "records"
    ctx = {"book": book, "book_label": BOOK_LABELS[book], "q": q, "tab": tab}
    if tab == "keywords":
        ctx["keywords"] = svc.list_keywords(book)
    else:
        ctx["entries"] = svc.list_entries(book, limit=300, q=q)
        ctx["employees"] = svc.list_employees()
        ctx["rooms"] = svc.list_enabled_rooms()
    return render(request, "panel/book.html", _nav_ctx(**ctx))


@login_required
@require_POST
def entry_add(request: HttpRequest, book: str) -> HttpResponse:
    from apps.panel import services as svc

    try:
        svc.add_ledger_entry(
            book,
            person=request.POST.get("person") or "",
            room=request.POST.get("room") or "",
            business_name=request.POST.get("business_name") or "",
            receive_amount=request.POST.get("receive_amount") or "0",
            amount_to_you=request.POST.get("amount_to_you") or "0",
            amount_to_me=request.POST.get("amount_to_me") or "0",
            profit_amount=request.POST.get("profit_amount") or "0",
            amount=request.POST.get("amount") or "0",
            direction=request.POST.get("direction") or "income",
        )
        messages.success(request, "已新增记录")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect(f"{reverse('panel:book', kwargs={'book': book})}?tab=records")


@login_required
@require_POST
def entry_action(request: HttpRequest, book: str) -> HttpResponse:
    from apps.panel import services as svc

    eid = int(request.POST.get("entry_id") or 0)
    action = (request.POST.get("action") or "").strip()
    try:
        svc.mutate_entry(eid, action)
        messages.success(request, "操作成功")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect(f"{reverse('panel:book', kwargs={'book': book})}?tab=records")


@login_required
@require_POST
def keyword_add(request: HttpRequest, book: str) -> HttpResponse:
    from apps.panel import services as svc

    try:
        svc.add_keyword(
            book,
            request.POST.get("keyword") or "",
            request.POST.get("usages") or "",
        )
        messages.success(request, "关键词已添加")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect(f"{reverse('panel:book', kwargs={'book': book})}?tab=keywords")


@login_required
@require_POST
def keyword_toggle(request: HttpRequest, book: str) -> HttpResponse:
    from apps.panel import services as svc

    try:
        on = svc.toggle_keyword(int(request.POST.get("kw_id") or 0))
        messages.success(request, "已启用" if on else "已停用")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect(f"{reverse('panel:book', kwargs={'book': book})}?tab=keywords")


@login_required
@require_POST
def keyword_delete(request: HttpRequest, book: str) -> HttpResponse:
    from apps.panel import services as svc

    try:
        svc.delete_keyword(int(request.POST.get("kw_id") or 0))
        messages.success(request, "已删除关键词")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return redirect(f"{reverse('panel:book', kwargs={'book': book})}?tab=keywords")


@login_required
def logs_page(request: HttpRequest) -> HttpResponse:
    from apps.panel import event_log

    lines = event_log.read_tail(300)
    return render(
        request,
        "panel/logs.html",
        _nav_ctx(lines=lines, log_path=str(event_log.LOG_PATH)),
    )


@login_required
def logs_raw(request: HttpRequest) -> HttpResponse:
    from apps.panel import event_log
    from django.http import HttpResponse as PlainResponse

    try:
        n = min(max(int(request.GET.get("n") or 300), 1), 1000)
    except ValueError:
        n = 300
    body = "\n".join(event_log.read_tail(n))
    return PlainResponse(body, content_type="text/plain; charset=utf-8")


@login_required
@require_POST
def logs_clear(request: HttpRequest) -> HttpResponse:
    from apps.panel import event_log

    event_log.clear()
    event_log.append("日志已清空", source="panel")
    messages.success(request, "日志已清空")
    return redirect("panel:logs")
