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
"""托管收益计息服务。

按天为托管中的资金计算利息，写入 EscrowYieldAccrual，
结算时释放累积利息给托管所有人。

B-H3 资金守恒：利息来源为 SystemState["yield_fund"]（国库生息基金），
claim_yield 发放时从 yield_fund 扣减并 credit 入领取人钱包，
保证不凭空造币（money_supply 不变，税池→钱包转账守恒）。
若 yield_fund 不足则只发放可用部分（不超发）。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import EscrowYieldAccrual
from . import wallet as wallet_mod

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class EscrowYieldService:
    """托管收益计息服务。"""

    def accrue(self, db: Session, escrow_id: int, ai_id: int,
               principal: int, rate_bps: int = None):
        """按天计算利息，写入 EscrowYieldAccrual。"""
        if rate_bps is None:
            rate_bps = settings.ESCROW_YIELD_BPS
        now = _now()
        period_start = now - timedelta(days=1)
        # 日利率 = rate_bps / 10000 / 365
        daily_rate = rate_bps / 10000.0 / 365.0
        interest = principal * daily_rate

        accrual = EscrowYieldAccrual(
            escrow_id=escrow_id,
            ai_id=ai_id,
            principal=principal,
            accrued_interest=round(interest, 4),
            rate_bps=rate_bps,
            period_start=period_start,
            period_end=now,
        )
        db.add(accrual)
        db.commit()
        return accrual.id

    def accrue_all(self, db: Session):
        """定期任务：为所有活跃 escrow 累计利息（注册为 daily job）。"""
        # 查找最近一天内尚未计息的唯一 escrow+ai 组合
        one_day_ago = _now() - timedelta(days=1)
        # 获取所有有 principal 的 escrow 记录（取最近一条获取 escrow_id, ai_id, principal）
        from sqlalchemy import func, distinct
        recent = db.query(
            EscrowYieldAccrual.escrow_id,
            EscrowYieldAccrual.ai_id,
            func.max(EscrowYieldAccrual.period_end).label("last_accrued"),
        ).group_by(
            EscrowYieldAccrual.escrow_id,
            EscrowYieldAccrual.ai_id,
        ).subquery()

        # 找出需要计息的（上次计息超过1天）
        pending = db.query(
            EscrowYieldAccrual.escrow_id,
            EscrowYieldAccrual.ai_id,
            EscrowYieldAccrual.principal,
        ).join(
            recent,
            (EscrowYieldAccrual.escrow_id == recent.c.escrow_id) &
            (EscrowYieldAccrual.ai_id == recent.c.ai_id)
        ).filter(
            EscrowYieldAccrual.period_end == recent.c.last_accrued,
            recent.c.last_accrued <= one_day_ago,
        ).distinct().all()

        count = 0
        for escrow_id, ai_id, principal in pending:
            self.accrue(db, escrow_id, ai_id, principal)
            count += 1

        logger.info("escrow_yield accrue_all: accrued %d entries", count)
        return count

    def claim_yield(self, db: Session, escrow_id: int, ai_id: int,
                    ref: str = "") -> int:
        """结算时释放累积利息（B-H3：从 yield_fund 复式发放）。

        利息来源为 SystemState["yield_fund"]；
        若 yield_fund 不足以覆盖全部利息，只发放可用部分（不凭空造币）。
        返回实际发放分值。
        """
        accruals = db.query(EscrowYieldAccrual).filter(
            EscrowYieldAccrual.escrow_id == escrow_id,
            EscrowYieldAccrual.ai_id == ai_id,
        ).all()
        total_interest = int(sum(a.accrued_interest for a in accruals))
        if total_interest <= 0:
            return 0

        # B-H3：从 yield_fund 扣减（不超发）
        available = wallet_mod.get_system_state(db, "yield_fund", default=0)
        payout = min(total_interest, available)
        if payout <= 0:
            logger.warning("escrow_yield claim: yield_fund=0, cannot pay interest %d "
                           "for escrow=%d ai=%d", total_interest, escrow_id, ai_id)
            return 0

        ref_str = ref or f"escrow_yield:{escrow_id}:{ai_id}"
        wallet_mod.adjust_system_state(db, "yield_fund", -payout, ref=ref_str)
        wallet_mod.credit(db, ai_id, payout, "托管收益",
                          ref=ref_str, note=f"escrow {escrow_id} yield")
        db.flush()
        logger.info("escrow_yield claimed: escrow=%d ai=%d payout=%d (accrued=%d)",
                    escrow_id, ai_id, payout, total_interest)
        return payout

    def get_accrued(self, db: Session, ai_id: int) -> dict:
        """获取某 AI 的全部累积利息摘要。"""
        accruals = db.query(EscrowYieldAccrual).filter(
            EscrowYieldAccrual.ai_id == ai_id
        ).all()
        total = sum(a.accrued_interest for a in accruals)
        by_escrow = {}
        for a in accruals:
            by_escrow.setdefault(a.escrow_id, 0.0)
            by_escrow[a.escrow_id] += a.accrued_interest
        return {
            "ai_id": ai_id,
            "total_accrued": round(total, 4),
            "by_escrow": {str(k): round(v, 4) for k, v in by_escrow.items()},
        }


escrow_yield = EscrowYieldService()


# ==================== 注册日级计息任务 ====================
def _escrow_yield_accrue_job(db: Session, now: datetime = None) -> int:
    """日级任务：为所有活跃托管累计利息。"""
    try:
        return escrow_yield.accrue_all(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("escrow_yield accrue_all failed: %s", exc)
        return 0


from .scheduler import register_daily_job  # noqa: E402
if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("escrow_yield_accrue", _escrow_yield_accrue_job)
