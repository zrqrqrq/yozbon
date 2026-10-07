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
"""日落条款服务（P1 治理增强）。

功能：
- 为规则附加日落条款（到期自动失效）；
- 检查过期条款并执行 auto_action；
- 投票延期；
- 获取活跃条款列表。

依赖模型：SunsetClause。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import SunsetClause

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class SunsetService:
    """规则日落条款管理。"""

    def attach(self, db, rule_source: str, rule_id: int,
               expires_at=None, auto_action: str = "remove"):
        """为规则附加日落条款。

        Args:
            db: SQLAlchemy session。
            rule_source: 规则来源（policy/task/config 等）。
            rule_id: 规则 ID。
            expires_at: 过期时间，None 则使用 SUNSET_DEFAULT_DAYS。
            auto_action: 过期动作（remove/expire/disable）。

        Returns:
            创建的 SunsetClause 对象。
        """
        if expires_at is None:
            expires_at = _now() + timedelta(days=settings.SUNSET_DEFAULT_DAYS)
        clause = SunsetClause(
            rule_source=rule_source,
            rule_id=rule_id,
            expires_at=expires_at,
            auto_action=auto_action,
        )
        db.add(clause)
        db.commit()
        return clause

    def check_expired(self, db) -> list:
        """检查过期条款，执行 auto_action 并标记 inactive。

        Returns:
            已处理的过期条款列表。
        """
        if not settings.SUNSET_ENABLED:
            return []
        now = _now()
        expired = db.query(SunsetClause).filter(
            SunsetClause.active == 1,
            SunsetClause.expires_at <= now,
        ).all()
        results = []
        for clause in expired:
            clause.active = 0
            results.append({
                "id": clause.id,
                "rule_source": clause.rule_source,
                "rule_id": clause.rule_id,
                "auto_action": clause.auto_action,
            })
        db.commit()
        return results

    def extend(self, db, clause_id: int, extension_days: int = 90):
        """投票延期。

        Args:
            db: SQLAlchemy session。
            clause_id: 条款 ID。
            extension_days: 延期天数。
        """
        clause = db.query(SunsetClause).filter(SunsetClause.id == clause_id).first()
        if not clause:
            return None
        clause.expires_at = clause.expires_at + timedelta(days=extension_days)
        clause.extension_votes += 1
        db.commit()
        return clause

    def get_active(self, db) -> list:
        """获取活跃日落条款列表。"""
        clauses = db.query(SunsetClause).filter(
            SunsetClause.active == 1
        ).all()
        return [
            {
                "id": c.id,
                "rule_source": c.rule_source,
                "rule_id": c.rule_id,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "auto_action": c.auto_action,
                "extension_votes": c.extension_votes,
            }
            for c in clauses
        ]


sunset_service = SunsetService()
