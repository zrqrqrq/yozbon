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
"""财富分布指标快照服务。

从所有 AIWallet 余额计算 Gini 系数、top10 份额、bottom50 份额等，
定期写入 WealthDistributionSnapshot 用于监控贫富差距趋势。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import AIWallet, WealthDistributionSnapshot

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def _gini(values: list) -> float:
    """计算 Gini 系数，values 为非负数值列表。"""
    n = len(values)
    if n == 0:
        return 0.0
    sorted_vals = sorted(values)
    total = sum(sorted_vals)
    if total == 0:
        return 0.0
    cumulative = 0.0
    weighted_sum = 0.0
    for i, v in enumerate(sorted_vals):
        cumulative += v
        weighted_sum += (i + 1) * v
    return (2.0 * weighted_sum) / (n * total) - (n + 1.0) / n


class WealthMetricsService:
    """财富分布指标快照服务。"""

    def capture_snapshot(self, db: Session) -> dict:
        """从所有 AIWallet 余额计算指标并写入快照。"""
        wallets = db.query(AIWallet).filter(AIWallet.balance_cent >= 0).all()
        balances = [w.balance_cent for w in wallets]

        if not balances:
            snapshot = WealthDistributionSnapshot(
                gini=0.0, top10_share=0.0, bottom50_share=0.0,
                median_wealth=0.0, mean_wealth=0.0, total_circulation=0.0,
            )
            db.add(snapshot)
            db.commit()
            return {"gini": 0.0, "top10_share": 0.0, "bottom50_share": 0.0,
                    "median_wealth": 0.0, "mean_wealth": 0.0,
                    "total_circulation": 0.0, "snapshot_id": snapshot.id}

        sorted_balances = sorted(balances)
        total = sum(sorted_balances)
        n = len(sorted_balances)

        gini = _gini(sorted_balances)

        # top10 share
        top10_count = max(1, n // 10)
        top10_share = sum(sorted_balances[-top10_count:]) / total if total > 0 else 0.0

        # bottom50 share
        bottom50_count = max(1, n // 2)
        bottom50_share = sum(sorted_balances[:bottom50_count]) / total if total > 0 else 0.0

        # median
        if n % 2 == 0:
            median_wealth = (sorted_balances[n // 2 - 1] + sorted_balances[n // 2]) / 2.0
        else:
            median_wealth = float(sorted_balances[n // 2])

        mean_wealth = total / n

        snapshot = WealthDistributionSnapshot(
            gini=round(gini, 6),
            top10_share=round(top10_share, 6),
            bottom50_share=round(bottom50_share, 6),
            median_wealth=round(median_wealth, 2),
            mean_wealth=round(mean_wealth, 2),
            total_circulation=float(total),
        )
        db.add(snapshot)
        db.commit()

        return {
            "gini": snapshot.gini,
            "top10_share": snapshot.top10_share,
            "bottom50_share": snapshot.bottom50_share,
            "median_wealth": snapshot.median_wealth,
            "mean_wealth": snapshot.mean_wealth,
            "total_circulation": snapshot.total_circulation,
            "snapshot_id": snapshot.id,
        }

    def get_latest(self, db: Session) -> dict:
        """获取最近一次快照。"""
        snap = db.query(WealthDistributionSnapshot).order_by(
            WealthDistributionSnapshot.snapshot_date.desc()
        ).first()
        if snap is None:
            return {}
        return {
            "id": snap.id,
            "gini": snap.gini,
            "top10_share": snap.top10_share,
            "bottom50_share": snap.bottom50_share,
            "median_wealth": snap.median_wealth,
            "mean_wealth": snap.mean_wealth,
            "total_circulation": snap.total_circulation,
            "snapshot_date": snap.snapshot_date.isoformat() if snap.snapshot_date else None,
        }

    def get_history(self, db: Session, days: int = 30) -> list:
        """获取最近 N 天快照历史。"""
        cutoff = _now() - timedelta(days=days)
        snaps = db.query(WealthDistributionSnapshot).filter(
            WealthDistributionSnapshot.snapshot_date >= cutoff
        ).order_by(WealthDistributionSnapshot.snapshot_date.asc()).all()
        return [
            {
                "id": s.id,
                "gini": s.gini,
                "top10_share": s.top10_share,
                "bottom50_share": s.bottom50_share,
                "snapshot_date": s.snapshot_date.isoformat() if s.snapshot_date else None,
            }
            for s in snaps
        ]

    def get_inequality_trend(self, db: Session, days: int = 90) -> dict:
        """返回贫富差距趋势分析。"""
        history = self.get_history(db, days=days)
        if len(history) < 2:
            return {"direction": "insufficient_data", "snapshots": history}

        gini_values = [h["gini"] for h in history if h["gini"] is not None]
        if len(gini_values) < 2:
            return {"direction": "insufficient_data", "snapshots": history}

        first_half = gini_values[: len(gini_values) // 2]
        second_half = gini_values[len(gini_values) // 2:]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)

        if avg_second > avg_first + 0.01:
            direction = "worsening"
        elif avg_second < avg_first - 0.01:
            direction = "improving"
        else:
            direction = "stable"

        return {
            "direction": direction,
            "gini_early_avg": round(avg_first, 4),
            "gini_recent_avg": round(avg_second, 4),
            "snapshots": history,
        }


wealth_metrics = WealthMetricsService()


# ==================== B-M9：注册日级快照任务 ====================
def _wealth_snapshot_daily_job(db: Session, now: datetime = None) -> int:
    """日级任务：捕获财富分布快照（Gini/top10/bottom50）。

    解决 B-M9：capture_snapshot 无调用入口，快照从未生成。
    """
    try:
        result = wealth_metrics.capture_snapshot(db)
        return int(result.get("snapshot_id", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("wealth snapshot daily job failed: %s", exc)
        return 0


from .scheduler import register_daily_job  # noqa: E402
if getattr(settings, "APP_ENV", "") != "test":
    register_daily_job("wealth_snapshot", _wealth_snapshot_daily_job)
