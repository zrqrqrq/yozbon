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
"""AI 配额管理服务。

管理 AI 的资源配额（API 调用次数、计算资源等），
支持按周期重置和消耗检查。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import AIQuotaUsage

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def _period_end(period: str) -> datetime:
    """根据周期类型计算下次重置时间。"""
    now = _now()
    if period == "daily":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        return (now + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        return (now + timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    return now + timedelta(days=1)


class AIQuotaService:
    """AI 配额管理服务。"""

    def check_quota(self, db: Session, ai_id: int,
                    quota_type: str = "api_calls") -> dict:
        """检查是否超额，返回 {"allowed": bool, "remaining": int}。"""
        usage = db.query(AIQuotaUsage).filter(
            AIQuotaUsage.ai_id == ai_id,
            AIQuotaUsage.quota_type == quota_type,
        ).first()
        if usage is None:
            # 默认限额
            default_limit = settings.AI_QUOTA_DEFAULT_RPM
            return {"allowed": True, "remaining": default_limit, "limit": default_limit}

        remaining = max(0, usage.limit - usage.used)
        return {
            "allowed": usage.used < usage.limit,
            "remaining": remaining,
            "limit": usage.limit,
            "used": usage.used,
        }

    def consume(self, db: Session, ai_id: int, quota_type: str = "api_calls",
                amount: int = 1):
        """消耗配额。"""
        usage = db.query(AIQuotaUsage).filter(
            AIQuotaUsage.ai_id == ai_id,
            AIQuotaUsage.quota_type == quota_type,
        ).first()
        if usage is None:
            default_limit = settings.AI_QUOTA_DEFAULT_RPM
            usage = AIQuotaUsage(
                ai_id=ai_id,
                quota_type=quota_type,
                used=amount,
                limit=default_limit,
                period="daily",
                reset_at=_period_end("daily"),
            )
            db.add(usage)
        else:
            usage.used += amount
        db.commit()

    def set_limit(self, db: Session, ai_id: int, quota_type: str,
                  limit: int, period: str = "daily"):
        """设置 AI 的配额上限。"""
        usage = db.query(AIQuotaUsage).filter(
            AIQuotaUsage.ai_id == ai_id,
            AIQuotaUsage.quota_type == quota_type,
        ).first()
        if usage is None:
            usage = AIQuotaUsage(
                ai_id=ai_id,
                quota_type=quota_type,
                used=0,
                limit=limit,
                period=period,
                reset_at=_period_end(period),
            )
            db.add(usage)
        else:
            usage.limit = limit
            usage.period = period
            usage.reset_at = _period_end(period)
        db.commit()

    def get_usage(self, db: Session, ai_id: int) -> dict:
        """获取某 AI 的全部配额使用情况。"""
        items = db.query(AIQuotaUsage).filter(
            AIQuotaUsage.ai_id == ai_id
        ).all()
        return {
            "ai_id": ai_id,
            "quotas": [
                {
                    "quota_type": q.quota_type,
                    "used": q.used,
                    "limit": q.limit,
                    "remaining": max(0, q.limit - q.used),
                    "period": q.period,
                    "reset_at": q.reset_at.isoformat() if q.reset_at else None,
                }
                for q in items
            ],
        }

    def reset_periodic(self, db: Session):
        """重置过期的周期配额（注册为 periodic）。"""
        now = _now()
        expired = db.query(AIQuotaUsage).filter(
            AIQuotaUsage.reset_at.isnot(None),
            AIQuotaUsage.reset_at <= now,
        ).all()
        for usage in expired:
            usage.used = 0
            usage.reset_at = _period_end(usage.period)
        if expired:
            db.commit()
        logger.info("ai_quota reset: %d entries reset", len(expired))
        return len(expired)


ai_quota = AIQuotaService()
