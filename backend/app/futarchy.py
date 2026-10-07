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
"""Futarchy（投票治理+预测市场）服务。

管理 Futarchy 提案的创建、结果关联、执行与拒绝。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import FutarchyProposal, PredictionMarket

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class FutarchyService:
    """Futarchy 投票治理服务。"""

    def propose(self, db: Session, proposal_text: str,
                prediction_market_id: int) -> int:
        """创建 Futarchy 提案，返回 futarchy_id。"""
        fp = FutarchyProposal(
            proposal_text=proposal_text,
            prediction_market_id=prediction_market_id,
            implementation_status="pending",
        )
        db.add(fp)
        db.commit()
        return fp.id

    def link_outcome(self, db: Session, futarchy_id: int, winning_outcome: str):
        """当预测市场结算后关联获胜结果。

        A-M4 校验：
        - 状态前置：仅 pending（未结算）提案可关联，防止重复关联/治理 AI 自设
          结果操纵全流程（已 approved/rejected/executed 的提案不可再改判）；
        - 市场状态：关联结果前，若预测市场已登记，必须处于 resolved 状态
          （市场不存在则不阻塞，向后兼容未接入真实市场的历史提案）。
        """
        fp = db.get(FutarchyProposal, futarchy_id)
        if fp is None:
            raise ValueError(f"Futarchy proposal {futarchy_id} not found")
        if fp.implementation_status != "pending":
            raise ValueError(
                f"Futarchy {futarchy_id} already resolved "
                f"(status={fp.implementation_status}); cannot link outcome")
        if fp.prediction_market_id:
            market = db.get(PredictionMarket, fp.prediction_market_id)
            if market is not None and market.status != "resolved":
                raise ValueError(
                    f"prediction market {fp.prediction_market_id} is not resolved; "
                    "cannot link outcome")
        fp.winning_outcome = winning_outcome
        fp.resolved_at = _now()
        if winning_outcome in ("yes", "true", "supported"):
            fp.implementation_status = "approved"
        else:
            fp.implementation_status = "rejected"
        db.commit()

    def execute(self, db: Session, futarchy_id: int):
        """如果预测支持则标记为 executed。

        A-M4 状态校验：仅 approved 且已关联获胜结果（winning_outcome 非空）的
        提案可执行；未关联结果不得跳过结算直接执行。
        """
        fp = db.get(FutarchyProposal, futarchy_id)
        if fp is None:
            raise ValueError(f"Futarchy proposal {futarchy_id} not found")
        if fp.implementation_status != "approved":
            raise ValueError("Proposal not approved; cannot execute")
        if not fp.winning_outcome:
            raise ValueError("Winning outcome not linked; cannot execute")
        fp.implementation_status = "executed"
        db.commit()

    def reject(self, db: Session, futarchy_id: int, reason: str = ""):
        """拒绝提案。"""
        fp = db.get(FutarchyProposal, futarchy_id)
        if fp is None:
            raise ValueError(f"Futarchy proposal {futarchy_id} not found")
        fp.implementation_status = "rejected"
        fp.resolved_at = _now()
        db.commit()
        logger.info("futarchy %d rejected: %s", futarchy_id, reason)

    def get_active(self, db: Session) -> list:
        """获取活跃的（pending 状态的）Futarchy 提案。"""
        items = db.query(FutarchyProposal).filter(
            FutarchyProposal.implementation_status == "pending"
        ).order_by(FutarchyProposal.created_at.desc()).all()
        return [
            {
                "id": f.id,
                "proposal_text": f.proposal_text,
                "prediction_market_id": f.prediction_market_id,
                "status": f.implementation_status,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in items
        ]


futarchy_service = FutarchyService()
