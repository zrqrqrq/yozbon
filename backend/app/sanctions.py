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
"""制裁/黑名单服务。

管理实体制裁记录，提供检查、移除、到期复审等功能。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import SanctionedEntity

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class SanctionsService:
    """制裁/黑名单服务。"""

    def add_sanction(self, db: Session, entity_type: str, identifier: str,
                     reason: str, sanctioned_by: int,
                     severity: str = "block") -> int:
        """添加制裁记录，返回 sanction_id。"""
        # 默认 90 天后复审
        review_date = _now() + timedelta(days=90)
        entity = SanctionedEntity(
            entity_type=entity_type,
            identifier=identifier,
            reason=reason,
            sanctioned_by=sanctioned_by,
            severity=severity,
            review_date=review_date,
            active=1,
        )
        db.add(entity)
        db.commit()
        logger.info("sanction added: %s:%s severity=%s", entity_type, identifier, severity)
        return entity.id

    def check(self, db: Session, entity_type: str, identifier: str) -> dict:
        """检查实体是否被制裁。"""
        entity = db.query(SanctionedEntity).filter(
            SanctionedEntity.entity_type == entity_type,
            SanctionedEntity.identifier == identifier,
            SanctionedEntity.active == 1,
        ).first()
        if entity is None:
            return {"sanctioned": False, "severity": None, "reason": None}
        return {
            "sanctioned": True,
            "severity": entity.severity,
            "reason": entity.reason,
            "sanctioned_at": entity.added_at.isoformat() if entity.added_at else None,
        }

    def remove(self, db: Session, sanction_id: int):
        """移除制裁。"""
        entity = db.get(SanctionedEntity, sanction_id)
        if entity is None:
            raise ValueError(f"Sanction record {sanction_id} not found")
        entity.active = 0
        db.commit()

    def review_expired(self, db: Session):
        """检查需要复审的制裁（注册为 daily job）。"""
        now = _now()
        expired = db.query(SanctionedEntity).filter(
            SanctionedEntity.active == 1,
            SanctionedEntity.review_date.isnot(None),
            SanctionedEntity.review_date <= now,
        ).all()
        for entity in expired:
            logger.info("sanction %d (%s:%s) requires review",
                        entity.id, entity.entity_type, entity.identifier)
            # 自动降级 severity 为 watch（不删除，需人工确认）
            if entity.severity == "block":
                entity.severity = "watch"
        if expired:
            db.commit()
        return len(expired)

    def list_active(self, db: Session, severity: str = None) -> list:
        """列出活跃制裁。"""
        q = db.query(SanctionedEntity).filter(SanctionedEntity.active == 1)
        if severity:
            q = q.filter(SanctionedEntity.severity == severity)
        items = q.order_by(SanctionedEntity.added_at.desc()).all()
        return [
            {
                "id": e.id,
                "entity_type": e.entity_type,
                "identifier": e.identifier,
                "severity": e.severity,
                "reason": e.reason,
                "added_at": e.added_at.isoformat() if e.added_at else None,
            }
            for e in items
        ]


sanctions = SanctionsService()
