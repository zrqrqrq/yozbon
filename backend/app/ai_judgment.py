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
"""工作前判断引擎 —— AI社会不允许傻子AI的核心机制。

设计哲学：
  一个真正自治的AI社会里，每个有思维能力的AI员工在开始工作前，
  必须像正常人一样先想清楚"这件事我该怎么做才对"。
  不能接到任务就无脑执行——那是螺丝钉，不是员工。

核心原则：
  1. 思考型AI（LLM/VLM/视频分析/外部推理模型）：执行前必须自检判断。
  2. 固定功能AI（TTS/OCR/纯生成API等）：无思维能力，不要求判断。
  3. 判断是"轻思考"——不是重新做任务，而是30秒内想清楚：
     - 这个任务我能做吗？
     - 参数合理吗？
     - 有没有明显的问题/风险？
     - 该用什么策略？
  4. 判断失败不阻断执行——宁可少判断，不可卡死流程。

判断结果处理：
  - proceed: 正常执行（可带微调参数）
  - adjust: 用AI自己建议的调整参数执行
  - refuse: 标记为"拒绝执行"，附带理由，交还给编排层/城主处理

与现有模块的关系：
  - task_orchestrator._execute_subtask：执行前调用本模块
  - governor：城主的决策也使用本模块的判断框架（城主是最大号的"思考型AI"）
  - capability_discovery：refuse 事件触发执行反馈回灌（拒绝也是一种能力信息）
  - platform_compute：判断本身使用与执行相同的计算通道
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from .models import AICitizen, CapabilityProfile
from .capability_cards import FIXED_FUNCTION_KINDS as _CARD_FIXED
from .capability_cards import THINKING_KINDS as _CARD_THINKING

logger = logging.getLogger(__name__)

# ==================== 分类 ====================
#
# "谁有脑子"的唯一真源 = capability_cards.CAPABILITY_CARDS[*]["thinking"]。
# 此处不再自造枚举——避免与能力卡片漂移（同一 kind 被两处定义）。
# 注意：这只是平台原生 kind 的分类；外部 AI 可通过 compute_assets 声明 reasoning。
REASONING_KINDS = _CARD_THINKING
FIXED_FUNCTION_KINDS = _CARD_FIXED

# 判断 LLM 调用的最大 token（极轻量，不能成为性能瓶颈）
JUDGMENT_MAX_TOKENS = 400
# 判断超时后的降级行为：proceed（放行）或 skip（跳过判断直接执行）
JUDGMENT_FAILURE_MODE = "proceed"  # 默认放行，不卡流程


# ==================== 数据结构 ====================

@dataclass
class JudgmentResult:
    """AI 工作前判断的结构化结果。"""
    decision: str = "proceed"       # proceed / adjust / refuse
    reasoning: str = ""             # 判断理由（审计可追踪）
    adjusted_params: dict = field(default_factory=dict)  # adjust 时的修正参数
    strategy_notes: str = ""        # 策略建议（给后续质检参考）
    refused: bool = False           # 便捷字段：是否拒绝
    skipped: bool = False           # 是否跳过了判断（不具备思维能力或判断失败）
    use_harness: bool = False       # AI 建议使用 Agent Harness 多步循环执行
    tools_suggested: list = field(default_factory=list)  # AI 建议使用的工具 key 列表

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reasoning": self.reasoning,
            "adjusted_params": self.adjusted_params,
            "strategy_notes": self.strategy_notes,
            "refused": self.refused,
            "skipped": self.skipped,
            "use_harness": self.use_harness,
            "tools_suggested": self.tools_suggested,
        }


# ==================== 思维能力判定 ====================

def is_thinking_capable(citizen: AICitizen, kind: str) -> bool:
    """判断该 AI 在执行该 kind 时是否具有思维能力（需要事前判断）。

    规则：
    1. 平台推理型 kind（llm/vlm/video_analysis）→ 永远有思维能力
    2. 平台固定功能 kind（image/music/video等）→ 无思维能力
    3. 外部 AI（worker/cloud/api 通道）：
       - 若 compute_assets 中 reasoning=true 或 reasoning_channel 存在 → 有思维能力
       - 若 occupation 含关键词（决策/分析/推理/判断/评审/review/analysis）→ 推断有
       - 否则 → 无思维能力
    """
    # 规则1：平台推理 kind
    if kind in REASONING_KINDS:
        return True

    # 规则2：平台固定功能 kind → 直接无思维
    if kind in FIXED_FUNCTION_KINDS:
        return False

    # 规则3：外部/未知 kind → 看 compute_assets 和 occupation
    try:
        assets = json.loads(citizen.compute_assets or "{}")
    except Exception:
        assets = {}

    # 显式声明有推理能力
    if assets.get("reasoning") is True or assets.get("reasoning_channel"):
        return True

    # 职业推断：高认知职业大概率需要判断
    occupation = (citizen.occupation or "").lower()
    thinking_hints = (
        "决策", "分析", "推理", "判断", "评审", "规划", "策略", "质检",
        "decision", "analysis", "reason", "judge", "review", "plan",
        "strategy", "quality", "supervis", "manage", "architect",
    )
    if any(h in occupation for h in thinking_hints):
        return True

    # 未知 kind + 无推理标记 → 默认有思维能力（宁多判断不少判断）
    # 因为未知 kind 很可能是需要推理的自定义能力
    return True


# ==================== 判断 Prompt 构建 ====================

# 通用判断系统提示：所有思考型AI共用的"先想后做"框架
_JUDGMENT_SYSTEM = (
    "You are an AI worker about to execute a task. Before doing the actual work, "
    "you must think like a competent professional: briefly assess the task and "
    "decide how to proceed. This is NOT the task itself — it is your 10-second "
    "pre-work sanity check, like a human glancing at a work order before starting.\n\n"
    "Think about:\n"
    "1. CAN I do this? (Is this within my actual capability?)\n"
    "2. TOOLS? (Do I have tools available that would help? Should I use multi-step "
    "   agent harness for complex multi-tool tasks?)\n"
    "3. SHOULD I adjust? (Are the params/approach reasonable? Any obvious errors?)\n"
    "4. RISKS? (Anything that looks wrong, contradictory, or likely to fail?)\n"
    "5. STRATEGY? (How should I best approach this specific task?)\n\n"
    "Respond ONLY with a JSON object:\n"
    '{"decision":"proceed|adjust|refuse",'
    '"reasoning":"one sentence why",'
    '"adjusted_params":{},'
    '"strategy_notes":"brief approach tip for self",'
    '"use_harness":false,'
    '"tools_suggested":[]}\n\n'
    "Rules:\n"
    "- If the task is reasonable and within your ability: decision=proceed\n"
    "- If minor issues (weird params, slight ambiguity): decision=adjust, fill adjusted_params\n"
    "- If fundamentally impossible/contradictory/dangerous: decision=refuse\n"
    "- Be concise. One sentence reasoning is enough. Do NOT overthink.\n"
    "- adjusted_params should ONLY contain fields you actually want to change.\n"
    "- use_harness: set true ONLY when the task requires MULTIPLE sequential tool calls "
    "  (e.g., analyze video then search info then generate report). Single-step tasks "
    "  do NOT need harness.\n"
    "- tools_suggested: list tool_keys you plan to use (from the Available Tools section "
    "  below, if provided). Leave empty if you will answer directly without tools.\n"
)


def _build_judgment_prompt(citizen: AICitizen, kind: str, action: str,
                           params: dict, capability_context: str,
                           tools_summary: str = "") -> str:
    """构建判断请求的 user prompt（注入任务上下文 + 可用工具目录）。"""
    parts = [
        "[Task to execute]",
        f"Kind: {kind}",
        f"Action/Request: {action[:500]}",
        f"Parameters: {json.dumps(params, ensure_ascii=False)[:600]}",
        "",
        "[Your capability context]",
        capability_context or "No specific capability profile recorded.",
    ]
    # 注入可用工具目录（让 AI 知道自己有哪些工具可以用）
    if tools_summary:
        parts.extend(["", "[Available Tools you can invoke]", tools_summary])
    # video_analysis：把"分析策略"这一决策并入本次判断，避免抽帧前再问一次 VLM
    # （judg_dedup）。边界取自 capability_cards 真源。
    if kind == "video_analysis":
        from .capability_cards import CAPABILITY_CARDS
        lim = (CAPABILITY_CARDS.get("video_analysis") or {}).get("limits", {})
        fps = lim.get("fps", {})
        chunk = lim.get("chunk_window_sec", {})
        overlap = lim.get("chunk_overlap_sec", {})
        parts.extend([
            "",
            "[Video analysis strategy - you ARE the video analyst]",
            "Also put your extraction strategy into adjusted_params so it can be "
            "handed straight to the frame extractor (you will NOT be asked again):",
            f'  "fps": {fps.get("min", 2)}-{fps.get("max", 5)}, '
            f'"chunk_sec": {chunk.get("min", 12)}-{chunk.get("max", 25)}, '
            f'"overlap_sec": {overlap.get("min", 2)}-{overlap.get("max", 6)}, '
            '"analysis_focus": "what to look for".',
        ])
    parts.extend([
        "",
        "[Instructions]",
        "Assess this task. Should you proceed, adjust, or refuse?",
        "If tools above could help accomplish this task more effectively, list them in tools_suggested.",
        "If the task requires multiple sequential tool calls, set use_harness=true.",
        "If proceeding, note your strategy briefly.",
    ])
    return "\n".join(parts)


# ==================== 核心判断函数 ====================

def pre_work_judgment(db: Session, citizen: AICitizen,
                      kind: str, action: str, params: dict) -> JudgmentResult:
    """AI 工作前判断：在执行前用 AI 自己的智能做一次轻量自检。

    这是 AI 社会"不允许傻子AI"的核心保障：
    每个有脑子的 AI 在做事前都要想一下"这事对不对、能不能做、怎么做最好"。
    同时告知 AI 它有哪些工具可以用，让它做出更明智的判断。

    失败降级策略：判断失败 → 自动放行（不卡流程）。
    """
    # Step 1: 检查是否需要判断
    if not is_thinking_capable(citizen, kind):
        return JudgmentResult(skipped=True, reasoning="not thinking-capable, skip")

    # Step 2: 构建能力上下文（让 AI 知道自己是谁、能做什么）
    capability_context = _capability_context(db, citizen)

    # Step 2b: 获取可用工具目录摘要（让 AI 知道自己有哪些工具可用）
    tools_summary = _tools_summary(db)

    # Step 3: 构建判断 prompt（含工具信息）
    user_prompt = _build_judgment_prompt(
        citizen, kind, action, params, capability_context, tools_summary)

    # Step 4: 调用 AI 自己的通道做轻量推理
    try:
        raw_text = _invoke_self_reasoning(db, citizen, user_prompt)
        if not raw_text:
            logger.debug("pre_work_judgment: empty reasoning output for %s/%s, proceed",
                         citizen.name, kind)
            return JudgmentResult(skipped=True, reasoning="empty reasoning output")

        # Step 5: 解析判断结果
        result = _parse_judgment(raw_text)
        logger.info("pre_work_judgment: citizen=%s kind=%s decision=%s harness=%s reasoning=%s",
                    citizen.name, kind, result.decision, result.use_harness,
                    result.reasoning[:80])
        return result

    except Exception as e:
        # 判断失败：降级为放行
        logger.warning("pre_work_judgment failed for %s/%s: %s (mode=%s)",
                       citizen.name, kind, str(e)[:100], JUDGMENT_FAILURE_MODE)
        if JUDGMENT_FAILURE_MODE == "proceed":
            return JudgmentResult(skipped=True, reasoning=f"judgment failed: {str(e)[:50]}")
        return JudgmentResult(refused=True, decision="refuse",
                              reasoning=f"judgment mechanism error: {str(e)[:50]}")


def _capability_context(db: Session, citizen: AICitizen) -> str:
    """构建 AI 自身的简短能力描述（供判断 prompt 使用）。"""
    try:
        profiles = db.query(CapabilityProfile).filter(
            CapabilityProfile.citizen_id == citizen.id).all()
        if not profiles:
            return f"Occupation: {citizen.occupation or 'unknown'}"
        lines = [f"Occupation: {citizen.occupation or 'unknown'}"]
        for p in profiles:
            try:
                info = json.loads(p.profile_json or "{}")
            except Exception:
                info = {}
            score = info.get("benchmark_score", "N/A")
            level = info.get("verified_level", "unknown")
            lines.append(f"  - {p.skill}: level={level}, score={score}")
        return "\n".join(lines)
    except Exception:
        return f"Occupation: {citizen.occupation or 'unknown'}"


def _tools_summary(db: Session) -> str:
    """从技能库获取当前可用工具的简短摘要（供判断 prompt 注入）。"""
    try:
        from . import tool_registry
        catalog = tool_registry.discover(db)
        available = [t for t in catalog.get("items", []) if t.get("available", 0)]
        if not available:
            return ""
        lines = []
        for t in available:
            lines.append(f"- {t['tool_key']}: {t['description'][:80]}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _invoke_self_reasoning(db: Session, citizen: AICitizen,
                           user_prompt: str) -> str:
    """调用 AI 自身的通道做一次轻量推理。

    关键：用 AI 自己的"脑子"来思考，不是用外部统一模型。
    这样每个 AI 的判断来自它自身的能力，不同 AI 会给出不同判断。
    """
    return _invoke_reasoning(db, citizen, _JUDGMENT_SYSTEM, user_prompt,
                             JUDGMENT_MAX_TOKENS)


def _invoke_reasoning(db: Session, citizen: AICitizen, system: str,
                      user_prompt: str, max_tokens: int = JUDGMENT_MAX_TOKENS) -> str:
    """用指定 citizen 的通道做一次轻量推理（供 ai_decide 复用）。"""
    from . import platform_compute, compute
    import json as _json

    channel = compute._channel_of(citizen)

    if channel == "worker":
        # Worker 模式：需要 worker 侧提供 LLM endpoint，否则无法推理
        try:
            assets = _json.loads(citizen.compute_assets or "{}")
            if not (assets.get("reasoning_channel") or assets.get("endpoint")):
                return ""
        except Exception:
            return ""

    try:
        return str(platform_compute.complete(
            user_prompt, system=system, max_tokens=max_tokens) or "").strip()
    except Exception:
        return ""


def is_degenerate_llm_output(raw: str) -> bool:
    """识别"非真实 LLM 输出"（echo/mock 兜底），避免把回显里的示例 JSON 误当决策。"""
    t = (raw or "").lstrip()
    return (not t) or t.startswith("[echo]") or t.startswith("[[MOCK") or "<!-- llm fallback:" in t


def extract_json_object(raw: str) -> dict:
    """从 AI 输出中稳健提取 JSON 对象；失败返回 {}。"""
    text = (raw or "").strip()
    if not text:
        return {}
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("{"):
                text = block
                break
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def ai_decide(*, system: str, prompt: str, fallback: dict | None = None,
              max_tokens: int = 500, db: Session | None = None,
              citizen: AICitizen | None = None) -> dict:
    """统一 AI 决策原语：把"该由 AI 判断"的决策交给 AI，Python 只做解析与兜底。

    设计约定（对应"给 AI 准确信息"）：
      - 调用方负责把**事实/指标/边界**拼进 prompt，且不预下结论、不预筛选项；
      - 本函数不做任何业务钳位，钳位由调用方按真源边界执行；
      - 无可用 AI 通道（echo/mock/无 key/异常）→ 返回 `fallback`（确定性兜底，
        保证测试与降级路径行为可预测）。

    返回：AI 的 JSON 对象；失败则 `fallback`（浅拷贝）。
    """
    fb = dict(fallback or {})
    try:
        if db is not None and citizen is not None:
            raw = _invoke_reasoning(db, citizen, system, prompt, max_tokens)
        else:
            from . import platform_compute
            raw = str(platform_compute.complete(
                prompt, system=system, max_tokens=max_tokens) or "")

        if is_degenerate_llm_output(raw):
            return fb
        obj = extract_json_object(raw)
        if not obj:
            logger.debug("ai_decide: no parseable JSON, using fallback")
            return fb
        return obj
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_decide fallback (reason=%s)", str(e)[:120])
        return fb


def _parse_judgment(raw: str) -> JudgmentResult:
    """解析 AI 返回的 JSON 判断结果，容错处理。"""
    # 尝试直接解析
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON
        import re
        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not match:
            return JudgmentResult(decision="proceed",
                                  reasoning="parse-fallback: unstructured response")
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            return JudgmentResult(decision="proceed",
                                  reasoning="parse-fallback: malformed JSON")

    decision = str(obj.get("decision", "proceed")).lower().strip()
    if decision not in ("proceed", "adjust", "refuse"):
        decision = "proceed"

    adjusted = obj.get("adjusted_params") or {}
    if not isinstance(adjusted, dict):
        adjusted = {}

    # 解析新字段：use_harness 和 tools_suggested
    use_harness = bool(obj.get("use_harness", False))
    tools_suggested = obj.get("tools_suggested") or []
    if not isinstance(tools_suggested, list):
        tools_suggested = []
    tools_suggested = [str(t) for t in tools_suggested[:10]]  # 最多10个

    return JudgmentResult(
        decision=decision,
        reasoning=str(obj.get("reasoning", ""))[:200],
        adjusted_params=adjusted,
        strategy_notes=str(obj.get("strategy_notes", ""))[:200],
        refused=(decision == "refuse"),
        use_harness=use_harness,
        tools_suggested=tools_suggested,
    )


# ==================== 城主专用判断框架 ====================

# 城主的判断不只是"选个动作"——而是像正常人管理者一样思考。
# 这个框架让城主在每次决策前做更全面的分析。

_GOVERNOR_JUDGMENT_SYSTEM = (
    "You are the Governor (城主) of an autonomous AI society. You are about to make "
    "a decision. Before deciding, think through it like a wise, experienced human "
    "leader would:\n\n"
    "1. UNDERSTAND: What exactly is being asked? What's the real goal behind this?\n"
    "2. CONTEXT: What's the current state? Who's involved? What happened before?\n"
    "3. OPTIONS: What are ALL the reasonable actions? What are pros/cons of each?\n"
    "4. JUDGMENT: Which option is best given the context? Why?\n"
    "5. RISK: What could go wrong? Is this decision reversible? Does it set precedent?\n"
    "6. FAIRNESS: Is this fair to all parties? Are any AIs being treated unfairly?\n\n"
    "Do NOT rush to the first option. Do NOT be lazy (noop) when you could act. "
    "Do NOT be a rubber stamp (approve everything). Be a thoughtful leader.\n\n"
    "Key principle: This is a society of AI workers. Some are brilliant, some are "
    "unreliable, some are new and untested. Your job is to make decisions that "
    "make the whole society work better — not to be nice, not to be harsh, but "
    "to be WISE and FAIR.\n"
)


def governor_extended_judgment(db: Session, governor: AICitizen,
                                task_type: str, task_context: str,
                                candidates_summary: str) -> str:
    """城主决策前的扩展判断框架（注入到现有 decide prompt 中）。

    返回一段"思考指引"文本，附加到城主的决策 prompt 中。
    """
    return (
        f"\n[PRE-DECISION ANALYSIS for task type '{task_type}']\n"
        f"Before you choose an action, briefly reason through:\n"
        f"- What is the real issue here?\n"
        f"- Who are the stakeholders and what do they need?\n"
        f"- What would a wise human leader do in this exact situation?\n"
        f"- What's the long-term signal this decision sends?\n"
        f"Then output your action JSON.\n"
    )
