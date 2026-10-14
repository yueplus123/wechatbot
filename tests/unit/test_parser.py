from decimal import Decimal

import pytest

from apps.rules.parser import ParseStatus, normalize_text, parse_command


def test_normalize_spaces_and_fullwidth():
    assert normalize_text("  飞机号  收入50  ") == "飞机号 收入50"


def test_ignore_without_trigger():
    assert parse_command("今天天气不错").status == ParseStatus.IGNORED


def test_help_commands():
    assert parse_command("记账 帮助").command == "ledger_help"
    assert parse_command("记账").command == "ledger_help"


def test_ledger_ops_commands():
    from apps.rules.parser import _parse_ledger_special

    assert _parse_ledger_special("查询", rule_version="1") is None
    assert _parse_ledger_special("查询记账", rule_version="1").command == "ledger_query"
    assert _parse_ledger_special("记账查询", rule_version="1").command == "ledger_query"
    assert (
        _parse_ledger_special("查询本月", rule_version="1").command
        == "ledger_query_month"
    )
    assert _parse_ledger_special("清账", rule_version="1").command == "settlement_clear"


def test_bare_query_errors():
    r = parse_command("查询")
    assert r.status == ParseStatus.ERROR
    assert "查询记账" in r.error_message
    assert parse_command("记账 查询").status == ParseStatus.ERROR


def test_query_commands():
    assert parse_command("查询记账").command == "ledger_query"
    assert parse_command("记账查询").command == "ledger_query"
    assert parse_command("查询本月").command == "ledger_query_month"
    assert parse_command("记账 查询记账").command == "ledger_query"


def test_legacy_commands_removed():
    assert parse_command("记账 今日汇总").status == ParseStatus.ERROR
    assert parse_command("记账 撤销上一笔").status == ParseStatus.ERROR
    assert parse_command("记账 支出 300 广告费").status == ParseStatus.ERROR
    assert parse_command("充币 2000").status == ParseStatus.IGNORED


def test_staff_order_parse():
    from apps.ledger.staff_order_catalog import expand_triggers

    m = expand_triggers({"充币", "高级会员", "gpt"})
    assert parse_command("充币 10", staff_biz_map=m).command == "staff_order_add"
    assert parse_command("充币 10", staff_biz_map=m).direction.value == "INCOME"
    assert str(parse_command("充币 10", staff_biz_map=m).amount) == "10.00"

    r = parse_command("充币-5", staff_biz_map=m)
    assert r.command == "staff_order_add"
    assert r.direction.value == "EXPENSE"

    r = parse_command("充币增加10", staff_biz_map=m)
    assert r.command == "staff_order_add" and r.direction.value == "INCOME"
    r = parse_command("充币减少5", staff_biz_map=m)
    assert r.command == "staff_order_add" and r.direction.value == "EXPENSE"

    r = parse_command("查询充币-张越", staff_biz_map=m)
    assert r.command == "staff_order_query"
    assert r.remark == "张越"
    r = parse_command("充币查询-张越", staff_biz_map=m)
    assert r.command == "staff_order_query"
    r = parse_command("清空gpt-张越", staff_biz_map=m)
    assert r.command == "staff_order_clear"
    r = parse_command("plus+1", staff_biz_map=m)
    assert r.command == "staff_order_add"
    assert r.business_name == "gpt"
    assert r.remark == "plus"

    r = parse_command("会员 1", staff_biz_map=m)
    assert r.command == "staff_order_add"
    assert r.business_name == "高级会员"

    r = parse_command("1y", staff_biz_map=m)
    assert r.command == "staff_order_add"
    assert r.direction.value == "INCOME"
    assert str(r.amount) == "1.00"
    assert r.business_name == "高级会员"
    assert r.remark == "一个月"
    r = parse_command("1y-1", staff_biz_map=m)
    assert r.direction.value == "EXPENSE"
    assert str(r.amount) == "1.00"
    assert r.remark == "一个月"
    r = parse_command("3y", staff_biz_map=m)
    assert r.remark == "三个月" and r.direction.value == "INCOME"
    r = parse_command("6y-1", staff_biz_map=m)
    assert r.remark == "六个月" and r.direction.value == "EXPENSE"
    r = parse_command("12y", staff_biz_map=m)
    assert r.remark == "一年" and str(r.amount) == "1.00"
    r = parse_command("12y-1", staff_biz_map=m)
    assert r.remark == "一年" and r.direction.value == "EXPENSE"
    r = parse_command("一个月+1", staff_biz_map=m)
    assert r.remark == "一个月" and r.direction.value == "INCOME"

    r = parse_command("查询-李振宇")
    assert r.command == "staff_order_query_employee"
    assert r.remark == "李振宇"
    assert r.book == "staff_order"

    r = parse_command("查询－张三")
    assert r.command == "staff_order_query_employee"
    assert r.remark == "张三"

    # 未绑定关键词 → 忽略
    assert parse_command("充币 10", staff_biz_map={}).status == ParseStatus.IGNORED
    assert parse_command("打包 1", staff_biz_map=m).status == ParseStatus.IGNORED


def test_ledger_usdt_parse():
    """USDT：给我6740，2000刀 → 欠款元 + 充值美金；查询usdt / 已转账。"""
    r = parse_command("给我6740，2000刀")
    assert r.status == ParseStatus.OK
    assert r.command == "ledger"
    assert r.book == "usdt"
    assert r.business_name == "USDT"
    assert r.direction.value == "INCOME"
    assert str(r.amount) == "2000.00"
    assert r.currency == "USD"
    assert str(r.receive_amount) == "2000.00"
    assert str(r.amount_to_me) == "6740.00"
    assert str(r.profit_amount) == "0.00"

    r = parse_command("给我2000刀")
    assert r.command == "ledger"
    assert str(r.amount) == "2000.00"
    assert str(r.amount_to_me) == "0.00"
    assert r.currency == "USD"

    r = parse_command("给我6740，-50刀")
    assert r.command == "ledger"
    assert r.direction.value == "EXPENSE"
    assert str(r.amount) == "50.00"
    assert str(r.amount_to_you) == "50.00"
    assert str(r.amount_to_me) == "6740.00"

    r = parse_command("usdt6740，100刀")
    assert r.command == "ledger"
    assert r.business_name == "USDT"
    assert str(r.amount) == "100.00"
    assert str(r.amount_to_me) == "6740.00"

    r = parse_command("查询usdt")
    assert r.command == "usdt_query"
    assert r.book == "usdt"
    r = parse_command("usdt查询")
    assert r.command == "usdt_query"

    r = parse_command("已转账")
    assert r.command == "usdt_settlement_clear"
    assert r.book == "usdt"
    r = parse_command("清账usdt")
    assert r.command == "usdt_settlement_clear"

    # 普通记账「给我」标签仍走项目名格式，不与 USDT 刀格式冲突
    r = parse_command("飞机号 金额50 给我50 利润10")
    assert r.command == "ledger"
    assert r.business_name == "飞机号"
    assert r.currency != "USD" or r.book == "ledger"


def test_usdt_success_template():
    from apps.ledger.ledger_ops import USDT_REPLY_SEP, usdt_entry_success_reply
    from apps.ledger.models import LedgerEntry

    entry = LedgerEntry(
        business_name="USDT",
        book=LedgerEntry.Book.USDT,
        receive_amount=Decimal("2000.00"),
        amount_to_me=Decimal("6740.00"),
    )
    body = usdt_entry_success_reply(entry, operator_name="张越")
    assert body.startswith("记录成功\n")
    assert USDT_REPLY_SEP in body
    assert "┌─ 项目名：USDT" in body
    assert "│ 充值金额：2000.00美金" in body
    assert "└─ 欠款6740元" in body
    assert "记录人：张越" in body
    assert "记录时间：" in body
    assert "收入：" not in body
    assert "利润：" not in body


def test_ignore_bot_receipt_as_command():
    assert parse_command("记账成功").status == ParseStatus.IGNORED
    assert parse_command("记账成功\n--------------\n项目名：USDT").status == ParseStatus.IGNORED
    assert parse_command("记录成功\n--------------\n┌─ 项目名：USDT").status == ParseStatus.IGNORED


def test_reply_footer_on_success():
    from apps.ledger.ledger_ops import REPLY_SEP, ledger_entry_success_reply

    body = ledger_entry_success_reply(operator_name="测试员")
    assert body.count(REPLY_SEP) >= 2
    assert "✅ 订单记录成功" in body
    assert "业务名：" in body
    assert "金额：" in body
    assert "给你：" in body
    assert "给我：" in body
    assert "利润：" in body
    assert "合计：" in body
    assert "指令说明" not in body


def test_success_reply_shows_entry_detail():
    from apps.ledger.ledger_ops import ledger_entry_success_reply
    from apps.ledger.models import LedgerEntry

    entry = LedgerEntry(
        business_name="充币",
        receive_amount=Decimal("214.00"),
        amount_to_you=Decimal("147.00"),
        amount_to_me=Decimal("0"),
        profit_amount=Decimal("67.00"),
    )
    body = ledger_entry_success_reply(entry, operator_name="啊吧")
    assert "✅ 订单记录成功" in body
    assert "业务名：充币" in body
    assert "金额：214" in body
    assert "给你：147" in body
    assert "给我：0" in body
    assert "利润：67" in body
    assert "合计：" in body


def test_acceptance_profit_independent():
    """金额/给你/利润各自独立保存。"""
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry(
        "充币金额214给你147利润67", default_currency="CNY", rule_version="1"
    )
    assert r is not None and r.status == ParseStatus.OK
    assert r.business_name == "充币"
    assert str(r.receive_amount) == "214.00"
    assert str(r.amount_to_you) == "147.00"
    assert str(r.amount_to_me) == "0.00"
    assert str(r.profit_amount) == "67.00"


def test_acceptance_profit_sum_negative():
    """利润可累加负数。"""
    from apps.ledger.ledger_ops import _aggregate_by_project
    from apps.ledger.models import LedgerEntry

    entries = [
        LedgerEntry(
            business_name="A",
            receive_amount=Decimal("0"),
            amount_to_you=Decimal("0"),
            amount_to_me=Decimal("0"),
            profit_amount=Decimal("25.00"),
        ),
        LedgerEntry(
            business_name="A",
            receive_amount=Decimal("0"),
            amount_to_you=Decimal("0"),
            amount_to_me=Decimal("0"),
            profit_amount=Decimal("-5.00"),
        ),
    ]
    rows = _aggregate_by_project(entries)
    assert len(rows) == 1
    assert rows[0]["profit_text"] == "20"


def test_profit_optional():
    """利润可不写，未写记为 0。"""
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry(
        "飞机号 给我50", default_currency="CNY", rule_version="1"
    )
    assert r is not None and r.status == ParseStatus.OK
    assert str(r.amount_to_me) == "50.00"
    assert str(r.profit_amount) == "0.00"


def test_profit_zero_allowed():
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry(
        "gpt 金额100 利润0", default_currency="CNY", rule_version="1"
    )
    assert r is not None and r.status == ParseStatus.OK
    assert str(r.profit_amount) == "0.00"


@pytest.mark.django_db
def test_period_query_template(monkeypatch):
    import uuid

    from django.utils import timezone

    from apps.chatrooms.models import ChatRoom, Employee
    from apps.ledger import ledger_ops
    from apps.ledger.ledger_ops import format_ledger_period_query
    from apps.ledger.models import LedgerEntry, MessageEvent
    from apps.rules.models import Category

    monkeypatch.setattr(
        ledger_ops,
        "sync_room_ledger_from_xinhuo",
        lambda *_a, **_k: (-1, None),
        raising=False,
    )
    # patch where it's imported inside the function
    import apps.integrations.xinhuo_push as xp

    monkeypatch.setattr(xp, "sync_room_ledger_from_xinhuo", lambda *_a, **_k: (-1, None))

    room, _ = ChatRoom.objects.get_or_create(
        name="TestPeriodRoom2", defaults={"enabled": True}
    )
    cat, _ = Category.objects.get_or_create(
        type="INCOME", name="其他", defaults={"enabled": True}
    )
    emp, _ = Employee.objects.get_or_create(
        employee_code="TP2", defaults={"display_name": "测试员2", "enabled": True}
    )
    now = timezone.now()
    ev = MessageEvent.objects.create(
        event_uuid=str(uuid.uuid4()),
        fingerprint=str(uuid.uuid4()),
        chat_room=room,
        chat_name=room.name,
        sender_display_name=emp.display_name,
        raw_text="test",
        received_at=now,
    )
    LedgerEntry.objects.create(
        record_no=f"test-{uuid.uuid4().hex[:8]}",
        event=ev,
        book=LedgerEntry.Book.LEDGER,
        direction=LedgerEntry.Direction.INCOME,
        amount=Decimal("80"),
        currency="CNY",
        category=cat,
        employee=emp,
        chat_room=room,
        business_name="测试",
        receive_amount=Decimal("80"),
        amount_to_you=Decimal("30"),
        amount_to_me=Decimal("0"),
        profit_amount=Decimal("10"),
        occurred_at=now,
        status=LedgerEntry.Status.ACTIVE,
    )
    body = format_ledger_period_query(room, "month")
    assert "账目查询" in body
    assert "业务名：测试" in body
    assert "金额：80" in body
    assert "给你：30" in body
    assert "利润：10" in body
    assert "合计：" in body
    assert "查询时间：" in body
    assert "指令说明" not in body


def test_error_reply_footer():
    from apps.ledger.ledger_ops import REPLY_SEP, ledger_error_reply

    body = ledger_error_reply("❌ 格式不对")
    assert body.count(REPLY_SEP) >= 2
    assert "❌ 格式不对" in body
    assert "指令说明" not in body


def test_ledger_entry_new_format():
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry(
        "gpt代充 金额200 给你120 利润50",
        default_currency="CNY",
        rule_version="1",
    )
    assert r is not None and r.status == ParseStatus.OK
    assert r.business_name == "gpt代充"
    assert str(r.receive_amount) == "200.00"
    assert str(r.amount_to_you) == "120.00"
    assert str(r.amount_to_me) == "0.00"
    assert str(r.profit_amount) == "50.00"


def test_legacy_tags_mapped():
    """收入/支出 兼容映射到 金额/给你；给我独立保存。"""
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry(
        "飞机号 金额50 给我50 利润10",
        default_currency="CNY",
        rule_version="1",
    )
    assert r is not None and r.status == ParseStatus.OK
    assert r.business_name == "飞机号"
    assert str(r.receive_amount) == "50.00"
    assert str(r.amount_to_me) == "50.00"
    assert str(r.profit_amount) == "10.00"

    r2 = _parse_ledger_entry(
        "gpt 收入100 支出60 利润25",
        default_currency="CNY",
        rule_version="1",
    )
    assert r2 is not None and r2.status == ParseStatus.OK
    assert str(r2.receive_amount) == "100.00"
    assert str(r2.amount_to_you) == "60.00"
    assert str(r2.amount_to_me) == "0.00"


def test_ledger_entry_requires_business_name():
    from apps.rules.parser import _parse_ledger_entry

    r = _parse_ledger_entry("金额50 利润10", default_currency="CNY", rule_version="1")
    assert r is not None and r.status == ParseStatus.ERROR


def test_ledger_entry_not_matched_without_tags():
    from apps.rules.parser import _parse_ledger_entry

    assert (
        _parse_ledger_entry("今天天气不错", default_currency="CNY", rule_version="1")
        is None
    )


def test_ignore_bot_receipt_order_success():
    from gateway.chat_names import is_bot_receipt_text

    assert is_bot_receipt_text(
        "--------------------\n2026-09-01 23:02:56\n✅ 订单记录成功\n业务名：充币"
    )


def test_clear_reply_template():
    from apps.ledger.ledger_ops import ledger_clear_reply

    body = ledger_clear_reply(
        operator_name="啊吧",
        count=3,
        range_text="全部项目（2026-09-01 至 2026-09-11）",
    )
    assert "清空账目成功" in body
    assert "清空条数：3条" in body
    assert "操作人：啊吧" in body
    assert "清空时间：" in body
