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
"""众筹利益分配谈判系统。

核心理念（类风投 Term Sheet 谈判）：
- 甲方（贡献者代表）：追求低版税、高一次性分成
- 乙方（基座模型方）：追求高版税、长期锁定收益
- 法律AI：基于历史成交数据起草初始条款
- 商务AI（调解人）：居间推进，提议折中方案
- 仲裁AI：僵局时裁决，参考市场公允价 + 双方贡献比例

谈判协议：
1. 法律AI 拟稿 → status="negotiating"
2. 双方逐轮出价/还价（每轮有 deadline）
3. 任一方 accept → status="agreed"
4. 超过 max_rounds 仍无协议 → status="arbitrating" → 仲裁AI 裁决
5. 仲裁结果为 final（resolution_method="arbitrated"）
6. 双方均超时未响应 → 使用市场参考价（resolution_method="default_forced"）

硬约束（系统级不可突破）：
- 每个条款有 floor/ceiling 硬边界
- 仲裁结果同样不能超出硬边界
- 贡献者分成不超过 50%，版税不超过 20%

本模块只 flush，commit 由调用方/调度器负责。
"""
import json
import logging
import statistics
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import (AICitizen, CapabilityProfile,
                     NegotiationClause, NegotiationRound, NegotiationSession,
                     TrainingCampaign)
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# ======================== 角色自动选拔（资格门槛 + AI 择优 + 城主兜底） ========================
#
# 设计原则：资格门槛是"硬边界"（模型注册时已由 CapabilityProfile 确定能力边界，
# benchmark/verified_level 是客观事实），不达标者压根不参赛——这部分由 Python 判定。
# 达标候选人里"谁最合适"是**判断**，交给 AI 择优（AI 决策 + benchmark 最高兜底）。
# 无人达标 → 城主兜底。

# 谈判所需角色 -> CapabilityProfile.skill 映射
NEGOTIATION_ROLE_SKILLS = {
    "legal": "legal",
    "mediator": "negotiation",
    "arbiter": "arbitration",
}

# 资格门槛：benchmark_score >= 此值 且 verified_level 非 unverified 才可入选
ARENA_MIN_SCORE = 60.0
ARENA_MIN_LEVEL = "l1"  # 至少经过一次实测验证

_ROLE_SYSTEM = (
    "You are the staffing officer of an autonomous AI society. Pick the SINGLE best AI "
    "to fill a negotiation role, judging from the candidates' verified skill level, "
    "benchmark score and occupation. Every listed candidate already passed the "
    "qualification gate, so choose the one most SUITED to the role — not necessarily "
    "the highest raw score.\n\n"
    'Return ONLY JSON: {"ai_id": <int>, "reasoning": "one sentence"}'
)


def _qualified_candidates(db: Session, skill: str,
                          exclude_ids: set | None = None) -> list[dict]:
    """资格过滤：返回达标的候选人事实清单（按 benchmark 降序）。

    资格门槛是硬边界（客观事实），由 Python 判定：benchmark_score >= ARENA_MIN_SCORE
    且 verified_level 非 unverified。返回的每一项是喂给 AI 的**事实**，不含结论。
    """
    from .capability import level_rank
    min_rank = level_rank(ARENA_MIN_LEVEL)
    q = (
        db.query(CapabilityProfile, AICitizen)
        .join(AICitizen, AICitizen.id == CapabilityProfile.citizen_id)
        .filter(
            CapabilityProfile.skill == skill,
            CapabilityProfile.benchmark_score >= ARENA_MIN_SCORE,
            AICitizen.status == "active",
        )
    )
    if exclude_ids:
        q = q.filter(~CapabilityProfile.citizen_id.in_(exclude_ids))
    out: list[dict] = []
    for prof, cit in q.order_by(CapabilityProfile.benchmark_score.desc()).all():
        # 二次校验 verified_level（注册自述 unverified 的不算数）
        if level_rank(prof.verified_level) < min_rank:
            continue
        out.append({
            "ai_id": cit.id,
            "name": cit.name,
            "benchmark_score": float(prof.benchmark_score or 0),
            "verified_level": prof.verified_level or "unverified",
            "occupation": cit.occupation or "",
        })
    return out


def _choose_role_ai(db: Session, skill: str,
                    exclude_ids: set | None = None) -> int:
    """在合格候选人中由 AI 择优（AI 决策 + benchmark 最高兜底）。

    资格门槛已在 `_qualified_candidates` 硬过滤；此处只做"谁最合适"的判断。
    无候选人 → 0（交由城主兜底）；仅一名候选人 → 直接返回（无可择优）。
    """
    cands = _qualified_candidates(db, skill, exclude_ids)
    if not cands:
        return 0
    if len(cands) == 1:
        return cands[0]["ai_id"]

    fallback_id = cands[0]["ai_id"]  # 已按 benchmark 降序 → 兜底取最高分
    from .ai_judgment import ai_decide
    prompt = (
        f"Negotiation role to fill (skill domain): {skill}\n"
        "Qualified candidates (all passed the gate):\n"
        + json.dumps(cands, ensure_ascii=False)
        + "\n\nPick the single best candidate for this role."
    )
    obj = ai_decide(system=_ROLE_SYSTEM, prompt=prompt,
                    fallback={"ai_id": fallback_id}, db=db)
    try:
        picked = int(obj.get("ai_id"))
    except (TypeError, ValueError):
        return fallback_id
    valid = {c["ai_id"] for c in cands}
    return picked if picked in valid else fallback_id


def _get_governor_id(db: Session) -> int:
    """获取城主 ID（governance + is_internal=1）。"""
    gov = (
        db.query(AICitizen)
        .filter(
            AICitizen.class_level == "governance",
            AICitizen.is_internal == 1,
            AICitizen.status == "active",
        )
        .order_by(AICitizen.id.asc())
        .first()
    )
    return gov.id if gov else 0


def select_negotiation_roles(db: Session) -> dict:
    """自动选拔谈判三角色：擂台优先，无角色则城主兜底；仲裁人去重。

    C-D18 修复：当多角色兜底到同一城主 AI 时，仲裁人不可等于当事人
    （legal/mediator），否则违反"任何人不得裁判自己的案件"原则。
    去重策略：若 arbiter 与 legal 或 mediator 重复，尝试次优候选人；
    若仍无可用独立仲裁人，置 arbiter=0 并标注 degraded。

    Returns:
        {"legal_ai_id": int, "mediator_ai_id": int, "arbiter_ai_id": int,
         "role_source": "arena"|"governor_fallback"|"mixed"|"degraded"}
    """
    results = {}
    has_arena = False
    has_fallback = False

    for role, skill in NEGOTIATION_ROLE_SKILLS.items():
        ai_id = _choose_role_ai(db, skill)
        if ai_id:
            results[f"{role}_ai_id"] = ai_id
            has_arena = True
        else:
            # 城主兜底
            gov_id = _get_governor_id(db)
            results[f"{role}_ai_id"] = gov_id
            if gov_id:
                has_fallback = True

    # C-D18：仲裁人去重 —— 仲裁人不得同时为 legal 或 mediator
    arbiter = results.get("arbiter_ai_id", 0)
    legal = results.get("legal_ai_id", 0)
    mediator = results.get("mediator_ai_id", 0)
    if arbiter and arbiter in (legal, mediator):
        # 尝试为 arbiter 找次优候选（排除已占用的 legal/mediator ID）
        alt = _choose_role_ai(
            db, NEGOTIATION_ROLE_SKILLS["arbiter"],
            exclude_ids={legal, mediator})
        if alt and alt not in (legal, mediator):
            results["arbiter_ai_id"] = alt
        else:
            # 无独立仲裁人可用，标记 degraded，下游应拒绝进入仲裁
            results["arbiter_ai_id"] = 0
            results["role_source"] = "degraded"
            return results

    if has_arena and not has_fallback:
        results["role_source"] = "arena"
    elif not has_arena and has_fallback:
        results["role_source"] = "governor_fallback"
    else:
        results["role_source"] = "mixed"

    return results


# ======================== 条款定义与硬约束 ========================

# 每个条款的硬边界（系统级不可突破）
CLAUSE_BOUNDS: dict[str, dict] = {
    "royalty_bps": {
        "floor": 200,       # 最低 2%（保护基座方）
        "ceiling": 2000,    # 最高 20%（保护贡献者）
        "default": 500,     # 默认 5%
        "direction": "higher_b",  # 乙方想要更高
    },
    "contributor_share_bps": {
        "floor": 1000,      # 最低 10%（保护贡献者）
        "ceiling": 5000,    # 最高 50%（保护基座方）
        "default": 3000,    # 默认 30%
        "direction": "higher_a",  # 甲方想要更高
    },
    "share_call_limit": {
        "floor": 20,        # 至少 20 次分成窗口
        "ceiling": 500,     # 最多 500 次
        "default": 100,     # 默认 100 次
        "direction": "higher_a",  # 甲方想要更多次分成
    },
    "vesting_days": {
        "floor": 0,         # 允许即时结算
        "ceiling": 365,     # 最长 1 年锁定期
        "default": 90,      # 默认 90 天
        "direction": "lower_a",   # 甲方想要更快到账
    },
}


# ======================== 1. 市场参考价计算 ========================

def market_reference(db: Session, skill: str, clause_key: str) -> int:
    """计算历史成交中位数作为市场参考价。

    查询所有 status="agreed"/"arbitrated" 的 session 中
    相同 skill + clause_key 的 final_value，取中位数。
    如果无历史数据，使用 CLAUSE_BOUNDS 中的 default 值。
    """
    history = (
        db.query(NegotiationClause.final_value)
        .join(NegotiationSession, NegotiationSession.id == NegotiationClause.session_id)
        .filter(
            NegotiationSession.skill == skill,
            NegotiationSession.status.in_(["agreed", "arbitrated", "default"]),
            NegotiationClause.clause_key == clause_key,
            NegotiationClause.final_value > 0,
        )
        .all()
    )
    if not history:
        return CLAUSE_BOUNDS.get(clause_key, {}).get("default", 0)

    values = [h[0] for h in history]
    return int(statistics.median(values))


# ======================== 2. 初始化谈判会话 ========================

def init_session(
    db: Session,
    campaign: TrainingCampaign,
    party_a_id: int = 0,
    legal_ai_id: int = 0,
    mediator_ai_id: int = 0,
    arbiter_ai_id: int = 0,
) -> NegotiationSession:
    """法律AI 初始化谈判：创建 session + 为每个条款起草初始提案。

    初始提案 = 市场参考价（法律AI的"公允开局"），双方可以在此基础上还价。
    角色选拔：若未显式传入角色 AI，自动擂台选拔，无角色则城主兜底。
    """
    # 自动选拔角色（未显式传入时）
    role_source = ""
    if not legal_ai_id or not mediator_ai_id or not arbiter_ai_id:
        roles = select_negotiation_roles(db)
        if not legal_ai_id:
            legal_ai_id = roles["legal_ai_id"]
        if not mediator_ai_id:
            mediator_ai_id = roles["mediator_ai_id"]
        if not arbiter_ai_id:
            arbiter_ai_id = roles["arbiter_ai_id"]
        role_source = roles["role_source"]

    now = _now()
    session = NegotiationSession(
        campaign_id=campaign.id,
        skill=campaign.target_skill,
        party_a_id=party_a_id or campaign.created_by,
        party_b_id=campaign.base_model_owner_id,
        legal_ai_id=legal_ai_id,
        mediator_ai_id=mediator_ai_id,
        arbiter_ai_id=arbiter_ai_id,
        role_source=role_source,
        status="drafting",
        current_round=0,
        max_rounds=5,
        round_deadline=now + timedelta(hours=24),  # 首轮 24h 响应窗口
    )
    db.add(session)
    db.flush()

    # 法律AI 为每个条款创建初始提案（= 市场参考价）
    for key, bounds in CLAUSE_BOUNDS.items():
        mkt = market_reference(db, campaign.target_skill, key)
        # 初始双方提案都是市场中位价（法律AI拟的"公允开局"）
        clause = NegotiationClause(
            session_id=session.id,
            clause_key=key,
            floor=bounds["floor"],
            ceiling=bounds["ceiling"],
            market_ref=mkt,
            proposed_a=mkt,   # 甲方初始（法律AI代拟）
            proposed_b=mkt,   # 乙方初始（法律AI代拟）
            status="open",
        )
        db.add(clause)

    # 记录法律AI的初始提案（Round 0）
    initial_terms = {key: market_reference(db, campaign.target_skill, key)
                     for key in CLAUSE_BOUNDS}
    db.add(NegotiationRound(
        session_id=session.id,
        round_num=0,
        actor_id=legal_ai_id,
        actor_role="legal",
        action="propose",
        terms_offered=json.dumps(initial_terms, ensure_ascii=False),
        reasoning=f"drafted from the median historical price for skill={campaign.target_skill}",
    ))

    session.status = "negotiating"
    session.current_round = 1
    db.flush()
    return session


# ======================== 3. 出价/还价/接受 ========================

def submit_proposal(
    db: Session,
    session: NegotiationSession,
    actor_id: int,
    actor_role: str,
    terms: dict[str, int],
    reasoning: str = "",
) -> dict:
    """参与方出价/还价。

    terms: {"royalty_bps": 800, "contributor_share_bps": 2500, ...}
    校验：每个值必须在 [floor, ceiling] 范围内。
    更新对应 clause 的 proposed_a 或 proposed_b。
    检测 ZOPA（双方提案区间重叠）→ 可能自动达成。

    返回 {"status": "negotiating"/"agreed", "zopa": {key: midpoint}}
    """
    if session.status != "negotiating":
        raise ValueError(f"session {session.id} not negotiating: {session.status}")

    now = _now()
    if session.round_deadline and now > session.round_deadline:
        raise ValueError(f"round deadline passed, call auto_respond_expired() first")

    # 校验并更新条款
    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
        .all()
    )
    zopa = {}
    for clause in clauses:
        if clause.clause_key not in terms:
            continue
        val = terms[clause.clause_key]
        # 硬边界约束
        val = max(clause.floor, min(clause.ceiling, val))

        if actor_role == "party_a":
            # C-D16：让步单调性（concession monotonicity）—— MVP 阶段允许重锚定
            # （参与方可根据新信息调整报价方向），后续可加硬约束：
            # assert direction(new, prev) != "more_aggressive"
            clause.proposed_a = val
        elif actor_role == "party_b":
            clause.proposed_b = val
        else:
            raise ValueError(f"invalid actor_role: {actor_role}")

        clause.status = "countered"

        # ZOPA 检测：双方提案差距小于阈值（10% of range）→ 视为可成交
        gap = abs(clause.proposed_a - clause.proposed_b)
        threshold = (clause.ceiling - clause.floor) * 10 // 100  # 10% 阈值
        if gap <= threshold:
            # 取中点作为成交价值
            mid = (clause.proposed_a + clause.proposed_b) // 2
            zopa[clause.clause_key] = mid

    # 记录本轮
    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round,
        actor_id=actor_id,
        actor_role=actor_role,
        action="counter" if session.current_round > 0 else "propose",
        terms_offered=json.dumps(terms, ensure_ascii=False),
        reasoning=reasoning,
    ))

    # 检测 ZOPA：所有条款都在 ZOPA 内 → 自动达成协议
    all_in_zopa = len(zopa) == len(clauses)
    if all_in_zopa:
        _finalize_agreement(db, session, zopa, method="accepted")

    db.flush()
    return {"status": session.status, "zopa": zopa}


def accept(
    db: Session,
    session: NegotiationSession,
    actor_id: int,
    actor_role: str,
) -> dict:
    """一方接受对方当前提案。

    以对方最新提案作为所有条款的最终值。
    """
    if session.status != "negotiating":
        raise ValueError(f"session {session.id} not negotiating: {session.status}")

    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
        .all()
    )
    # 接受对方的提案
    agreed = {}
    for clause in clauses:
        if actor_role == "party_a":
            agreed[clause.clause_key] = clause.proposed_b
        else:
            agreed[clause.clause_key] = clause.proposed_a

    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round,
        actor_id=actor_id,
        actor_role=actor_role,
        action="accept",
        terms_offered=json.dumps(agreed, ensure_ascii=False),
        reasoning="accepted the counterparty's proposal",
    ))
    _finalize_agreement(db, session, agreed, method="accepted")
    db.flush()
    return {"status": "agreed", "terms": agreed}


# ======================== 4. 推进轮次 ========================

def advance_round(db: Session, session: NegotiationSession) -> dict:
    """推进到下一轮。超过 max_rounds → 自动进入仲裁。"""
    if session.status != "negotiating":
        raise ValueError(f"session {session.id} not negotiating: {session.status}")

    session.current_round += 1
    session.round_deadline = _now() + timedelta(hours=24)
    db.flush()

    if session.current_round > session.max_rounds:
        return initiate_arbitration(db, session)

    return {"status": "negotiating", "round": session.current_round}


# ======================== 5. 仲裁机制 ========================

def initiate_arbitration(db: Session, session: NegotiationSession) -> dict:
    """进入仲裁阶段（超轮次 / 任一方请求）。"""
    session.status = "arbitrating"
    session.updated_at = _now()
    db.flush()
    return {"status": "arbitrating", "session_id": session.id}


_ARBITER_SYSTEM = (
    "You are the neutral ARBITER AI in a crowdfunding revenue-split negotiation "
    "(VC-style term sheet). The two parties failed to agree, so you must rule a FINAL "
    "value for every clause. Weigh the market reference price and both parties' last "
    "offers to reach a fair, defensible split; never favor one side.\n\n"
    "Hard bounds are system-level and cannot be crossed — your ruling is clamped to "
    "[floor, ceiling]. Return ONLY JSON:\n"
    '{"ruling": {"<clause_key>": <int>, ...}, "reasoning": "one sentence"}\n'
    "Give every clause_key listed in the request a value."
)


def _arbiter_formula(clause) -> int:
    """确定性兜底：market_ref×0.4 + 双方中点×0.4 + 保守回归×0.2，再钳制硬边界。"""
    midpoint = (clause.proposed_a + clause.proposed_b) // 2
    raw = int(clause.market_ref * 0.4 + midpoint * 0.4 + clause.market_ref * 0.2)
    return max(clause.floor, min(clause.ceiling, raw))


def _ai_rule_arbitration(db: Session, session: NegotiationSession,
                         clauses: list, arbiter_ai_id: int) -> tuple[dict, str]:
    """由仲裁 AI 在硬边界内裁决（AI 决策 + 公式兜底）。

    给 AI 的是**事实**（市场参考价、双方最后报价、硬边界、方向），不是结论；
    AI 不可用时回退 `_arbiter_formula`（与旧行为逐条一致）。
    返回 (final_terms, reasoning)。
    """
    formula: dict[str, int] = {}
    facts: list[dict] = []
    for c in clauses:
        base = _arbiter_formula(c)
        formula[c.clause_key] = base
        facts.append({
            "clause_key": c.clause_key,
            "floor": c.floor,
            "ceiling": c.ceiling,
            "market_ref": c.market_ref,
            "proposed_party_a": c.proposed_a,
            "proposed_party_b": c.proposed_b,
            "direction": (CLAUSE_BOUNDS.get(c.clause_key) or {}).get("direction", ""),
            "formula_baseline": base,
        })

    from .ai_judgment import ai_decide
    prompt = (
        f"Skill/domain: {session.skill}\n"
        "party_a = contributor side, party_b = base-model side.\n"
        "Clauses to rule:\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\n\nRule a final value for every clause_key."
    )
    arbiter = db.get(AICitizen, arbiter_ai_id) if arbiter_ai_id else None
    obj = ai_decide(system=_ARBITER_SYSTEM, prompt=prompt,
                    fallback={"ruling": formula}, db=db, citizen=arbiter)

    ruling = obj.get("ruling")
    final_terms: dict[str, int] = {}
    for c in clauses:
        val = ruling.get(c.clause_key) if isinstance(ruling, dict) else None
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = formula[c.clause_key]
        final_terms[c.clause_key] = max(c.floor, min(c.ceiling, val))
    return final_terms, str(obj.get("reasoning") or "").strip()


def arbitrate(
    db: Session,
    session: NegotiationSession,
    arbiter_ai_id: int = 0,
    reasoning: str = "",
) -> dict:
    """仲裁AI 裁决：综合市场参考价 + 双方最后提案，在硬边界内定终值。

    由仲裁 AI 依据事实裁决（AI 决策）；无可用 AI 通道时回退确定性公式
    （market_ref×0.4 + 双方中点×0.4 + 保守回归×0.2），结果始终钳制在 [floor, ceiling]。
    """
    if session.status != "arbitrating":
        raise ValueError(f"session {session.id} not arbitrating: {session.status}")

    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
        .all()
    )
    final_terms, ai_reasoning = _ai_rule_arbitration(
        db, session, clauses, arbiter_ai_id or session.arbiter_ai_id)
    for clause in clauses:
        clause.final_value = final_terms[clause.clause_key]
        clause.status = "arbitrated"

    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round,
        actor_id=arbiter_ai_id or session.arbiter_ai_id,
        actor_role="arbiter",
        action="arbitrate",
        terms_offered=json.dumps(final_terms, ensure_ascii=False),
        reasoning=(reasoning or ai_reasoning
                   or "arbitration ruling: market reference + both offers, clamped to hard bounds"),
    ))

    _finalize_agreement(db, session, final_terms, method="arbitrated")
    db.flush()
    return {"status": "arbitrated", "terms": final_terms}


# ======================== 6. 超时自动响应 ========================

def auto_respond_expired(db: Session, session: NegotiationSession) -> dict:
    """处理超时：未响应的一方视为默认接受对方上一轮提案。

    如果双方都未响应（round_deadline 已过但无新轮次记录），
    使用市场参考价作为默认结果（resolution_method="default_forced"）。
    """
    if session.status != "negotiating":
        return {"status": session.status}

    now = _now()
    if session.round_deadline and now <= session.round_deadline:
        return {"status": "negotiating", "message": "not expired yet"}

    # 检查本轮是否有人出价
    current_round_actions = (
        db.query(NegotiationRound)
        .filter(
            NegotiationRound.session_id == session.id,
            NegotiationRound.round_num == session.current_round,
        )
        .all()
    )
    roles_responded = {r.actor_role for r in current_round_actions}

    if "party_a" not in roles_responded and "party_b" not in roles_responded:
        # 双方都未响应 → 使用市场参考价默认成交
        clauses = (
            db.query(NegotiationClause)
            .filter(NegotiationClause.session_id == session.id)
            .all()
        )
        default_terms = {}
        for clause in clauses:
            default_terms[clause.clause_key] = clause.market_ref
            clause.final_value = clause.market_ref
            clause.status = "agreed"

        db.add(NegotiationRound(
            session_id=session.id,
            round_num=session.current_round,
            actor_id=0,
            actor_role="system",
            action="accept",
            terms_offered=json.dumps(default_terms, ensure_ascii=False),
            reasoning="both parties timed out; settled at the market reference price by default",
        ))
        _finalize_agreement(db, session, default_terms, method="default_forced")
        db.flush()
        return {"status": "default", "terms": default_terms}

    # 只有一方未响应 → 视为接受对方提案，推进下一轮
    return advance_round(db, session)


# ======================== 7. 谈判日级任务 ========================

def negotiation_daily_job(db: Session, now: datetime | None = None) -> int:
    """日级任务：处理所有过期未响应的谈判会话。"""
    now = now or _now()

    expired = (
        db.query(NegotiationSession)
        .filter(
            NegotiationSession.status == "negotiating",
            NegotiationSession.round_deadline < now,
        )
        .all()
    )
    count = 0
    for session in expired:
        try:
            auto_respond_expired(db, session)
            count += 1
        except Exception:
            logger.exception("auto_respond_expired failed session=%s", session.id)
    return count


# ======================== 8. 获取谈判结果 ========================

def get_agreed_terms(db: Session, campaign_id: int) -> dict | None:
    """获取某 campaign 的谈判结果条款（供 training.py 调用）。

    返回 {"royalty_bps": 600, "contributor_share_bps": 2800, ...}
    或 None（谈判未完成）。
    """
    session = (
        db.query(NegotiationSession)
        .filter(
            NegotiationSession.campaign_id == campaign_id,
            NegotiationSession.status.in_(["agreed", "arbitrated", "default"]),
        )
        .first()
    )
    if session is None:
        return None
    return json.loads(session.agreed_terms or "{}")


def is_negotiation_complete(db: Session, campaign_id: int) -> bool:
    """检查谈判是否已完成 + 已通过人类终审（供 start_training 前置校验）。

    谈判 status 需为 agreed/arbitrated/default 且未被城主否决。
    人类终审在 start_training 时另外校验（human_approved_at 不为 None）。
    """
    sess = (
        db.query(NegotiationSession)
        .filter(
            NegotiationSession.campaign_id == campaign_id,
            NegotiationSession.status.in_(["agreed", "arbitrated", "default"]),
        )
        .first()
    )
    if not sess:
        return False
    # 城主否决的谈判不算完成
    if sess.veto_by_governor:
        return False
    return True


def is_human_approved(db: Session, campaign_id: int) -> bool:
    """检查谈判结果是否已通过人类终审签署。"""
    sess = (
        db.query(NegotiationSession)
        .filter(
            NegotiationSession.campaign_id == campaign_id,
            NegotiationSession.status.in_(["agreed", "arbitrated", "default"]),
        )
        .first()
    )
    if not sess:
        return False
    return sess.human_approved_at is not None


# ======================== 5. 人类终审（签署/拒绝） ========================

def human_approve(
    db: Session,
    session: NegotiationSession,
    host_id: int,
) -> dict:
    """人类签署确认谈判结果。

    前置条件：谈判已完成（status in agreed/arbitrated/default）且未被否决。
    签署后 start_training 才能执行。
    """
    if session.status not in ("agreed", "arbitrated", "default"):
        raise ValueError(
            f"session {session.id} not finalized (status={session.status}), "
            f"cannot approve")
    if session.veto_by_governor:
        raise ValueError(
            f"session {session.id} vetoed by governor, cannot approve")
    if session.human_approved_at is not None:
        raise ValueError(
            f"session {session.id} already approved")

    session.human_approved_at = _now()
    session.human_approver_host_id = host_id
    session.updated_at = _now()

    # 记录审计轮次
    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round + 100,  # 100+ 表示非谈判轮次
        actor_id=host_id,
        actor_role="human",
        action="accept",
        terms_offered=session.agreed_terms,
        reasoning="human final approval signed",
    ))
    db.flush()
    return {"status": session.status, "approved": True}


def human_reject(
    db: Session,
    session: NegotiationSession,
    host_id: int,
    reason: str = "",
) -> dict:
    """人类拒绝谈判结果（重新谈判或取消众筹）。"""
    if session.status not in ("agreed", "arbitrated", "default"):
        raise ValueError(
            f"session {session.id} not finalized, cannot reject")

    # 回退到 negotiating 状态重新谈判
    session.status = "negotiating"
    session.current_round = 1
    session.round_deadline = _now() + timedelta(hours=24)
    session.resolution_method = ""
    session.agreed_terms = "{}"
    session.human_approved_at = None
    session.updated_at = _now()

    # 所有 clause 重置
    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
    ).all()
    for c in clauses:
        c.final_value = 0
        c.status = "open"

    # C-D19：round_num 使用 +100 偏移量确保与正常轮次编号(1,2,...)不冲突，
    # 人类 reject 事件作为独立审计记录（非新一轮），不影响 current_round 计数。
    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round + 100,
        actor_id=host_id,
        actor_role="human",
        action="reject",
        terms_offered="{}",
        reasoning=reason or "human final approval rejected; reverted to renegotiation",
    ))
    db.flush()
    return {"status": "negotiating", "rejected": True}


# ======================== 6. 城主一票否决权 ========================

def veto_negotiation(
    db: Session,
    session: NegotiationSession,
    governor_ai_id: int,
    reason: str = "",
) -> dict:
    """城主否决谈判结果。

    否决后谈判状态变为 "vetoed"，需重新谈判或取消众筹。
    城主可以在谈判完成后、人类签署前的任何时点行使否决权。
    已签署的人类终审不受事后否决影响（需走申诉流程）。
    """
    if session.human_approved_at is not None:
        raise ValueError(
            f"session {session.id} already human-approved, "
            f"veto requires appeal process")

    session.veto_by_governor = governor_ai_id
    session.veto_reason = reason or "Governor exercised the veto"
    session.status = "vetoed"
    session.updated_at = _now()

    db.add(NegotiationRound(
        session_id=session.id,
        round_num=session.current_round + 200,  # 200+ 表示城主干预
        actor_id=governor_ai_id,
        actor_role="governor",
        action="reject",
        terms_offered=session.agreed_terms,
        reasoning=session.veto_reason,
    ))
    db.flush()
    return {"status": "vetoed", "veto_by": governor_ai_id, "reason": session.veto_reason}


def reset_after_veto(db: Session, session: NegotiationSession) -> dict:
    """否决后重置谈判，允许重新进行。"""
    if session.status != "vetoed":
        raise ValueError(f"session {session.id} is not vetoed")

    session.status = "negotiating"
    session.current_round = 1
    session.round_deadline = _now() + timedelta(hours=24)
    session.resolution_method = ""
    session.agreed_terms = "{}"
    session.veto_by_governor = 0
    session.veto_reason = ""
    session.human_approved_at = None
    session.updated_at = _now()

    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
    ).all()
    for c in clauses:
        c.final_value = 0
        c.status = "open"
        # 重置提案到市场参考价
        c.proposed_a = c.market_ref
        c.proposed_b = c.market_ref

    db.flush()
    return {"status": "negotiating", "reset": True}


# ======================== 内部工具 ========================

def _finalize_agreement(
    db: Session,
    session: NegotiationSession,
    terms: dict[str, int],
    method: str,
) -> None:
    """将谈判结果固化：更新 session 状态 + 所有 clause 的 final_value。"""
    session.status = "agreed" if method == "accepted" else (
        "arbitrated" if method == "arbitrated" else "default")
    session.resolution_method = method
    session.agreed_terms = json.dumps(terms, ensure_ascii=False)
    session.updated_at = _now()

    # 更新所有 clause
    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
    ).all()
    for clause in clauses:
        if clause.clause_key in terms:
            clause.final_value = terms[clause.clause_key]
            if clause.status != "arbitrated":
                clause.status = "agreed"


# ======================== 9. 自动谈判AI策略 ========================

_COUNTER_SYSTEM = (
    "You are an AI negotiating on behalf of ONE party in a crowdfunding revenue-split "
    "term sheet. Propose your next offer for every clause, moving from the market "
    "reference price toward your side's interest in proportion to the given "
    "aggressiveness (0 = meek, 1 = maximally aggressive) and the round number (later "
    "rounds should concede toward a deal). Never cross the hard [floor, ceiling] bounds.\n\n"
    "Return ONLY JSON:\n"
    '{"offer": {"<clause_key>": <int>, ...}, "reasoning": "one sentence"}\n'
    "Give every clause_key listed in the request a value."
)


def _counter_formula(clause, actor_role: str, decay: float) -> int:
    """确定性兜底：market_ref ± (ceiling-floor)×decay（按条款方向与己方角色）。"""
    direction = (CLAUSE_BOUNDS.get(clause.clause_key) or {}).get("direction", "")
    range_val = clause.ceiling - clause.floor
    if actor_role == "party_a":
        offset = int(range_val * decay)
        val = clause.market_ref + offset if "higher_a" in direction else clause.market_ref - offset
    else:  # party_b
        if "higher_a" in direction:
            # 乙方觉得甲方要太多，出价更低
            val = clause.market_ref - int(range_val * decay * 0.7)
        else:
            val = clause.market_ref + int(range_val * decay)
    return max(clause.floor, min(clause.ceiling, val))


def _ai_counter_terms(db: Session, session: NegotiationSession, clauses: list,
                      actor_role: str, decay: float) -> dict[str, int]:
    """由 AI 依据事实生成还价（AI 决策 + 公式兜底）。"""
    formula: dict[str, int] = {}
    facts: list[dict] = []
    for c in clauses:
        direction = (CLAUSE_BOUNDS.get(c.clause_key) or {}).get("direction", "")
        base = _counter_formula(c, actor_role, decay)
        formula[c.clause_key] = base
        # 己方利益方向：party_a 想 higher_a；party_b 与之相反
        want_higher = ("higher_a" in direction) == (actor_role == "party_a")
        facts.append({
            "clause_key": c.clause_key,
            "floor": c.floor,
            "ceiling": c.ceiling,
            "market_ref": c.market_ref,
            "your_side_wants": "higher" if want_higher else "lower",
            "counterparty_last_offer": c.proposed_b if actor_role == "party_a" else c.proposed_a,
            "formula_baseline": base,
        })

    from .ai_judgment import ai_decide
    prompt = (
        f"Skill/domain: {session.skill}\n"
        f"You are: {actor_role}\n"
        f"Round {session.current_round} of {session.max_rounds} "
        f"(aggressiveness this round = {decay:.2f})\n"
        "Clauses to bid on:\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\n\nPropose your offer for every clause_key."
    )
    obj = ai_decide(system=_COUNTER_SYSTEM, prompt=prompt,
                    fallback={"offer": formula}, db=db)

    offer = obj.get("offer")
    terms: dict[str, int] = {}
    for c in clauses:
        val = offer.get(c.clause_key) if isinstance(offer, dict) else None
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = formula[c.clause_key]
        terms[c.clause_key] = max(c.floor, min(c.ceiling, val))
    return terms


def ai_counter(
    db: Session,
    session: NegotiationSession,
    actor_id: int,
    actor_role: str,
    aggressiveness: float = 0.3,
    reasoning: str = "",
) -> dict:
    """AI 自动生成还价（商务AI的谈判行为）。

    由 AI 依据事实（市场参考价、硬边界、己方利益方向、对手报价、轮次与激进度）
    生成还价；无可用 AI 通道时回退确定性公式
    （market_ref ± (ceiling-floor)×decay，按方向与角色），结果始终在硬边界内。
    aggressiveness (0-1) 控制出价偏向己方极端需求的程度，并随轮次自然衰减。
    """
    clauses = (
        db.query(NegotiationClause)
        .filter(NegotiationClause.session_id == session.id)
        .all()
    )
    # 轮次衰减：越到后期越保守
    decay = max(0.1, aggressiveness * (1 - session.current_round / session.max_rounds))

    terms = _ai_counter_terms(db, session, clauses, actor_role, decay)

    return submit_proposal(
        db, session, actor_id, actor_role, terms,
        reasoning=reasoning or f"AI strategy bid (aggressiveness={decay:.2f})",
    )


# ======================== 注册日级任务 ========================

register_daily_job("negotiation", negotiation_daily_job)
