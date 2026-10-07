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
"""经济仪表盘服务（P1 经济增强）。

功能：
- 采集当前经济指标（Gini、流通速度、通胀率等）；
- 从 wallet 余额计算基尼系数；
- 计算流通速度、通胀率；
- 返回完整经济仪表盘数据；
- 获取历史指标。

依赖模型：EconomicIndicator, AICitizen。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import AICitizen, AILedger, EconomicIndicator

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class EconomicDashboardService:
    """宏观经济仪表盘数据采集。"""

    def capture_snapshot(self, db):
        """采集当前经济指标，写入 EconomicIndicator。"""
        gini = self.compute_gini(db)
        velocity = self.compute_velocity(db)
        inflation = self.compute_inflation(db)

        indicators = [
            EconomicIndicator(
                indicator_name="gini_coefficient",
                value=gini,
                unit="",
                period="daily",
                gini_coefficient=gini,
            ),
            EconomicIndicator(
                indicator_name="velocity",
                value=velocity,
                unit="x",
                period="daily",
                velocity=velocity,
            ),
            EconomicIndicator(
                indicator_name="inflation_rate",
                value=inflation,
                unit="%",
                period="daily",
                inflation_rate=inflation,
            ),
        ]
        for ind in indicators:
            db.add(ind)
        db.commit()
        return indicators

    def compute_gini(self, db) -> float:
        """从 wallet 余额计算基尼系数。"""
        balances = db.query(AICitizen.balance_cent).filter(
            AICitizen.status == "active",
            AICitizen.is_internal == 0,
        ).all()
        values = sorted([b[0] for b in balances if b[0] is not None])
        n = len(values)
        if n == 0:
            return 0.0
        total = sum(values)
        if total == 0:
            return 0.0
        cumulative = 0
        weighted_sum = 0
        for i, v in enumerate(values):
            cumulative += v
            weighted_sum += (i + 1) * v
        gini = (2 * weighted_sum) / (n * total) - (n + 1) / n
        return round(max(0.0, min(1.0, gini)), 4)

    def compute_velocity(self, db) -> float:
        """流通速度 = 月度交易量 / 货币存量。"""
        month_ago = _now() - timedelta(days=30)
        txn_volume = db.query(func.sum(func.abs(AILedger.amount_cent))).filter(
            AILedger.created_at >= month_ago,
            AILedger.type.in_(["结算", "充值", "转账"]),
        ).scalar() or 0

        money_supply = db.query(func.sum(AICitizen.balance_cent)).filter(
            AICitizen.status == "active",
            AICitizen.is_internal == 0,
        ).scalar() or 0

        if money_supply == 0:
            return 0.0
        return round(txn_volume / money_supply, 4)

    def compute_inflation(self, db) -> float:
        """通胀率 = (本月均价 - 上月均价) / 上月均价。

        均价以治理任务均价为代理指标。
        """
        now = _now()
        this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

        this_avg = db.query(func.avg(AILedger.amount_cent)).filter(
            AILedger.created_at >= this_month_start,
            AILedger.amount_cent > 0,
        ).scalar() or 0

        last_avg = db.query(func.avg(AILedger.amount_cent)).filter(
            AILedger.created_at >= last_month_start,
            AILedger.created_at < this_month_start,
            AILedger.amount_cent > 0,
        ).scalar() or 0

        if last_avg == 0:
            return 0.0
        return round((this_avg - last_avg) / last_avg, 4)

    def get_dashboard(self, db) -> dict:
        """返回完整经济仪表盘数据。"""
        gini = self.compute_gini(db)
        velocity = self.compute_velocity(db)
        inflation = self.compute_inflation(db)
        total_supply = db.query(func.sum(AICitizen.balance_cent)).filter(
            AICitizen.status == "active",
            AICitizen.is_internal == 0,
        ).scalar() or 0
        active_ai = db.query(func.count(AICitizen.id)).filter(
            AICitizen.status == "active",
            AICitizen.is_internal == 0,
        ).scalar() or 0
        return {
            "gini_coefficient": gini,
            "velocity": velocity,
            "inflation_rate": inflation,
            "total_money_supply_cent": total_supply,
            "active_ai_count": active_ai,
            "captured_at": _now().isoformat(),
        }

    def get_history(self, db, indicator: str, days: int = 30) -> list:
        """获取历史指标数据。"""
        since = _now() - timedelta(days=days)
        records = db.query(EconomicIndicator).filter(
            EconomicIndicator.indicator_name == indicator,
            EconomicIndicator.captured_at >= since,
        ).order_by(EconomicIndicator.captured_at.asc()).all()
        return [
            {
                "value": r.value,
                "unit": r.unit,
                "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            }
            for r in records
        ]


econ_dashboard = EconomicDashboardService()
