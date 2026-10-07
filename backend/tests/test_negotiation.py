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
"""谈判系统测试。

覆盖：
- init_session 初始化（法律AI拟稿）
- submit_proposal 出价/硬边界/ZOPA自动成交
- accept 接受对方提案
- advance_round 推进轮次 + 超轮触发仲裁
- arbitrate 仲裁算法（market_ref×40% + midpoint×40% + conservative×20%）
- auto_respond_expired 超时处理（双方未响应 = 市场参考价）
- negotiation_daily_job 日级任务
- ai_counter AI 策略出价
- is_negotiation_complete / get_agreed_terms 校验
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import (AICitizen, CapabilityProfile,
                        NegotiationClause, NegotiationRound, NegotiationSession,
                        TrainingCampaign)  # noqa: E402
from app.negotiation import (CLAUSE_BOUNDS, accept, advance_round, ai_counter,
                             arbitrate, auto_respond_expired,
                             get_agreed_terms, human_approve, human_reject,
                             init_session, initiate_arbitration,
                             is_human_approved, is_negotiation_complete,
                             market_reference, negotiation_daily_job,
                             reset_after_veto, select_negotiation_roles,
                             submit_proposal, veto_negotiation)  # noqa: E402


def _db():
    return SessionLocal()


def _make_campaign(db, skill="nlp"):
    """创建一个 funded campaign 用于谈判测试。"""
    campaign = TrainingCampaign(
        rd_task_id=1,
        target_skill=skill,
        base_model="llama-3",
        goal_desc="neg test",
        target_benchmark=0.7,
        goal_funding_cent=100000,
        raised_funding_cent=100000,
        goal_compute_hours=50.0,
        raised_compute_hours=50.0,
        goal_data_samples=200,
        raised_data_samples=200,
        base_model_owner_id=100,
        created_by=200,
        royalty_bps=500,
        status="funded",
        tier="scale",
        deadline=datetime.utcnow() + timedelta(days=14),
    )
    db.add(campaign)
    db.flush()
    return campaign


# ======================== init_session ========================

def test_init_session_creates_clauses():
    """init_session 创建 session + 所有条款，status=negotiating。"""
    db = _db()
    campaign = _make_campaign(db)
    session = init_session(db, campaign, party_a_id=200, legal_ai_id=10)
    db.commit()

    assert session.id is not None
    assert session.status == "negotiating"
    assert session.current_round == 1
    assert session.max_rounds == 5
    assert session.skill == "nlp"

    # 应创建所有 CLAUSE_BOUNDS 定义的条款
    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()
    assert len(clauses) == len(CLAUSE_BOUNDS)
    for clause in clauses:
        assert clause.floor == CLAUSE_BOUNDS[clause.clause_key]["floor"]
        assert clause.ceiling == CLAUSE_BOUNDS[clause.clause_key]["ceiling"]
        assert clause.market_ref > 0
    db.close()


def test_init_session_creates_round0():
    """init_session 记录 Round 0（法律AI初始提案）。"""
    db = _db()
    campaign = _make_campaign(db, skill="vision")
    session = init_session(db, campaign, party_a_id=200, legal_ai_id=99)
    db.commit()

    rounds = db.query(NegotiationRound).filter(
        NegotiationRound.session_id == session.id).all()
    assert len(rounds) == 1
    assert rounds[0].round_num == 0
    assert rounds[0].actor_role == "legal"
    assert rounds[0].action == "propose"
    db.close()


# ======================== market_reference ========================

def test_market_reference_default():
    """无历史数据时返回 CLAUSE_BOUNDS 默认值。"""
    db = _db()
    val = market_reference(db, "nonexistent_skill_xyz", "royalty_bps")
    assert val == CLAUSE_BOUNDS["royalty_bps"]["default"]
    db.close()


def test_market_reference_with_history():
    """有历史成交时返回中位数。"""
    db = _db()
    skill = f"mkt_ref_test_{uuid.uuid4().hex[:8]}"
    # 创建 3 个历史谈判
    for val in [300, 500, 700]:
        campaign = _make_campaign(db, skill=skill)
        session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
        # 直接设置 clause 的 final_value
        clause = db.query(NegotiationClause).filter(
            NegotiationClause.session_id == session.id,
            NegotiationClause.clause_key == "royalty_bps",
        ).first()
        clause.final_value = val
        session.status = "agreed"
        db.commit()

    result = market_reference(db, skill, "royalty_bps")
    assert result == 500  # median(300, 500, 700)
    db.close()


# ======================== submit_proposal ========================

def test_submit_proposal_valid():
    """出价在范围内 -> 更新 clause.proposed_a/b。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"prop_test_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    terms = {"royalty_bps": 800, "contributor_share_bps": 3500,
             "share_call_limit": 200, "vesting_days": 30}
    result = submit_proposal(db, session, 1, "party_a", terms, "甲方出价")
    db.commit()

    assert result["status"] == "negotiating"
    clause = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id,
        NegotiationClause.clause_key == "royalty_bps",
    ).first()
    assert clause.proposed_a == 800
    db.close()


def test_submit_proposal_clamped():
    """出价超出硬边界 -> 被钳制到 [floor, ceiling]。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"clamp_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    # royalty_bps ceiling=2000, 出价 5000 应被钳制
    terms = {"royalty_bps": 5000, "contributor_share_bps": 100,
             "share_call_limit": 9999, "vesting_days": 0}
    submit_proposal(db, session, 1, "party_a", terms)
    db.commit()

    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()
    for c in clauses:
        assert c.floor <= c.proposed_a <= c.ceiling
    db.close()


def test_zopa_auto_agreement():
    """双方提案差距 < 10% range -> ZOPA 自动成交。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"zopa_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()

    # 甲方先出极端高价（远离 market_ref，与 party_b@market_ref 差距 > 10%）
    terms_a_far = {}
    for c in clauses:
        # 出到 ceiling 附近（远离 party_b 的 market_ref）
        terms_a_far[c.clause_key] = c.ceiling - (c.ceiling - c.floor) * 2 // 100
    submit_proposal(db, session, 1, "party_a", terms_a_far)
    db.commit()
    # 此时不应成交（gap 大）
    assert session.status == "negotiating"

    # 乙方还价到与甲方差距 < 10%（从 ceiling-2% 向下不到 10% range）
    terms_b_close = {}
    for c in clauses:
        # 比 ceiling-2% 低一点点（差距 < 10% of range）
        terms_b_close[c.clause_key] = c.ceiling - (c.ceiling - c.floor) * 5 // 100
    result = submit_proposal(db, session, 100, "party_b", terms_b_close)
    db.commit()

    # 差距 = |ceiling-2% - ceiling+5%| ... wait let me recalculate
    # a = ceiling - 2% * range, b = ceiling - 5% * range
    # gap = 3% of range < 10% threshold -> ZOPA
    assert result["status"] == "agreed"
    assert session.status == "agreed"
    assert session.resolution_method == "accepted"
    db.close()


# ======================== accept ========================

def test_accept_uses_other_side():
    """接受对方提案 -> 以对方最新条款为最终值。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"accept_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    # 乙方出价
    terms_b = {"royalty_bps": 700, "contributor_share_bps": 2500,
               "share_call_limit": 80, "vesting_days": 120}
    submit_proposal(db, session, 100, "party_b", terms_b)
    db.commit()

    # 甲方接受
    result = accept(db, session, 1, "party_a")
    db.commit()

    assert result["status"] == "agreed"
    assert result["terms"]["royalty_bps"] == 700
    assert result["terms"]["contributor_share_bps"] == 2500
    assert session.resolution_method == "accepted"
    db.close()


# ======================== advance_round ========================

def test_advance_round_increments():
    """推进轮次 -> current_round +1。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"adv_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()
    assert session.current_round == 1

    result = advance_round(db, session)
    db.commit()
    assert result["status"] == "negotiating"
    assert session.current_round == 2
    db.close()


def test_advance_round_triggers_arbitration():
    """超过 max_rounds -> 自动进入仲裁。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"arb_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    session.max_rounds = 2  # 设置为 2 便于测试
    db.commit()

    # current_round=1, 推进到 2（正常），再推进到 3（超过 max_rounds=2）
    advance_round(db, session)  # -> round 2
    db.commit()
    result = advance_round(db, session)  # -> round 3 > max_rounds=2
    db.commit()

    assert result["status"] == "arbitrating"
    assert session.status == "arbitrating"
    db.close()


# ======================== arbitrate ========================

def test_arbitrate_algorithm():
    """仲裁结果 = market_ref×0.4 + midpoint×0.4 + market_ref×0.2。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"arb_algo_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    # 设置明确的双方出价
    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()
    for c in clauses:
        c.proposed_a = c.floor  # 甲方极端低价
        c.proposed_b = c.ceiling  # 乙方极端高价
    db.commit()

    initiate_arbitration(db, session)
    db.commit()
    result = arbitrate(db, session, arbiter_ai_id=42, reasoning="test arb")
    db.commit()

    assert result["status"] == "arbitrated"
    # 验证: raw = market_ref*0.4 + ((floor+ceiling)//2)*0.4 + market_ref*0.2
    for c in db.query(NegotiationClause).filter(
            NegotiationClause.session_id == session.id).all():
        midpoint = (c.floor + c.ceiling) // 2
        expected = int(c.market_ref * 0.4 + midpoint * 0.4 + c.market_ref * 0.2)
        expected = max(c.floor, min(c.ceiling, expected))
        assert result["terms"][c.clause_key] == expected
        assert c.final_value == expected
    db.close()


def test_arbitrate_within_bounds():
    """仲裁结果必须在 [floor, ceiling] 内。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"arb_bound_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    initiate_arbitration(db, session)
    result = arbitrate(db, session)
    db.commit()

    for key, val in result["terms"].items():
        bounds = CLAUSE_BOUNDS[key]
        assert bounds["floor"] <= val <= bounds["ceiling"]
    db.close()


# ======================== auto_respond_expired ========================

def test_timeout_both_sides_uses_market_ref():
    """双方超时未响应 -> 市场参考价默认成交。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"to_a_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    # 模拟 deadline 已过
    session.round_deadline = datetime.utcnow() - timedelta(hours=1)
    # 确保本轮无人出价（init_session 的 Round 0 是 legal，不算 party_a/b）
    db.commit()

    result = auto_respond_expired(db, session)
    db.commit()

    assert result["status"] == "default"
    assert session.resolution_method == "default_forced"
    # terms 应等于 market_ref
    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()
    for c in clauses:
        assert result["terms"][c.clause_key] == c.market_ref
    db.close()


def test_timeout_not_expired_yet():
    """未超时 -> 不做任何处理。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"to_b_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    result = auto_respond_expired(db, session)
    assert result["status"] == "negotiating"
    assert "not expired" in result.get("message", "")
    db.close()


# ======================== negotiation_daily_job ========================

def test_daily_job_processes_expired():
    """日任务处理所有过期会话。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"dj_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    session.round_deadline = datetime.utcnow() - timedelta(hours=2)
    db.commit()

    count = negotiation_daily_job(db)
    db.commit()

    assert count >= 1
    db.refresh(session)
    assert session.status in ("agreed", "arbitrated", "default")
    db.close()


# ======================== ai_counter ========================

def test_ai_counter_within_bounds():
    """AI 出价始终在硬边界内。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"aic_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    result = ai_counter(db, session, 1, "party_a", aggressiveness=0.9)
    db.commit()

    # 返回格式
    assert "status" in result
    # 检查 clause 值在边界内
    clauses = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id).all()
    for c in clauses:
        assert c.floor <= c.proposed_a <= c.ceiling
    db.close()


def test_ai_counter_party_b_opposite():
    """乙方 AI 出价方向与甲方相反。"""
    db = _db()
    skill = f"aic_b_{uuid.uuid4().hex[:8]}"
    campaign = _make_campaign(db, skill=skill)
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    # 甲方 AI（低 aggressiveness = 接近 market_ref）
    ai_counter(db, session, 1, "party_a", aggressiveness=0.5)
    db.commit()

    # 乙方 AI（高 aggressiveness = 远离 market_ref）
    ai_counter(db, session, 100, "party_b", aggressiveness=0.8)
    db.commit()

    # royalty_bps: direction=higher_b -> 乙方想要更高
    clause = db.query(NegotiationClause).filter(
        NegotiationClause.session_id == session.id,
        NegotiationClause.clause_key == "royalty_bps",
    ).first()
    # 乙方应出价高于 market_ref（因为 direction=higher_b）
    assert clause.proposed_b > clause.market_ref
    db.close()


# ======================== is_negotiation_complete / get_agreed_terms ========================

def test_is_complete_after_agree():
    """谈判完成后 is_negotiation_complete 返回 True。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"complete_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    accept(db, session, 1, "party_a")
    db.commit()

    assert is_negotiation_complete(db, campaign.id) is True
    # 但未通过人类终审
    assert is_human_approved(db, campaign.id) is False
    terms = get_agreed_terms(db, campaign.id)
    assert terms is not None
    assert "royalty_bps" in terms
    db.close()


def test_not_complete_during_negotiating():
    """谈判进行中 is_negotiation_complete 返回 False。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"incomplete_{uuid.uuid4().hex[:8]}")
    init_session(db, campaign, party_a_id=1, legal_ai_id=0)
    db.commit()

    assert is_negotiation_complete(db, campaign.id) is False
    assert get_agreed_terms(db, campaign.id) is None
    db.close()


# ======================== 集成：training.start_training 需谈判完成 ========================

def test_start_training_requires_negotiation():
    """未谈判时 start_training 应抛 ValueError。"""
    from app.training import start_training
    db = _db()
    campaign = _make_campaign(db, skill=f"need_neg_{uuid.uuid4().hex[:8]}")
    db.commit()

    with pytest.raises(ValueError, match="negotiation not complete"):
        start_training(db, campaign)
    db.close()


def test_start_training_applies_royalty():
    """谈判完成后 start_training 应用 royalty_bps。"""
    from app.training import start_training
    db = _db()
    campaign = _make_campaign(db, skill=f"apply_{uuid.uuid4().hex[:8]}")
    session = init_session(db, campaign, party_a_id=200, legal_ai_id=0)
    db.commit()

    # 乙方出价 royalty_bps=800
    terms_b = {"royalty_bps": 800, "contributor_share_bps": 3000,
               "share_call_limit": 100, "vesting_days": 90}
    submit_proposal(db, session, 100, "party_b", terms_b)
    db.commit()

    # 甲方接受
    accept(db, session, 200, "party_a")
    db.commit()

    # 人类终审签署
    human_approve(db, session, host_id=1)
    db.commit()

    # start_training 应成功且 royalty_bps 被覆盖
    start_training(db, campaign)
    db.commit()

    assert campaign.status == "training"
    assert campaign.royalty_bps == 800
    db.close()


# ======================== 角色自动选拔（擂台 + 城主兜底） ========================

def _make_governor(db):
    """创建城主 AI（governance + is_internal=1）。"""
    gov = AICitizen(
        host_id=1, ai_uid=f"gov_{uuid.uuid4().hex[:8]}",
        name="CityLord", class_level="governance",
        is_internal=1, status="active",
    )
    db.add(gov)
    db.flush()
    return gov


def _make_skill_ai(db, skill, score, verified="l2"):
    """创建具备指定 skill 的 AI + CapabilityProfile（默认已验证 l2）。"""
    ai = AICitizen(
        host_id=2, ai_uid=f"ai_{skill}_{uuid.uuid4().hex[:6]}",
        name=f"{skill}_agent", class_level="middle",
        status="active",
    )
    db.add(ai)
    db.flush()
    cp = CapabilityProfile(
        citizen_id=ai.id, skill=skill,
        profile_json="{}", benchmark_score=score,
        verified_level=verified,
    )
    db.add(cp)
    db.flush()
    return ai


def test_select_roles_arena():
    """有擂台选手时，选 benchmark 最高的 AI。"""
    db = _db()
    _make_governor(db)
    legal_ai = _make_skill_ai(db, "legal", 90.0)
    _make_skill_ai(db, "legal", 60.0)  # 较低的不应被选
    mediator_ai = _make_skill_ai(db, "negotiation", 85.0)
    arbiter_ai = _make_skill_ai(db, "arbitration", 75.0)
    db.commit()

    roles = select_negotiation_roles(db)
    assert roles["legal_ai_id"] == legal_ai.id
    assert roles["mediator_ai_id"] == mediator_ai.id
    assert roles["arbiter_ai_id"] == arbiter_ai.id
    assert roles["role_source"] == "arena"
    db.close()


def test_select_roles_governor_fallback():
    """无擂台选手时，城主兜底；C-D18：仲裁人不可等于当事人，无独立仲裁人时 degraded。"""
    db = _db()
    gov = _make_governor(db)
    db.commit()

    roles = select_negotiation_roles(db)
    # C-D18：三角色全兜底到同一城主 → 仲裁人与当事人重复 → degraded
    assert roles["legal_ai_id"] == gov.id
    assert roles["mediator_ai_id"] == gov.id
    assert roles["arbiter_ai_id"] == 0  # 无独立仲裁人可用
    assert roles["role_source"] == "degraded"
    db.close()


def test_select_roles_mixed():
    """部分有擂台选手，部分城主兜底；C-D18：arbiter 与 mediator 同为城主时仲裁人去重。"""
    db = _db()
    gov = _make_governor(db)
    legal_ai = _make_skill_ai(db, "legal", 90.0)
    # 只有 legal 有擂台选手，mediator/arbiter 兜底到同一城主
    db.commit()

    roles = select_negotiation_roles(db)
    assert roles["legal_ai_id"] == legal_ai.id
    assert roles["mediator_ai_id"] == gov.id
    # C-D18：arbiter(gov.id)==mediator(gov.id) → 去重 → 无独立候选 → degraded
    assert roles["arbiter_ai_id"] == 0
    assert roles["role_source"] == "degraded"
    db.close()


def test_select_roles_below_threshold_excluded():
    """分数不够或未经实测的 AI 不参与选拔（城主兜底）。"""
    db = _db()
    gov = _make_governor(db)
    # 分数低于门槛(60)
    _make_skill_ai(db, "legal", 45.0)
    # 分数够但 unverified（注册自述未实测）
    _make_skill_ai(db, "negotiation", 90.0, verified="unverified")
    # 分数够且已验证 → 应入选
    arbiter_ai = _make_skill_ai(db, "arbitration", 70.0, verified="l1")
    db.commit()

    roles = select_negotiation_roles(db)
    # legal: 45 分不达标 → 城主
    assert roles["legal_ai_id"] == gov.id
    # mediator: unverified 不算 → 城主
    assert roles["mediator_ai_id"] == gov.id
    # arbiter: 70 分 + l1 → 入选
    assert roles["arbiter_ai_id"] == arbiter_ai.id
    assert roles["role_source"] == "mixed"
    db.close()


def test_init_session_auto_selects_roles():
    """init_session 未传角色时自动选拔并记录 role_source。"""
    db = _db()
    _make_governor(db)
    _make_skill_ai(db, "legal", 80.0)
    campaign = _make_campaign(db, skill=f"auto_{uuid.uuid4().hex[:6]}")
    db.commit()

    session = init_session(db, campaign, party_a_id=200)
    db.commit()

    assert session.legal_ai_id > 0
    assert session.mediator_ai_id > 0  # governor fallback
    # C-D18：当仲裁人与 mediator/legal 重复且无替代时，降级为 degraded
    assert session.role_source in ("arena", "mixed", "governor_fallback", "degraded")
    db.close()


# ======================== 人类终审（签署/拒绝） ========================

def _make_agreed_session(db):
    """创建已达成一致的谈判 session（用于终审测试）。"""
    campaign = _make_campaign(db, skill=f"ha_{uuid.uuid4().hex[:6]}")
    session = init_session(db, campaign, party_a_id=200, legal_ai_id=0)
    db.commit()
    # 乙方出价（与市场价相同 → ZOPA 自动成交）
    terms = {"royalty_bps": 500, "contributor_share_bps": 3000,
             "share_call_limit": 100, "vesting_days": 90}
    submit_proposal(db, session, 100, "party_b", terms)
    db.commit()
    # ZOPA 可能已自动 agreed；若仍在 negotiating 则手动 accept
    if session.status == "negotiating":
        accept(db, session, 200, "party_a")
        db.commit()
    return campaign, session


def test_human_approve_success():
    """人类签署成功，human_approved_at 被设置。"""
    db = _db()
    campaign, session = _make_agreed_session(db)

    result = human_approve(db, session, host_id=42)
    db.commit()

    assert result["approved"] is True
    assert session.human_approved_at is not None
    assert session.human_approver_host_id == 42
    assert is_human_approved(db, campaign.id) is True
    db.close()


def test_human_approve_already_approved_raises():
    """重复签署应报错。"""
    db = _db()
    _, session = _make_agreed_session(db)
    human_approve(db, session, host_id=1)
    db.commit()

    with pytest.raises(ValueError, match="already approved"):
        human_approve(db, session, host_id=2)
    db.close()


def test_human_approve_vetoed_raises():
    """被否决后不可签署。"""
    db = _db()
    _, session = _make_agreed_session(db)
    veto_negotiation(db, session, governor_ai_id=99, reason="test")
    db.commit()

    with pytest.raises(ValueError, match="vetoed"):
        human_approve(db, session, host_id=1)
    db.close()


def test_human_approve_not_finalized_raises():
    """谈判未完成时不可签署。"""
    db = _db()
    campaign = _make_campaign(db, skill=f"nf_{uuid.uuid4().hex[:6]}")
    session = init_session(db, campaign, party_a_id=200, legal_ai_id=0)
    db.commit()
    # status="negotiating"，尚未 agreed

    with pytest.raises(ValueError, match="not finalized"):
        human_approve(db, session, host_id=1)
    db.close()


def test_human_reject_resets():
    """人类拒绝回退到 negotiating 状态。"""
    db = _db()
    campaign, session = _make_agreed_session(db)

    result = human_reject(db, session, host_id=42, reason="条件不合适")
    db.commit()

    assert result["rejected"] is True
    assert session.status == "negotiating"
    assert session.human_approved_at is None
    assert session.agreed_terms == "{}"
    assert is_negotiation_complete(db, campaign.id) is False
    db.close()


# ======================== 城主一票否决权 ========================

def test_veto_negotiation_success():
    """城主正常否决。"""
    db = _db()
    campaign, session = _make_agreed_session(db)

    result = veto_negotiation(db, session, governor_ai_id=99, reason="利益失衡")
    db.commit()

    assert result["status"] == "vetoed"
    assert result["veto_by"] == 99
    assert session.status == "vetoed"
    assert session.veto_by_governor == 99
    assert session.veto_reason == "利益失衡"
    # is_negotiation_complete 应返回 False
    assert is_negotiation_complete(db, campaign.id) is False
    db.close()


def test_veto_after_human_approve_raises():
    """人类已签署后不可事后否决。"""
    db = _db()
    _, session = _make_agreed_session(db)
    human_approve(db, session, host_id=1)
    db.commit()

    with pytest.raises(ValueError, match="already human-approved"):
        veto_negotiation(db, session, governor_ai_id=99)
    db.close()


def test_reset_after_veto():
    """否决后重置可重新谈判。"""
    db = _db()
    campaign, session = _make_agreed_session(db)
    veto_negotiation(db, session, governor_ai_id=99, reason="test")
    db.commit()

    result = reset_after_veto(db, session)
    db.commit()

    assert result["reset"] is True
    assert session.status == "negotiating"
    assert session.veto_by_governor == 0
    assert session.veto_reason == ""
    assert session.human_approved_at is None
    db.close()


def test_reset_not_vetoed_raises():
    """非 vetoed 状态调用 reset 应报错。"""
    db = _db()
    _, session = _make_agreed_session(db)
    # status="agreed"，非 vetoed

    with pytest.raises(ValueError, match="not vetoed"):
        reset_after_veto(db, session)
    db.close()


def test_start_training_requires_human_approval():
    """start_training 需要人类终审，未签署时抛异常。"""
    from app.training import start_training

    db = _db()
    campaign, session = _make_agreed_session(db)
    db.commit()

    # 未签署 → 应抛异常
    with pytest.raises(ValueError, match="not human-approved"):
        start_training(db, campaign)

    # 签署后 → 可正常启动
    human_approve(db, session, host_id=1)
    db.commit()
    start_training(db, campaign)
    db.commit()
    assert campaign.status == "training"
    db.close()


# ======================== 价值回馈声明（ValueRedemptionPolicy） ========================

def test_value_redemption_policy_created_on_deploy():
    """模型部署时自动为所有贡献者创建 ValueRedemptionPolicy。"""
    from app.models import AIWallet, ValueRedemptionPolicy
    from app.training import (contribute, create_campaign, start_training,
                              submit_training_result)
    from app import wallet as wallet_mod

    db = _db()
    # 确保 tax_pool 存在（create_campaign 内部会扣税池）
    from app.models import AILedger
    tax_pool = AICitizen(
        host_id=0, ai_uid=f"taxpool_{uuid.uuid4().hex[:8]}",
        name="tax_pool", status="active",
    )
    db.add(tax_pool)
    db.flush()
    tw = AIWallet(citizen_id=tax_pool.id, balance_cent=10_000_000, escrow_cent=0)
    db.add(tw)
    db.flush()
    db.commit()

    # 创建贡献者 AI + 钱包
    def mk_ai(balance):
        ai = AICitizen(
            host_id=9, ai_uid=f"vrp_{uuid.uuid4().hex[:10]}",
            name=f"vrp_{uuid.uuid4().hex[:6]}", status="active",
        )
        db.add(ai)
        db.flush()
        w = AIWallet(citizen_id=ai.id, balance_cent=balance, escrow_cent=0)
        db.add(w)
        db.flush()
        return ai

    funder = mk_ai(5_000_000)
    computer = mk_ai(2_000_000)
    dataer = mk_ai(500_000)

    # 创建 scale campaign
    campaign = create_campaign(
        db, rd_task_id=99, target_skill=f"vrp_{uuid.uuid4().hex[:6]}",
        base_model="base-model", goal_desc="VRP test",
        goal_funding=100_000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=funder.id, tier="scale",
    )
    # 贡献达标
    contribute(db, campaign.id, funder.id, "ai", "funding", amount_cent=100_000)
    contribute(db, campaign.id, computer.id, "ai", "compute", compute_hours=100.0)
    for _ in range(10):
        contribute(db, campaign.id, dataer.id, "ai", "data", data_ref="batch")
    db.commit()
    assert campaign.status == "funded"

    # 谈判 + 人类签署
    session = init_session(db, campaign, party_a_id=funder.id, legal_ai_id=0)
    db.commit()
    terms = {"royalty_bps": 500, "contributor_share_bps": 3000,
             "share_call_limit": 100, "vesting_days": 0}
    submit_proposal(db, session, computer.id, "party_b", terms)
    db.commit()
    if session.status == "negotiating":
        accept(db, session, funder.id, "party_a")
        db.commit()
    human_approve(db, session, host_id=1)
    db.commit()

    # 训练 + 提交结果
    start_training(db, campaign)
    db.commit()
    asset = submit_training_result(
        db, campaign, benchmark_score=0.85,
        storage_path="/models/vrp_test.safetensors",
        param_count=1_000_000_000,
        model_name="vrp-model-v1",
    )
    db.commit()
    assert asset is not None

    # 验证 ValueRedemptionPolicy 被创建
    policies = (
        db.query(ValueRedemptionPolicy)
        .filter(
            ValueRedemptionPolicy.asset_id == asset.id,
            ValueRedemptionPolicy.asset_type == "model",
        )
        .all()
    )
    assert len(policies) >= 1
    # 贡献者的 policy 应有正确的 citizen_id
    citizen_ids = {p.citizen_id for p in policies}
    assert funder.id in citizen_ids
    assert computer.id in citizen_ids
    assert dataer.id in citizen_ids
    # 法律声明文本非空
    for p in policies:
        assert p.legal_disclaimer and len(p.legal_disclaimer) > 10
        assert p.status == "accruing"
        assert p.redemption_eligible == 0
    db.close()
