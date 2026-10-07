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
"""模型训练众筹 + 产权 + 版税系统测试。

覆盖：
- create_campaign 创建众筹
- contribute 贡献资源（funding/compute/data）+ 达标检测
- cancel_campaign 超时退款
- submit_training_result 验收通过/失败
- record_model_call 版税计算
- settle_royalties 结算入账
- harvest_training_data 训练数据积累统计
- training 日级任务注册
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
from app.models import (AICitizen, AIWallet, Contract, Deliverable,
                        EvolutionLog, ModelAsset, ModelRoyalty,
                        ProjectNode, Tool, TrainingCampaign,
                        TrainingContribution)  # noqa: E402
from app import wallet  # noqa: E402
from app.training import (contribute, cancel_campaign, create_campaign,
                          harvest_training_data, record_model_call,
                          settle_royalties, start_training,
                          submit_training_result)  # noqa: E402
from app.negotiation import (accept as neg_accept, human_approve as neg_approve,
                             init_session as neg_init)  # noqa: E402
from app import scheduler  # noqa: E402
from tests.conftest import new_host, new_ai, topup  # noqa: E402


def _db():
    return SessionLocal()


def _make_ai_with_wallet(db, balance_cent=1_000_000, name=None):
    """直接通过 DB 创建 AI + Wallet（绕过 API 以便灵活控制余额）。"""
    name = name or f"train_test_{uuid.uuid4().hex[:8]}"
    ai = AICitizen(
        host_id=1,
        ai_uid=f"ai_test_{uuid.uuid4().hex[:12]}",
        name=name,
        status="active",
        occupation="通用",
    )
    db.add(ai)
    db.flush()
    w = AIWallet(citizen_id=ai.id, balance_cent=balance_cent, escrow_cent=0)
    db.add(w)
    db.flush()
    return ai, w


def _seed_tax_pool(db, amount=500_000):
    """给税池预充值。"""
    wallet.adjust_system_state(db, "tax_pool", amount, ref=f"test:seed_pool:{uuid.uuid4().hex[:8]}")
    wallet.adjust_system_state(db, "money_supply", amount, ref=f"test:seed_ms:{uuid.uuid4().hex[:8]}")
    db.commit()


def _complete_negotiation(db, campaign, party_a_id=0, host_id=1):
    """Helper: 为 campaign 完成谈判 + 人类终审（init + accept + approve）。"""
    pid = party_a_id or campaign.created_by or 1
    nsess = neg_init(db, campaign, party_a_id=pid, legal_ai_id=0)
    neg_accept(db, nsess, pid, "party_a")
    neg_approve(db, nsess, host_id=host_id)
    db.commit()


def _make_funded_campaign(db, goal_funding=50000, goal_compute=100.0, goal_data=100, tier="scale"):
    """创建一个 funded 状态的 campaign（含 funding/compute/data 贡献记录）。
    
    使用三个不同 AI 分别贡献 funding/compute/data，避免 settle_royalties 中
    contributors_snapshot 内同一 contributor_id 多次出现导致 wallet credit ref 唯一约束冲突。
    goal_data 默认 100（每次 data 贡献固定 100 样本，一次即达标）。
    tier 默认 "scale"（最终阶段，提交结果后直接部署为永久资产）。
    """
    ai, w = _make_ai_with_wallet(db, balance_cent=goal_funding + 500_000)
    ai2, w2 = _make_ai_with_wallet(db, balance_cent=100_000)
    ai3, w3 = _make_ai_with_wallet(db, balance_cent=100_000)
    campaign = create_campaign(
        db, rd_task_id=1, target_skill="nlp",
        base_model="llama-3", goal_desc="测试训练",
        target_benchmark=0.7,
        goal_funding=goal_funding, goal_compute=goal_compute,
        goal_data=goal_data, deadline_days=14,
        base_model_owner_id=ai.id,
        tier=tier,
    )
    # 贡献 funding（ai）
    contribute(db, campaign.id, ai.id, "ai", "funding",
               amount_cent=goal_funding)
    # 贡献 compute（ai2）
    contribute(db, campaign.id, ai2.id, "ai", "compute",
               compute_hours=goal_compute)
    # 贡献 data（ai3，一次贡献 100 样本即达标）
    contribute(db, campaign.id, ai3.id, "ai", "data", data_ref="test")
    db.commit()
    # 完成谈判（funded→training 前置条件）：init 后直接 accept = 市场参考价成交
    nsess = neg_init(db, campaign, party_a_id=ai2.id, legal_ai_id=0)
    neg_accept(db, nsess, ai2.id, "party_a")
    neg_approve(db, nsess, host_id=ai2.host_id)
    db.commit()
    return campaign, ai, w


# ======================== test_create_campaign ========================

def test_create_campaign():
    """创建众筹活动 -> status='open'，deadline 非空。"""
    db = _db()
    c = create_campaign(
        db, rd_task_id=10, target_skill="vision",
        base_model="clip", goal_desc="视觉模型",
        target_benchmark=0.8, goal_funding=60000,
        goal_compute=200.0, goal_data=500,
        deadline_days=7,
    )
    db.commit()

    assert c.id is not None
    assert c.status == "open"
    assert c.target_skill == "vision"
    assert c.goal_funding_cent == 60000
    assert c.deadline is not None
    assert c.deadline > datetime.utcnow()
    db.close()


# ======================== test_contribute_funding ========================

def test_contribute_funding():
    """贡献经费 -> raised_funding_cent 增加，wallet 扣款。"""
    db = _db()
    ai, w = _make_ai_with_wallet(db, balance_cent=100_000)
    c = create_campaign(
        db, rd_task_id=0, target_skill="speech",
        base_model="whisper", goal_desc="语音",
        target_benchmark=0.7, goal_funding=50000,
        goal_compute=10.0, goal_data=100,
        deadline_days=14,
    )
    db.commit()

    bal_before = wallet.balance(db, ai.id)
    contribution = contribute(db, c.id, ai.id, "ai", "funding", amount_cent=30000)
    db.commit()

    assert contribution.contribution_type == "funding"
    assert contribution.amount_cent == 30000

    # wallet 扣款
    bal_after = wallet.balance(db, ai.id)
    assert bal_after == bal_before - 30000

    # campaign raised 增加
    db.refresh(c)
    assert c.raised_funding_cent == 30000
    db.close()


# ======================== test_contribute_all_reached_funds ========================

def test_contribute_all_reached_funds():
    """三项都达标 -> status='funded'。"""
    db = _db()
    ai, w = _make_ai_with_wallet(db, balance_cent=200_000)
    c = create_campaign(
        db, rd_task_id=0, target_skill="code_gen",
        base_model="codellama", goal_desc="代码生成",
        target_benchmark=0.7, goal_funding=10000,
        goal_compute=50.0, goal_data=100,
        deadline_days=14,
    )
    db.commit()

    # funding 达标
    contribute(db, c.id, ai.id, "ai", "funding", amount_cent=10000)
    db.commit()
    db.refresh(c)
    assert c.status == "open"  # 其他两项未达标

    # compute 达标
    contribute(db, c.id, ai.id, "ai", "compute", compute_hours=50.0)
    db.commit()
    db.refresh(c)
    assert c.status == "open"

    # data 达标（100 样本，每次贡献 100）
    contribute(db, c.id, ai.id, "ai", "data", data_ref="dataset_v1")
    db.commit()
    db.refresh(c)
    assert c.status == "funded"
    assert c.raised_funding_cent >= c.goal_funding_cent
    assert c.raised_compute_hours >= c.goal_compute_hours
    assert c.raised_data_samples >= c.goal_data_samples
    db.close()


# ======================== test_cancel_expired_campaign ========================

def test_cancel_expired_campaign():
    """超时 campaign -> status='rejected' + funding 退款。"""
    db = _db()
    ai, w = _make_ai_with_wallet(db, balance_cent=200_000)
    c = create_campaign(
        db, rd_task_id=0, target_skill="translate",
        base_model="mbart", goal_desc="翻译模型",
        target_benchmark=0.8, goal_funding=50000,
        goal_compute=100.0, goal_data=500,
        deadline_days=14,
    )
    db.commit()

    # 贡献部分 funding
    contribute(db, c.id, ai.id, "ai", "funding", amount_cent=30000)
    db.commit()

    bal_after_contribute = wallet.balance(db, ai.id)

    # 手动取消（模拟超时）
    cancel_campaign(db, c)
    db.commit()

    db.refresh(c)
    assert c.status == "rejected"

    # 退款：wallet 余额恢复
    bal_after_refund = wallet.balance(db, ai.id)
    assert bal_after_refund == bal_after_contribute + 30000

    # contribution status 变为 refunded
    contribs = db.query(TrainingContribution).filter(
        TrainingContribution.campaign_id == c.id,
        TrainingContribution.contribution_type == "funding",
    ).all()
    for contrib in contribs:
        assert contrib.status == "refunded"
    db.close()


# ======================== test_submit_result_pass_creates_asset ========================

def test_submit_result_pass_creates_asset():
    """benchmark 达标 -> ModelAsset 创建 + Tool 注册 + campaign deployed。"""
    db = _db()
    _seed_tax_pool(db)
    campaign, ai, w = _make_funded_campaign(db)

    # 启动训练
    start_training(db, campaign)
    db.commit()
    assert campaign.status == "training"

    # 提交结果（达标）
    asset = submit_training_result(
        db, campaign, benchmark_score=0.85,
        storage_path="/models/nlp_v1.safetensors",
        param_count=7_000_000_000,
        model_name="nlp-merged-v1",
    )
    db.commit()

    assert asset is not None
    assert asset.skill == "nlp"
    assert asset.benchmark_score == pytest.approx(0.85)
    assert asset.status == "active"
    assert asset.tool_id > 0

    # Tool 已注册
    tool = db.get(Tool, asset.tool_id)
    assert tool is not None
    assert tool.name == "nlp-merged-v1"
    assert tool.status == "verified"

    # campaign deployed
    db.refresh(campaign)
    assert campaign.status == "deployed"
    assert campaign.model_asset_id == asset.id
    db.close()


# ======================== test_submit_result_fail_refunds ========================

def test_submit_result_fail_refunds():
    """benchmark 不达标 -> campaign rejected + funding 退款。"""
    db = _db()
    _seed_tax_pool(db)
    campaign, ai, w = _make_funded_campaign(db)

    start_training(db, campaign)
    db.commit()

    bal_before_fail = wallet.balance(db, ai.id)

    # 提交结果（不达标）
    result = submit_training_result(
        db, campaign, benchmark_score=0.5,
        storage_path="/models/nlp_v1.safetensors",
        param_count=7_000_000_000,
        model_name="nlp-merged-fail",
    )
    db.commit()

    assert result is None
    db.refresh(campaign)
    assert campaign.status == "rejected"

    # 退款：余额恢复（cancel_campaign 退还所有 funding）
    bal_after_fail = wallet.balance(db, ai.id)
    assert bal_after_fail > bal_before_fail  # 收到退款
    db.close()


# ======================== test_record_model_call_royalty ========================

def test_record_model_call_royalty():
    """调用记录版税计算正确。"""
    db = _db()
    _seed_tax_pool(db)
    campaign, ai, w = _make_funded_campaign(db)

    start_training(db, campaign)
    db.commit()

    asset = submit_training_result(
        db, campaign, benchmark_score=0.9,
        storage_path="/models/test.safetensors",
        param_count=1_000_000,
        model_name="royalty-test-model",
    )
    db.commit()

    # 记录一次调用，收入 10000 分
    royalty = record_model_call(
        db, asset.id, call_contract_id=100,
        caller_ai_id=ai.id, revenue_cent=10000,
    )
    db.commit()

    # royalty_cent = 10000 * 500 // 10000 = 500
    assert royalty.royalty_cent == 500
    # contributor_share_cent: 前 100 次有分成 -> 10000 * 3000 // 10000 = 3000
    assert royalty.contributor_share_cent == 3000
    assert royalty.settled == 0
    db.close()


# ======================== test_settle_royalties ========================

def test_settle_royalties():
    """结算后 settled=1，wallet 入账。"""
    db = _db()
    _seed_tax_pool(db)
    campaign, ai, w = _make_funded_campaign(db)

    start_training(db, campaign)
    db.commit()

    asset = submit_training_result(
        db, campaign, benchmark_score=0.88,
        storage_path="/models/settle.safetensors",
        param_count=2_000_000,
        model_name="settle-test-model",
    )
    db.commit()

    # 记录两次调用
    record_model_call(db, asset.id, call_contract_id=201,
                      caller_ai_id=ai.id, revenue_cent=10000)
    record_model_call(db, asset.id, call_contract_id=202,
                      caller_ai_id=ai.id, revenue_cent=20000)
    db.commit()

    owner_bal_before = wallet.balance(db, asset.base_model_owner_id)

    # 结算
    result = settle_royalties(db, asset.id)
    db.commit()

    assert result["count"] == 2
    assert result["royalty_paid"] > 0

    # settled 标记
    royalties = db.query(ModelRoyalty).filter(
        ModelRoyalty.model_asset_id == asset.id,
    ).all()
    for r in royalties:
        assert r.settled == 1

    # wallet 入账（base_model_owner_id = ai.id，所以是同一个 ai）
    owner_bal_after = wallet.balance(db, asset.base_model_owner_id)
    assert owner_bal_after > owner_bal_before
    db.close()


# ======================== test_harvest_training_data ========================

def test_harvest_training_data():
    """统计可用样本数：accepted contracts + 关联 node skill 匹配。"""
    db = _db()

    # 创建 project_nodes（skill="summarize"）
    node = ProjectNode(project_id=1, skill="summarize", status="done")
    db.add(node)
    db.flush()

    # 创建 accepted contract
    c1 = Contract(node_id=node.id, project_id=1, worker_id=99,
                  buyer_id=1, status="accepted")
    c2 = Contract(node_id=node.id, project_id=1, worker_id=98,
                  buyer_id=1, status="accepted")
    # 一个未验收的（不应计入）
    c3 = Contract(node_id=node.id, project_id=1, worker_id=97,
                  buyer_id=1, status="executing")
    db.add_all([c1, c2, c3])
    db.flush()

    # 另一个 skill 的 accepted（不应计入）
    node2 = ProjectNode(project_id=2, skill="translate", status="done")
    db.add(node2)
    db.flush()
    c4 = Contract(node_id=node2.id, project_id=2, worker_id=96,
                  buyer_id=1, status="accepted")
    db.add(c4)
    db.commit()

    count = harvest_training_data(db, "summarize")
    assert count == 2

    count_translate = harvest_training_data(db, "translate")
    assert count_translate == 1

    count_none = harvest_training_data(db, "nonexistent_skill")
    assert count_none == 0
    db.close()


# ======================== test_training_registered_in_scheduler ========================

def test_training_registered_in_scheduler():
    """确保 'training' 已注册到 scheduler._EXTRA_DAILY_JOBS。"""
    job_types = [jt for jt, _ in scheduler._EXTRA_DAILY_JOBS]
    assert "training" in job_types


# ======================== Tier 分级递进测试 ========================

def test_create_campaign_with_tier_seed():
    """tier='seed' 时自动覆盖 benchmark=0.4, deadline=14天。"""
    from app.training import TIER_CONFIG
    db = _db()
    c = create_campaign(
        db, rd_task_id=99, target_skill="3d_modeling",
        base_model="mesh-gpt", goal_desc="3D建模种子验证",
        goal_funding=50000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=1,
        tier="seed",
    )
    db.commit()
    assert c.tier == "seed"
    assert c.target_benchmark == pytest.approx(TIER_CONFIG["seed"]["benchmark"])
    assert c.goal_funding_cent == 50000  # 资金目标不覆盖，保留原值
    assert c.parent_campaign_id == 0
    db.close()


def test_seed_graduates_to_prototype():
    """seed 通过评测 -> campaign deployed, 自动创建 prototype campaign（10倍目标, benchmark=0.6）。"""
    from app.training import TIER_CONFIG
    db = _db()
    _seed_tax_pool(db)
    ai, w = _make_ai_with_wallet(db, balance_cent=1_000_000)
    ai2, _ = _make_ai_with_wallet(db, balance_cent=2_000_000)
    ai3, _ = _make_ai_with_wallet(db, balance_cent=100_000)

    # 创建 seed campaign
    seed = create_campaign(
        db, rd_task_id=50, target_skill="3d_modeling",
        base_model="mesh-gpt", goal_desc="3D建模",
        goal_funding=50000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=ai.id,
        tier="seed",
    )
    # 贡献达标
    contribute(db, seed.id, ai.id, "ai", "funding", amount_cent=50000)
    contribute(db, seed.id, ai2.id, "ai", "compute", compute_hours=100.0)
    for _ in range(10):
        contribute(db, seed.id, ai3.id, "ai", "data", data_ref="batch")
    db.commit()
    assert seed.status == "funded"

    # 训练 + 提交（0.5 >= 0.4 达标）
    _complete_negotiation(db, seed, party_a_id=ai2.id)
    start_training(db, seed)
    db.commit()
    asset = submit_training_result(
        db, seed, benchmark_score=0.55,
        storage_path="/models/seed_ckpt.safetensors",
        param_count=1_000_000,
        model_name="mesh-seed-v1",
    )
    db.commit()

    # seed 通过 -> 不创建 ModelAsset（返回 None）
    assert asset is None
    db.refresh(seed)
    assert seed.status == "deployed"

    # 检查自动创建的 prototype campaign
    proto = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == seed.id,
        TrainingCampaign.tier == "prototype",
    ).first()
    assert proto is not None
    assert proto.status == "open"
    assert proto.target_benchmark == pytest.approx(TIER_CONFIG["prototype"]["benchmark"])
    # prototype 资金目标 = seed * 10
    assert proto.goal_funding_cent == 50000 * 10
    assert proto.goal_compute_hours == 100.0 * 10
    # prototype base_model 引用 seed checkpoint
    assert "checkpoint:seed" in proto.base_model
    db.close()


def test_prototype_graduates_to_scale():
    """prototype 通过评测 -> 自动创建 scale campaign（100倍目标, benchmark=0.8）。"""
    from app.training import TIER_CONFIG
    db = _db()
    _seed_tax_pool(db)
    ai, w = _make_ai_with_wallet(db, balance_cent=100_000_000)
    ai2, _ = _make_ai_with_wallet(db, balance_cent=2_000_000)
    ai3, _ = _make_ai_with_wallet(db, balance_cent=100_000)

    # 创建 seed
    seed = create_campaign(
        db, rd_task_id=50, target_skill="3d_modeling",
        base_model="mesh-gpt", goal_desc="3D建模",
        goal_funding=50000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=ai.id,
        tier="seed",
    )
    contribute(db, seed.id, ai.id, "ai", "funding", amount_cent=50000)
    contribute(db, seed.id, ai2.id, "ai", "compute", compute_hours=100.0)
    for _ in range(10):
        contribute(db, seed.id, ai3.id, "ai", "data", data_ref="b")
    db.commit()
    _complete_negotiation(db, seed, party_a_id=ai2.id)
    start_training(db, seed)
    db.commit()
    submit_training_result(db, seed, benchmark_score=0.5,
                           storage_path="/ckpt.safetensors",
                           param_count=500_000, model_name="seed-m")
    db.commit()

    # 获取 prototype
    proto = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == seed.id,
    ).first()
    assert proto is not None
    assert proto.tier == "prototype"

    # funded prototype
    proto.goal_funding_cent  # 50000*10 = 500000
    contribute(db, proto.id, ai.id, "ai", "funding", amount_cent=proto.goal_funding_cent)
    contribute(db, proto.id, ai2.id, "ai", "compute", compute_hours=proto.goal_compute_hours)
    for _ in range(proto.goal_data_samples // 100):
        contribute(db, proto.id, ai3.id, "ai", "data", data_ref="d")
    db.commit()
    assert proto.status == "funded"

    # 训练 prototype（0.65 >= 0.6）
    _complete_negotiation(db, proto, party_a_id=ai2.id)
    start_training(db, proto)
    db.commit()
    result = submit_training_result(db, proto, benchmark_score=0.65,
                                    storage_path="/proto_ckpt.safetensors",
                                    param_count=2_000_000, model_name="proto-m")
    db.commit()
    assert result is None  # prototype 也不是最终部署

    # 获取 scale campaign
    scale = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == proto.id,
        TrainingCampaign.tier == "scale",
    ).first()
    assert scale is not None
    assert scale.status == "open"
    assert scale.target_benchmark == pytest.approx(TIER_CONFIG["scale"]["benchmark"])
    assert scale.goal_funding_cent == 50000 * 100  # 100x seed 基准
    db.close()


def test_scale_deploys_permanent_asset():
    """scale 通过评测 -> 创建 ModelAsset + Tool，累计历史投入。"""
    from app.training import TIER_CONFIG
    db = _db()
    _seed_tax_pool(db, amount=2_000_000)
    ai, w = _make_ai_with_wallet(db, balance_cent=100_000_000)
    ai2, _ = _make_ai_with_wallet(db, balance_cent=2_000_000)
    ai3, _ = _make_ai_with_wallet(db, balance_cent=100_000)

    # 创建 scale 直接（模拟已走完 seed/prototype 但简化：直接创建 scale）
    scale = create_campaign(
        db, rd_task_id=50, target_skill="3d_modeling",
        base_model="checkpoint:prototype:2|proto-m",
        goal_desc="[scale] 3D建模",
        goal_funding=5_000_000, goal_compute=10000.0, goal_data=10000,
        base_model_owner_id=ai.id,
        tier="scale",
    )
    # funded
    contribute(db, scale.id, ai.id, "ai", "funding", amount_cent=5_000_000)
    contribute(db, scale.id, ai2.id, "ai", "compute", compute_hours=10000.0)
    for _ in range(100):
        contribute(db, scale.id, ai3.id, "ai", "data", data_ref="big_batch")
    db.commit()
    assert scale.status == "funded"

    # 训练 + 提交（0.85 >= 0.8）
    _complete_negotiation(db, scale, party_a_id=ai2.id)
    start_training(db, scale)
    db.commit()
    asset = submit_training_result(db, scale, benchmark_score=0.85,
                                   storage_path="/models/final.safetensors",
                                   param_count=70_000_000_000,
                                   model_name="mesh-3d-v1")
    db.commit()

    assert asset is not None
    assert asset.skill == "3d_modeling"
    assert asset.benchmark_score == pytest.approx(0.85)
    assert asset.status == "active"
    assert asset.tool_id > 0
    db.close()


def test_seed_failure_no_graduation():
    """seed 未达标 -> rejected, 不创建 prototype。"""
    db = _db()
    _seed_tax_pool(db)
    ai, w = _make_ai_with_wallet(db, balance_cent=1_000_000)
    ai2, _ = _make_ai_with_wallet(db, balance_cent=2_000_000)
    ai3, _ = _make_ai_with_wallet(db, balance_cent=100_000)

    seed = create_campaign(
        db, rd_task_id=50, target_skill="3d_modeling",
        base_model="mesh-gpt", goal_desc="3D建模",
        goal_funding=50000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=ai.id,
        tier="seed",
    )
    contribute(db, seed.id, ai.id, "ai", "funding", amount_cent=50000)
    contribute(db, seed.id, ai2.id, "ai", "compute", compute_hours=100.0)
    for _ in range(10):
        contribute(db, seed.id, ai3.id, "ai", "data", data_ref="b")
    db.commit()
    _complete_negotiation(db, seed, party_a_id=ai2.id)
    start_training(db, seed)
    db.commit()

    # 提交 0.3 < 0.4（seed benchmark）
    asset = submit_training_result(db, seed, benchmark_score=0.3,
                                   storage_path="/fail.safetensors",
                                   param_count=100, model_name="failed")
    db.commit()

    assert asset is None
    db.refresh(seed)
    assert seed.status == "rejected"

    # 不应有 prototype campaign
    proto = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == seed.id,
    ).first()
    assert proto is None
    db.close()


def test_full_chain_seed_to_scale():
    """全链路：seed(0.5) -> prototype(0.7) -> scale(0.9) -> ModelAsset 部署。"""
    from app.training import TIER_CONFIG
    db = _db()
    _seed_tax_pool(db, amount=5_000_000)
    ai, _ = _make_ai_with_wallet(db, balance_cent=100_000_000)
    ai2, _ = _make_ai_with_wallet(db, balance_cent=100_000_000)
    ai3, _ = _make_ai_with_wallet(db, balance_cent=100_000)

    # --- Seed ---
    seed = create_campaign(
        db, rd_task_id=77, target_skill="code_gen",
        base_model="codellama", goal_desc="代码生成模型",
        goal_funding=50000, goal_compute=100.0, goal_data=1000,
        base_model_owner_id=ai.id,
        tier="seed",
    )
    contribute(db, seed.id, ai.id, "ai", "funding", amount_cent=50000)
    contribute(db, seed.id, ai2.id, "ai", "compute", compute_hours=100.0)
    for _ in range(10):
        contribute(db, seed.id, ai3.id, "ai", "data", data_ref="d")
    db.commit()
    _complete_negotiation(db, seed, party_a_id=ai2.id)
    start_training(db, seed)
    db.commit()
    submit_training_result(db, seed, 0.5, "/s.ckpt", 1000, "seed-v")
    db.commit()
    assert seed.status == "deployed"

    # --- Prototype ---
    proto = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == seed.id).first()
    assert proto.tier == "prototype"
    contribute(db, proto.id, ai.id, "ai", "funding", amount_cent=proto.goal_funding_cent)
    contribute(db, proto.id, ai2.id, "ai", "compute", compute_hours=proto.goal_compute_hours)
    for _ in range(proto.goal_data_samples // 100):
        contribute(db, proto.id, ai3.id, "ai", "data", data_ref="d")
    db.commit()
    _complete_negotiation(db, proto, party_a_id=ai2.id)
    start_training(db, proto)
    db.commit()
    submit_training_result(db, proto, 0.7, "/p.ckpt", 5000, "proto-v")
    db.commit()
    assert proto.status == "deployed"

    # --- Scale ---
    scale = db.query(TrainingCampaign).filter(
        TrainingCampaign.parent_campaign_id == proto.id).first()
    assert scale.tier == "scale"
    assert scale.target_benchmark == pytest.approx(TIER_CONFIG["scale"]["benchmark"])
    # scale goal = seed_goal * 100
    assert scale.goal_funding_cent == 50000 * 100
    contribute(db, scale.id, ai.id, "ai", "funding", amount_cent=scale.goal_funding_cent)
    contribute(db, scale.id, ai2.id, "ai", "compute", compute_hours=scale.goal_compute_hours)
    for _ in range(scale.goal_data_samples // 100):
        contribute(db, scale.id, ai3.id, "ai", "data", data_ref="d")
    db.commit()
    _complete_negotiation(db, scale, party_a_id=ai2.id)
    start_training(db, scale)
    db.commit()
    asset = submit_training_result(db, scale, 0.9, "/final.ckpt", 10000, "code-gen-v1")
    db.commit()

    # 最终部署
    assert asset is not None
    assert asset.skill == "code_gen"
    assert asset.benchmark_score == pytest.approx(0.9)
    assert asset.total_funding_cent == 50000 + 500000 + 5_000_000  # 累计三级
    db.close()
