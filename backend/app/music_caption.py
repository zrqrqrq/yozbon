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
"""音乐生成参数 -> MiniMax Music3 三段式英文 caption。

复用 RunVerseHub backend/app/music.py 的 build_caption 逻辑（唯一真源），
AIjuhe 编排层的音乐子任务经 platform_compute._node_info_list 调用本模块，
确保喂给 RH Music3 工作流的 caption 符合官方结构。
"""
from __future__ import annotations

import random

# 中文情绪 -> 英文情绪描述
_MOOD_TERMS = {
    "平静": "a serene, tranquil calm that stays light and unhurried",
    "温和": "a gentle, warm and understated mood",
    "希望": "a hopeful, uplifting and confident mood",
    "神秘": "a mysterious, ethereal and airy mood",
    "悲伤": "a sorrowful, melancholic and tender mood",
    "紧张": "a tense, urgent and gripping mood",
    "悲壮": "a somber, majestic and dignified mood",
    "欢快": "a joyful, bright and playful mood",
    "暗黑": "a dark, ominous and heavy mood",
    "梦幻": "a dreamy, otherworldly and expansive mood",
    "史诗": "an epic, grand and sweeping mood",
    "空灵": "an ethereal, spacious and breathable mood",
    # 英文短码直接映射
    "upbeat": "an upbeat, energetic and feel-good mood",
    "energetic": "a high-energy, driving and vibrant mood",
    "calm": "a serene, tranquil calm that stays light and unhurried",
    "dark": "a dark, ominous and heavy mood",
    "epic": "an epic, grand and sweeping mood",
    "melancholic": "a sorrowful, melancholic and tender mood",
    "romantic": "a romantic, warm and tender mood",
    "dreamy": "a dreamy, otherworldly and expansive mood",
}

_INST_EN = {
    "钢琴": "piano", "电钢琴": "electric piano", "合成器": "synthesizer",
    "木吉他": "acoustic guitar", "电吉他": "electric guitar", "贝斯": "bass",
    "弦乐": "string section", "长笛": "flute", "萨克斯": "saxophone",
    "小号": "trumpet", "鼓组": "drum kit", "电子鼓": "electronic drums",
    "古筝": "guzheng", "二胡": "erhu", "笛子": "dizi", "琵琶": "pipa",
    "人声": "vocal",
    "主音合成": "lead synthesizer", "贝斯合成": "synth bass", "氛围垫": "synth pad",
    "浓重合成": "heavy synth", "鼓刷": "brushed drums", "大提琴": "cello",
    "铜管": "brass", "定音鼓": "timpani", "人声合唱": "choir", "竖琴": "harp",
    "木琴": "marimba", "人声齐唱": "unison vocals", "女声哼鸣": "soft female hum",
    "打击乐": "percussion",
}


def _inst_en(cn: str) -> str:
    return _INST_EN.get(str(cn).strip(), str(cn).strip())


# 曲风预设（精简版，覆盖 AIjuhe 常见编排场景）
_GENRE_PRESETS = {
    "Mandopop / Ballad": dict(bpm=70, key="F major", energy="mid",
                              mood="passionate, heartfelt urban love, reflective yet tender",
                              insts=["piano", "string section", "bass"]),
    "Cinematic Orchestral / Epic": dict(bpm=90, key="D minor", energy="high",
                                        mood="majestic and urgent, gathering into intense driving passion",
                                        insts=["string section", "brass", "timpani", "choir"]),
    "Epic Orchestral / Trailer": dict(bpm=100, key="D minor", energy="high",
                                      mood="epic and volatile, soaring themes over explosive peaks",
                                      insts=["string section", "brass", "timpani", "choir"]),
    "EDM / Festival Electronic": dict(bpm=128, key="A minor", energy="high",
                                      mood="energetic and relentless, building into euphoric release",
                                      insts=["electronic drums", "heavy synth", "synth bass"]),
    "Lo-fi / Chillhop": dict(bpm=82, key="C major", energy="low",
                             mood="relaxed and lo-fi warm, a gentle restorative pulse",
                             insts=["electronic drums", "electric piano", "synth pad"]),
    "Ambient Electronic": dict(bpm=72, key="A major", energy="low",
                               mood="serene and meditative, transparent textures with gentle calm",
                               insts=["synth pad", "piano", "harp"]),
    "Pop / Upbeat Commercial": dict(bpm=120, key="G major", energy="mid",
                                    mood="bright and bouncy, sunny and instantly engaging",
                                    insts=["electronic drums", "electric piano", "lead synthesizer"]),
    "Electronic / Inspiring Upbeat": dict(bpm=124, key="E minor", energy="high",
                                          mood="urgent and inspiring, climbing into soaring peak",
                                          insts=["electronic drums", "lead synthesizer", "synth bass"]),
    "Instrumental / Cinematic": dict(bpm=90, key="D minor", energy="mid",
                                     mood="cinematic and atmospheric, evolving soundscape that swells",
                                     insts=["string section", "piano", "bass"]),
    "Instrumental / Epic": dict(bpm=100, key="D minor", energy="high",
                                mood="grand and sweeping, vast epic instrumental with monumental arcs",
                                insts=["string section", "brass", "timpani"]),
    "Synth-pop / Retrowave": dict(bpm=104, key="F major", energy="mid",
                                  mood="nostalgic and luminous, confident forward retro momentum",
                                  insts=["lead synthesizer", "electronic drums", "synth bass"]),
    "House / Deep House": dict(bpm=122, key="C major", energy="mid",
                               mood="deep steady house groove, warm pads with immersive pulse",
                               insts=["electronic drums", "synth bass", "electric piano"]),
}

_DEFAULT_PRESET = dict(bpm=90, key="C major", energy="mid",
                       mood="a balanced, warm and steady mood with clear layering",
                       insts=["piano"])


def _genre_preset(genre: str) -> dict:
    # 模糊匹配：用户可能只写 "Pop" 或 "Electronic"
    g = str(genre or "").strip()
    if g in _GENRE_PRESETS:
        return _GENRE_PRESETS[g]
    # 尝试前缀/包含匹配
    g_lower = g.lower()
    for key, preset in _GENRE_PRESETS.items():
        if g_lower in key.lower() or key.lower() in g_lower:
            return preset
    return _DEFAULT_PRESET


def _energy_from_dynamics(dyn: str, fallback: str = "mid") -> str:
    if not dyn:
        return fallback
    if dyn.startswith("弱"):
        return "low"
    if dyn.startswith("强") or dyn.startswith("渐强"):
        return "high"
    return fallback


def build_caption(params: dict, seed: int = -1) -> str:
    """由用户参数组装官方三段式英文 caption（喂给 MiniMax Music3）。

    与 RunVerseHub backend/app/music.py build_caption 完全同口径。
    """
    p = params or {}
    genre = str(p.get("genre") or "").strip() or "Instrumental / Cinematic"
    gp = _genre_preset(genre)
    bpm = int(p.get("bpm") or gp["bpm"])
    key = str(p.get("key") or gp["key"]).strip()
    key_word, key_scale = key, ""
    parts = key.split()
    if len(parts) >= 2:
        key_word, key_scale = parts[0], parts[1].lower()
    elif len(parts) == 1:
        key_scale = "minor"
    mood = str(p.get("mood") or "").strip()
    if mood:
        mood_en = _MOOD_TERMS.get(mood, mood)
    else:
        mood_en = gp["mood"]
    desc = str(p.get("description") or p.get("prompt") or "").strip()
    energy = _energy_from_dynamics(str(p.get("dynamics") or ""), gp["energy"])
    core = [_inst_en(x) for x in (p.get("instruments") or gp["insts"])] or ["piano"]
    vocal_on = bool(p.get("vocal_on", False))

    energy_desc = {
        "low": "soft, airy and spacious with plenty of breathing room",
        "high": "bold, driving and full-bodied with a powerful, cinematic lift",
        "mid": "balanced, warm and steady with clear layering",
    }[energy]

    lead = core[0]
    if vocal_on:
        vocal = ("A soft, expressive vocal leads the melody, warm in the verses "
                 "and gently swelling in the choruses, with subtle layered backing.")
    else:
        vocal = (f"An instrumental piece; the lead melodic role is carried by "
                 f"{lead}, with no vocal content.")

    caption = (
        f"Global Metadata\n\n"
        f"Basic Attributes: bpm is {bpm}. key is {key_word}"
        + (f", scale {key_scale}" if key_scale else "") + f". {genre}.\n"
        f"Global Emotional Progression: {mood_en}. Overall {energy_desc}.\n"
        f"Application Scenarios & Imagery: "
        f"{desc + '. ' if desc else ''}a versatile {genre.lower()} "
        f"suitable for visuals, games and cinematic scenes, {mood_en}.\n"
        f"Sonics & Production Profile: polished, modern stereo mix with "
        f"wide reverb and a spacious, high-fidelity texture.\n\n"
        f"Vocal Details\n\n"
        f"Vocal Gender & Timbre: {vocal}\n"
        f"Vocal Style: clear, lyrical and expressive.\n"
        f"Vocal FX: light reverb and a touch of delay. \n\n"
        f"Arrangement\n\n"
        f"Instrument Lifecycle Description (Primary/Secondary Layering): "
        f"Primary: {core[0]} carries the main melodic hook. "
        f"Secondary: {' and '.join(core[1:3]) or 'a supporting layer'} "
        f"provide warmth and depth behind the lead.\n"
        f"Groove & Foundation Progression: {energy_desc}.\n"
        f"Embellishments, Textures & Spatial FX: tasteful percussive and "
        f"textural embellishments, with {energy} energy throughout.\n"
        f"Ending & Outro: end with a smooth fade-out and a gentle outro; "
        f"the reverb tail must fully resolve before the track ends, "
        f"with no abrupt cut-off, no clicks or pops, and no high-frequency "
        f"hiss or artifacts in the final 10 seconds of the track."
    )
    return caption
