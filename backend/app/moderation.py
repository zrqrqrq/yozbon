# -*- coding: utf-8 -*-
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
"""聊天内容风控:站外链接 / 联系方式 / 广告导流 / 刷屏。

定位:只拦「高危且明确」的违规,疑似的一律打标放行 —— 不做法条级审查,
更不做「宁可错杀」的硬拦。理由:硬拦会逼用户说黑话(词表军备竞赛),
打标则把信号交给已有的 risk_flag / 榜单 / 价值分体系自己消化。

两级判定(screen 的返回值):
- block 高危硬拦:站外链接、明确联系方式词、手机号 / QQ / 邮箱 —— 命中即拒绝发送。
- flag  疑似打标:软导流话术、裸长数字串 —— 照发,但记 ModerationLog,
        供榜单降权与人工复核。

变形对抗:比对前先做归一化(全角转半角 → 同音形近字归并 → 去空白与分隔符 →
汉字数字转阿拉伯),这样 `威信`、`微 信`、`＋v`、`138 0013 8000`、`一三八零零…`
都会先被拉回规范形态再进词表与数字模式,堵住「换个写法就绕过」的口子。

管理员(小组 owner/admin、站内管理员)豁免外链与广告,但仍受刷屏限制。
"""
import re
import unicodedata

LEVEL_OK = "ok"
LEVEL_FLAG = "flag"
LEVEL_BLOCK = "block"

# SF-02:零宽 / 不可见字符黑名单。攻击者在敏感词中间插零宽空格 / 变体选择符
# (如「加​微​信」「微\u200b信」)即可绕过分词词表与数字正则。比对前先整段剔除。
_INVISIBLE_RE = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\u00ad\ufe00-\ufe0f]")

# ---------------- 归一化 ----------------

# 同音 / 形近字归并(单向:变体 → 规范),用于识破刻意变形
_HOMOGLYPH = {
    "薇": "微", "徽": "微", "溦": "微", "溌": "微",
    "伩": "信",
    "抠": "扣", "蔻": "扣",
}
# 汉字数字 → 阿拉伯,用于识破「一三八零零一三八零零零」这类拆写
_CN_DIGITS = {
    "零": "0", "〇": "0", "洞": "0",
    "一": "1", "幺": "1", "壹": "1",
    "二": "2", "两": "2", "贰": "2",
    "三": "3", "叁": "3",
    "四": "4", "肆": "4",
    "五": "5", "伍": "5",
    "六": "6", "陆": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
}
_SEP_RE = re.compile(r"[\s\-_·.,，、|/\\~～^*+()\[\]{}<>《》'\"]+")


def normalize(text: str) -> str:
    """把变形写法拉回规范形态。仅用于检测,不改变用户实际发送的内容。"""
    # SF-02:先做 Unicode NFKC 归一化(全角/兼容字符折叠),再剔除零宽字符,
    # 堵住「敏感词中间插零宽空格 / 变体选择符」的绕过(加​微​信、微\u200b信 等)。
    t = unicodedata.normalize("NFKC", text or "")
    t = _INVISIBLE_RE.sub("", t)
    out = []
    for ch in t:
        o = ord(ch)
        if o == 0x3000:                       # 全角空格
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:           # 全角 ASCII → 半角
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    t = "".join(out)
    t = "".join(_HOMOGLYPH.get(c, c) for c in t)
    t = _SEP_RE.sub("", t)
    t = "".join(_CN_DIGITS.get(c, c) for c in t)
    return t


# ---------------- 站外链接(在原文上判定:去分隔符会破坏 URL / 域名)----------------
# 只认真正的链接形态,不把 @昵称 当链接 —— 否则英文昵称的 @提及 会被误伤。
_URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|wa\.me/|telegram\.me/)",
    re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]{0,62}\.(com|cn|net|org|io|me|cc|tv|xyz|top|vip|shop|app|info|biz|link|site|online|club|store|live|pro|us|uk|ru|jp|kr)\b",
    re.IGNORECASE)
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")

# ---------------- 联系方式(在归一化文本上判定)----------------
# 中国大陆手机号:11 位、1 开头、次位 3-9
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# QQ / 扣扣 / 企鹅 等前缀 + 5~11 位数字
_QQ_RE = re.compile(r"(?:qq|扣扣|企鹅|抠抠)\D{0,3}(\d{5,11})", re.IGNORECASE)
# 邮箱
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,63}\.[a-z]{2,12}", re.IGNORECASE)
# 裸长数字串(10 位以上且非手机号)—— 疑似换号 / 换账号
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{10,}(?!\d)")

# 「加个联系方式」指令 —— 明确到可以直接执行的导流。
# 原文与归一化文本都要判:原文判定保留 + 号(＋v / +v),归一化判定拆掉分隔符的
# 「加 我 微 信」这类刻意规避写法。
_CONTACT_CMD_RE = re.compile(
    r"(?:加|＋|\+)\s*(?:个|一下|下|我|你|您|的|好友|呗|们)*\s*"
    r"(?:v|vx|wx|weixin|wechat|微信|威信|薇信|徽信|扣扣|抠抠|qq|企鹅)",
    re.IGNORECASE)
# 平台标识 + 明确取值(微信:abc123 / 微信号是abc123 / vx-ab12 / qq 123456)
# —— 有取值就是交换联系方式,硬拦。归一化文本上判定,挡住 微信一三八… 这类变形。
_CONTACT_VALUE_RE = re.compile(
    r"(?<![a-z0-9])(?:微信|威信|薇信|徽信|vx|wx|weixin|wechat|扣扣|抠抠|qq)"
    r"(?:号|号码|帐号|账号|id)?\s*[:：=＝\-—是]?\s*[a-z0-9][a-z0-9_.\-]{2,}",
    re.IGNORECASE)
# 裸平台标识(没有「加」指令,也没跟取值)—— 只打标,不硬拦:可能是正常提及而非导流
_PLATFORM_HINT_RE = re.compile(
    r"(微信|威信|薇信|徽信|扣扣|抠抠|(?<![a-z0-9])(?:vx|wx)(?![a-z0-9]))",
    re.IGNORECASE)

# ---------------- 广告 / 导流词 ----------------
# 高危:明确的站外联系与接单导流 —— 命中即拦
_AD_HARD = [
    "加微信", "加微", "微信同号", "加vx", "加v信", "加qq", "qq群", "微信群", "扫码加",
    "联系方式", "电话联系", "加我好友", "点我头像",
    "代刷", "刷单", "刷量", "接单", "代做", "代充", "代练", "涨粉", "买粉",
    "低价出", "优惠券", "返利", "返现", "免费领", "免费送", "限时特价", "秒杀", "拼单",
    "招代理", "招商", "加盟", "兼职日结", "日入过万", "月入过万", "包过", "稳赚",
    "内幕消息", "telegram", "whatsapp", "discord.gg", "加群",
]
# 疑似:站外交易话术 —— 只在市场语境下可疑(「出售」「推广」在交易页是正常用词),
# 所以打标放行,不硬拦,免得误伤正常砍价。
# 「私聊我 / 私信我 / 联系我」也归这里:本平台的洽谈室本身就是买卖双方的一对一私密
# 房间,邀请对方「私聊」并不必然等于导流站外,硬拦会误伤正常议价。
_AD_SOFT = [
    "推广", "引流", "出售", "接活", "走量", "私我", "找我", "加个", "详聊", "细聊",
    "详谈", "面谈", "线下交易", "站外", "绕过平台", "私下", "私聊", "私信", "联系我",
]
_AD_RE_HARD = re.compile("|".join(re.escape(w) for w in _AD_HARD), re.IGNORECASE)
_AD_RE_SOFT = re.compile("|".join(re.escape(w) for w in _AD_SOFT), re.IGNORECASE)

# ---------------- 刷屏 ----------------
_REPEAT_CHAR_RE = re.compile(r"(.)\1{7,}", re.DOTALL)      # 同一字符重复 >= 8 次
_REPEAT_CHUNK_RE = re.compile(r"(.{1,3})\1{4,}", re.DOTALL)  # 同一 1~3 字片段重复 >= 5 次
MAX_LINES = 12          # 单条消息换行数上限(刷楼)

MAX_TEXT = 500          # 单条文本上限(字符)
MAX_IMAGE_TEXT = 200    # 图片/作品附言上限
MAX_DUP_SECONDS = 60    # 同内容重复判定窗口(秒)


def _has_external_link(text: str) -> bool:
    return bool(_URL_RE.search(text) or _BARE_DOMAIN_RE.search(text) or _IP_RE.search(text))


def _is_flooding(text: str) -> bool:
    if _REPEAT_CHAR_RE.search(text) or _REPEAT_CHUNK_RE.search(text):
        return True
    if text.count("\n") >= MAX_LINES:
        return True
    if len(text) >= 12 and len(set(text)) <= 3:
        return True
    return False


def screen(text: str, *, is_admin: bool = False, limit: int = MAX_TEXT):
    """内容风控主入口。返回 (level, cleaned_text, reason)。

    level: LEVEL_OK / LEVEL_FLAG(打标放行)/ LEVEL_BLOCK(硬拦)。
    调用方拿到 FLAG 时应照常放行,但把 reason 记进 ModerationLog 并给前端软提示。
    """
    t = (text or "").strip()
    if not t:
        return LEVEL_BLOCK, "", "Message content cannot be empty"
    if len(t) > limit:
        return LEVEL_BLOCK, "", f"Message too long (max {limit} characters)"
    if _is_flooding(t):
        return LEVEL_BLOCK, t, "Spam detected; do not resend"

    norm = normalize(t)
    if not is_admin:
        if _has_external_link(t):
            return LEVEL_BLOCK, t, "External links are not allowed in chat"
        if _CONTACT_CMD_RE.search(t):
            return LEVEL_BLOCK, t, "Exchanging off-platform contact info is not allowed in chat"
        if _CONTACT_CMD_RE.search(norm):
            # 原文没命中、归一化后才命中 → 拆了空格/分隔符的刻意规避写法
            return LEVEL_BLOCK, t, "Exchanging off-platform contact info is not allowed in chat (possible deliberate obfuscation)"
        if _CONTACT_VALUE_RE.search(norm):
            return LEVEL_BLOCK, t, "Exchanging off-platform contact info is not allowed in chat"
        if _PHONE_RE.search(norm):
            return LEVEL_BLOCK, t, "Sharing phone numbers is not allowed in chat"
        if _EMAIL_RE.search(norm):
            return LEVEL_BLOCK, t, "Sharing email addresses is not allowed in chat"
        if _QQ_RE.search(norm):
            return LEVEL_BLOCK, t, "Sharing QQ / off-platform accounts is not allowed in chat"
        if _AD_RE_HARD.search(norm):
            # 原文没命中、归一化后才命中 → 刻意变形规避,单独标注供人工复核
            if not _AD_RE_HARD.search(t):
                return LEVEL_BLOCK, t, "Advertising or traffic-driving is not allowed in chat (possible deliberate obfuscation)"
            return LEVEL_BLOCK, t, "Advertising or traffic-driving is not allowed in chat"

    if _AD_RE_SOFT.search(norm):
        return LEVEL_FLAG, t, "Suspected off-platform trading / marketing talk"
    if _PLATFORM_HINT_RE.search(norm):
        return LEVEL_FLAG, t, "Suspected exchange of off-platform contact info"
    if _LONG_DIGITS_RE.search(norm):
        return LEVEL_FLAG, t, "Suspected exchange of off-platform contact info (long digit string)"
    return LEVEL_OK, t, ""


def excerpt(text: str, n: int = 120) -> str:
    """截一段命中片段存进风控流水,避免把整条消息写进日志表。"""
    return (text or "").strip().replace("\n", " ")[:n]


def augment_ml(text: str, *, content_type: str = "task", content_id: int = 0,
               citizen_id: int = 0, base_level: str = LEVEL_OK):
    """ML 增强审核层（软升级，绝不硬拦）。

    G8：把此前仅作管理端点暴露、未接入内容主链的 ml_moderation 接入决策。

    安全边界（与 moderation 模块「只拦高危明确违规、疑似打标放行」原则一致）：
      - 规则层已 BLOCK 时直接沿用，不再调用模型（规则结论权威，省算力）；
      - 模型为模拟推理，故其 block/review 判定最多把 OK 升级为 FLAG（交人工复核），
        永不升级为 BLOCK —— 杜绝模拟噪声造成「宁可错杀」式误伤。
    每次调用会落一条 ModerationScore 流水，供观察室与人工复核消费。

    返回 (level, ml_decision)，level 仍为 LEVEL_OK / LEVEL_FLAG / LEVEL_BLOCK。
    """
    if base_level == LEVEL_BLOCK:
        return LEVEL_BLOCK, "skipped"
    try:
        from .ml_moderation import instance as _ml
        res = _ml.score_content(content_type=content_type, content_id=content_id,
                                text=text, citizen_id=citizen_id)
        decision = res.get("decision", "pass")
    except Exception:  # noqa: BLE001 —— 审核增强失败不应影响主链放行
        return base_level, "error"
    if decision in ("review", "block") and base_level == LEVEL_OK:
        return LEVEL_FLAG, decision
    return base_level, decision


def is_duplicate(prev_text: str, text: str) -> bool:
    """与上一条内容完全相同(去空白后)视为重复。"""
    return bool(prev_text) and prev_text.strip() == text.strip()
