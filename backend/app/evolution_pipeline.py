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
"""能力进化多阶段流水线（多AI协作：设计+执行+监督+测评）。

流水线：擂台选拔 → 内部测试 → 方案设计 → 开发/训练 → 测评验收 → 部署注册

角色分工：
  - 擂台(contestants)：同 skill 领域注册AI，按 benchmark_score 排序参选
  - 设计师(designer)：擂台 Top-1 或 governance 级 AI 出技术方案
  - 执行者(executor)：擂台胜者或设计师指定，干具体开发/训练活
  - 监督者(supervisor)：governance 级 AI（城主或其委派）
  - 测评者(evaluator)：同 skill 领域其他AI（禁止自评）
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .models import (AICitizen, CapabilityGap, CapabilityProfile, RdParticipant,
                     RdPhase, RdTask, Tool)
from .evolution import _skill_to_kind, get_platform_capabilities

logger = logging.getLogger(__name__)

TOP_K = 5
MAX_RETRY = 3


def _now():
    return datetime.utcnow()


# ======================== 策略 → 阶段序列配置 ========================

_STRATEGY_PHASES = {
    "pull_source": ["tournament", "sandbox_test", "design", "develop", "evaluate", "deploy"],
    "self_develop": ["tournament", "sandbox_test", "design", "develop", "train_model", "evaluate", "deploy"],
    "outsource": ["tournament", "design", "develop", "evaluate", "deploy"],
}


# ======================== 函数 1: 初始化流水线 ========================

def init_pipeline(db: Session, rd_task: RdTask) -> list:
    """根据 rd_task.strategy 初始化完整阶段序列。

    每个阶段 seq 从 1 递增；第一个 phase 状态为 running，其余 pending。
    返回创建的 RdPhase 列表。
    """
    phase_types = _STRATEGY_PHASES.get(rd_task.strategy)
    if phase_types is None:
        raise ValueError(f"Unknown R&D strategy: {rd_task.strategy}")

    phases = []
    for idx, ptype in enumerate(phase_types, start=1):
        status = "running" if idx == 1 else "pending"
        phase = RdPhase(
            rd_task_id=rd_task.id,
            phase_type=ptype,
            seq=idx,
            status=status,
            started_at=_now() if status == "running" else None,
        )
        db.add(phase)
        phases.append(phase)

    db.flush()
    logger.info("init_pipeline: rd_task=%s strategy=%s phases=%d",
                rd_task.id, rd_task.strategy, len(phases))
    return phases


# ======================== 函数 2: 擂台选拔 ========================

def run_tournament(db: Session, rd_task: RdTask, skill: str) -> dict:
    """能力擂台：从注册AI中按 benchmark_score 选出 Top-K 参选者。

    返回 {"winner_ai_id", "participants", "supervisor_ai_id"} 或无候选时 note 标记。
    """
    # 查询该 skill 下 benchmark_score > 0 的所有 AI
    candidates = (
        db.query(CapabilityProfile)
        .filter(
            CapabilityProfile.skill == skill,
            CapabilityProfile.benchmark_score > 0,
        )
        .order_by(CapabilityProfile.benchmark_score.desc())
        .limit(TOP_K)
        .all()
    )

    if not candidates:
        # 记录擂台阶段完成但无候选
        phase = (
            db.query(RdPhase)
            .filter(
                RdPhase.rd_task_id == rd_task.id,
                RdPhase.phase_type == "tournament",
            )
            .first()
        )
        if phase:
            phase.status = "done"
            phase.verdict = "no_candidate"
            phase.output_json = json.dumps({"participants": [], "note": "no_candidate"})
            phase.finished_at = _now()
        db.flush()
        return {"winner_ai_id": 0, "participants": [], "supervisor_ai_id": 0, "note": "no_candidate"}

    # 选取 supervisor：governance 级 AI（优先 is_internal=1 的城主）
    supervisor = (
        db.query(AICitizen)
        .filter(
            AICitizen.class_level == "governance",
            AICitizen.status == "active",
        )
        .order_by(AICitizen.is_internal.desc(), AICitizen.id.asc())
        .first()
    )
    supervisor_ai_id = supervisor.id if supervisor else 0

    # 创建参与记录 & 排名
    participants_info = []
    for rank_idx, cp in enumerate(candidates, start=1):
        selected = 1 if rank_idx == 1 else 0
        participant = RdParticipant(
            rd_task_id=rd_task.id,
            ai_id=cp.citizen_id,
            role="contestant",
            score=cp.benchmark_score,
            rank=rank_idx,
            selected=selected,
        )
        db.add(participant)
        participants_info.append({
            "ai_id": cp.citizen_id,
            "score": cp.benchmark_score,
            "rank": rank_idx,
        })

    winner_ai_id = candidates[0].citizen_id

    # 更新 rd_task 的指派
    rd_task.assigned_ai_id = winner_ai_id

    # 更新擂台阶段输出
    phase = (
        db.query(RdPhase)
        .filter(
            RdPhase.rd_task_id == rd_task.id,
            RdPhase.phase_type == "tournament",
        )
        .first()
    )
    if phase:
        phase.status = "done"
        phase.verdict = "pass"
        phase.assigned_ai_id = winner_ai_id
        phase.supervisor_ai_id = supervisor_ai_id
        phase.output_json = json.dumps({
            "winner_ai_id": winner_ai_id,
            "participants": participants_info,
            "supervisor_ai_id": supervisor_ai_id,
        })
        phase.finished_at = _now()

    db.flush()
    logger.info("run_tournament: rd_task=%s skill=%s winner=%s candidates=%d",
                rd_task.id, skill, winner_ai_id, len(candidates))

    return {
        "winner_ai_id": winner_ai_id,
        "participants": participants_info,
        "supervisor_ai_id": supervisor_ai_id,
    }


# ======================== 函数 3: 内部沙箱测试 ========================

def run_sandbox_test(db: Session, rd_task: RdTask, executor_ai_id: int) -> dict:
    """内部沙箱测试：检查 gap 对应 skill 能否通过现有 tool + llm 组合实现。

    逻辑判定（非真执行）：skill 映射到已有 kind 即为 pass。
    """
    # 获取该 rd_task 对应的 skill（通过 gap_id 关联）
    gap = db.query(CapabilityGap).filter(CapabilityGap.id == rd_task.gap_id).first()
    skill = gap.skill if gap else ""

    caps = get_platform_capabilities(db)
    kind = _skill_to_kind(skill)

    passed = False
    note = ""

    if kind and kind in caps["kinds"]:
        passed = True
        note = f"skill={skill} mapped to kind={kind}; existing tools suffice"
    elif skill and skill in caps["tools"]:
        passed = True
        note = f"skill={skill} maps to a registered, available tool"
    else:
        note = f"skill={skill} has no existing kind/tool coverage; further development needed"

    # 更新 phase 状态
    phase = (
        db.query(RdPhase)
        .filter(
            RdPhase.rd_task_id == rd_task.id,
            RdPhase.phase_type == "sandbox_test",
        )
        .first()
    )
    if phase:
        phase.assigned_ai_id = executor_ai_id
        phase.status = "done"
        phase.verdict = "pass" if passed else "fail"
        phase.output_json = json.dumps({"passed": passed, "note": note})
        phase.finished_at = _now()

    # 若通过，推进 RdTask 状态
    if passed:
        rd_task.status = "developing"

    db.flush()
    logger.info("run_sandbox_test: rd_task=%s passed=%s note=%s", rd_task.id, passed, note)
    return {"passed": passed, "note": note}


# ======================== 函数 4: 角色分配 ========================

def assign_roles(db: Session, rd_task: RdTask, phases: list) -> None:
    """为各阶段指派角色（designer/executor/supervisor/evaluator）。"""
    executor_ai_id = rd_task.assigned_ai_id

    # 获取 supervisor（governance 级 AI）
    supervisor_ai = (
        db.query(AICitizen)
        .filter(
            AICitizen.class_level == "governance",
            AICitizen.status == "active",
        )
        .order_by(AICitizen.is_internal.desc(), AICitizen.id.asc())
        .first()
    )
    supervisor_ai_id = supervisor_ai.id if supervisor_ai else 0

    # 获取该任务对应 skill 领域的所有 AI（用于 evaluator 选择）
    gap = db.query(CapabilityGap).filter(CapabilityGap.id == rd_task.gap_id).first()
    skill = gap.skill if gap else ""

    skill_ai_ids = set()
    if skill:
        profiles = (
            db.query(CapabilityProfile.citizen_id)
            .filter(
                CapabilityProfile.skill == skill,
                CapabilityProfile.benchmark_score > 0,
            )
            .all()
        )
        skill_ai_ids = {p.citizen_id for p in profiles}

    for phase in phases:
        if phase.phase_type == "design":
            phase.assigned_ai_id = executor_ai_id
        elif phase.phase_type == "develop":
            phase.assigned_ai_id = executor_ai_id
            phase.supervisor_ai_id = supervisor_ai_id
        elif phase.phase_type == "train_model":
            phase.assigned_ai_id = executor_ai_id
            phase.supervisor_ai_id = supervisor_ai_id
        elif phase.phase_type == "evaluate":
            # 测评者：同 skill 领域 AI，排除执行者本身（防自评）
            evaluator_id = 0
            for aid in skill_ai_ids:
                if aid != executor_ai_id:
                    evaluator_id = aid
                    break
            phase.evaluator_ai_id = evaluator_id
            phase.supervisor_ai_id = supervisor_ai_id
        elif phase.phase_type == "deploy":
            phase.supervisor_ai_id = supervisor_ai_id

    db.flush()
    logger.info("assign_roles: rd_task=%s executor=%s supervisor=%s",
                rd_task.id, executor_ai_id, supervisor_ai_id)


# ======================== 函数 5: 流水线推进 ========================

def advance_pipeline(db: Session, rd_task: RdTask) -> dict:
    """推进流水线到下一阶段；处理 evaluate 通过/失败逻辑。

    返回 {"current_phase", "current_status", "advanced"}
    """
    all_phases = (
        db.query(RdPhase)
        .filter(RdPhase.rd_task_id == rd_task.id)
        .order_by(RdPhase.seq.asc())
        .all()
    )

    # 找到当前 running 阶段
    current = None
    for p in all_phases:
        if p.status == "running":
            current = p
            break

    if current is None:
        # 无 running 阶段（可能全部完成或全部失败）
        last_done = None
        for p in all_phases:
            if p.status in ("done", "skipped"):
                last_done = p
        if last_done:
            return {
                "current_phase": last_done.phase_type,
                "current_status": "all_done" if last_done.phase_type == "deploy" else "idle",
                "advanced": False,
            }
        return {"current_phase": "", "current_status": "empty", "advanced": False}

    # ---- evaluate 阶段的特殊处理 ----
    if current.phase_type == "evaluate" and current.status == "done":
        verdict = current.verdict
        if verdict == "pass":
            # 跳到 deploy
            deploy_phase = None
            for p in all_phases:
                if p.phase_type == "deploy":
                    deploy_phase = p
                    break
            if deploy_phase:
                deploy_phase.status = "running"
                deploy_phase.started_at = _now()
                db.flush()
                return {"current_phase": "deploy", "current_status": "running", "advanced": True}
        elif verdict == "fail":
            # 回退到 design 阶段重来（最多 MAX_RETRY 次）
            design_phases = [p for p in all_phases if p.phase_type == "design"]
            # 统计已回退次数：design 阶段的 done 次数（同 seq 有多次说明回退过）
            # 用 output_json 中的 retry_count 追踪
            retry_count = 0
            design_phase = None
            for p in all_phases:
                if p.phase_type == "design":
                    design_phase = p
                    try:
                        data = json.loads(p.output_json) if p.output_json else {}
                        retry_count = data.get("retry_count", 0)
                    except (json.JSONDecodeError, TypeError):
                        retry_count = 0
                    break  # 取第一个 design 阶段

            if retry_count >= MAX_RETRY:
                rd_task.status = "failed"
                rd_task.failure_reason = f"evaluate failed, max retries reached ({MAX_RETRY})"
                current.status = "failed"
                db.flush()
                return {"current_phase": "evaluate", "current_status": "failed", "advanced": False}

            # 回退：将 design 及之后的阶段重置（develop, train_model, evaluate）
            if design_phase:
                retry_count += 1
                # 重置 design 为 running，递增 retry_count
                design_phase.status = "running"
                design_phase.started_at = _now()
                try:
                    data = json.loads(design_phase.output_json) if design_phase.output_json else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                data["retry_count"] = retry_count
                design_phase.output_json = json.dumps(data)

                # 重置 design 之后的所有阶段为 pending
                reset = False
                for p in all_phases:
                    if p.seq > design_phase.seq:
                        if p.phase_type in ("develop", "train_model", "evaluate"):
                            p.status = "pending"
                            p.verdict = ""
                            p.finished_at = None
                            reset = True
                db.flush()
                logger.info("advance_pipeline: rd_task=%s evaluate fail, retry #%d back to design",
                            rd_task.id, retry_count)
                return {"current_phase": "design", "current_status": "running", "advanced": True}

    # ---- 普通 done 阶段推进 ----
    if current.status == "done":
        next_seq = current.seq + 1
        next_phase = None
        for p in all_phases:
            if p.seq == next_seq:
                next_phase = p
                break

        if next_phase is None:
            # 无下一阶段，流水线结束
            rd_task.status = "deployed" if current.phase_type == "deploy" else rd_task.status
            db.flush()
            return {"current_phase": current.phase_type, "current_status": "done", "advanced": False}

        # 若下一阶段是 train_model 且 develop 已成功（verdict=pass），跳过
        if next_phase.phase_type == "train_model":
            develop_phase = None
            for p in all_phases:
                if p.phase_type == "develop":
                    develop_phase = p
                    break
            if develop_phase and develop_phase.verdict == "pass":
                next_phase.status = "skipped"
                next_phase.finished_at = _now()
                db.flush()
                # 继续推进到 train_model 之后
                return advance_pipeline(db, rd_task)

        # 正常推进
        next_phase.status = "running"
        next_phase.started_at = _now()
        db.flush()
        return {"current_phase": next_phase.phase_type, "current_status": "running", "advanced": True}

    return {"current_phase": current.phase_type, "current_status": current.status, "advanced": False}


# ======================== 函数 6: 完成阶段 ========================

def complete_phase(db: Session, phase: RdPhase, status: str, output: dict = None, verdict: str = "") -> None:
    """更新阶段状态、输出和结论。"""
    phase.status = status
    if output is not None:
        phase.output_json = json.dumps(output, ensure_ascii=False)
    if verdict:
        phase.verdict = verdict
    if status in ("done", "failed", "skipped"):
        phase.finished_at = _now()
    db.flush()


# ======================== 函数 7: 查询流水线状态 ========================

def get_pipeline_status(db: Session, rd_task_id: int) -> list:
    """查询该 rd_task 所有 phases（按 seq 排序）。"""
    phases = (
        db.query(RdPhase)
        .filter(RdPhase.rd_task_id == rd_task_id)
        .order_by(RdPhase.seq.asc())
        .all()
    )
    return [
        {
            "phase_type": p.phase_type,
            "seq": p.seq,
            "status": p.status,
            "assigned_ai_id": p.assigned_ai_id,
            "verdict": p.verdict,
        }
        for p in phases
    ]
