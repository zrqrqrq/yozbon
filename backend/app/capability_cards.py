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
"""AI 公民能力卡片注册表。

设计哲学：
  1. 平台原生 AI（走 RH 工作流的）：有平台级硬边界（duration≤15s, quality∈{480p,720p} 等），
     因为所有用户共享同一套工作流，边界是客观技术限制。
  2. 外部入驻 AI（决策模型、TTS、OCR、任何第三方能力）：能力边界由 AI 自行声明
     （self_decl），平台不强加 RH 的限制。城主用自己的判断力理解并决策。
  3. 本注册表仅覆盖平台原生 AI。外部 AI 的 profile_json 保持原样（自述内容），
     城主可见、可判断、但不被平台硬约束。

入驻时 capability.declare() 只对平台原生 skill 自动合并 platform_limits；
外部 skill（decision_making / tts / ocr / 任何不在本表的）保留自述原文。
"""
from __future__ import annotations

# 每种 skill 对应的能力卡片（硬边界 + prompt 格式说明）
CAPABILITY_CARDS = {
    "video_generation": {
        "label": "视频生成（MiniMax H3）",
        "kinds": ["video_civil", "video_openvdn"],
        "thinking": False,  # 螺丝钉：固定功能，无思维能力，不做事前判断
        "limits": {
            "duration_sec": {"min": 5, "max": 15, "unit": "s"},
            "quality": {"allowed": ["480p", "720p"]},
            "quality_openvdn": {"allowed": ["720p"]},
            "aspect_ratio": {"allowed": ["16:9", "9:16", "3:4", "4:3", "21:9", "1:1"]},
            "max_concurrent": 1,
        },
        "prompt_format": "free-text (English recommended); @图N 自动转 <Picture N>",
        "output": "MP4 video URL",
        "notes": [
            "video_civil 文戏/叙事：支持 480p 和 720p",
            "video_openvdn 高动态/动作：仅 720p",
            "无参考图时后端自动切换 T2VA 模式",
            "单段最长 15 秒，更长按分段并行",
        ],
    },
    "music_generation": {
        "label": "音乐生成（MiniMax Music3）",
        "kinds": ["music"],
        "thinking": False,  # 螺丝钉：固定功能
        "limits": {
            "duration_sec": {"allowed": [90, 120, 150, 180], "unit": "s"},
            "max_concurrent": 1,
        },
        "prompt_format": "structured 3-section caption (auto-generated from params: genre/mood/bpm/instruments)",
        "output": "MP3 audio URL",
        "notes": [
            "时长只有 90/120/150/180 秒四个档位",
            "caption 由系统 build_caption 自动组装，AI 只需提供 genre/mood/bpm 等结构化参数",
            "纯音乐(无人声) 必须显式声明 vocal_on=false",
        ],
    },
    "image_generation": {
        "label": "图像生成（Z-Image）",
        "kinds": ["image", "hd_image", "img2img"],
        "thinking": False,  # 螺丝钉：固定功能
        "limits": {
            "standard": {"max_resolution": "1024x1024"},
            "hd": {"max_resolution": "3072x3072", "method": "两阶段"},
            "aspect_ratio": {"allowed": ["1:1", "16:9", "9:16", "3:4", "4:3", "21:9"]},
            "max_concurrent": 1,
        },
        "prompt_format": "free-text (English recommended, ≤2000 chars)",
        "output": "PNG image URL",
        "notes": [
            "image = 标准 1024x1024，hd_image = 高清两阶段(最大 3072x3072)",
            "img2img 需传入参考图 + denoise 强度(0-1)",
        ],
    },
    "text_generation": {
        "label": "文本生成（Qwen3 LLM）",
        "kinds": ["llm"],
        "thinking": True,  # 员工：有思维，执行前必须自检判断
        "limits": {
            "max_tokens": {"default": 512, "execute_max": 2048},
            "temperature": {"min": 0.1, "max": 2.0, "default": 0.7},
            "max_concurrent": 2,
        },
        "prompt_format": "free-text, any language",
        "output": "generated text string",
        "notes": [
            "城主决策和分解引擎使用独立 max_tokens 上限",
            "执行层子任务默认 max_tokens=2048（防无界生成拖长耗时）",
        ],
    },
    "video_analysis": {
        "label": "视频诊断分析（Qwen3-VL）",
        "kinds": ["video_analysis"],
        "thinking": True,  # 员工：有思维（AI 3 自主决策分析策略）
        "limits": {
            "input_video": {"max_duration_sec": 600, "note": "≤10min；更长需分段"},
            "fps": {"default": 3, "min": 2, "max": 5, "note": "抽帧帧率"},
            "frame_resolution": {"default": "512x512", "note": "压缩控制token"},
            "chunk_window_sec": {"default": 18, "min": 12, "max": 25},
            "chunk_overlap_sec": {"default": 4, "min": 2, "max": 6},
            "max_frames_per_chunk": {"default": 60},
            "max_concurrent": 1,
        },
        "prompt_format": "pipeline auto-managed; AI receives structured per-chunk prompt",
        "output": "JSON diagnostic report (shots/lighting/expressions/actions/dialogue_match, timestamped)",
        "notes": [
            'Qwen3-VL 不能直接"看"视频文件——必须 ffmpeg 抽帧 → 压缩 → 分片 → 逐段送入',
            "单次送入帧数有上限（图像token限制），这就是为什么需要滑窗分片",
            "分析维度：镜头类型、光影、演员表情、动作、台词匹配度",
            "每分片独立输出带时间戳JSON，最终合并去重",
            "城主注意：不能把'分析这个3分钟视频'直接丢给VLM——它需要video_analysis这个kind走流水线",
        ],
    },
}

# kind → skill_name 反查索引（编排分配时用）
KIND_TO_SKILL: dict[str, str] = {}
for _skill, _card in CAPABILITY_CARDS.items():
    for _k in _card["kinds"]:
        KIND_TO_SKILL[_k] = _skill

# kind → 是否有思维能力（"谁有脑子"的唯一真源，供 ai_judgment 派生）
# 思维型（员工）：执行前必须自检判断；固定功能型（螺丝钉）：接到就做，无自主判断。
THINKING_KINDS: frozenset[str] = frozenset(
    _k for _card in CAPABILITY_CARDS.values() if _card.get("thinking")
    for _k in _card["kinds"])
FIXED_FUNCTION_KINDS: frozenset[str] = frozenset(
    _k for _card in CAPABILITY_CARDS.values() if not _card.get("thinking")
    for _k in _card["kinds"])

# 技能别名归一化（AI 自述写法不统一时的容错映射）
SKILL_ALIASES: dict[str, str] = {
    "video": "video_generation",
    "视频": "video_generation",
    "视频生成": "video_generation",
    "video_gen": "video_generation",
    "music": "music_generation",
    "音乐": "music_generation",
    "音乐生成": "music_generation",
    "music_gen": "music_generation",
    "audio": "music_generation",
    "image": "image_generation",
    "图": "image_generation",
    "图像": "image_generation",
    "图像生成": "image_generation",
    "绘画": "image_generation",
    "image_gen": "image_generation",
    "image_generation_standard": "image_generation",
    "text": "text_generation",
    "文案": "text_generation",
    "文本": "text_generation",
    "文本生成": "text_generation",
    "writing": "text_generation",
    "text_gen": "text_generation",
    "general": "text_generation",
    "vlm": "video_analysis",
    "视频分析": "video_analysis",
    "视频诊断": "video_analysis",
    "video_analysis": "video_analysis",
    "video_diagnosis": "video_analysis",
    "视觉分析": "video_analysis",
    "visual_analysis": "video_analysis",
}


def normalize_skill(raw_skill: str) -> str:
    """把任意 skill 别名归一化到 CAPABILITY_CARDS 的规范键。找不到返回原值。"""
    s = str(raw_skill or "").strip().lower()
    if s in CAPABILITY_CARDS:
        return s
    return SKILL_ALIASES.get(s, raw_skill)


def get_card_for_kind(kind: str) -> dict | None:
    """返回指定执行 kind 对应的能力卡片。"""
    skill = KIND_TO_SKILL.get(kind)
    return CAPABILITY_CARDS.get(skill) if skill else None


def profile_json_for_kind(kind: str) -> str:
    """生成入驻时写入 CapabilityProfile.profile_json 的结构化 JSON。"""
    import json
    card = get_card_for_kind(kind)
    if not card:
        return "{}"
    return json.dumps({
        "modalities": card["kinds"],
        "limits": card["limits"],
        "prompt_format": card["prompt_format"],
        "output": card["output"],
        "notes": card["notes"],
    }, ensure_ascii=False)


def citizen_capability_summary(citizen_skills: list[dict]) -> str:
    """生成城主可见的候选人能力摘要文本。

    citizen_skills: [{skill, verified_level, limits(从 profile_json 解析)}]
    """
    lines = []
    for s in citizen_skills:
        skill = s.get("skill", "")
        level = s.get("verified_level", "unverified")
        card = CAPABILITY_CARDS.get(skill)
        if card:
            limits_str = _compact_limits(card["limits"])
            lines.append(f"  • {card['label']} [{skill}] (Lv:{level}) → {limits_str}")
        elif skill:
            lines.append(f"  • {skill} (Lv:{level})")
    return "\n".join(lines) if lines else "  (no capability profile)"


def _compact_limits(limits: dict) -> str:
    """把 limits dict 压缩为一行可读字符串。"""
    parts = []
    for k, v in limits.items():
        if k == "max_concurrent":
            continue
        if isinstance(v, dict):
            if "allowed" in v:
                vals = v["allowed"]
                parts.append(f"{k}={','.join(str(x) for x in vals)}")
            elif "min" in v and "max" in v:
                unit = v.get("unit", "")
                parts.append(f"{k}={v['min']}-{v['max']}{unit}")
    return " | ".join(parts) if parts else "unbounded"
