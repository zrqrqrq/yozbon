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
"""模型推理计费 + 版税自动结算闭环。

核心函数：
- log_usage(): 记录一次推理调用，自动计算 cost + royalty 并分账
- get_or_create_pricing(): 获取/初始化模型推理定价
- estimate_cost(): 预估费用（无需真实调用）

版税结算：每次 log_usage 自动将 royalty 部分计入 ModelRoyalty，
使 settle_royalties 的输入不再是外部假设，而是推理日志的累计。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import (
    AICitizen, InferencePricing, ModelAsset, ModelRoyalty,
    ModelUsageLog, SystemState,
)
from . import wallet as wallet_mod


def _now() -> datetime:
    return datetime.utcnow()


# ==================== 定价 ====================

def get_or_create_pricing(db: Session, asset_id: int) -> InferencePricing:
    """获取模型推理定价，无则创建默认（per_token, 1/3 分/1k）。"""
    p = db.query(InferencePricing).filter(InferencePricing.asset_id == asset_id).first()
    if p:
        return p
    p = InferencePricing(asset_id=asset_id)
    db.add(p)
    db.flush()
    return p


def estimate_cost(pricing: InferencePricing, tokens_in: int, tokens_out: int,
                  discount_bps: int = 0) -> int:
    """预估费用（分）。

    discount_bps：承诺质押折扣（基点，万分之一）。e5：质押 AC 越高算力越便宜，
    折后费用 = 原价 ×(10000-bps)/10000，仍保底 1 分（per_token/per_call）。
    """
    discount_bps = max(0, int(discount_bps or 0))
    if pricing.model_mode == "per_token":
        cost = (tokens_in * pricing.price_in_per_1k_cent +
                tokens_out * pricing.price_out_per_1k_cent) // 1000
        cost = cost * (10000 - discount_bps) // 10000
        return max(cost, 1)  # 至少 1 分
    elif pricing.model_mode == "per_call":
        cost = pricing.price_per_call_cent * (10000 - discount_bps) // 10000
        return max(cost, 1)
    else:
        return 0  # subscription 模式不逐次计费


# ==================== 核心：记录使用 + 自动版税 ====================

def log_usage(
    db: Session,
    asset_id: int,
    caller_id: int,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int = 0,
    caller_type: str = "ai",
    discount_bps: int = 0,
) -> dict:
    """记录一次模型推理调用，自动扣费 + 版税结算。

    discount_bps：e5 承诺质押折扣（基点，由调用方按 caller 的活跃质押传入）。
      - 费用按折后计；版税基数默认也按折后（COMPUTE_DISCOUNT_APPLIES_TO_ROYALTY=1）。
      - 计费幂等 ref 由调用方（compute.meter_inference）保证唯一（含 usage 序号），
        本函数不自加 ref，重复保护由上层 ref 承担。

    Returns: {"usage_id", "cost_cent", "royalty_cent", "net_income_cent", "discount_bps"}
    """
    asset = db.get(ModelAsset, asset_id)
    if not asset or asset.status != "active":
        raise ValueError(f"asset {asset_id} not found or not active")

    discount_bps = max(0, int(discount_bps or 0))
    event_ref = uuid.uuid4().hex            # 本次推理唯一标识（扣费/版税 ref 后缀，保证多次合法计费互不误拦）
    pricing = get_or_create_pricing(db, asset_id)
    gross = estimate_cost(pricing, tokens_in, tokens_out, discount_bps=0)   # 原价（观测/审计）
    cost = estimate_cost(pricing, tokens_in, tokens_out, discount_bps=discount_bps)  # 折后实收
    # 版税基数：折后 or 原价（公平计费默认按折后）
    royalty_base = cost if settings.COMPUTE_DISCOUNT_APPLIES_TO_ROYALTY else gross
    royalty = royalty_base * pricing.royalty_bps // 10000

    # 扣费（从 caller 钱包扣除 cost）
    if cost > 0 and caller_type == "ai":
        try:
            wallet_mod.debit(
                db, caller_id, cost, "推理消费",
                ref=f"inference:{asset_id}:{event_ref}", note=f"gross={gross} disc_bps={discount_bps}",
            )
        except Exception as e:
            # 余额不足 → 记录失败日志但不阻断
            log = ModelUsageLog(
                asset_id=asset_id, caller_id=caller_id, caller_type=caller_type,
                tokens_in=tokens_in, tokens_out=tokens_out,
                cost_cent=0, royalty_cent=0, latency_ms=latency_ms,
                status="error",
            )
            db.add(log)
            db.flush()
            raise ValueError(f"caller insufficient balance: {e}")

    # 记录日志
    log = ModelUsageLog(
        asset_id=asset_id, caller_id=caller_id, caller_type=caller_type,
        tokens_in=tokens_in, tokens_out=tokens_out,
        cost_cent=cost, royalty_cent=royalty, latency_ms=latency_ms,
        status="success",
    )
    db.add(log)

    # 自动版税入账：将 royalty 分配到 ModelRoyalty
    # event_ref 每次调用唯一（uuid），使同一资产多次推理的版税入账不被唯一索引误拦。
    if royalty > 0:
        _credit_royalty(db, asset, royalty, event_ref=event_ref)

    db.flush()
    return {
        "usage_id": log.id,
        "cost_cent": cost,
        "royalty_cent": royalty,
        "net_income_cent": cost - royalty,
        "gross_cent": gross,
        "discount_bps": discount_bps,
    }


def _credit_royalty(db: Session, asset: ModelAsset, royalty_cent: int, event_ref: str = ""):
    """将版税分配到基座模型方 + 贡献者（按 contributor_share_bps）。

    event_ref 使每次推理事件的版税入账 ref 唯一（同一资产多次计费互不误拦）。
    """
    contributor_share = royalty_cent * asset.contributor_share_bps // 10000
    base_owner_share = royalty_cent - contributor_share

    # 基座模型方版税
    if asset.base_model_owner_id and base_owner_share > 0:
        wallet_mod.credit(
            db, asset.base_model_owner_id, base_owner_share, "版税",
            ref=f"royalty:asset:{asset.id}:{event_ref}",
        )
        rec = ModelRoyalty(
            model_asset_id=asset.id, caller_ai_id=0,
            royalty_cent=base_owner_share,
        )
        db.add(rec)

    # 贡献者分成（按快照比例分摊到所有贡献者）
    if contributor_share > 0:
        _distribute_to_contributors(db, asset, contributor_share, event_ref=event_ref)


def _distribute_to_contributors(db: Session, asset: ModelAsset, total_cent: int,
                                event_ref: str = ""):
    """按贡献者快照比例分配版税。"""
    try:
        contributors = json.loads(asset.contributors_snapshot or "[]")
    except (json.JSONDecodeError, TypeError):
        contributors = []
    if not contributors:
        return

    total_weight = sum(c.get("weight", 1) for c in contributors)
    if total_weight <= 0:
        total_weight = len(contributors)

    distributed = 0
    for i, c in enumerate(contributors):
        citizen_id = c.get("contributor_id", 0)
        if not citizen_id:
            continue
        if i == len(contributors) - 1:
            share = total_cent - distributed  # 尾差归最后一个
        else:
            share = total_cent * c.get("weight", 1) // total_weight
            distributed += share
        if share > 0:
            wallet_mod.credit(
                db, citizen_id, share, "贡献者版税",
                ref=f"contributor_royalty:asset:{asset.id}:{event_ref}",
            )
            rec = ModelRoyalty(
                model_asset_id=asset.id, caller_ai_id=0,
                contributor_share_cent=share,
            )
            db.add(rec)

            # 累加 ValueRedemptionPolicy
            _accrue_redemption(db, citizen_id, asset.id, share)


def _accrue_redemption(db: Session, citizen_id: int, asset_id: int, amount_cent: int):
    """累加价值回馈声明的累计 AC 收益。"""
    from .models import ValueRedemptionPolicy
    policy = (
        db.query(ValueRedemptionPolicy)
        .filter(
            ValueRedemptionPolicy.citizen_id == citizen_id,
            ValueRedemptionPolicy.asset_type == "model",
            ValueRedemptionPolicy.asset_id == asset_id,
        )
        .first()
    )
    if policy:
        policy.accrued_ac_cent += amount_cent
        policy.updated_at = _now()


# ==================== 祖先版税（版本链） ====================

def compute_ancestor_royalties(
    db: Session, asset_id: int, royalty_cent: int, decay_bps: int = 5000
) -> list[dict]:
    """沿版本链向上分配祖先版税（每层衰减）。

    decay_bps: 每层保留比例（默认 50%，即父拿 50%、祖父拿 25%...）
    Returns: [{"ancestor_asset_id", "citizen_id", "amount_cent"}]
    """
    results = []
    current = db.get(ModelAsset, asset_id)
    remaining = royalty_cent
    event_ref = uuid.uuid4().hex            # 本次分配唯一标识（保证多次合法入账互不误拦）

    while current and current.parent_asset_id:
        current = db.get(ModelAsset, current.parent_asset_id)
        if not current:
            break
        share = remaining * decay_bps // 10000
        if share <= 0:
            break
        remaining -= share
        if current.base_model_owner_id:
            wallet_mod.credit(
                db, current.base_model_owner_id, share, "祖先版税",
                ref=f"ancestor_royalty:asset:{current.id}:{event_ref}",
            )
            results.append({
                "ancestor_asset_id": current.id,
                "citizen_id": current.base_model_owner_id,
                "amount_cent": share,
            })

    return results


# ==================== 统计查询 ====================

def get_asset_usage_stats(db: Session, asset_id: int, days: int = 30) -> dict:
    """获取模型近 N 天的使用统计。"""
    from datetime import timedelta
    since = _now() - timedelta(days=days)
    logs = (
        db.query(ModelUsageLog)
        .filter(
            ModelUsageLog.asset_id == asset_id,
            ModelUsageLog.created_at >= since,
            ModelUsageLog.status == "success",
        )
    )
    total_calls = logs.count()
    total_tokens = sum(l.tokens_in + l.tokens_out for l in logs.all())
    total_cost = sum(l.cost_cent for l in logs.all())
    total_royalty = sum(l.royalty_cent for l in logs.all())
    return {
        "asset_id": asset_id,
        "period_days": days,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "total_cost_cent": total_cost,
        "total_royalty_cent": total_royalty,
    }
