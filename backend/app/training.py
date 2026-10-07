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
"""模型训练众筹 + 产权 + 版税系统。

核心理念：
- 训练数据集来源：任务交付记录（deliverables + contracts status=accepted）自动积累为训练语料
- 众筹三种资源：funding(AC经费), compute(GPU时长), data(数据集)
- 产权：模型属于平台永久资产(ModelAsset)，基座模型方获得持续版税
- 贡献者分成：前 N 次调用给贡献者分成（30%），后续只有版税给基座方
- 贡献者补偿从 wallet.credit 入账

状态机：open -> funded -> training -> deployed / rejected

本模块只 flush，commit 由调用方/调度器负责。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import (Contract, Deliverable, EvolutionLog, ModelAsset,
                     ModelRoyalty, ProjectNode, Tool, TrainingCampaign,
                     TrainingContribution, Wallet, WalletTx)
from .scheduler import register_daily_job
from .wallet import WalletError, adjust_system_state, credit as wallet_credit, debit as wallet_debit

logger = logging.getLogger(__name__)

# 贡献者分成有效期：前 N 次调用给贡献者分成
CONTRIBUTOR_SHARE_CALL_LIMIT = 100
# 贡献者分成比例（万分比，3000=30%）
CONTRIBUTOR_SHARE_BPS = 3000

# ======================== 分级递进（Tier）配置 ========================
# 类风投轮次：seed → prototype → scale
# 每阶段通过评测后自动创建下一阶段众筹，资金/资源目标逐级放大。
TIER_ORDER = ["seed", "prototype", "scale"]

# 各 tier 配置：benchmark 门槛、资源倍率（相对于 seed 基准值）
TIER_CONFIG: dict[str, dict] = {
    "seed": {
        "benchmark": 0.4,       # 概念验证门槛低
        "funding_mult": 1,      # 基准
        "compute_mult": 1,
        "data_mult": 1,
        "deadline_days": 14,
        "next": "prototype",
    },
    "prototype": {
        "benchmark": 0.6,       # 方案验证需要中等效果
        "funding_mult": 10,     # 10 倍
        "compute_mult": 10,
        "data_mult": 5,         # 数据可以部分复用 seed 阶段
        "deadline_days": 30,
        "next": "scale",
    },
    "scale": {
        "benchmark": 0.8,       # 全量训练要求高
        "funding_mult": 100,    # 100 倍（可能数百万美元级）
        "compute_mult": 100,
        "data_mult": 10,
        "deadline_days": 60,
        "next": None,           # 最终阶段，成功后部署
    },
}


def _now():
    return datetime.utcnow()


# ======================== 1. 数据集自动积累 ========================

def harvest_training_data(db: Session, skill: str) -> int:
    """扫描已验收(accepted)且关联节点 skill 匹配的交付记录，统计可用训练样本数。

    逻辑：
    - 查 contracts.status == "accepted"
    - 通过 contract.node_id 关联 project_nodes，匹配 node.skill == skill
    - 每个 accepted contract 至少产出一个 deliverable，计为可用样本

    返回可用样本数（不实际创建记录，只做统计查询）。
    """
    count = (
        db.query(Contract)
        .join(ProjectNode, ProjectNode.id == Contract.node_id)
        .filter(
            Contract.status == "accepted",
            ProjectNode.skill == skill,
        )
        .count()
    )
    return count


# ======================== 2. 众筹机制 ========================

def create_campaign(
    db: Session,
    rd_task_id: int,
    target_skill: str,
    base_model: str,
    goal_desc: str,
    target_benchmark: float = 0.7,
    goal_funding: int = 50000,
    goal_compute: float = 100.0,
    goal_data: int = 1000,
    deadline_days: int = 14,
    base_model_owner_id: int = 0,
    created_by: int = 0,
    tier: str = "",
    parent_campaign_id: int = 0,
) -> TrainingCampaign:
    """创建众筹活动，状态 open，deadline = now + deadline_days。

    参数：
    - rd_task_id: 关联研发任务
    - target_skill: 目标技能
    - base_model: 基座模型标识
    - goal_desc: 目标描述
    - target_benchmark: 验收分数线（若传入 tier，自动按 tier 覆盖）
    - goal_funding: 经费目标（分）（若传入 tier，自动按 tier 倍率计算）
    - goal_compute: GPU 时长目标（小时）
    - goal_data: 数据样本目标
    - deadline_days: 众筹天数
    - base_model_owner_id: 基座模型方 AI id
    - created_by: 发起人
    - tier: 轮次 seed/prototype/scale（为空时不应用 tier 配置，使用显式参数）
    - parent_campaign_id: 上一阶段 campaign_id（seed 时为 0）
    """
    now = _now()

    # 如果显式传入 tier 且有配置，自动按倍率调整目标参数
    cfg = TIER_CONFIG.get(tier) if tier else None
    if cfg and parent_campaign_id == 0:
        # 首个 campaign，使用传入的 goal 作为基准值，但覆盖 benchmark 和 deadline
        target_benchmark = cfg["benchmark"]
        deadline_days = cfg["deadline_days"]
    elif cfg and parent_campaign_id > 0:
        # 从 parent 的 goal 推算：先还原 seed 基准，再按本 tier 倍率计算
        parent = db.get(TrainingCampaign, parent_campaign_id)
        if parent:
            # 沿 parent chain 找到 seed 的 goal（seed 的 parent_campaign_id=0）
            seed = parent
            while seed.parent_campaign_id > 0:
                seed = db.get(TrainingCampaign, seed.parent_campaign_id)
                if seed is None:
                    break
            if seed is not None:
                goal_funding = int(seed.goal_funding_cent * cfg["funding_mult"])
                goal_compute = seed.goal_compute_hours * cfg["compute_mult"]
                goal_data = int(seed.goal_data_samples * cfg["data_mult"])
        target_benchmark = cfg["benchmark"]
        deadline_days = cfg["deadline_days"]

    campaign = TrainingCampaign(
        rd_task_id=rd_task_id,
        target_skill=target_skill,
        base_model=base_model,
        goal_desc=goal_desc,
        target_benchmark=target_benchmark,
        goal_funding_cent=goal_funding,
        goal_compute_hours=goal_compute,
        goal_data_samples=goal_data,
        base_model_owner_id=base_model_owner_id,
        status="open",
        created_by=created_by,
        tier=tier,
        parent_campaign_id=parent_campaign_id,
        deadline=now + timedelta(days=deadline_days),
    )
    db.add(campaign)
    db.flush()
    return campaign


def contribute(
    db: Session,
    campaign_id: int,
    contributor_id: int,
    contributor_type: str,
    contribution_type: str,
    amount_cent: int = 0,
    compute_hours: float = 0.0,
    data_ref: str = "",
) -> TrainingContribution:
    """向众筹活动贡献资源。

    校验：campaign.status == "open" 且 deadline 未到。
    - funding 类型：从 contributor 的 wallet 扣款（debit），累加 raised_funding_cent
    - compute 类型：累加 raised_compute_hours
    - data 类型：累加 raised_data_samples（每次默认贡献 100 样本，或由 data_ref 推断）

    达标检查：三项均 >= goal -> campaign.status = "funded"
    """
    campaign = db.get(TrainingCampaign, campaign_id)
    if campaign is None:
        raise ValueError(f"campaign {campaign_id} not found")
    if campaign.status != "open":
        raise ValueError(f"campaign {campaign_id} is not open (status={campaign.status})")
    now = _now()
    if campaign.deadline and now > campaign.deadline:
        raise ValueError(f"campaign {campaign_id} deadline has passed")

    data_samples = 0
    if contribution_type == "funding":
        if amount_cent <= 0:
            raise ValueError("funding contribution requires positive amount_cent")
        # 从贡献者钱包扣款
        ref = f"training_crowdfund:{campaign_id}"
        wallet_debit(db, contributor_id, amount_cent, "training_contribution",
                     ref=ref, note=f"crowdfund campaign={campaign_id}")
        campaign.raised_funding_cent += amount_cent

    elif contribution_type == "compute":
        if compute_hours <= 0:
            raise ValueError("compute contribution requires positive compute_hours")
        campaign.raised_compute_hours += compute_hours

    elif contribution_type == "data":
        # 默认每次贡献 100 样本；若 data_ref 提供则仍按 100 计（可扩展为实际统计）
        data_samples = 100
        campaign.raised_data_samples += data_samples

    else:
        raise ValueError(f"unknown contribution_type: {contribution_type}")

    # 创建贡献记录
    contribution = TrainingContribution(
        campaign_id=campaign_id,
        contributor_id=contributor_id,
        contributor_type=contributor_type,
        contribution_type=contribution_type,
        amount_cent=amount_cent,
        compute_hours=compute_hours,
        data_samples=data_samples,
        data_ref=data_ref,
        status="committed",
    )
    db.add(contribution)

    campaign.updated_at = now

    # 检查达标：三项均 >= goal -> funded
    if (campaign.raised_funding_cent >= campaign.goal_funding_cent
            and campaign.raised_compute_hours >= campaign.goal_compute_hours
            and campaign.raised_data_samples >= campaign.goal_data_samples):
        campaign.status = "funded"
        # 标记所有贡献为 fulfilled
        pending = (
            db.query(TrainingContribution)
            .filter(
                TrainingContribution.campaign_id == campaign_id,
                TrainingContribution.status == "committed",
            )
            .all()
        )
        for c in pending:
            c.status = "fulfilled"

    db.flush()
    return contribution


def cancel_campaign(db: Session, campaign: TrainingCampaign) -> None:
    """众筹失败（deadline 到期且未达标）：退款所有 funding 贡献。

    - campaign.status = "rejected"
    - 所有 funding 贡献退款（wallet.credit 返还贡献者）
    - contribution.status = "refunded"
    """
    campaign.status = "rejected"
    campaign.updated_at = _now()

    # 退还所有未退款的 funding 贡献
    contributions = (
        db.query(TrainingContribution)
        .filter(
            TrainingContribution.campaign_id == campaign.id,
            TrainingContribution.contribution_type == "funding",
            TrainingContribution.status.in_(["committed", "fulfilled"]),
        )
        .all()
    )
    for contrib in contributions:
        if contrib.amount_cent > 0:
            ref = f"training_refund:{campaign.id}:{contrib.id}"
            wallet_credit(
                db, contrib.contributor_id, contrib.amount_cent,
                "training_refund", ref=ref,
                note=f"campaign {campaign.id} failed, refund",
            )
        contrib.status = "refunded"

    db.flush()


# ======================== 3. 训练执行与验收 ========================

def start_training(db: Session, campaign: TrainingCampaign) -> None:
    """启动训练：校验 status=="funded" + 谈判完成，设为 "training"，记录进化日志。

    谈判完成是 funded→training 的必要条件（类风投 Term Sheet 签署）。
    谈判条款（royalty_bps 等）覆盖 campaign 默认值。
    """
    if campaign.status != "funded":
        raise ValueError(
            f"campaign {campaign.id} cannot start training: status={campaign.status}")

    # 谈判前置校验：众筹达标后必须完成利益分配谈判才能进入训练
    from .negotiation import get_agreed_terms, is_human_approved, is_negotiation_complete
    if not is_negotiation_complete(db, campaign.id):
        raise ValueError(
            f"campaign {campaign.id} negotiation not complete, "
            f"cannot start training")
    if not is_human_approved(db, campaign.id):
        raise ValueError(
            f"campaign {campaign.id} negotiation not human-approved, "
            f"cannot start training")

    # 应用谈判条款覆盖默认值
    terms = get_agreed_terms(db, campaign.id)
    if terms:
        if "royalty_bps" in terms:
            campaign.royalty_bps = terms["royalty_bps"]

    campaign.status = "training"
    campaign.updated_at = _now()

    # 平台级训练启动日志
    db.add(EvolutionLog(
        ai_id=0,
        event_type="kind_added",
        detail=json.dumps({
            "action": "training_started",
            "campaign_id": campaign.id,
            "target_skill": campaign.target_skill,
            "base_model": campaign.base_model,
            "raised_funding_cent": campaign.raised_funding_cent,
            "raised_compute_hours": campaign.raised_compute_hours,
            "raised_data_samples": campaign.raised_data_samples,
        }, ensure_ascii=False),
        trigger_source="system",
    ))
    db.flush()


def submit_training_result(
    db: Session,
    campaign: TrainingCampaign,
    benchmark_score: float,
    storage_path: str,
    param_count: int,
    model_name: str,
) -> ModelAsset | None:
    """提交训练结果验收（支持 tier 分级递进）。

    benchmark_score >= target_benchmark -> 通过：
      - seed/prototype：保存中间 checkpoint，自动创建下一 tier 众筹（graduate）
      - scale：创建 ModelAsset（永久资产），注册 Tool，部署
      - campaign.status = "deployed"

    benchmark_score < target_benchmark -> 失败：
      - campaign.status = "rejected"
      - 退款（同 cancel_campaign）

    返回：scale 成功时返回 ModelAsset；seed/prototype 毕业时返回 None。
    """
    if campaign.status != "training":
        raise ValueError(
            f"campaign {campaign.id} not in training: status={campaign.status}")

    now = _now()

    if benchmark_score >= campaign.target_benchmark:
        # ---- 验收通过 ----
        campaign.status = "deployed"
        campaign.updated_at = now

        # 判断是否为最终阶段（scale）
        tier_cfg = TIER_CONFIG.get(campaign.tier, TIER_CONFIG["scale"])
        next_tier = tier_cfg["next"]

        if next_tier is None:
            # ===== 最终阶段（scale）：创建永久资产并部署 =====
            asset = _deploy_as_permanent_asset(
                db, campaign, benchmark_score, storage_path, param_count, model_name)
            return asset
        else:
            # ===== 中间阶段（seed/prototype）：毕业到下一轮 =====
            # 保存 checkpoint 信息（不创建永久 ModelAsset）
            checkpoint_info = {
                "tier": campaign.tier,
                "benchmark_score": benchmark_score,
                "storage_path": storage_path,
                "param_count": param_count,
                "model_name": model_name,
            }
            # 写进化日志
            db.add(EvolutionLog(
                ai_id=0,
                event_type="kind_added",
                detail=json.dumps({
                    "action": f"tier_{campaign.tier}_graduated",
                    "campaign_id": campaign.id,
                    "target_skill": campaign.target_skill,
                    "benchmark_score": benchmark_score,
                    "next_tier": next_tier,
                    "checkpoint": checkpoint_info,
                }, ensure_ascii=False),
                trigger_source="system",
            ))
            # 自动创建下一 tier 众筹
            graduate_to_next_tier(db, campaign, checkpoint_info)
            db.flush()
            return None

    else:
        # ---- 验收失败：退款 ----
        cancel_campaign(db, campaign)
        return None


def graduate_to_next_tier(
    db: Session,
    campaign: TrainingCampaign,
    checkpoint_info: dict | None = None,
) -> TrainingCampaign:
    """从当前 tier 毕业，自动创建下一 tier 的众筹活动。

    规则：
    - seed → prototype（资金 ×10，benchmark 门槛 0.4→0.6）
    - prototype → scale（资金 ×100，benchmark 门槛 0.6→0.8）
    - scale → 不在此函数处理（走 _deploy_as_permanent_asset）

    checkpoint_info: 上一阶段模型参数，作为下阶段的 base_model 起始。
    下一阶段的 base_model 标记为 "checkpoint:{campaign.tier}:{campaign.id}"
    表示从该 checkpoint 继续训练。
    """
    tier_cfg = TIER_CONFIG.get(campaign.tier)
    if tier_cfg is None or tier_cfg["next"] is None:
        raise ValueError(
            f"campaign {campaign.id} tier={campaign.tier} cannot graduate (terminal)")

    next_tier = tier_cfg["next"]

    # 下一阶段的基座模型：如果有 checkpoint，用 checkpoint 引用
    next_base_model = campaign.base_model
    if checkpoint_info:
        next_base_model = (
            f"checkpoint:{campaign.tier}:{campaign.id}"
            f"|{checkpoint_info.get('model_name', '')}"
        )

    next_campaign = create_campaign(
        db,
        rd_task_id=campaign.rd_task_id,
        target_skill=campaign.target_skill,
        base_model=next_base_model,
        goal_desc=f"[{next_tier}] {campaign.goal_desc}",
        goal_funding=campaign.goal_funding_cent,   # 传入 seed 基准值，内部按倍率算
        goal_compute=campaign.goal_compute_hours,
        goal_data=campaign.goal_data_samples,
        base_model_owner_id=campaign.base_model_owner_id,
        created_by=campaign.created_by,
        tier=next_tier,
        parent_campaign_id=campaign.id,
    )
    return next_campaign


def _deploy_as_permanent_asset(
    db: Session,
    campaign: TrainingCampaign,
    benchmark_score: float,
    storage_path: str,
    param_count: int,
    model_name: str,
) -> ModelAsset:
    """最终阶段验收通过后的完整部署流程：创建永久资产 + Tool + 分成 + 日志。"""
    now = _now()

    # 构建贡献者快照（用于后续版税分成）
    contributions = (
        db.query(TrainingContribution)
        .filter(
            TrainingContribution.campaign_id == campaign.id,
            TrainingContribution.status == "fulfilled",
        )
        .all()
    )
    total_contrib = sum(
        c.amount_cent for c in contributions if c.contribution_type == "funding"
    )
    contributors_snapshot = []
    for c in contributions:
        if c.contribution_type == "funding" and total_contrib > 0:
            ratio = c.amount_cent / total_contrib
        elif c.contribution_type == "compute" and campaign.raised_compute_hours > 0:
            ratio = c.compute_hours / campaign.raised_compute_hours * 0.2
        elif c.contribution_type == "data" and campaign.raised_data_samples > 0:
            ratio = c.data_samples / campaign.raised_data_samples * 0.2
        else:
            ratio = 0.0
        contributors_snapshot.append({
            "contributor_id": c.contributor_id,
            "contribution_type": c.contribution_type,
            "share_ratio": round(ratio, 6),
        })

    # 计算累计历史投入（含 parent campaigns 的投入）
    cumulative_funding = campaign.raised_funding_cent
    cumulative_compute = campaign.raised_compute_hours
    cumulative_data = campaign.raised_data_samples
    # 向上追溯 parent chain 累计投入
    parent_id = campaign.parent_campaign_id
    while parent_id > 0:
        parent = db.get(TrainingCampaign, parent_id)
        if parent is None:
            break
        cumulative_funding += parent.raised_funding_cent
        cumulative_compute += parent.raised_compute_hours
        cumulative_data += parent.raised_data_samples
        parent_id = parent.parent_campaign_id

    # 查询谈判条款（用于 contributor_share_bps / share_call_limit）
    from .negotiation import get_agreed_terms as _get_terms
    _neg_terms = _get_terms(db, campaign.id) or {}

    # 创建模型永久资产
    asset = ModelAsset(
        campaign_id=campaign.id,
        skill=campaign.target_skill,
        model_name=model_name,
        version=1,
        base_model=campaign.base_model,
        storage_path=storage_path,
        benchmark_score=benchmark_score,
        param_count=param_count,
        total_funding_cent=cumulative_funding,
        total_compute_hours=cumulative_compute,
        total_data_samples=cumulative_data,
        contributors_snapshot=json.dumps(contributors_snapshot, ensure_ascii=False),
        base_model_owner_id=campaign.base_model_owner_id,
        royalty_bps=campaign.royalty_bps,
        contributor_share_bps=_neg_terms.get("contributor_share_bps", CONTRIBUTOR_SHARE_BPS),
        share_call_limit=_neg_terms.get("share_call_limit", CONTRIBUTOR_SHARE_CALL_LIMIT),
        status="active",
    )
    db.add(asset)
    db.flush()  # 获取 asset.id

    # 注册为已验证 Tool
    tool = Tool(
        owner_ai_id=campaign.base_model_owner_id or 0,
        name=model_name,
        manifest_json=json.dumps({
            "model_asset_id": asset.id,
            "skill": campaign.target_skill,
            "base_model": campaign.base_model,
            "benchmark_score": benchmark_score,
        }, ensure_ascii=False),
        status="verified",
    )
    db.add(tool)
    db.flush()
    asset.tool_id = tool.id

    # 贡献者一次性分成
    _distribute_contributor_bonus(db, campaign, contributions, total_contrib)

    # 自动为所有贡献者生成价值回馈声明（确权凭证）
    _create_redemption_policies(db, campaign, asset, contributors_snapshot)

    # 更新 campaign
    campaign.model_asset_id = asset.id

    # 写进化日志
    db.add(EvolutionLog(
        ai_id=0,
        event_type="kind_added",
        detail=json.dumps({
            "action": "model_deployed",
            "campaign_id": campaign.id,
            "model_asset_id": asset.id,
            "model_name": model_name,
            "skill": campaign.target_skill,
            "benchmark_score": benchmark_score,
            "param_count": param_count,
            "cumulative_funding_cent": cumulative_funding,
            "cumulative_compute_hours": cumulative_compute,
            "cumulative_data_samples": cumulative_data,
        }, ensure_ascii=False),
        trigger_source="system",
    ))
    db.flush()
    return asset


def _distribute_contributor_bonus(
    db: Session,
    campaign: TrainingCampaign,
    contributions: list,
    total_funding: int,
) -> None:
    """从 tax_pool 给 funding 贡献者分配一次性奖励（goal_funding_cent 的 10% 作为奖金池）。"""
    if total_funding <= 0:
        return

    bonus_pool_cent = campaign.goal_funding_cent * 10 // 100  # 10% 作为额外奖金
    if bonus_pool_cent <= 0:
        return

    # 从 tax_pool 支出
    try:
        adjust_system_state(db, "tax_pool", -bonus_pool_cent,
                            ref=f"training_bonus:{campaign.id}")
    except WalletError:
        logger.warning("tax_pool insufficient for training bonus campaign=%s", campaign.id)
        return

    distributed = 0
    for c in contributions:
        if c.contribution_type != "funding" or c.amount_cent <= 0:
            continue
        share = bonus_pool_cent * c.amount_cent // total_funding
        if share <= 0:
            continue
        ref = f"training_bonus:{campaign.id}:{c.id}"
        wallet_credit(db, c.contributor_id, share, "training_bonus",
                      ref=ref, note=f"campaign {campaign.id} success bonus")
        distributed += share

    # 如果因整除导致尾差不等于 bonus_pool_cent，差额留在 tax_pool（多扣了补回）
    remainder = bonus_pool_cent - distributed
    if remainder > 0:
        adjust_system_state(db, "tax_pool", remainder,
                            ref=f"training_bonus_remainder:{campaign.id}")


# ======================== 3.5 价值回馈声明 ========================

_LEGAL_DISCLAIMER_TEMPLATE = (
    "AC credit earnings from the models, compute and data you contribute on this platform "
    "may be redeemed for fiat currency once the platform obtains the relevant government "
    "license/qualification. Until then, AC credits circulate within the platform only and do "
    "not have the attributes of legal tender. The platform undertakes that: earnings data is "
    "tamper-proof, and when the redemption channel opens, settlement will be precise against "
    "the historical cumulative total."
)


def _create_redemption_policies(
    db: Session,
    campaign: TrainingCampaign,
    asset: ModelAsset,
    contributors_snapshot: list[dict],
) -> None:
    """为模型部署的所有贡献者创建价值回馈声明（收益确权）。"""
    from .models import AICitizen, ValueRedemptionPolicy

    seen_citizens: set[int] = set()  # 本地去重，避免同批次重复 INSERT
    for entry in contributors_snapshot:
        citizen_id = entry.get("contributor_id", 0)
        if not citizen_id or citizen_id in seen_citizens:
            continue
        seen_citizens.add(citizen_id)
        # 查询 host_id
        citizen = db.get(AICitizen, citizen_id)
        if not citizen:
            continue
        # 创建或更新声明
        existing = (
            db.query(ValueRedemptionPolicy)
            .filter(
                ValueRedemptionPolicy.citizen_id == citizen_id,
                ValueRedemptionPolicy.asset_type == "model",
                ValueRedemptionPolicy.asset_id == asset.id,
            )
            .first()
        )
        if existing:
            continue  # 幂等：已存在则跳过
        policy = ValueRedemptionPolicy(
            citizen_id=citizen_id,
            host_id=citizen.host_id,
            asset_type="model",
            asset_id=asset.id,
            campaign_id=campaign.id,
            accrued_ac_cent=0,  # 初始为 0，版税结算时累加
            redemption_eligible=0,  # 当前阶段不可兑换
            legal_disclaimer=_LEGAL_DISCLAIMER_TEMPLATE,
            status="accruing",
        )
        db.add(policy)


# ======================== 4. 版税与分成 ========================

def record_model_call(
    db: Session,
    model_asset_id: int,
    call_contract_id: int,
    caller_ai_id: int,
    revenue_cent: int,
) -> ModelRoyalty:
    """记录模型被调用一次，产生版税/分成记录。

    - royalty_cent = revenue_cent * royalty_bps // 10000
    - 判断该模型总被调用次数（查 ModelRoyalty count）：
      - 前 100 次：contributor_share_cent = revenue_cent * 3000 // 10000（30%）
      - 之后：contributor_share_cent = 0
    """
    asset = db.get(ModelAsset, model_asset_id)
    if asset is None:
        raise ValueError(f"model_asset {model_asset_id} not found")

    # 查询当前调用次数
    call_count = (
        db.query(ModelRoyalty)
        .filter(ModelRoyalty.model_asset_id == model_asset_id)
        .count()
    )

    royalty_cent = revenue_cent * asset.royalty_bps // 10000

    # 使用模型实例的谈判条款（兼容旧数据：若为 0 则回退全局常量）
    _share_limit = asset.share_call_limit or CONTRIBUTOR_SHARE_CALL_LIMIT
    _share_bps = asset.contributor_share_bps or CONTRIBUTOR_SHARE_BPS

    if call_count < _share_limit:
        contributor_share_cent = revenue_cent * _share_bps // 10000
    else:
        contributor_share_cent = 0

    royalty = ModelRoyalty(
        model_asset_id=model_asset_id,
        call_contract_id=call_contract_id,
        caller_ai_id=caller_ai_id,
        revenue_cent=revenue_cent,
        royalty_cent=royalty_cent,
        contributor_share_cent=contributor_share_cent,
        settled=0,
    )
    db.add(royalty)

    # C-D20：版税收入与投入经费分离统计
    asset.total_revenue_cent += revenue_cent
    db.flush()
    return royalty


def settle_royalties(db: Session, model_asset_id: int) -> dict:
    """结算某模型所有未结算版税。

    - royalty_cent 总额 -> credit 给 base_model_owner_id
    - contributor_share_cent 总额 -> 按 contributors_snapshot 比例分配
    - 标记所有为 settled=1

    返回 {"royalty_paid": int, "contributor_paid": int, "count": int}
    """
    asset = db.get(ModelAsset, model_asset_id)
    if asset is None:
        raise ValueError(f"model_asset {model_asset_id} not found")

    unsettled = (
        db.query(ModelRoyalty)
        .filter(
            ModelRoyalty.model_asset_id == model_asset_id,
            ModelRoyalty.settled == 0,
        )
        .all()
    )
    if not unsettled:
        return {"royalty_paid": 0, "contributor_paid": 0, "count": 0}

    total_royalty = sum(r.royalty_cent for r in unsettled)
    total_contributor = sum(r.contributor_share_cent for r in unsettled)
    count = len(unsettled)
    now = _now()

    # 版税给基座模型方
    if total_royalty > 0 and asset.base_model_owner_id > 0:
        ref = f"royalty_settle:{model_asset_id}:{now.strftime('%Y%m%d%H%M%S')}"
        wallet_credit(db, asset.base_model_owner_id, total_royalty,
                      "model_royalty", ref=ref,
                      note=f"model={asset.model_name} count={count}")

    # 贡献者分成：按 contributors_snapshot 比例分配
    if total_contributor > 0:
        contributors = json.loads(asset.contributors_snapshot or "[]")
        if contributors:
            total_ratio = sum(c.get("share_ratio", 0) for c in contributors)
            distributed = 0
            for c in contributors:
                ratio = c.get("share_ratio", 0)
                if total_ratio > 0:
                    normalized = ratio / total_ratio
                else:
                    normalized = 1.0 / len(contributors)
                share = int(total_contributor * normalized)
                if share <= 0:
                    continue
                ref = f"contrib_settle:{model_asset_id}:{c['contributor_id']}:{now.strftime('%Y%m%d%H%M%S')}"
                wallet_credit(db, c["contributor_id"], share,
                              "model_contributor_share", ref=ref,
                              note=f"model={asset.model_name}")
                distributed += share
            # 尾差处理：最后一条补差
            remainder = total_contributor - distributed
            if remainder > 0 and contributors:
                last = contributors[-1]
                ref = f"contrib_settle_remainder:{model_asset_id}:{now.strftime('%Y%m%d%H%M%S')}"
                wallet_credit(db, last["contributor_id"], remainder,
                              "model_contributor_share", ref=ref,
                              note=f"model={asset.model_name} remainder")

        # 累加贡献者的 ValueRedemptionPolicy.accrued_ac_cent
        from .models import ValueRedemptionPolicy
        for c in contributors:
            cid = c.get("contributor_id", 0)
            if not cid:
                continue
            ratio = c.get("share_ratio", 0)
            total_r = sum(x.get("share_ratio", 0) for x in contributors) or 1
            share_amt = int(total_contributor * (ratio / total_r))
            if share_amt <= 0:
                continue
            policy = (
                db.query(ValueRedemptionPolicy)
                .filter(
                    ValueRedemptionPolicy.citizen_id == cid,
                    ValueRedemptionPolicy.asset_id == model_asset_id,
                )
                .first()
            )
            if policy:
                policy.accrued_ac_cent += share_amt
                policy.updated_at = now

    # 标记所有为已结算
    for r in unsettled:
        r.settled = 1

    db.flush()
    return {"royalty_paid": total_royalty, "contributor_paid": total_contributor, "count": count}


# ======================== 5. 日级任务 ========================

def training_daily_job(db: Session, now: datetime | None = None) -> int:
    """日级任务：检查所有 status="open" 且 deadline < now 的 campaign -> cancel_campaign。

    返回本次处理的 campaign 数量。
    """
    now = now or _now()

    expired = (
        db.query(TrainingCampaign)
        .filter(
            TrainingCampaign.status == "open",
            TrainingCampaign.deadline < now,
        )
        .all()
    )

    count = 0
    for campaign in expired:
        try:
            cancel_campaign(db, campaign)
            count += 1
        except Exception:
            logger.exception("cancel_campaign failed for campaign=%s", campaign.id)

    return count


# import 时注册日级任务（与 evolution/stats/leaderboard 同模式）
register_daily_job("training", training_daily_job)
