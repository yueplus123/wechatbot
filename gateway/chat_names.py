"""群名规范化与会话分类（过滤个人/消息预览噪音）。"""

from __future__ import annotations

import re

_MEMBER_COUNT_SUFFIX = re.compile(r"\s*[（(]\d+[）)]\s*$")
_HAS_MEMBER_COUNT = re.compile(r"[（(]\d+[）)]\s*$")

# 明显不是业务群的系统会话
_SYSTEM_CHAT_NAMES = {
    "文件传输助手",
    "微信团队",
    "微信支付",
    "微信运动",
    "服务通知",
    "腾讯新闻",
    "订阅号",
    "订阅号消息",
    "公众号",
    "朋友推荐消息",
    "群发助手",
    "QQ邮箱提醒",
}

# 像聊天记录/消息预览，而不是会话名
_MESSAGE_LIKE = re.compile(
    r"(https?://|www\.|。|！|？|\?|！|吗|吧|呀|啊|呢|了吗|可以|我们|他们|你的|我的|改密码|想自己)"
)


def strip_member_count_suffix(name: str) -> str:
    """去掉末尾「 (3)」/「（3）」人数后缀。"""
    return _MEMBER_COUNT_SUFFIX.sub("", (name or "").strip()).strip()


def normalize_chat_name(name: str) -> str:
    return strip_member_count_suffix(name)


# 真邮箱；企微展示名「张三@公司」不要误伤
_EMAIL_LIKE = re.compile(
    r"^[^\s@]+@[^\s@]+\.[a-zA-Z]{2,}$|[^\s@]+@[^\s@]+\.(?:com|cn|net|org|edu)\b",
    re.I,
)
# 聊天记录列表项：「内容 2026年7月22日 23:24」
_CHAT_RECORD_ITEM = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}"
)
_CHAT_MEDIA_PREFIX = (
    "图片",
    "动画表情",
    "视频",
    "语音",
    "文件",
    "卡片",
    "链接",
    "位置",
    "你撤回了一条消息",
)


def is_email_like_nickname(name: str) -> bool:
    return bool(_EMAIL_LIKE.search((name or "").strip()))


def looks_like_chat_record_text(name: str) -> bool:
    """是否像「聊天记录」窗里的消息行（不是群成员昵称）。"""
    s = (name or "").strip()
    if not s:
        return True
    if _CHAT_RECORD_ITEM.search(s):
        return True
    if any(s == p or s.startswith(p + " ") or s.startswith(p + "\u3000") for p in _CHAT_MEDIA_PREFIX):
        return True
    if s.startswith("http://") or s.startswith("https://"):
        return True
    if "密码" in s or "----" in s:
        return True
    # [睡] / [微笑] 等表情标签开头
    if re.match(r"\[[^\]]{1,12}\]", s):
        return True
    return False


def looks_like_chat_record_list(rows: list[str]) -> bool:
    """抽样判断一个 List 是否为聊天记录消息列表。"""
    sample = [str(r or "").strip() for r in rows[:24] if str(r or "").strip()]
    if not sample:
        return False
    hits = sum(1 for r in sample if looks_like_chat_record_text(r))
    return hits >= max(2, (len(sample) + 2) // 3)


def normalize_group_nickname(name: str) -> str:
    """
    规范化群昵称：
    - 去掉 UIA 偶发的「字 间 空 格」
    - 全角括号统一为半角，便于绑定匹配
    - 保留企微「昵称@企业」完整展示
    """
    s = (name or "").strip().splitlines()[0].strip()
    if not s:
        return ""
    # 全角括号 → 半角（绑定常为半角，OCR 常出全角）
    s = s.replace("（", "(").replace("）", ")")
    # 中文之间多余空格：啊 吧(跨境版) → 啊吧(跨境版)
    if " " in s and re.search(r"[\u4e00-\u9fff]", s):
        s = re.sub(r"(?<=[\u4e00-\u9fff()])\s+(?=[\u4e00-\u9fff()])", "", s)
        s = re.sub(r"\s*([(])\s*", r"\1", s)
        s = re.sub(r"\s*([)])\s*", r"\1", s)
        s = re.sub(r"\s+", " ", s).strip()
    return s


def nickname_match_key(name: str) -> str:
    """
    模糊匹配用 key：去空格、大小写不敏感；去掉企微「@企业」后缀便于兜底。
    """
    s = normalize_group_nickname(name)
    if not s:
        return ""
    if "@" in s and not is_email_like_nickname(s):
        base = s.rsplit("@", 1)[0].strip()
        if base:
            s = base
    s = re.sub(r"\s+", "", s)
    return s.casefold()


def is_plausible_group_nickname(name: str) -> bool:
    """过滤误把机器人回复/指令/时间/邮箱/聊天记录当成群昵称的情况。"""
    raw = (name or "").strip().splitlines()[0].strip()
    if looks_like_chat_record_text(raw):
        return False
    s = normalize_group_nickname(name) or raw
    if not s or s in {"未知发送人", "我", "系统", "所有人", "@所有人", "全体成员"}:
        return False
    if looks_like_chat_record_text(s):
        return False
    # 企微名可能稍长
    if len(s) > 48 or "\n" in s or "\r" in s:
        return False
    if any(s.startswith(p) for p in ("✅", "❌", "ℹ️", "⚠", "记账", "記賬")):
        return False
    # 机器人回执字段行（曾被误当成发送人：分类：其他）
    if re.match(r"^(类型|金額|金额|分类|分類|提交人|备注|備註|记录号|記錄號)\s*[：:]", s):
        return False
    if s.startswith(("类型：", "类型:", "金额：", "金额:", "分类：", "分类:", "备注：", "备注:")):
        return False
    bad = (
        "记录成功",
        "无法确认",
        "未找到分类",
        "格式不正确",
        "已撤销",
        "记录号",
        "提交人",
        "类型：",
        "金额：",
        "分类：",
        "备注：",
    )
    if any(x in s for x in bad):
        return False
    # 时间、纯数字、真邮箱（保留 昵称@企业）
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        return False
    if re.fullmatch(r"\d+", s):
        return False
    if is_email_like_nickname(s):
        return False
    # 旧记录号 / 带井号流水
    if re.search(r"#?\d{8}-\d+", s) or re.search(r"#\d{4}", s):
        return False
    return True


def is_bot_receipt_text(text: str) -> bool:
    """机器人回执/片段，不应当作群消息上报。"""
    s = (text or "").strip()
    if not s:
        return True
    if s.startswith(("✅", "❌", "ℹ️", "⚠", "📒")):
        return True
    first = s.splitlines()[0].strip()
    # 「记账成功」会被解析成指令「记账」+「成功」，必须丢掉
    if first in {
        "记账成功",
        "記賬成功",
        "入账成功",
        "入帳成功",
        "记单成功",
        "記單成功",
        "记录成功",
        "記錄成功",
        "订单记录成功",
        "訂單記錄成功",
        "USDT查询",
        "USDT查詢",
    }:
        return True
    if first in {"已转账", "已轉賬"} and ("--------------" in s or "\n" in s):
        return True
    if first.startswith(
        (
            "记账成功",
            "記賬成功",
            "入账成功",
            "记单成功",
            "记单查询",
            "記單查詢",
            "记录成功",
            "✅ 订单记录成功",
            "✅订单记录成功",
        )
    ):
        return True
    if re.match(
        r"^(类型|金額|金额|分类|分類|提交人|备注|備註|记录号|記錄號|项目名|業務名|业务名)\s*[：:]",
        s,
    ):
        return True
    if any(
        x in s
        for x in (
            "记录成功",
            "订单记录成功",
            "记账成功",
            "記賬成功",
            "无法确认提交人",
            "格式不正确",
            "指令说明",
            "发「记账」查看",
        )
    ):
        return True
    return False


def had_member_count_suffix(raw: str) -> bool:
    return bool(_HAS_MEMBER_COUNT.search((raw or "").strip()))


def classify_session_title(raw: str) -> str:
    """
    粗分会话标题：
    - group: 较像群（会话列表带人数括号等）
    - system: 系统号
    - noise: 消息预览/垃圾
    - person: 更像个人（默认）
    """
    text = (raw or "").strip()
    if not text:
        return "noise"
    first = text.splitlines()[0].strip()
    if not first or len(first) > 60:
        return "noise"
    if first in _SYSTEM_CHAT_NAMES:
        return "system"
    if _MESSAGE_LIKE.search(first):
        return "noise"
    # 中文句子特征：空格很少但很长
    if len(first) >= 18 and " " not in first and not had_member_count_suffix(first):
        # 仍可能是长群名；若含明显口语则当噪音
        if any(x in first for x in ("也", "就", "还", "不", "吗", "吧")):
            return "noise"
    if had_member_count_suffix(first):
        return "group"
    # 含「群」字更像群
    if "群" in first or "Group" in first or "group" in first:
        return "group"
    return "person"


def is_group_candidate(raw: str, *, groups_only: bool = True) -> bool:
    kind = classify_session_title(raw)
    if kind in {"noise", "system"}:
        return False
    if groups_only:
        return kind == "group"
    return kind in {"group", "person"}
