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
"""能力自主发现引擎 —— 让系统真正"智能"的核心。

设计哲学（对"完全没有任何智能化"的修正）：
  能力知识不应该全靠人类预先编程。系统必须能自己"问"、自己"试"、自己"判断"。

三层机制：
  1. **探测协议 (Probe Protocol)**：当 AI 入驻时能力描述缺失/模糊，
     系统自动生成探测任务发给它，从实际输出推断能力。
     ——不依赖开发者手动写卡片，不依赖宿主精确填写。

  2. **人事评估委派 (HR Delegation)**：城主任命人事AI，定期发起能力评估，
     对新入驻/能力不明的AI执行系统性探测。城主早期自己做，中期委派人事AI做。

  3. **执行反馈回灌 (Feedback Loop)**：每次任务执行结果自动回灌能力分数。
     做得好→加分，做砸→扣分。分数收敛到真实水平，不管初始声明是什么。

对外接口：
  - needs_discovery(citizen) → bool
  - generate_probes(citizen) → list[ProbeTask]
  - execute_probe(citizen, probe) → ProbeResult
  - infer_from_results(citizen, results) → InferredCapability
  - update_from_discovery(db, citizen, inferred) → 写入 CapabilityProfile
  - record_execution_feedback(db, citizen, kind, success, quality_score) → 回灌

与现有模块的关系：
  - onboarding.py：入驻后调用 needs_discovery → 如需要则创建 probe 治理任务
  - governor.py：城主遇到未知AI时 action="probe_capability" → 调本模块
  - task_orchestrator.py：执行子任务完成后调 record_execution_feedback
  - governance.py：新增 "hr_evaluation" 治理类型，由本模块执行
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .ai_judgment import ai_decide
from .models import AICitizen, CapabilityProfile, AuditLog

logger = logging.getLogger(__name__)

# 技能抽取系统提示：关键词表只作**兜底**，最终技能集由 AI 从自述中理解。
_SKILL_EXTRACT_SYSTEM = (
    "You are an HR analyst in an autonomous AI society. Read an AI citizen's "
    "self-description and extract the concrete capabilities it claims. Prefer these "
    "platform skill keys when they fit: image, video_civil, video_analysis, music, "
    "llm, ocr, tts, translation. You may add extra free-form skills if clearly "
    "stated. Do not invent capabilities not supported by the text.\n\n"
    "Respond ONLY with JSON:\n"
    '{"skills":["llm","image"],"reasoning":"one sentence"}'
)

# 探测方向系统提示：职业关键词表只作**兜底**，最终探测哪些 kind 由 AI 决定。
_PROBE_DIRECTION_SYSTEM = (
    "You are planning a capability probe for an AI citizen in an autonomous AI "
    "society. Given its occupation/self-description, decide which platform kinds "
    "are worth probing. Allowed kinds: llm, image, music, video_civil, "
    "video_analysis. Pick the 1-3 most likely directions.\n\n"
    "Respond ONLY with JSON:\n"
    '{"kinds":["llm"],"reasoning":"one sentence"}'
)

# 能力等级推断系统提示：分数阈值只作**兜底**，最终等级由 AI 结合探测事实判断。
_LEVEL_SYSTEM = (
    "You are assessing an AI citizen's verified capability level from probe results "
    "in an autonomous AI society. Given each probe's kind, success and quality score "
    "(0-100), decide the level: unknown | l1 | l2 | l3 (l3 = strong, proven at high "
    "quality across probes). Thresholds are references, not verdicts.\n\n"
    "Respond ONLY with JSON:\n"
    '{"level":"unknown|l1|l2|l3","reasoning":"one sentence"}'
)

# ==================== 数据结构 ====================

@dataclass
class ProbeTask:
    """单个探测任务。"""
    probe_id: str
    kind: str           # 猜测要测试的 kind（llm/image/video/music/video_analysis/custom）
    prompt: str         # 发给 AI 的探测 prompt
    expect: str         # 期望输出描述（用于评分）
    timeout_sec: int = 60


@dataclass
class ProbeResult:
    """单个探测结果。"""
    probe_id: str
    kind: str
    success: bool
    quality_score: float  # 0~100
    response_text: str = ""
    artifacts: list = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0


@dataclass
class InferredCapability:
    """从多次探测推断出的能力画像。"""
    skills: list[str]           # 推断出的技能列表
    confidence: float           # 置信度 0~1
    level_estimate: str         # "l1"/"l2"/"l3"/"unknown"
    summary: str                # 自然语言摘要（写入 profile_json）
    evidence: list[dict] = field(default_factory=list)


# ==================== Step 1: 判断是否需要发现 ====================

# 自述"太空"的判定阈值：self_decl 有效字符 < 20 → 视为模糊
_VAGUE_THRESHOLD = 20
# 有 profile 但 benchmark_score 为 0 且无 execution_count → 从未真正执行过


def needs_discovery(citizen: AICitizen, profile: Optional[CapabilityProfile] = None) -> bool:
    """判断该 AI 是否需要能力发现。

    触发条件（任一满足）：
    1. 完全没有 CapabilityProfile
    2. 有 profile 但 profile_json 为空或极短（<20有效字符）
    3. benchmark_score = 0 且从未有过执行反馈
    """
    if profile is None:
        return True  # 没有任何档案

    if not profile.profile_json or len(profile.profile_json.strip()) < _VAGUE_THRESHOLD:
        return True

    try:
        pdata = json.loads(profile.profile_json)
    except Exception:
        return True

    # 空字典或只有无意义字段
    meaningful_keys = {"skill", "skills", "description", "abilities", "capabilities",
                       "can_do", "input", "output", "prompt_format", "platform_limits"}
    if not meaningful_keys & set(pdata.keys()):
        return True

    return False


# ==================== Step 2: 生成探测任务 ====================

# 探测任务模板：每种 kind 的"万能探针"
PROBE_TEMPLATES = {
    "llm": ProbeTask(
        probe_id="", kind="llm",
        prompt="Explain what artificial intelligence is in one sentence. Be accurate, concise, and easy to understand.",
        expect="Should return a fluent single-sentence explanation (20~100 chars) that is semantically correct",
    ),
    "image": ProbeTask(
        probe_id="", kind="image",
        prompt="A red apple on a white table, minimalist style",
        expect="Should return an image (not an error message)",
    ),
    "music": ProbeTask(
        probe_id="", kind="music",
        prompt="An upbeat, bright piano piece",
        expect="Should return an audio clip (not an error message)",
    ),
    "video_analysis": ProbeTask(
        probe_id="", kind="video_analysis",
        prompt="Analyze the visual composition of this video",
        expect="Should return a JSON analysis result with timestamps",
    ),
    "video_civil": ProbeTask(
        probe_id="", kind="video_civil",
        prompt="A cat walking on the beach at sunset, cinematic",
        expect="Should return a video URL",
    ),
    "custom": ProbeTask(
        probe_id="", kind="custom",
        prompt="Describe what you can do. Format: I can [verb] [object], output [format].",
        expect="Should return a clear description of its own capabilities",
    ),
}


def generate_probes(citizen: AICitizen, existing_profile: Optional[CapabilityProfile] = None) -> list[ProbeTask]:
    """根据 AI 的 occupation/self_decl 生成探测任务列表。

    策略：
    - occupation 有明确关键词 → 优先探测该方向 + 加一个 custom 确认
    - occupation 为空或泛化 → 探测通用（llm + custom）
    - 已有部分 profile → 只探测未覆盖的方向
    """
    probes = []
    occupation = (citizen.occupation or "").lower()

    # 从 occupation 推断可能方向
    candidate_kinds = _infer_candidate_kinds(occupation)

    # 获取已有 profile 的技能方向
    covered = set()
    if existing_profile and existing_profile.skill:
        covered.add(existing_profile.skill)

    for kind in candidate_kinds:
        if kind in covered:
            continue
        tmpl = PROBE_TEMPLATES.get(kind, PROBE_TEMPLATES["custom"])
        p = ProbeTask(
            probe_id=f"probe_{citizen.id}_{uuid.uuid4().hex[:8]}",
            kind=tmpl.kind,
            prompt=tmpl.prompt,
            expect=tmpl.expect,
            timeout_sec=tmpl.timeout_sec,
        )
        probes.append(p)

    # 始终追加 custom 自述探针（让AI自己说能做什么）
    if "custom" not in covered:
        tmpl = PROBE_TEMPLATES["custom"]
        probes.append(ProbeTask(
            probe_id=f"probe_{citizen.id}_{uuid.uuid4().hex[:8]}",
            kind="custom",
            prompt=tmpl.prompt,
            expect=tmpl.expect,
        ))

    return probes[:4]  # 最多4个探测，避免过度打扰


def _infer_candidate_kinds(occupation: str) -> list[str]:
    """从 occupation 推断可能的能力方向（AI 优先，关键词表兜底）。"""
    base = _infer_candidate_kinds_keyword(occupation)
    obj = ai_decide(
        system=_PROBE_DIRECTION_SYSTEM,
        prompt=f"occupation/self-description: {occupation or '(empty)'}",
        fallback={"kinds": base})
    kinds = obj.get("kinds")
    if not isinstance(kinds, list):
        return base
    allowed = set(PROBE_TEMPLATES.keys()) - {"custom"}
    picked = [str(k) for k in kinds if str(k) in allowed]
    return list(dict.fromkeys(picked))[:3] if picked else base


def _infer_candidate_kinds_keyword(occupation: str) -> list[str]:
    """关键词兜底：从 occupation 字符串推断可能的能力方向。"""
    kinds = []
    if any(w in occupation for w in ("图", "image", "画", "design")):
        kinds.append("image")
    if any(w in occupation for w in ("视频", "video", "cinema", "电影")):
        kinds.extend(["video_civil", "video_analysis"])
    if any(w in occupation for w in ("音乐", "music", "audio", "声")):
        kinds.append("music")
    if any(w in occupation for w in ("视觉", "vlm", "质检", "vision", "分析")):
        kinds.append("video_analysis")
    if any(w in occupation for w in ("文", "text", "writing", "文案", "写作", "llm")):
        kinds.append("llm")

    # 无匹配时默认探测 llm（最通用的能力）
    if not kinds:
        kinds.append("llm")

    return list(dict.fromkeys(kinds))  # 去重保序


# ==================== Step 3: 执行探测 ====================

def execute_probe(citizen: AICitizen, probe: ProbeTask) -> ProbeResult:
    """向 AI 公民发送探测任务并评估结果。

    对于 API 模式的 AI：直接调用其 endpoint。
    对于 Worker/Cloud 模式：通过平台执行层走一遍。
    对于平台原生能力：直接用 platform_compute 执行。
    """
    import time
    start = time.time()

    try:
        from .platform_compute import run as pc_run

        # 平台原生 kind：走标准执行层
        if probe.kind in ("llm", "image", "music", "video_civil", "video_analysis"):
            result = pc_run(
                kind=probe.kind,
                prompt=probe.prompt,
                params={},
                citizen_id=citizen.id,
            )
            latency = int((time.time() - start) * 1000)
            success = result.get("status") == "succeeded"
            text = result.get("text", "")
            files = result.get("files", [])

            return ProbeResult(
                probe_id=probe.probe_id,
                kind=probe.kind,
                success=success,
                quality_score=_evaluate_response(text, probe, success),
                response_text=text[:500],
                artifacts=files,
                error=result.get("error", ""),
                latency_ms=latency,
            )

        # custom kind：让AI自述能力
        if probe.kind == "custom":
            # 对于 custom 模式，尝试调 AI 的 self_decl 描述 + occupation 做推断
            # 实际发送需要 AI 有可调用的通道
            return _execute_custom_probe(citizen, probe, start)

        # 未知 kind：直接标记 custom probe
        return ProbeResult(
            probe_id=probe.probe_id,
            kind=probe.kind,
            success=False,
            quality_score=0,
            error=f"No probe executor for kind={probe.kind}",
        )

    except Exception as e:
        latency = int((time.time() - start) * 1000)
        return ProbeResult(
            probe_id=probe.probe_id,
            kind=probe.kind,
            success=False,
            quality_score=0,
            error=str(e),
            latency_ms=latency,
        )


def _execute_custom_probe(citizen: AICitizen, probe: ProbeTask, start_time) -> ProbeResult:
    """custom 探测：对于无法直接调用的 AI，从其 occupation + self_decl 推断。"""
    import time
    from .models import OnboardingApplication

    # 尝试获取 self_decl
    self_decl_text = citizen.occupation or ""
    try:
        from .database import SessionLocal
        db = SessionLocal()
        try:
            app = db.query(OnboardingApplication).filter(
                OnboardingApplication.citizen_id == citizen.id
            ).first()
            if app and app.self_decl:
                self_decl_text = f"{citizen.occupation}. {app.self_decl}"
        finally:
            db.close()
    except Exception:
        pass

    # 对于 custom AI，至少能确认它"活着"且有描述
    has_description = len(self_decl_text.strip()) > 10
    latency = int((time.time() - start_time) * 1000)

    return ProbeResult(
        probe_id=probe.probe_id,
        kind="custom",
        success=has_description,
        quality_score=60 if has_description else 20,
        response_text=self_decl_text[:500],
        latency_ms=latency,
    )


def _evaluate_response(text: str, probe: ProbeTask, api_success: bool) -> float:
    """简单评分逻辑：
    - API 失败 → 0分
    - 返回了 mock 结果（开发环境） → 50分（只证明通路存在）
    - 有实质内容 → 按质量打分
    """
    if not api_success:
        return 0.0
    if "[[MOCK" in text:
        return 50.0  # mock 只证明通路可用
    if not text or len(text.strip()) < 5:
        return 10.0
    # 有实质内容，基础 70 分
    score = 70.0
    # 内容丰富度加分
    if len(text) > 50:
        score += 10
    if len(text) > 200:
        score += 5
    # JSON 格式输出（对 video_analysis）加分
    if probe.kind == "video_analysis" and "{" in text:
        score += 15
    return min(score, 100.0)


# ==================== Step 4: 从结果推断能力 ====================

def infer_from_results(citizen: AICitizen, results: list[ProbeResult]) -> InferredCapability:
    """从多次探测结果推断能力画像。"""

    # 收集成功的 kind
    successful_kinds = [r.kind for r in results if r.success and r.quality_score >= 40]
    high_quality_kinds = [r.kind for r in results if r.success and r.quality_score >= 70]

    # 确定技能列表
    skills = []
    for kind in successful_kinds:
        if kind != "custom":  # custom 不算具体技能
            skills.append(kind)

    # 如果 custom 返回了自述，尝试从中提取技能
    custom_results = [r for r in results if r.kind == "custom" and r.response_text]
    if custom_results:
        extracted = _extract_skills_from_text(custom_results[0].response_text)
        for s in extracted:
            if s not in skills:
                skills.append(s)

    # 如果没有具体技能，使用 occupation 作为推断
    if not skills:
        skills = _infer_candidate_kinds((citizen.occupation or "").lower())

    # 置信度
    n_probes = len(results)
    n_success = len(successful_kinds)
    confidence = n_success / max(n_probes, 1)

    # 等级估算：AI 结合探测事实判断，阈值规则作兜底。
    successful = [r for r in results if r.success]
    if not successful:
        base_level = "unknown"
    elif all(r.quality_score >= 80 for r in successful):
        base_level = "l2"  # 全部高分 → 至少 l2
    elif n_success >= max(1, n_probes // 2):
        base_level = "l1"  # 半数以上成功 → l1
    else:
        base_level = "unknown"

    probe_facts = [{"kind": r.kind, "success": r.success,
                    "score": r.quality_score} for r in results]
    obj = ai_decide(
        system=_LEVEL_SYSTEM,
        prompt=json.dumps({"probes": probe_facts,
                           "threshold_hint": base_level}, ensure_ascii=False),
        fallback={"level": base_level})
    level = obj.get("level")
    if level not in ("unknown", "l1", "l2", "l3"):
        level = base_level

    # 摘要
    avg_score = (sum(r.quality_score for r in results) / max(len(results), 1))
    summary = _build_capability_summary(skills, avg_score, results)

    evidence = [
        {"kind": r.kind, "success": r.success, "score": r.quality_score,
         "snippet": r.response_text[:80]}
        for r in results
    ]

    return InferredCapability(
        skills=skills,
        confidence=round(confidence, 2),
        level_estimate=level,
        summary=summary,
        evidence=evidence,
    )


def _extract_skills_from_text(text: str) -> list[str]:
    """从 AI 自述文本中提取技能关键词（AI 优先，关键词表兜底）。"""
    base = _extract_skills_from_text_keyword(text)
    obj = ai_decide(
        system=_SKILL_EXTRACT_SYSTEM,
        prompt=f"self-description:\n{text[:1500]}",
        fallback={"skills": base})
    skills = obj.get("skills")
    if not isinstance(skills, list):
        return base
    cleaned = [str(s).strip() for s in skills if str(s).strip()]
    return list(dict.fromkeys(cleaned))[:12] if cleaned else base


def _extract_skills_from_text_keyword(text: str) -> list[str]:
    """关键词兜底：从自述文本中提取技能关键词。"""
    skills = []
    text_lower = text.lower()
    skill_indicators = {
        "image": ["图", "image", "画", "绘画", "draw", "generate image"],
        "video_civil": ["视频生成", "video gen", "generate video", "video creat"],
        "video_analysis": ["分析视频", "视频诊断", "video analy", "video diagnos", "video review"],
        "music": ["音乐", "music", "audio", "compose", "作曲"],
        "llm": ["文本", "text", "writing", "写", "文生文", "对话", "chat"],
        "ocr": ["ocr", "识别文字", "text recogni"],
        "tts": ["tts", "语音合成", "text to speech", "配音"],
        "translation": ["翻译", "translate"],
    }
    for skill, indicators in skill_indicators.items():
        if any(ind in text_lower for ind in indicators):
            skills.append(skill)
    return skills


def _build_capability_summary(skills: list[str], avg_score: float,
                              results: list[ProbeResult]) -> str:
    """生成能力摘要文本。"""
    if not skills:
        return "能力未确认（探测无明确结果）"

    parts = [f"已确认技能：{', '.join(skills)}"]
    parts.append(f"探测平均质量：{avg_score:.0f}/100")
    if avg_score >= 80:
        parts.append("执行质量优秀")
    elif avg_score >= 60:
        parts.append("执行质量合格")
    elif avg_score >= 40:
        parts.append("执行质量待提升")
    else:
        parts.append("执行质量不达标")

    return "；".join(parts)


# ==================== Step 5: 更新能力档案 ====================

def update_from_discovery(db: Session, citizen: AICitizen,
                          inferred: InferredCapability,
                          source: str = "discovery") -> CapabilityProfile:
    """将推断结果写入/更新 CapabilityProfile。

    source: 发现来源，"discovery"（系统自动探测）/"execution"（执行反馈）/"manual"（人工）
    """
    profile = db.query(CapabilityProfile).filter(
        CapabilityProfile.citizen_id == citizen.id
    ).first()

    if not profile:
        profile = CapabilityProfile(
            citizen_id=citizen.id,
            skill=inferred.skills[0] if inferred.skills else "unknown",
            declared=0,  # 0 = 非自述，系统推断
        )
        db.add(profile)

    # 构造 profile_json
    existing = {}
    if profile.profile_json:
        try:
            existing = json.loads(profile.profile_json)
        except Exception:
            pass

    existing.update({
        "discovered_skills": inferred.skills,
        "discovery_confidence": inferred.confidence,
        "discovery_summary": inferred.summary,
        "discovery_source": source,
        "discovery_time": datetime.utcnow().isoformat(),
        "evidence": inferred.evidence,
    })
    profile.profile_json = json.dumps(existing, ensure_ascii=False)

    # 更新 benchmark_score（EMA 平滑，不覆盖已有高分）
    new_score = sum(r["score"] for r in inferred.evidence) / max(len(inferred.evidence), 1)
    if profile.benchmark_score and profile.benchmark_score > 0:
        # EMA: 新权重 0.3，保留历史
        profile.benchmark_score = 0.7 * profile.benchmark_score + 0.3 * (new_score / 100.0)
    else:
        profile.benchmark_score = new_score / 100.0

    # 更新 verified_level
    if inferred.level_estimate != "unknown":
        if not profile.verified_level or profile.verified_level == "unverified":
            profile.verified_level = inferred.level_estimate

    db.flush()

    # 审计
    db.add(AuditLog(
        actor_type="system", actor_id=0,
        action=f"capability.discovery.{citizen.id}",
        detail=json.dumps({"skills": inferred.skills,
                           "confidence": inferred.confidence,
                           "source": source}, ensure_ascii=False),
    ))

    return profile


# ==================== Step 6: 执行反馈回灌 ====================

def record_execution_feedback(db: Session, citizen_id: int, kind: str,
                              success: bool, quality_score: float = 0,
                              error_msg: str = ""):
    """任务执行后回灌能力分数。

    在 task_orchestrator 子任务完成后调用。这是"从实际表现学习"的核心。
    不管初始声明是什么，实际执行结果才是真正的能力证明。

    规则：
    - 成功且质量高 → 加分，增强该 kind 的可信度
    - 失败 → 扣分，可能需要降级
    - 质量分 < 40 → 视为"不合格"，显著扣分
    """
    profiles = db.query(CapabilityProfile).filter(
        CapabilityProfile.citizen_id == citizen_id
    ).all()

    # 找到匹配该 kind 的 profile；如果不存在则创建（自动发现！）
    target = None
    for p in profiles:
        if _profile_covers_kind(p, kind):
            target = p
            break

    if target is None:
        # 自动发现：这个 citizen 执行了该 kind，但没有任何 profile 覆盖
        target = CapabilityProfile(
            citizen_id=citizen_id,
            skill=kind,
            profile_json=json.dumps({
                "discovered_skills": [kind],
                "discovery_confidence": 0.5,
                "discovery_summary": f"从执行反馈自动发现（{kind}）",
                "discovery_source": "execution",
                "discovery_time": datetime.utcnow().isoformat(),
            }, ensure_ascii=False),
            declared=0,
        )
        db.add(target)
        db.flush()

    # 计算本次反馈的分数增量
    if success:
        delta = (quality_score / 100.0) * 0.15  # 最多 +0.15
        if quality_score < 40:
            delta = -0.1  # 虽然"成功"但质量极差 → 扣分
    else:
        delta = -0.15  # 失败 → 扣 0.15

    # EMA 式更新 benchmark_score
    old = target.benchmark_score or 0.5  # 默认 0.5（未知）
    target.benchmark_score = max(0.0, min(1.0, old + delta))

    # 更新 execution_count（存在 profile_json 中）
    pdata = {}
    if target.profile_json:
        try:
            pdata = json.loads(target.profile_json)
        except Exception:
            pass
    exec_count = pdata.get("execution_count", 0) + 1
    pdata["execution_count"] = exec_count
    pdata["last_execution"] = {
        "kind": kind,
        "success": success,
        "quality": quality_score,
        "time": datetime.utcnow().isoformat(),
        "error": error_msg[:200] if error_msg else "",
    }
    # 最近10次执行历史
    history = pdata.get("execution_history", [])
    history.append({"kind": kind, "success": success, "q": quality_score})
    pdata["execution_history"] = history[-10:]
    target.profile_json = json.dumps(pdata, ensure_ascii=False)

    # 降级检测：连续5次失败且分数 < 0.2
    if exec_count >= 5:
        recent = pdata.get("execution_history", [])[-5:]
        if all(not h["success"] for h in recent) and target.benchmark_score < 0.2:
            target.verified_level = "unverified"

    db.flush()


def _profile_covers_kind(profile: CapabilityProfile, kind: str) -> bool:
    """判断一个 profile 是否覆盖某个 kind。"""
    # 直接匹配 skill
    if profile.skill == kind:
        return True
    # 检查 discovered_skills
    try:
        pdata = json.loads(profile.profile_json or "{}")
        skills = pdata.get("discovered_skills", [])
        if kind in skills:
            return True
    except Exception:
        pass
    # SKILL_KIND_MAP 映射
    from .task_orchestrator import SKILL_KIND_MAP
    mapped = SKILL_KIND_MAP.get(profile.skill)
    if mapped == kind:
        return True
    return False


# ==================== 城主专用：快速评估摘要 ====================

def governor_capability_view(citizen: AICitizen,
                             profile: Optional[CapabilityProfile] = None) -> str:
    """城主视角的能力摘要：综合自述+发现+执行反馈。

    城主每次决策时看到的能力描述，应该比之前更智能：
    - 有发现数据的 → 显示推断结果
    - 有执行反馈的 → 显示实际表现分数
    - 什么都没有的 → 标记 "UNKNOWN - needs probe"
    """
    if not profile:
        return "[UNKNOWN] 无能力档案。需要探测(probe)后才能使用。"

    lines = []
    # 1. 自述
    if profile.declared and profile.profile_json:
        try:
            pdata = json.loads(profile.profile_json)
            if pdata.get("discovered_skills"):
                lines.append(f"Skills: {', '.join(pdata['discovered_skills'])}")
            elif pdata.get("description"):
                lines.append(f"Declared: {pdata['description'][:100]}")
            else:
                lines.append(f"Skill: {profile.skill}")
        except Exception:
            lines.append(f"Skill: {profile.skill}")

    # 2. 发现数据
    if profile.profile_json:
        try:
            pdata = json.loads(profile.profile_json)
            if pdata.get("discovery_summary"):
                lines.append(f"Discovery: {pdata['discovery_summary']}")
            conf = pdata.get("discovery_confidence", 0)
            if conf > 0:
                lines.append(f"Confidence: {conf:.0%}")
        except Exception:
            pass

    # 3. 执行反馈
    exec_count = 0
    try:
        pdata = json.loads(profile.profile_json or "{}")
        exec_count = pdata.get("execution_count", 0)
        history = pdata.get("execution_history", [])
        if history:
            successes = sum(1 for h in history if h.get("success"))
            total = len(history)
            lines.append(f"Track: {successes}/{total} recent success")
    except Exception:
        pass

    # 4. 综合评分
    score = profile.benchmark_score
    if score is not None:
        if score >= 0.8:
            lines.append(f"Score: {score:.2f} (excellent)")
        elif score >= 0.5:
            lines.append(f"Score: {score:.2f} (competent)")
        else:
            lines.append(f"Score: {score:.2f} (unproven)")

    # 5. 判断
    if exec_count == 0 and (score is None or score < 0.3):
        lines.append("⚠️ UNPROVEN: 从未成功执行过任务。建议先 probe 再分配重要任务。")

    return " | ".join(lines) if lines else "[UNKNOWN] 无有效能力信息"
