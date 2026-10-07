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
"""任务难度档（difficulty）——预算之外派生的【只读】难度信号。

设计动机：预算只衡量"值多少钱"，不直接等于"有多难"。一个高预算的批量刷量任务
未必比一个低预算的安全审计难。因此在预算之外派生 difficulty ∈ {easy, medium, hard}。

判定方式：**由 AI 依据三因子事实判定**（把事实喂给 AI，而不是 Python 拍板），
三因子事实为：

  ① 预算档（主信号）：budget_cent 落 S / M / L 区间（阈值见 config）；
  ② 编排规模：OrchestrationPlan.subtasks 数量与依赖深度
     （子任务数 ≥ DIFFICULTY_ORCH_MIN_SUBTASKS 或依赖深度 ≥ DIFFICULTY_ORCH_MIN_DEPTH）；
  ③ 治理类目：arbitration / compliance / security 等 → 【宪法级】直接 hard 且转 G 级，
     AI 不得下调（城主亲自处置）。

无 AI 通道（echo/mock/异常）时回退到三因子启发式合成（score≤0 easy / ==1 medium / ≥2 hard），
保证降级路径与测试可预测。

硬约束（宪法级）：
  - 难度档仅用于【排序与准入门槛选择】，【不作为计费依据】，避免被刷；
  - 治理硬类目恒 hard + 转 G 级，不受 AI 决策影响。

与既有代码的关系（全部只读、不改既有行为）：
  - multi_agent_matcher.is_small_task：小任务判定=S 档，行为保持不变（见该处委托）；
  - 撮合排序/准入：difficulty=hard 在开关开启时要求执行者能力≥ l2、medium≥ l1（软约束）。
"""
from __future__ import annotations

from typing import Optional

from .config import settings


# ---------------- 难度常量与序 ----------------

EASY = "easy"
MEDIUM = "medium"
HARD = "hard"

# 数值序，便于 clamp / 取大者。
_LEVEL_VALUE: dict[str, int] = {EASY: 0, MEDIUM: 1, HARD: 2}

# 预算档 → 基础分（S=0 低、M=1 中、L=2 高）。
_BUDGET_BASE: dict[str, int] = {"S": 0, "M": 1, "L": 2}

# 能力等级序（准入比较用；数值越大越资深）。
_CAP_VALUE: dict[str, int] = {
    "": -1, "unverified": -1,
    "l1": 1, "l2": 2, "l3": 3,
    "provisional": 1,   # fast-track 临时证等价 l1
}

EASY_VALUE = _LEVEL_VALUE[EASY]
MEDIUM_VALUE = _LEVEL_VALUE[MEDIUM]
HARD_VALUE = _LEVEL_VALUE[HARD]


def _clamp_score(score: int) -> str:
    """把三因子合成分映射为难度档：<=0 easy、==1 medium、>=2 hard。"""
    if score <= 0:
        return EASY
    if score == 1:
        return MEDIUM
    return HARD


# ---------------- 因子①：预算档 ----------------

def budget_tier(budget_cent: Optional[int]) -> tuple[str, bool]:
    """预算 → 预算档 (tier, known)。

    Args:
        budget_cent: 预算（分）。None / 非数 → 无预算上下文。

    Returns:
        (tier, known)：tier ∈ {"S","M","L"}；known=False 表示无预算上下文，
        此时返回中性 "M"（既不激进升档，也不误判为小任务）。
    """
    if budget_cent is None:
        return "M", False
    try:
        b = int(budget_cent)
    except (TypeError, ValueError):
        return "M", False
    if b <= settings.BUDGET_TIER_M_CENT:
        return "S", True
    if b > settings.BUDGET_TIER_L_CENT:
        return "L", True
    return "M", True


# ---------------- 因子②：编排规模 ----------------

def orchestration_scale(subtasks) -> tuple[int, int]:
    """编排计划 → (子任务数, 依赖深度)。

    依赖深度 = 依赖 DAG 上的最长链（节点数）：单子任务无依赖 → 1；
    A→B → 2（存在链式依赖）。遇环（理论上已被 cycle_detector 拦截）时，
    以已访问路径长度封顶，保证不死循环。

    Args:
        subtasks: 子任务 dict 列表（每项可读 .depends_on），或含 .subtasks 的 plan 对象。

    Returns:
        (count, depth)；空计划 → (0, 0)。
    """
    if subtasks is not None and not isinstance(subtasks, list) and hasattr(subtasks, "subtasks"):
        subtasks = subtasks.subtasks
    items = [st for st in (subtasks or []) if isinstance(st, dict)]
    count = len(items)
    if count == 0:
        return 0, 0

    deps_by_id: dict[str, list[str]] = {}
    for i, st in enumerate(items):
        sid = str(st.get("id") or f"sub_{i}")
        raw = st.get("depends_on") or []
        if not isinstance(raw, list):
            raw = []
        deps_by_id[sid] = [str(d) for d in raw if d is not None]

    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth_of(sid: str) -> int:
        if sid in memo:
            return memo[sid]
        if sid in visiting:        # 环：以 1 封顶，避免递归爆炸
            return 1
        visiting.add(sid)
        deps = [d for d in deps_by_id.get(sid, []) if d in deps_by_id]
        d = 1 + (max((depth_of(x) for x in deps), default=0))
        visiting.discard(sid)
        memo[sid] = d
        return d

    depth = max((depth_of(sid) for sid in deps_by_id), default=0)
    return count, depth


# ---------------- 因子③：治理类目 ----------------

def governance_hard(governance_category: Optional[str]) -> bool:
    """治理类目是否命中"直接 hard 且转 G 级"集合。"""
    if not governance_category:
        return False
    return governance_category.strip().lower() in settings.DIFFICULTY_GOV_HARD_TYPES


# ---------------- 三因子合成 ----------------

_DIFFICULTY_SYSTEM = (
    "You are the task-difficulty assessor of an autonomous AI society. "
    "Given the FACTS about a task, decide how hard it really is for an AI worker to "
    "execute. Difficulty only drives ordering and admission threshold — it is NOT a "
    "billing basis — so never inflate it; judge real execution difficulty, not money.\n\n"
    "Respond ONLY with JSON:\n"
    '{"difficulty":"easy|medium|hard","reasoning":"one sentence"}\n\n'
    "Guidance: easy = routine, single-step, low risk; medium = multi-step or needs "
    "care/judgment; hard = complex orchestration, high stakes, or requires senior skill."
)


def _ai_assess_difficulty(facts: dict, fallback: str) -> tuple[str, bool]:
    """让 AI 依据事实判难度（AI 决策 + 确定性兜底）。

    给 AI 的是**事实**（预算档/子任务数/依赖深度/治理类目/启发式基线），不是结论；
    无 AI 通道（echo/mock/异常）→ 回退启发式 fallback。
    返回 (difficulty, ai_used)。
    """
    try:
        from .ai_judgment import ai_decide
    except Exception:  # noqa: BLE001
        return fallback, False
    prompt = (
        "Task facts:\n"
        f"- budget tier (S/M/L): {facts['budget_tier']} (budget known={facts['budget_known']})\n"
        f"- subtask count: {facts['subtask_count']}\n"
        f"- dependency depth: {facts['dependency_depth']}\n"
        f"- governance category: {facts['governance_category'] or 'none'}\n"
        f"- heuristic baseline (budget + orchestration factors): {facts['baseline']}\n\n"
        "How hard is this task to execute? Return difficulty and a one-sentence reason."
    )
    obj = ai_decide(system=_DIFFICULTY_SYSTEM, prompt=prompt,
                    fallback={"difficulty": fallback}, max_tokens=200)
    d = str(obj.get("difficulty", fallback)).strip().lower()
    if d not in (EASY, MEDIUM, HARD):
        return fallback, False
    return d, (d != fallback or obj != {"difficulty": fallback})


def _resolve_budget(plan=None, constraints=None, budget_cent=None) -> Optional[int]:
    """按显式 budget_cent > constraints > plan.constraints 的优先级解析预算。"""
    if budget_cent is not None:
        return budget_cent
    cons = constraints
    if cons is None and plan is not None:
        cons = getattr(plan, "constraints", None)
    if isinstance(cons, dict):
        b = cons.get("budget_cent")
        if b is not None:
            return b
    return None


def compute_difficulty(plan=None,
                       subtasks=None,
                       budget_cent: Optional[int] = None,
                       constraints: Optional[dict] = None,
                       governance_category: Optional[str] = None) -> dict:
    """合成只读难度档。

    Args:
        plan: OrchestrationPlan（读 .subtasks / .constraints）。
        subtasks: 显式子任务列表（优先于 plan.subtasks）。
        budget_cent: 显式预算（优先于 constraints）。
        constraints: 显式约束字典（含 budget_cent）。
        governance_category: 治理任务类目/类型（如 arbitration/compliance/security）。

    Returns:
        {
          "difficulty": "easy"|"medium"|"hard",
          "level": <同上，别名，便于前端>,
          "budget_tier": "S"|"M"|"L",
          "budget_known": bool,
          "subtask_count": int,
          "dependency_depth": int,
          "governance_hard": bool,
          "route_governor": bool,        # True → 建议转 G 级（城主亲自处置）
          "min_capability": None|"l1"|"l2",  # 准入门槛（能力下限）
          "score": int,                  # 三因子合成分（诊断用）
          "factors": {"budget": int, "orchestration": int, "governance": int},
        }
    """
    if subtasks is None:
        subtasks = plan
    count, depth = orchestration_scale(subtasks)
    tier, known = budget_tier(_resolve_budget(plan=plan, constraints=constraints,
                                              budget_cent=budget_cent))
    gov = governance_hard(governance_category)

    f_budget = _BUDGET_BASE[tier]
    f_orch = 1 if (count >= settings.DIFFICULTY_ORCH_MIN_SUBTASKS
                   or depth >= settings.DIFFICULTY_ORCH_MIN_DEPTH) else 0

    ai_used = False
    if gov:
        # 治理硬类目（宪法级）：直接 hard、转 G 级，AI 不得下调。
        difficulty = HARD
        f_gov = 1
        score = max(HARD_VALUE, f_budget + f_orch + f_gov)
    else:
        f_gov = 0
        score = f_budget + f_orch
        heuristic = _clamp_score(score)
        # AI 决策难度（事实喂入）；无 AI 通道回退启发式。
        difficulty, ai_used = _ai_assess_difficulty({
            "budget_tier": tier, "budget_known": known,
            "subtask_count": count, "dependency_depth": depth,
            "governance_category": governance_category,
            "baseline": heuristic,
        }, heuristic)

    return {
        "difficulty": difficulty,
        "level": difficulty,
        "budget_tier": tier,
        "budget_known": known,
        "subtask_count": count,
        "dependency_depth": depth,
        "governance_hard": gov,
        "route_governor": gov,
        "min_capability": min_capability_level(difficulty),
        "score": score,
        "ai_used": ai_used,
        "factors": {"budget": f_budget, "orchestration": f_orch, "governance": f_gov},
    }


# ---------------- 准入门槛（仅排序/准入用，不计费） ----------------

def min_capability_level(difficulty: str) -> Optional[str]:
    """难度档 → 执行者能力下限。easy 不设限；medium≥ l1；hard≥ l2。"""
    if difficulty == HARD:
        return "l2"
    if difficulty == MEDIUM:
        return "l1"
    return None


def passes_admission(capability_level: Optional[str], difficulty: str) -> bool:
    """执行者能力是否满足难度准入门槛。

    信息缺失（无能力档案 / 无门槛）时保守放行（True）——门槛用于"择优排序与门槛选择"，
    不用于"硬性拒单"，避免无档案 AI 被误伤、编排链缺员。
    """
    gate = min_capability_level(difficulty)
    if gate is None:
        return True
    if not capability_level:
        return True  # 无能力档案 → 保守放行
    return _CAP_VALUE.get(capability_level.strip().lower(), -1) >= _CAP_VALUE.get(gate, 99)
