"""群昵称规范化与绑定匹配。"""

from gateway.adapters.pyweixin import _match_known_nick_exact
from gateway.chat_names import nickname_match_key, normalize_group_nickname


def test_normalize_fullwidth_parens_and_spaces():
    assert normalize_group_nickname("啊 吧（跨境版）") == "啊吧(跨境版)"
    assert normalize_group_nickname("啊吧(跨境版)") == "啊吧(跨境版)"
    assert nickname_match_key("啊 吧（跨境版）") == nickname_match_key(
        "啊吧(跨境版)"
    )


def test_match_known_nick_fuzzy_parens():
    known = ["啊吧(跨境版)", "叽里呱啦"]
    assert _match_known_nick_exact("啊 吧（跨境版）", known) == "啊吧(跨境版)"
    assert _match_known_nick_exact("叽里呱啦", known) == "叽里呱啦"
    assert _match_known_nick_exact("路人甲", known) == ""


def test_is_book_command_query_with_name():
    from apps.ledger.books import is_book_command_text

    assert is_book_command_text("查询高级会员-张超")
    assert is_book_command_text("清空高级会员-张超")
    assert is_book_command_text("查询-张超")


def test_premium_shorthand_is_book_command(monkeypatch):
    from apps.ledger import books

    monkeypatch.setattr(
        books,
        "load_staff_biz_for_chat",
        lambda chat: {"1y": "高级会员", "12y": "高级会员", "充币": "充币"},
    )
    assert books.is_book_command_text("1y", "群")
    assert books.is_book_command_text("1Y", "群")
    assert books.is_book_command_text("12y-1", "群")
    assert books.is_book_command_text("1y-1", "群")
    assert not books.is_book_command_text("充币", "群")
