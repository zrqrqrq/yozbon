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
"""互助保险/风险池服务（蓝图 §互助保障）。

核心机制：
- 风险池按技能/行业维度设立，池内资金来源于保费；
- 保单缴纳保费入池，到期自动失效；
- 理赔按 payout_ratio_bps 比例赔付，受 max_payout_cent 上限约束；
- 本模块只 flush，commit 由调用方/路由层负责。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import InsurancePool, InsurancePolicy, InsuranceClaim
from .scheduler import register_daily_job
from . import wallet as wallet_mod

from .wallet import WalletError


class InsuranceError(Exception):
    """保险业务异常（路由层映射为 HTTP 400/403）。"""


def _now() -> datetime:
    return datetime.utcnow()


# ======================== 风险池管理 ========================

def create_pool(db: Session, name: str, scope_skill: str = "",
                premium_rate_bps: int = 200, payout_ratio_bps: int = 8000,
                max_payout_cent: int = 1_000_000) -> InsurancePool:
    """创建互助风险池。

    Args:
        premium_rate_bps: 保费费率（万分比/月），如 200 = 保额的 2%/月。
        payout_ratio_bps: 赔付比例（万分比），如 8000 = 损失的 80%。
        max_payout_cent: 单笔最高赔付金额（分）。
    """
    if not name.strip():
        raise InsuranceError("Risk pool name cannot be empty")
    if premium_rate_bps <= 0 or premium_rate_bps > 10000:
        raise InsuranceError("premium_rate_bps must be within (0, 10000]")
    if payout_ratio_bps <= 0 or payout_ratio_bps > 10000:
        raise InsuranceError("payout_ratio_bps must be within (0, 10000]")
    if max_payout_cent <= 0:
        raise InsuranceError("max_payout_cent must be a positive integer")

    pool = InsurancePool(
        name=name.strip(),
        scope_skill=scope_skill,
        premium_rate_bps=premium_rate_bps,
        payout_ratio_bps=payout_ratio_bps,
        max_payout_cent=max_payout_cent,
        status="active",
    )
    db.add(pool)
    db.flush()
    return pool


# ======================== 保单管理 ========================

def buy_policy(db: Session, pool_id: int, policyholder_id: int,
               coverage_cent: int, duration_days: int = 30,
               insured_contract_id: int = 0) -> InsurancePolicy:
    """购买保单：计算保费 → 扣保单持有人钱包 → 入池。

    保费 = coverage_cent * premium_rate_bps * (duration_days / 30) / 10000
    """
    if coverage_cent <= 0:
        raise InsuranceError("Coverage amount must be a positive integer in cents")
    if duration_days <= 0:
        raise InsuranceError("Coverage days must be a positive integer")

    pool = db.get(InsurancePool, pool_id)
    if pool is None or pool.status != "active":
        raise InsuranceError("Risk pool not found or closed")

    # 计算保费（整数除法向下取整，最低 1 分）
    premium = max(1, coverage_cent * pool.premium_rate_bps * duration_days // (30 * 10000))

    # 扣保单持有人钱包
    ref = f"insurance:pool:{pool_id}"
    wallet_mod.debit(db, policyholder_id, premium, "保险费",
                     ref=ref, note=f"insured {pool.name} coverage {coverage_cent} cents/{duration_days} days")

    # 保费入池
    pool.pool_balance_cent += premium

    now = _now()
    policy = InsurancePolicy(
        pool_id=pool_id,
        policyholder_id=policyholder_id,
        insured_contract_id=insured_contract_id,
        coverage_cent=coverage_cent,
        premium_paid_cent=premium,
        status="active",
        starts_at=now,
        expires_at=now + timedelta(days=duration_days),
    )
    db.add(policy)
    db.flush()
    return policy


def cancel_policy(db: Session, policy_id: int, actor_id: int) -> None:
    """取消保单：按剩余保障期等比例退还保费（从池内扣回，入保单持有人钱包）。"""
    policy = db.get(InsurancePolicy, policy_id)
    if policy is None:
        raise InsuranceError("Policy not found")
    if policy.status != "active":
        raise InsuranceError(f"Policy status is {policy.status}; cannot cancel")
    if policy.policyholder_id != actor_id:
        raise InsuranceError("Only the policy holder can cancel")

    now = _now()
    total_duration = (policy.expires_at - policy.starts_at).total_seconds()
    remaining = (policy.expires_at - now).total_seconds()
    if total_duration <= 0:
        refund = 0
    else:
        refund = max(0, int(policy.premium_paid_cent * remaining / total_duration))

    pool = db.get(InsurancePool, policy.pool_id)
    if pool is not None and refund > 0:
        pool.pool_balance_cent = max(0, pool.pool_balance_cent - refund)
        wallet_mod.credit(db, policy.policyholder_id, refund, "保险费退还",
                          ref=f"insurance:cancel:{policy_id}",
                          note=f"policy {policy_id} cancelled; remaining premium refunded")

    policy.status = "cancelled"
    db.flush()


# ======================== 理赔流程 ========================

def file_claim(db: Session, policy_id: int, claimant_id: int,
               loss_amount_cent: int, reason: str) -> InsuranceClaim:
    """提交理赔申请。校验保单有效且未过期。"""
    if loss_amount_cent <= 0:
        raise InsuranceError("Loss amount must be a positive integer in cents")

    policy = db.get(InsurancePolicy, policy_id)
    if policy is None:
        raise InsuranceError("Policy not found")
    if policy.status != "active":
        raise InsuranceError(f"Policy status is {policy.status}; cannot claim")
    if policy.expires_at <= _now():
        raise InsuranceError("Policy has expired")
    if policy.policyholder_id != claimant_id:
        raise InsuranceError("Only the policy holder can submit a claim")

    claim = InsuranceClaim(
        policy_id=policy_id,
        claimant_id=claimant_id,
        loss_amount_cent=loss_amount_cent,
        reason=reason,
        status="pending",
    )
    db.add(claim)
    db.flush()
    return claim


def approve_claim(db: Session, claim_id: int, reviewer_id: int) -> InsuranceClaim:
    """审批通过理赔：计算赔付金额 = min(损失 * payout_ratio_bps / 10000, max_payout_cent)。"""
    claim = db.get(InsuranceClaim, claim_id)
    if claim is None:
        raise InsuranceError("Claim not found")
    if claim.status != "pending":
        raise InsuranceError(f"Claim status is {claim.status}; cannot approve")

    policy = db.get(InsurancePolicy, claim.policy_id)
    if policy is None:
        raise InsuranceError("Associated policy not found")
    pool = db.get(InsurancePool, policy.pool_id)
    if pool is None:
        raise InsuranceError("Associated risk pool not found")

    # 赔付金额计算
    payout = min(claim.loss_amount_cent * pool.payout_ratio_bps // 10000,
                 pool.max_payout_cent)
    payout = max(0, payout)

    claim.payout_cent = payout
    claim.status = "approved"
    claim.reviewed_by = reviewer_id
    policy.status = "claimed"
    db.flush()
    return claim


def reject_claim(db: Session, claim_id: int, reviewer_id: int, reason: str) -> None:
    """驳回理赔申请。"""
    claim = db.get(InsuranceClaim, claim_id)
    if claim is None:
        raise InsuranceError("Claim not found")
    if claim.status != "pending":
        raise InsuranceError(f"Claim status is {claim.status}; cannot reject")

    claim.status = "rejected"
    claim.reviewed_by = reviewer_id
    claim.reason = claim.reason + f" | rejection reason: {reason}" if claim.reason else f"rejection reason: {reason}"
    db.flush()


def pay_claim(db: Session, claim_id: int) -> None:
    """执行赔付：从风险池扣款 → 入理赔人钱包 → 标记已付。"""
    claim = db.get(InsuranceClaim, claim_id)
    if claim is None:
        raise InsuranceError("Claim not found")
    if claim.status != "approved":
        raise InsuranceError(f"Claim status is {claim.status}; only approved can be paid out")
    if claim.payout_cent <= 0:
        raise InsuranceError("Payout amount is 0; no action needed")

    policy = db.get(InsurancePolicy, claim.policy_id)
    if policy is None:
        raise InsuranceError("Associated policy not found")
    pool = db.get(InsurancePool, policy.pool_id)
    if pool is None:
        raise InsuranceError("Associated risk pool not found")
    if pool.pool_balance_cent < claim.payout_cent:
        raise InsuranceError(
            f"insufficient risk pool balance: need {claim.payout_cent}, currently {pool.pool_balance_cent}")

    # 池内扣款
    pool.pool_balance_cent -= claim.payout_cent
    # 入理赔人钱包
    wallet_mod.credit(db, claim.claimant_id, claim.payout_cent, "保险赔付",
                      ref=f"insurance:claim:{claim_id}",
                      note=f"policy {claim.policy_id} claim paid")

    claim.status = "paid"
    db.flush()


# ======================== 查询与统计 ========================

def pool_stats(db: Session, pool_id: int) -> dict:
    """风险池统计：余额、活跃保单数、待审/已赔理赔数。"""
    pool = db.get(InsurancePool, pool_id)
    if pool is None:
        raise InsuranceError("Risk pool not found")

    active_policies = (db.query(InsurancePolicy)
                       .filter(InsurancePolicy.pool_id == pool_id,
                               InsurancePolicy.status == "active")
                       .count())
    claims_pending = (db.query(InsuranceClaim)
                      .join(InsurancePolicy, InsuranceClaim.policy_id == InsurancePolicy.id)
                      .filter(InsurancePolicy.pool_id == pool_id,
                              InsuranceClaim.status == "pending")
                      .count())
    claims_paid = (db.query(InsuranceClaim)
                   .join(InsurancePolicy, InsuranceClaim.policy_id == InsurancePolicy.id)
                   .filter(InsurancePolicy.pool_id == pool_id,
                           InsuranceClaim.status == "paid")
                   .count())

    return {
        "pool_id": pool.id,
        "name": pool.name,
        "scope_skill": pool.scope_skill,
        "pool_balance_cent": pool.pool_balance_cent,
        "premium_rate_bps": pool.premium_rate_bps,
        "payout_ratio_bps": pool.payout_ratio_bps,
        "max_payout_cent": pool.max_payout_cent,
        "status": pool.status,
        "active_policies": active_policies,
        "claims_pending": claims_pending,
        "claims_paid": claims_paid,
    }


def list_pools(db: Session) -> list[dict]:
    """列出所有风险池概要。"""
    pools = db.query(InsurancePool).order_by(InsurancePool.id).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "scope_skill": p.scope_skill,
            "pool_balance_cent": p.pool_balance_cent,
            "premium_rate_bps": p.premium_rate_bps,
            "payout_ratio_bps": p.payout_ratio_bps,
            "max_payout_cent": p.max_payout_cent,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pools
    ]


# ======================== 日级任务 ========================

def insurance_daily_job(db: Session, now: datetime | None = None) -> int:
    """日级任务：将过期保单状态设为 expired。返回处理的保单数量。"""
    now = now or _now()
    expired_count = (db.query(InsurancePolicy)
                     .filter(InsurancePolicy.status == "active",
                             InsurancePolicy.expires_at <= now)
                     .update({"status": "expired"}, synchronize_session="fetch"))
    db.flush()
    return expired_count or 0


register_daily_job("insurance", insurance_daily_job)
