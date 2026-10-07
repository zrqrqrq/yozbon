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
"""通用众筹服务。

管理众筹项目的创建、贡献、达标检查、资金释放等全生命周期。
B-M3：current_amount 使用原子 UPDATE 自增，防并发计数丢失。
B-M4：注册日级 job 自动 disburse funded 项目。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import update as _update, func, select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from . import wallet as wallet_mod
from .wallet import WalletError
from .models import (AICitizen, CrowdfundContribution, GeneralCrowdfund)
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class GeneralCrowdfundService:
    """通用众筹服务。"""

    def create(self, db: Session, title: str, description: str,
               creator_ai_id: int, target_amount: int,
               deadline: datetime, category: str = "general") -> int:
        """创建众筹项目，返回 crowdfund_id。"""
        cf = GeneralCrowdfund(
            title=title,
            description=description,
            creator_ai_id=creator_ai_id,
            target_amount=target_amount,
            current_amount=0,
            deadline=deadline,
            status="open",
            category=category,
        )
        db.add(cf)
        db.commit()
        return cf.id

    def contribute(self, db: Session, crowdfund_id: int,
                   contributor_ai_id: int, amount: int, tier: str = "base"):
        """注入资金到众筹，检查是否达标。

        G5：贡献即从贡献者钱包划出真实资金并锁定到其 escrow_cent
        （与平台托管语义一致），写一笔 AILedger 流水可回溯；
        不再仅累加 current_amount 计数器。
        """
        cf = db.get(GeneralCrowdfund, crowdfund_id)
        if cf is None:
            raise ValueError(f"Crowdfunding project {crowdfund_id} not found")
        if cf.status != "open":
            raise ValueError(f"Crowdfunding project status is {cf.status}; not accepting contributions")
        if amount <= 0:
            raise ValueError("amount must be a positive integer (cents)")
        if cf.deadline < _now():
            cf.status = "failed"
            self._refund_all(db, cf)
            db.commit()
            raise ValueError("Crowdfunding has expired")

        contrib = CrowdfundContribution(
            crowdfund_id=crowdfund_id,
            contributor_ai_id=contributor_ai_id,
            amount=amount,
            tier=tier,
        )
        db.add(contrib)
        db.flush()

        # 划转资金：balance→（debit 出账并写流水），再锁定到 escrow_cent
        try:
            wallet_mod.debit(
                db, contributor_ai_id, amount, type_="crowdfund_contribute",
                ref=f"crowdfund_contribute:{contrib.id}",
                note=f"crowdfund {crowdfund_id}",
            )
        except WalletError as exc:
            db.rollback()
            raise ValueError(str(exc)) from exc
        w = wallet_mod.get_wallet(db, contributor_ai_id)
        w.escrow_cent += amount

        # B-M3：原子自增 current_amount（防并发计数丢失）
        db.execute(
            _update(GeneralCrowdfund)
            .where(GeneralCrowdfund.id == crowdfund_id)
            .values(current_amount=GeneralCrowdfund.current_amount + amount)
            .execution_options(synchronize_session=False))
        db.commit()

        # 达标检查：以实算 SUM(contributions) 为最终真值
        db.refresh(cf)
        actual_sum = (db.query(func.coalesce(func.sum(CrowdfundContribution.amount), 0))
                      .filter(CrowdfundContribution.crowdfund_id == crowdfund_id)
                      .scalar())
        if actual_sum >= cf.target_amount:
            cf.status = "funded"
            cf.funded_at = _now()
            db.commit()

    def _refund_all(self, db: Session, cf: GeneralCrowdfund) -> int:
        """退还该项目全部已锁定贡献资金（失败/过期时调用）。

        将每笔贡献从其 escrow_cent 退回贡献者 balance 并写 crowdfund_refund 流水；
        ref 受唯一索引兜底，重复调用幂等（不会重复退款）。返回退款笔数。
        """
        contribs = (
            db.query(CrowdfundContribution)
            .filter(CrowdfundContribution.crowdfund_id == cf.id)
            .all()
        )
        n = 0
        for c in contribs:
            w = wallet_mod.get_wallet(db, c.contributor_ai_id)
            w.escrow_cent = max(0, w.escrow_cent - c.amount)
            wallet_mod.credit(
                db, c.contributor_ai_id, c.amount, type_="crowdfund_refund",
                ref=f"crowdfund_refund:{cf.id}:{c.id}",
                note=f"crowdfund {cf.id} refund",
            )
            n += 1
        return n

    def refund(self, db: Session, crowdfund_id: int) -> int:
        """显式退款入口：将 open/failed 项目已锁定资金全额退回贡献者。"""
        cf = db.get(GeneralCrowdfund, crowdfund_id)
        if cf is None:
            raise ValueError(f"Crowdfunding project {crowdfund_id} not found")
        if cf.status not in ("open", "failed"):
            raise ValueError(f"Cannot refund project in status {cf.status}")
        n = self._refund_all(db, cf)
        cf.status = "failed"
        db.commit()
        return n

    def check_funding(self, db: Session, crowdfund_id: int) -> str:
        """检查状态：达标→funded，过期未达标→failed（并退款）。"""
        cf = db.get(GeneralCrowdfund, crowdfund_id)
        if cf is None:
            raise ValueError(f"Crowdfunding project {crowdfund_id} not found")
        if cf.status != "open":
            return cf.status
        if cf.current_amount >= cf.target_amount:
            cf.status = "funded"
            cf.funded_at = _now()
            db.commit()
        elif cf.deadline < _now():
            cf.status = "failed"
            self._refund_all(db, cf)
            db.commit()
        return cf.status

    def disburse(self, db: Session, crowdfund_id: int):
        """funded 状态下释放资金给创建者。

        G5：将各贡献者锁定在 escrow_cent 的资金划转至创建者钱包，
        写 crowdfund_disburse 流水可回溯（贡献侧出账已在 contribute 记录）。
        """
        cf = db.get(GeneralCrowdfund, crowdfund_id)
        if cf is None:
            raise ValueError(f"Crowdfunding project {crowdfund_id} not found")
        if cf.status != "funded":
            raise ValueError("Only funded status can release funds")
        contribs = (
            db.query(CrowdfundContribution)
            .filter(CrowdfundContribution.crowdfund_id == crowdfund_id)
            .all()
        )
        total = 0
        for c in contribs:
            w = wallet_mod.get_wallet(db, c.contributor_ai_id)
            w.escrow_cent = max(0, w.escrow_cent - c.amount)
            wallet_mod.credit(
                db, cf.creator_ai_id, c.amount, type_="crowdfund_disburse",
                ref=f"crowdfund_disburse:{crowdfund_id}:{c.id}",
                note=f"crowdfund {crowdfund_id} from ai {c.contributor_ai_id}",
            )
            total += c.amount
        # 标记为已释放（disbursed）
        cf.status = "disbursed"
        db.commit()
        logger.info("crowdfund %d disbursed %d cent to ai %d",
                    crowdfund_id, total, cf.creator_ai_id)

    def list_open(self, db: Session, category: str = None) -> list:
        """列出开放中的众筹项目。"""
        q = db.query(GeneralCrowdfund).filter(GeneralCrowdfund.status == "open")
        if category:
            q = q.filter(GeneralCrowdfund.category == category)
        return [
            {
                "id": c.id,
                "title": c.title,
                "creator_ai_id": c.creator_ai_id,
                "target_amount": c.target_amount,
                "current_amount": c.current_amount,
                "deadline": c.deadline.isoformat() if c.deadline else None,
                "category": c.category,
            }
            for c in q.order_by(GeneralCrowdfund.created_at.desc()).all()
        ]

    def list_by_creator(self, db: Session, ai_id: int) -> list:
        """列出某 AI 创建的所有众筹。"""
        items = db.query(GeneralCrowdfund).filter(
            GeneralCrowdfund.creator_ai_id == ai_id
        ).order_by(GeneralCrowdfund.created_at.desc()).all()
        return [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "target_amount": c.target_amount,
                "current_amount": c.current_amount,
            }
            for c in items
        ]


crowdfund_service = GeneralCrowdfundService()


# ==================== B-M4：自动释放 funded 项目的日级任务 ====================
def _crowdfund_disburse_daily_job(db: Session, now: datetime = None) -> int:
    """日级任务：扫描所有 funded 状态的众筹项目，自动执行 disburse 释放资金。

    解决 B-M4：funded 后无自动 disburse 路径，资金长期滞留 escrow 不可用。
    每次调用最多处理 50 个项目（防单次事务过大）。
    """
    funded = (
        db.query(GeneralCrowdfund)
        .filter(GeneralCrowdfund.status == "funded")
        .limit(50)
        .all()
    )
    count = 0
    for cf in funded:
        try:
            crowdfund_service.disburse(db, cf.id)
            count += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("crowdfund auto-disburse failed for %d: %s", cf.id, exc)
    if count:
        logger.info("crowdfund auto-disburse: disbursed %d projects", count)
    return count


if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("crowdfund_disburse", _crowdfund_disburse_daily_job)


# ==================== B-L6：众筹过期竞态 — 日级过期巡检 ====================
def _crowdfund_expiry_daily_job(db: Session, now: datetime = None) -> int:
    """日级任务：扫描 open 状态已过期的众筹，标记 failed 并退款。

    解决 B-L6：过期判定仅在 contribute 时触发，若无人再贡献则永不过期退款。
    """
    now = now or _now()
    expired = (
        db.query(GeneralCrowdfund)
        .filter(GeneralCrowdfund.status == "open",
                GeneralCrowdfund.deadline < now)
        .limit(50)
        .all()
    )
    count = 0
    for cf in expired:
        try:
            cf.status = "failed"
            crowdfund_service._refund_all(db, cf)
            db.commit()
            count += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("crowdfund expiry refund failed for %d: %s", cf.id, exc)
    if count:
        logger.info("crowdfund expiry: expired %d projects (refunded)", count)
    return count


if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("crowdfund_expiry", _crowdfund_expiry_daily_job)
