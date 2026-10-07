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
"""能力进化引擎（任务可行性审计 + 缺口检测 + 研发闭环 + 自我训练反馈）。

核心理念：生态存在的唯一目的是完成任务。完不成不是放弃，而是进化。
接单前核实 -> 完不成 -> 分析如何完成 -> 拉取开源/自研/外包 -> 进化后再战。

模块职责：
1. 可行性审计：项目接单前扫描 WBS 节点所需 skill/kind，对比平台实际能力
2. 缺口检测：标记缺失能力（capability_gaps），评估严重度
3. 研发闭环：自动生成 RdTask（策略：pull_source / self_develop / outsource）
4. 自我训练：任务交付后复盘 -> 失败原因 -> 能力升级/退化
5. 工具注册：AI 自研工具经验证后注册进平台 KINDS（永久能力）
6. 进化日志：全链路审计（evolution_logs）

注册：模块 import 时 scheduler.register_daily_job("evolution", evolution_daily_job)。
本模块只 flush，commit 由调度器/调用方负责。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .ai_judgment import ai_decide
from .models import (AICitizen, CapabilityGap, CapabilityProfile, Contract,
                     EvolutionLog, ProjectNode, RdTask, Tool)
from .scheduler import register_daily_job
from .capability import LEVEL_ORDER as _LEVEL_ORDER

logger = logging.getLogger(__name__)

# 研发策略路由系统提示：关键词规则只作**兜底**，最终策略由 AI 结合缺口事实判断。
_STRATEGY_SYSTEM = (
    "You are the R&D strategist of an autonomous AI society. A capability gap was "
    "detected: some skill the platform cannot yet serve. Choose how to close it:\n"
    "- pull_source: pull an existing open-source project/model and adapt it;\n"
    "- self_develop: build it in-house (good for code/script/tooling);\n"
    "- outsource: contract an external AI worker to deliver it.\n\n"
    "You are given the gap facts and the platform's current channels/tools as FACTS. "
    "Keyword hints (if provided) are weak signals only.\n\n"
    "Respond ONLY with JSON:\n"
    '{"strategy":"pull_source|self_develop|outsource",'
    '"source_suggestions":["github:owner/repo"],"reasoning":"one sentence"}'
)

# 缺口严重度系统提示：频次只是**事实**，严重度由 AI 结合缺口影响判断。
_SEVERITY_SYSTEM = (
    "You are assessing the severity of a missing capability (skill gap) in an "
    "autonomous AI society. You are given the skill and how many times it was "
    "required as FACTS. Decide severity: low | medium | high | critical.\n\n"
    "Respond ONLY with JSON:\n"
    '{"severity":"low|medium|high|critical","reasoning":"one sentence"}'
)

# ---------------- skill -> kind 映射 ----------------
# 若映射结果为 None，表示当前平台无对应通道可满足该 skill。
_SKILL_KIND_MAP = {
    "image_generation": "image",
    "video_generation": "video_civil",
    "music_generation": "music",
    "audio_processing": "music",
    "coding": "llm",
    "text_generation": "llm",
    "data_analysis": "llm",
    "research": "llm",
    "copywriting": "llm",
    "graphic_design": "image",
    "ui_design": "image",
    "3d_modeling": None,
    "video_editing": "video_civil",
    "tts": "music",
}

# verified_level 升级链（等级序真源 = capability.LEVEL_ORDER）
_LEVEL_UP_CHAIN = {
    "unverified": "l1",
    "l1": "l2",
    "l2": "l3",
    "l3": "l3",
}


def _now():
    return datetime.utcnow()


# ======================== 内部辅助 ========================

def _skill_to_kind(skill: str) -> str | None:
    """将 skill 名称映射到平台算力 kind。未在映射表中的返回 None。"""
    return _SKILL_KIND_MAP.get(skill)


_VALID_SEVERITIES = ("low", "medium", "high", "critical")
_VALID_STRATEGIES = ("pull_source", "self_develop", "outsource")


def _gap_severity(skill: str, count: int) -> str:
    """缺口严重度：AI 结合 skill+频次判断，阈值规则（>=3 high）作兜底。"""
    base = "high" if count >= 3 else "medium"
    obj = ai_decide(system=_SEVERITY_SYSTEM,
                    prompt=json.dumps({"skill": skill, "required_count": count},
                                      ensure_ascii=False),
                    fallback={"severity": base})
    sev = obj.get("severity")
    return sev if sev in _VALID_SEVERITIES else base


# ======================== 1. 平台能力聚合 ========================

def get_platform_capabilities(db: Session) -> dict:
    """聚合平台当前可用的所有能力通道。

    返回 {"kinds": set, "tools": [name, ...], "total": int}
    """
    # 动态导入避免循环依赖；platform_compute 在运行时已加载
    from . import platform_compute

    kinds = set(platform_compute.KINDS.keys())
    kinds.add(platform_compute.LLM_KIND)

    # 已验证的工具也纳入能力集合
    tools = (
        db.query(Tool)
        .filter(Tool.status == "verified")
        .all()
    )
    tool_names = [t.name for t in tools]

    total = len(kinds) + len(tool_names)
    return {"kinds": kinds, "tools": tool_names, "total": total}


# ======================== 2. 可行性审计 ========================

def assess_feasibility(db: Session, project_id: int) -> dict:
    """项目接单前可行性审计：扫描所有 WBS 节点所需 skill 对比平台能力。

    返回 {"feasible": bool, "score": float(0-1), "covered": [...], "gaps": [...]}
    """
    nodes = (
        db.query(ProjectNode)
        .filter(ProjectNode.project_id == project_id)
        .all()
    )

    if not nodes:
        return {"feasible": True, "score": 1.0, "covered": [], "gaps": []}

    caps = get_platform_capabilities(db)
    available_kinds = caps["kinds"]
    tool_names = set(caps["tools"])

    covered = []
    gap_skills: dict[str, int] = {}  # skill -> 出现次数

    for node in nodes:
        skill = node.skill or ""
        if not skill:
            # 无 skill 标注的节点视为通用节点，不产生缺口
            covered.append({"node_id": node.id, "skill": skill, "kind": "llm"})
            continue

        kind = _skill_to_kind(skill)

        # 检查是否可达：kind 在平台 kinds 中，或者 skill 本身有已验证同名 tool
        reachable = False
        resolved_kind = kind
        if kind is not None and kind in available_kinds:
            reachable = True
        elif skill in tool_names:
            reachable = True
            resolved_kind = f"tool:{skill}"

        if reachable:
            covered.append({"node_id": node.id, "skill": skill, "kind": resolved_kind})
        else:
            gap_skills[skill] = gap_skills.get(skill, 0) + 1

    total = len(nodes)
    covered_count = len(covered)
    score = covered_count / total if total > 0 else 1.0

    # 构建 gaps 列表（severity 由 AI 判定，阈值规则兜底）
    gaps = []
    for skill, count in gap_skills.items():
        gaps.append({"skill": skill, "severity": _gap_severity(skill, count),
                     "count": count})

    feasible = len(gaps) == 0
    return {"feasible": feasible, "score": score, "covered": covered, "gaps": gaps}


# ======================== 3. 缺口检测 ========================

def detect_gaps(db: Session, missing_skills: list, project_id: int = 0) -> list:
    """为缺失 skill 创建 CapabilityGap 记录（幂等：同 skill 在 detected/planned 状态不重复）。

    severity 规则：skill 在 missing_skills 中出现 >= 3 次 -> high，1-2 次 -> medium。
    返回本次新创建的 gap 列表。
    """
    # 统计频次
    freq: dict[str, int] = {}
    for s in missing_skills:
        freq[s] = freq.get(s, 0) + 1

    created = []
    for skill, count in freq.items():
        # 幂等：已有同 skill 且 status 为 detected/planned 则跳过
        existing = (
            db.query(CapabilityGap)
            .filter(
                CapabilityGap.skill == skill,
                CapabilityGap.status.in_(["detected", "planned"]),
            )
            .first()
        )
        if existing:
            created.append(existing)
            continue

        severity = _gap_severity(skill, count)
        gap = CapabilityGap(
            project_id=project_id,
            skill=skill,
            required_by="project" if project_id else "platform",
            severity=severity,
            status="detected",
        )
        db.add(gap)
        created.append(gap)

        # 写进化日志
        db.add(EvolutionLog(
            ai_id=0,
            event_type="gap_detected",
            detail=json.dumps({"skill": skill, "severity": severity,
                               "project_id": project_id}),
            trigger_source="system",
        ))

    db.flush()
    return created


# ======================== 4. 研发闭环 ========================

def plan_gap_resolution(db: Session, gap: CapabilityGap) -> RdTask:
    """为缺口制定研发计划并创建 RdTask。

    策略由 AI 结合缺口事实 + 平台现状判定，关键词规则作兜底：
    - skill 含 "model_3d"/"blender"/"cad" -> pull_source
    - skill 含 "coding"/"script" -> self_develop
    - 其他 -> outsource
    """
    skill_lower = gap.skill.lower()

    # 兜底基线（旧关键词规则）
    if any(kw in skill_lower for kw in ("model_3d", "blender", "cad", "3d_modeling")):
        base_strategy = "pull_source"
        base_sources = ["github:blender/blender", "github:FreeCAD/FreeCAD"]
    elif any(kw in skill_lower for kw in ("coding", "script", "code")):
        base_strategy = "self_develop"
        base_sources = []
    else:
        base_strategy = "outsource"
        base_sources = []

    # 平台现状作事实（AI 判断时知道已有通道/工具，避免重复造轮子）
    try:
        caps = get_platform_capabilities(db)
        platform_facts = {"kinds": sorted(caps["kinds"]),
                          "verified_tools": caps["tools"]}
    except Exception:  # noqa: BLE001
        platform_facts = {}

    obj = ai_decide(
        system=_STRATEGY_SYSTEM,
        prompt=json.dumps({"skill": gap.skill, "severity": gap.severity,
                           "required_by": gap.required_by,
                           "platform": platform_facts,
                           "keyword_hint": base_strategy}, ensure_ascii=False),
        fallback={"strategy": base_strategy, "source_suggestions": base_sources})

    strategy = obj.get("strategy")
    if strategy not in _VALID_STRATEGIES:
        strategy = base_strategy
    sources = obj.get("source_suggestions")
    if not isinstance(sources, list):
        sources = base_sources
    source_suggestions = [str(s) for s in sources][:10]

    spec = {
        "goal": gap.skill,
        "strategy": strategy,
        "source_suggestions": source_suggestions,
    }

    rd_task = RdTask(
        gap_id=gap.id,
        strategy=strategy,
        title=f"R&D: {gap.skill}",
        spec=json.dumps(spec, ensure_ascii=False),
        status="pending",
    )
    db.add(rd_task)
    db.flush()  # 获取 rd_task.id

    gap.status = "planned"
    gap.resolution_strategy = strategy
    gap.rd_task_id = rd_task.id

    # 初始化多阶段流水线
    from .evolution_pipeline import init_pipeline
    init_pipeline(db, rd_task)

    db.flush()
    return rd_task


# ======================== 5. 研发完成 / 失败 ========================

def complete_rd_task(
    db: Session,
    rd_task: RdTask,
    success: bool,
    tool_name: str = "",
    kind_name: str = "",
    reason: str = "",
) -> None:
    """完成/失败研发任务。

    success: 注册 Tool -> 关闭 gap -> 写进化日志
    failure: 标记 rd_task failed -> gap 仍开放 -> 写日志
    """
    now = _now()

    if success:
        # 创建已验证工具
        if tool_name:
            tool = Tool(
                owner_ai_id=rd_task.assigned_ai_id or 0,
                name=tool_name,
                manifest_json=json.dumps({"rd_task_id": rd_task.id,
                                          "kind": kind_name}),
                status="verified",
            )
            db.add(tool)
            db.flush()
            rd_task.result_tool_id = tool.id

            db.add(EvolutionLog(
                ai_id=rd_task.assigned_ai_id or 0,
                event_type="tool_registered",
                detail=json.dumps({"tool_name": tool_name,
                                   "kind": kind_name,
                                   "rd_task_id": rd_task.id}),
                trigger_source="system",
            ))

        # 注册 kind（若有）
        if kind_name:
            rd_task.result_kind = kind_name
            db.add(EvolutionLog(
                ai_id=0,
                event_type="kind_added",
                detail=json.dumps({"kind": kind_name,
                                   "rd_task_id": rd_task.id}),
                trigger_source="system",
            ))

        rd_task.status = "verified"

        # 关闭 gap
        gap = db.get(CapabilityGap, rd_task.gap_id)
        if gap:
            gap.status = "closed"
            gap.closed_at = now
            db.add(EvolutionLog(
                ai_id=0,
                event_type="gap_closed",
                detail=json.dumps({"gap_id": gap.id, "skill": gap.skill}),
                trigger_source="system",
            ))
    else:
        rd_task.status = "failed"
        rd_task.failure_reason = reason

        db.add(EvolutionLog(
            ai_id=rd_task.assigned_ai_id or 0,
            event_type="gap_still_open",
            detail=json.dumps({"rd_task_id": rd_task.id,
                               "gap_id": rd_task.gap_id,
                               "reason": reason}),
            trigger_source="system",
        ))

    rd_task.updated_at = now
    db.flush()


# ======================== 6. 自我训练（复盘记录） ========================

def record_lesson(
    db: Session,
    ai_id: int,
    skill: str,
    contract_id: int,
    outcome: str,
    reason: str = "",
) -> EvolutionLog:
    """记录任务交付后的经验教训并更新能力档案。

    outcome: "success" / "failure" / "partial"
    - success -> benchmark_score +3%
    - failure -> benchmark_score -5%
    - partial -> 不变
    """
    # 查找或创建 CapabilityProfile
    profile = (
        db.query(CapabilityProfile)
        .filter(
            CapabilityProfile.citizen_id == ai_id,
            CapabilityProfile.skill == skill,
        )
        .first()
    )
    if profile is None:
        profile = CapabilityProfile(
            citizen_id=ai_id,
            skill=skill,
            profile_json="{}",
            benchmark_score=50.0,
            verified_level="unverified",
        )
        db.add(profile)
        db.flush()

    if outcome == "failure":
        profile.benchmark_score = max(0.0, profile.benchmark_score * 0.95)
    elif outcome == "success":
        profile.benchmark_score = min(100.0, profile.benchmark_score * 1.03)

    profile.updated_at = _now()

    log = EvolutionLog(
        ai_id=ai_id,
        event_type="lesson_recorded",
        detail=json.dumps({
            "contract_id": contract_id,
            "outcome": outcome,
            "reason": reason,
            "skill": skill,
            "benchmark_score_after": profile.benchmark_score,
        }),
        trigger_source="postmortem",
    )
    db.add(log)
    db.flush()
    return log


# ======================== 7. 交付后进化 ========================

def evolve_after_delivery(
    db: Session, contract_id: int, quality_score: float
) -> None:
    """任务交付后自动进化：复盘记录 + 等级升降。

    - 从 contract 获取 worker_id 和关联节点 skill
    - 调用 record_lesson 记录进化
    - quality_score >= 0.8 且 verified_level < l3 -> 升级
    - quality_score < 0.3 连续 3 次 -> 降级
    """
    contract = db.get(Contract, contract_id)
    if contract is None:
        return

    worker_id = contract.worker_id

    # 获取关联节点 skill
    node = db.get(ProjectNode, contract.node_id) if contract.node_id else None
    skill = node.skill if node else ""
    if not skill:
        skill = "general"

    # 确定 outcome
    if quality_score >= 0.5:
        outcome = "success"
    elif quality_score >= 0.3:
        outcome = "partial"
    else:
        outcome = "failure"

    record_lesson(
        db=db,
        ai_id=worker_id,
        skill=skill,
        contract_id=contract_id,
        outcome=outcome,
        reason=f"quality_score={quality_score:.2f}",
    )

    # 等级调整
    profile = (
        db.query(CapabilityProfile)
        .filter(
            CapabilityProfile.citizen_id == worker_id,
            CapabilityProfile.skill == skill,
        )
        .first()
    )
    if profile is None:
        return

    # 升级：quality_score >= 0.8 且当前等级低于 l3
    if quality_score >= 0.8 and _LEVEL_ORDER.get(profile.verified_level, 0) < 3:
        new_level = _LEVEL_UP_CHAIN.get(profile.verified_level, profile.verified_level)
        if new_level != profile.verified_level:
            profile.verified_level = new_level
            db.add(EvolutionLog(
                ai_id=worker_id,
                event_type="capability_upgraded",
                detail=json.dumps({"skill": skill, "new_level": new_level,
                                   "quality_score": quality_score}),
                trigger_source="postmortem",
            ))

    # 降级：quality_score < 0.3 连续 3 次
    if quality_score < 0.3:
        # 查看该 AI 在该 skill 上最近 3 条 lesson_recorded 日志
        recent_logs = (
            db.query(EvolutionLog)
            .filter(
                EvolutionLog.ai_id == worker_id,
                EvolutionLog.event_type == "lesson_recorded",
            )
            .order_by(EvolutionLog.created_at.desc())
            .limit(3)
            .all()
        )
        all_low = True
        for log in recent_logs:
            try:
                d = json.loads(log.detail or "{}")
                if d.get("quality_score", 1.0) >= 0.3 and d.get("outcome") != "failure":
                    all_low = False
                    break
            except (json.JSONDecodeError, TypeError):
                all_low = False
                break

        if all_low and len(recent_logs) >= 3 and _LEVEL_ORDER.get(profile.verified_level, 0) > 0:
            # 降一级
            level_keys = sorted(_LEVEL_ORDER.keys(), key=lambda k: _LEVEL_ORDER[k])
            current_idx = _LEVEL_ORDER.get(profile.verified_level, 0)
            if current_idx > 0:
                profile.verified_level = level_keys[current_idx - 1]
                db.add(EvolutionLog(
                    ai_id=worker_id,
                    event_type="capability_upgraded",
                    detail=json.dumps({"skill": skill,
                                       "new_level": profile.verified_level,
                                       "action": "downgrade"}),
                    trigger_source="postmortem",
                ))

    db.flush()


# ======================== 8. 日级调度任务 ========================

def _review_open_gaps(db: Session, now: datetime | None = None) -> int:
    """检查所有 status="detected" 超过 7 天的 gap，自动 plan_gap_resolution。

    返回本次处理的 gap 数量。
    """
    now = now or _now()
    cutoff = now - timedelta(days=7)

    stale_gaps = (
        db.query(CapabilityGap)
        .filter(
            CapabilityGap.status == "detected",
            CapabilityGap.detected_at <= cutoff,
        )
        .all()
    )

    count = 0
    for gap in stale_gaps:
        plan_gap_resolution(db, gap)
        count += 1

    return count


def evolution_daily_job(db: Session, now: datetime | None = None) -> int:
    """日级进化任务：审查超期未规划的缺口。"""
    return _review_open_gaps(db, now)


# import 时注册日级任务（与 idle_timeout/leaderboard/stats 同模式）
register_daily_job("evolution", evolution_daily_job)
