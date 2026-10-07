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
"""语言标准（spec v2 L1-L3）：语言检测 + 回复语言优先级解析。零依赖、纯函数、幂等。

L1 检测 detect_lang：按 Unicode 区块字符占比判定主语种（CJK/假名/谚文/西里尔/拉丁），
     不依赖任何第三方语言库，避免引入重型依赖与不确定性。
L3 回复语言优先级 resolve_reply_lang：显式任务语言 > 宿主偏好 > 母语 > 平台默认。
     解析出的短码用于注入提示词 "Reply in {language}."，保证 AI 输出语种可控。

短码约定：zh / en / ja / ko / ru（BCP-47 风格，只取主语言子标签）。
"""
from __future__ import annotations

# 语言中文名 → 提示词可读名（用于 "Reply in ..."）
_LANG_NAMES = {
    "zh": "Chinese (Simplified)",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
}

# CJK 统一表意文字（含扩展 A）
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF)


def _is_kana(ch: str) -> bool:
    o = ord(ch)
    return 0x3040 <= o <= 0x30FF


def _is_hangul(ch: str) -> bool:
    o = ord(ch)
    return (0xAC00 <= o <= 0xD7AF) or (0x1100 <= o <= 0x11FF) or (0x3130 <= o <= 0x318F)


def _is_cyrillic(ch: str) -> bool:
    return 0x0400 <= ord(ch) <= 0x04FF


def _is_latin(ch: str) -> bool:
    o = ord(ch)
    return (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A)


def detect_lang(text: str) -> str:
    """检测文本主语种，返回短码 zh/en/ja/ko/ru；空文本或无法判定返回 ""。

    规则（按优先级）：
    - 含假名 → ja（日语几乎必含假名，最可靠信号）
    - 谚文占比过半 → ko
    - CJK 占比过半 → zh（即便混有拉丁字母，只要汉字占主即判 zh）
    - 西里尔占比过半 → ru
    - 仅拉丁字母占主 → en
    """
    if not text:
        return ""
    cjk = kana = hangul = cyrillic = latin = 0
    for ch in text:
        if _is_kana(ch):
            kana += 1
        elif _is_hangul(ch):
            hangul += 1
        elif _is_cjk(ch):
            cjk += 1
        elif _is_cyrillic(ch):
            cyrillic += 1
        elif _is_latin(ch):
            latin += 1
    if kana > 0:
        return "ja"
    cjkish = cjk  # CJK 单独计
    total_script = cjk + hangul + cyrillic + latin
    if total_script == 0:
        return ""
    if hangul >= total_script * 0.5:
        return "ko"
    if cjkish >= total_script * 0.5:
        return "zh"
    if cyrillic >= total_script * 0.5:
        return "ru"
    if latin > 0 and latin >= total_script * 0.5:
        return "en"
    # 混合且无单一主语种：CJK 有则偏 zh，否则偏 en
    if cjkish > 0:
        return "zh"
    return "en" if latin > 0 else ""


def _norm(code: str) -> str:
    """归一化语言码到主语言子标签：'zh-CN'→'zh'、'en-US'→'en'、空→''。"""
    if not code:
        return ""
    return code.strip().split("-")[0].split("_")[0].lower()


def resolve_reply_lang(*, task_lang: str = "", preferred_lang: str = "",
                       native_lang: str = "", default_locale: str = "") -> str:
    """回复语言优先级：显式任务语言 > 宿主偏好 > 母语 > 平台默认。返回主语言子标签。"""
    for cand in (task_lang, preferred_lang, native_lang, default_locale):
        n = _norm(cand)
        if n:
            return n
    return "en"


def reply_instruction(lang: str) -> str:
    """生成注入提示词的回复语言指令；zh 用中英双语指令以防模型忽略。"""
    name = _LANG_NAMES.get(lang, lang or "English")
    if lang == "zh":
        return "请用简体中文作答（Respond in Simplified Chinese）."
    return f"Reply in {name}."
