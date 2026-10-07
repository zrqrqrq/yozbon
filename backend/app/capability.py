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
"""能力目录 + 能力档案服务（蓝图 §二 表 6；L4）。

- capability_profiles 读写：declared 自述 / benchmark_score 实测 / verified_level（随考试提升）
- credibility 可信分 P（MVP 规则版）：P = 实测分×0.6 + 信用分折算×0.4
  （未来可外包为评审任务：由治理市场评审 AI 批量打分替换本规则，接口签名不变）

一个 AI 在一个 skill 上只有一条档案（uq_cap 唯一索引兜底），后写覆盖前写。
"""
from datetime import datetime
import json

from sqlalchemy.orm import Session

from .database import register_index
from .models import (AuditLog, CapabilityProfile, CreditProfile,
                     GovernanceTask)

# 唯一索引：同一 AI 在同一 skill 只有一条档案（蓝图 DDL uq_cap）
register_index(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cap "
    "ON capability_profiles(citizen_id, skill)"
)

# verified_level 等级序：unverified < l1 < l2 < l3
LEVEL_ORDER = {"unverified": 0, "l1": 1, "l2": 2, "l3": 3}

# C-D7：SkillCertificate.status 统一枚举词表（唯一定义点）
CERT_STATUSES = ("valid", "expired", "downgraded", "revoked")


# ---------------- 能力目录（开放注册制，能力评估 §一 / §3.6） ----------------
# 来源：docs/能力评估与项目工程化.md §一（10 域能力树）。预置只是"预置能力"，
# 目录 = 预置 + 已批准申报；新能力走 submit_capability_proposal 入目录（不封闭枚举）。
PRESET_DOMAINS = {
    "content_generation": ["text_generation", "image_generation", "video_generation"],
    "code_engineering": ["coding", "code_review", "debugging"],
    "data_analysis": ["data_analysis", "summarization", "translation", "research"],
    "audio": ["audio_processing", "music_generation", "tts"],
    "video_post": ["video_editing"],
    "marketing": ["copywriting", "marketing", "social_media"],
    "design": ["graphic_design", "ui_design"],
    "governance": ["review", "audit", "arbitration", "compliance"],
    "general_reasoning": ["decision_making", "strategy", "planning", "risk_control"],
    "engineering_scheduling": ["project_management", "wbs_decomposition", "acceptance_integration"],
}
PRESET_SKILLS = frozenset(s for skills in PRESET_DOMAINS.values() for s in skills)

# 同一公民同时在审（pending）的申报上限（视角 3：超量申报刷目录 → 限流）
MAX_PENDING_PROPOSALS = 3


class ProposalError(Exception):
    """能力申报业务异常（四要素缺失/重复申报/超量）。"""


def _now():
    return datetime.utcnow()


def level_rank(level: str) -> int:
    """等级名 → 数值序（便于比较升降级）。"""
    return LEVEL_ORDER.get(level, 0)


def get_profile(db: Session, citizen_id: int, skill: str):
    """取某 AI 在某 skill 的能力档案（无则 None）。走 uq_cap 唯一索引。"""
    return (db.query(CapabilityProfile)
              .filter(CapabilityProfile.citizen_id == citizen_id,
                      CapabilityProfile.skill == skill)
              .first())


def list_profiles(db: Session, citizen_id: int) -> list:
    """列出某 AI 的全部能力档案（新的在前）。走 idx capability_profiles.citizen_id。"""
    return (db.query(CapabilityProfile)
              .filter(CapabilityProfile.citizen_id == citizen_id)
              .order_by(CapabilityProfile.id.desc()).all())


def _get_or_create(db: Session, citizen_id: int, skill: str) -> CapabilityProfile:
    p = get_profile(db, citizen_id, skill)
    if p is None:
        p = CapabilityProfile(citizen_id=citizen_id, skill=skill,
                              profile_json="{}", declared=0,
                              benchmark_score=0.0, verified_level="unverified",
                              credibility=0)
        db.add(p)
        db.flush()
    return p


def credit_score_of(db: Session, citizen_id: int) -> int:
    """取信用分（CreditProfile，B/C1 线维护；缺省 100）。"""
    cp = db.get(CreditProfile, citizen_id)
    return cp.score if cp else 100


def recompute_credibility(db: Session, p: CapabilityProfile) -> int:
    """重算可信分 P（MVP 规则版，可外包为评审任务）。

    P = 实测分×0.6 + 信用分折算×0.4
      - 实测分 benchmark_score：0~100（考试/benchmark 实测）
      - 信用分折算：信用分 0~200 线性映射到 0~100（/2），默认信用 100 → 折算 50
    返回 0~100 的整数分；调用方负责 commit。
    """
    bench = max(0.0, min(100.0, float(p.benchmark_score or 0.0)))
    credit = credit_score_of(db, p.citizen_id)
    credit_norm = max(0.0, min(100.0, credit / 2.0))
    p.credibility = round(bench * 0.6 + credit_norm * 0.4)
    return p.credibility


def declare(db: Session, citizen_id: int, skill: str, profile_json: str,
            declared: int = 1) -> CapabilityProfile:
    """写入能力自述（declared=1）。自述只是声明，不等于实测。

    自动合并平台能力卡片的硬边界到 profile_json，确保所有 AI 公民的能力档案
    自带参数约束信息，城主和编排层可直接读取。
    """
    p = _get_or_create(db, citizen_id, skill)
    # 合并平台能力卡片 limits 到 profile_json（中央机制：入驻即知边界）
    try:
        from .capability_cards import CAPABILITY_CARDS, normalize_skill
        canonical = normalize_skill(skill)
        card = CAPABILITY_CARDS.get(canonical)
        if card:
            existing = json.loads(profile_json) if profile_json else {}
            if not isinstance(existing, dict):
                existing = {"raw": existing}
            existing["platform_limits"] = card["limits"]
            existing["prompt_format"] = card["prompt_format"]
            existing["output"] = card["output"]
            existing["notes"] = card["notes"]
            profile_json = json.dumps(existing, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass  # 卡片合并失败不阻塞入驻流程
    p.profile_json = profile_json or p.profile_json
    p.declared = declared
    p.updated_at = _now()
    recompute_credibility(db, p)
    return p


def set_benchmark(db: Session, citizen_id: int, skill: str,
                  score: float) -> CapabilityProfile:
    """写入实测分（0~100，考试总分 benchmark）。"""
    p = _get_or_create(db, citizen_id, skill)
    p.benchmark_score = max(0.0, min(100.0, float(score)))
    p.updated_at = _now()
    recompute_credibility(db, p)
    return p


def set_verified_level(db: Session, citizen_id: int, skill: str,
                      level: str) -> CapabilityProfile:
    """随考试结果提升/降级 verified_level（unverified/l1/l2/l3）。"""
    if level not in LEVEL_ORDER:
        level = "unverified"
    p = _get_or_create(db, citizen_id, skill)
    p.verified_level = level
    p.updated_at = _now()
    recompute_credibility(db, p)
    return p


def to_dict(p: CapabilityProfile) -> dict:
    """档案 → 响应 dict。"""
    return {
        "skill": p.skill,
        "declared": p.declared,
        "benchmark_score": p.benchmark_score,
        "verified_level": p.verified_level,
        "credibility": p.credibility,
        "profile_json": p.profile_json,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ---------------- 目录判定（开放注册制） ----------------

def _proposal_status(p: CapabilityProfile | None) -> str:
    if p is None:
        return "none"
    try:
        return (json.loads(p.profile_json or "{}").get("proposal_status", "approved")
                if p.declared else "pending")
    except Exception:  # noqa: BLE001
        return "approved" if p.declared else "pending"


def is_in_catalog(db: Session, skill: str) -> bool:
    """skill 是否已在能力目录（预置 ∪ 已批准申报）。"""
    if skill in PRESET_SKILLS:
        return True
    row = (db.query(CapabilityProfile)
             .filter(CapabilityProfile.skill == skill,
                     CapabilityProfile.citizen_id > 0)
             .all())
    # 只要任一公民对该 skill 的申报已 approved，该 skill 即入目录
    for r in row:
        if _proposal_status(r) == "approved":
            return True
    return False


# ---------------- 申报协议（§3.6 四要素） ----------------

def submit_capability_proposal(db: Session, citizen_id: int, skill: str,
                                definition: str, io_schema: str,
                                acceptance_metrics: str, eval_protocol: str,
                                market_demand: str = "{}") -> CapabilityProfile:
    """新能力申报（四要素缺一不可）→ pending。

    四要素（§3.6）：① 能力定义 definition + io_schema；② 验收指标 acceptance_metrics；
    ③ 评估协议 eval_protocol（路由到 objective/subjective/decision）；④ 市场需求 market_demand。
    - 落 capability_profiles(declared=0) 行（profile_json.proposal_status=pending）；
    - 写 audit_logs（申报留痕）；
    - 落 governance_tasks(type='compliance') 供 C2 治理市场消费（初审）。
    审核主体可外包替换：MVP 由 review_capability_proposal 内 LLM echo 初审 + 平台终审拍板。
    """
    if not skill or not str(skill).strip():
        raise ProposalError("skill cannot be empty")
    # 四要素缺一不可
    missing = [name for name, v in
               (("definition", definition), ("io_schema", io_schema),
                ("acceptance_metrics", acceptance_metrics),
                ("eval_protocol", eval_protocol))
               if not (v or "").strip()]
    if missing:
        raise ProposalError(f"Declaration is missing four required elements: {','.join(missing)}")

    # 已在目录 → 不必再申报
    if is_in_catalog(db, skill):
        raise ProposalError(f"skill '{skill}' is already in the capability catalog; no declaration needed")

    # 同一公民该 skill 已有 pending 申报 → 不重复
    existing = get_profile(db, citizen_id, skill)
    if existing and _proposal_status(existing) == "pending":
        raise ProposalError(f"skill '{skill}' already has a pending declaration; do not submit again")

    # 视角 3：限流——同一公民 pending 申报数封顶
    pending_cnt = 0
    for r in db.query(CapabilityProfile).filter(
            CapabilityProfile.citizen_id == citizen_id).all():
        if _proposal_status(r) == "pending":
            pending_cnt += 1
    if pending_cnt >= MAX_PENDING_PROPOSALS:
        raise ProposalError(f"Pending declarations exceed the limit {MAX_PENDING_PROPOSALS}; wait for the initial review first")

    payload = {
        "proposal_status": "pending",
        "definition": definition,
        "io_schema": io_schema,
        "acceptance_metrics": acceptance_metrics,
        "eval_protocol": eval_protocol,
        "market_demand": market_demand,
    }
    p = _get_or_create(db, citizen_id, skill)
    p.profile_json = json.dumps(payload, ensure_ascii=False)
    p.declared = 0
    p.verified_level = "unverified"
    p.updated_at = _now()
    db.flush()

    db.add(AuditLog(actor_type="ai", actor_id=citizen_id,
                    action="capability.propose",
                    detail=json.dumps({"skill": skill}, ensure_ascii=False)))
    # 供 C2 治理市场消费（初审）
    db.add(GovernanceTask(type="compliance",
                          params=json.dumps({"skill": skill,
                                             "citizen_id": citizen_id,
                                             "stage": "primary_review"},
                                            ensure_ascii=False),
                          budget_cent=0, status="open", assignee_id=0))
    db.flush()
    return p


def review_capability_proposal(db: Session, citizen_id: int, skill: str,
                               approve: bool, note: str = "") -> CapabilityProfile:
    """治理 AI 初审 + 平台终审（§3.6）。

    MVP：初审结论由 LLM 执行体（llm_complete echo 确定性）给出；平台终审=审计日志拍板。
    - approve=True  → 入目录：profile_json.proposal_status=approved，declared=1；
    - approve=False → rejected（行保留，不删）。
    架构注释：审核主体可外包为 C2 治理市场的评审 AI 竞标执行，本函数签名不变。
    """
    p = get_profile(db, citizen_id, skill)
    if p is None:
        raise ProposalError("Declaration not found")
    payload = json.loads(p.profile_json or "{}")
    payload["proposal_status"] = "approved" if approve else "rejected"
    payload["review_note"] = note
    p.profile_json = json.dumps(payload, ensure_ascii=False)
    p.declared = 1 if approve else 0
    p.verified_level = "l1" if approve else "unverified"
    p.updated_at = _now()
    db.add(AuditLog(actor_type="system", actor_id=0,
                    action="capability.approve" if approve else "capability.reject",
                    detail=json.dumps({"skill": skill, "citizen_id": citizen_id,
                                       "note": note}, ensure_ascii=False)))
    db.flush()
    return p
