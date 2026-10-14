"""网页听群：昵称下拉在添加绑定后仍应保留未绑定成员。"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.chatrooms.models import ChatRoom, Employee, SenderBinding
from apps.panel import nick_store
from apps.panel.views import _unbound_nicks
from gateway.sender_resolve import load_room_nicks, remember_room_nicks, set_room_nicks


@pytest.mark.django_db
def test_unbound_nicks_filters_bound_only():
    all_nicks = ["Alice", "Bob", "Carol"]
    bindings = [{"nick": "Bob"}]
    assert _unbound_nicks(all_nicks, bindings) == ["Alice", "Carol"]


@pytest.mark.django_db
def test_nick_store_survives_add_binding(tmp_path, monkeypatch):
    cache = tmp_path / "panel_member_nicks.json"
    monkeypatch.setattr("apps.panel.nick_store._CACHE", cache)

    room = ChatRoom.objects.create(name="测试群-store", enabled=True)
    nick_store.save_room_members(room.id, ["Alice", "Bob", "Carol"])

    from apps.panel import services as svc

    svc.add_binding(room.name, "Alice", "爱丽丝", "staff")
    left = nick_store.load_room_members(room.id)
    assert left == ["Alice", "Bob", "Carol"]
    unbound = nick_store.unbound_members(
        room.id, ["Alice"]
    )
    assert unbound == ["Bob", "Carol"]


@pytest.mark.django_db
def test_add_binding_does_not_wipe_room_nicks(tmp_path, monkeypatch):
    cache = tmp_path / "room_nicks.json"
    monkeypatch.setattr("gateway.sender_resolve._NICK_CACHE", cache)

    room = ChatRoom.objects.create(name="测试群-下拉", enabled=True)
    set_room_nicks(room.name, ["Alice", "Bob", "Carol"])

    from apps.panel import services as svc

    svc.add_binding(room.name, "Alice", "爱丽丝", "staff")
    left = load_room_nicks(room.name)
    assert "Alice" in left and "Bob" in left and "Carol" in left


@pytest.mark.django_db
def test_worker_merge_keeps_unread(tmp_path, monkeypatch):
    cache = tmp_path / "room_nicks.json"
    monkeypatch.setattr("gateway.sender_resolve._NICK_CACHE", cache)

    room = ChatRoom.objects.create(name="测试群-worker", enabled=True)
    emp = Employee.objects.create(
        employee_code="t01", display_name="张三", role="staff", enabled=True
    )
    SenderBinding.objects.create(
        chat_room=room, sender_display_name="Alice", employee=emp, active=True
    )
    set_room_nicks(room.name, ["Alice", "Bob", "Carol"])
    remember_room_nicks(room.name, ["Alice"])
    left = load_room_nicks(room.name)
    assert set(left) >= {"Alice", "Bob", "Carol"}


@pytest.mark.django_db
def test_ajax_bind_keeps_dropdown(client: Client, tmp_path, monkeypatch):
    cache = tmp_path / "panel_member_nicks.json"
    monkeypatch.setattr("apps.panel.nick_store._CACHE", cache)

    User = get_user_model()
    user = User.objects.create_user("panel_ajax", password="pass12345")
    client.force_login(user)

    room = ChatRoom.objects.create(name="🔥测试虚拟卡", enabled=True)
    nick_store.save_room_members(room.id, ["Hazen", "Nora", "Bob", "叽里呱啦"])

    r = client.post(
        f"/panel/rooms/{room.id}/bindings/add/",
        {"nick": "Hazen", "display": "张超", "role": "staff"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        HTTP_ACCEPT="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["nick_total"] == 4
    assert "Hazen" not in data["nicks"]
    assert set(data["nicks"]) == {"Nora", "Bob", "叽里呱啦"}

    r2 = client.post(
        f"/panel/rooms/{room.id}/bindings/add/",
        {"nick": "叽里呱啦", "display": "张越", "role": "admin"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        HTTP_ACCEPT="application/json",
    )
    data2 = r2.json()
    assert data2["ok"] is True
    assert data2["nick_total"] == 4
    assert set(data2["nicks"]) == {"Nora", "Bob"}
