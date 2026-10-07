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
"""Token 吊销服务（P0 安全）。

功能：
- 吊销单个 token（加入黑名单）；
- 查询 token 是否已被吊销且未过期；
- 清理已过期的 revocation 记录；
- BAN 时批量吊销某 AI 所有活跃 token。

依赖模型：TokenRevocation。
"""
import logging
import uuid
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import TokenRevocation

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class TokenRevocationService:
    """Token 吊销与黑名单管理。"""

    def revoke(self, db, jti: str, ai_id=None, host_id=None, reason: str = ""):
        """创建 TokenRevocation 记录，将 jti 加入黑名单。

        Args:
            db: SQLAlchemy session。
            jti: JWT ID。
            ai_id: 关联 AI 公民 ID（可选）。
            host_id: 关联宿主 ID（可选）。
            reason: 吊销原因。
        """
        existing = db.query(TokenRevocation).filter(TokenRevocation.jti == jti).first()
        if existing:
            return existing
        record = TokenRevocation(
            token_id=str(uuid.uuid4()),
            jti=jti,
            ai_id=ai_id,
            host_id=host_id,
            reason=reason,
            revoked_at=_now(),
            expires_at=_now() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
        )
        db.add(record)
        db.commit()
        return record

    def is_revoked(self, db, jti: str) -> bool:
        """查询 jti 是否在黑名单且未过期。

        Args:
            db: SQLAlchemy session。
            jti: JWT ID。

        Returns:
            True 表示该 token 已吊销。
        """
        if not settings.TOKEN_BLACKLIST_ENABLED:
            return False
        now = _now()
        record = db.query(TokenRevocation).filter(
            TokenRevocation.jti == jti,
        ).first()
        if record is None:
            return False
        # 如果记录已过期（token 本身也自然过期），不再视为有效黑名单
        if record.expires_at and record.expires_at < now:
            return False
        return True

    def cleanup_expired(self, db):
        """删除已过期的 revocation 记录（token 本身已自然过期无需再追踪）。"""
        now = _now()
        deleted = db.query(TokenRevocation).filter(
            TokenRevocation.expires_at < now
        ).delete()
        db.commit()
        logger.info("cleanup_expired: removed %d expired revocations", deleted)
        return deleted

    def revoke_all_for_ai(self, db, ai_id: int, reason: str = "ban"):
        """BAN 时吊销某 AI 所有活跃 token。

        标记策略：记录一条通配条目（ai_id 维度），后续校验时同时按 ai_id 维度判断。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            reason: 批量吊销原因。

        Returns:
            创建的批量标记记录。
        """
        # 通配 jti 前缀，后续按 ai_id 查询时匹配
        wildcard_jti = f"bulk_revoke_ai_{ai_id}_{int(_now().timestamp())}"
        record = TokenRevocation(
            token_id=str(uuid.uuid4()),
            jti=wildcard_jti,
            ai_id=ai_id,
            host_id=None,
            reason=reason,
            revoked_at=_now(),
            expires_at=_now() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
        )
        db.add(record)
        db.commit()
        logger.info("revoke_all_for_ai: ai_id=%d reason=%s", ai_id, reason)
        return record


token_revocation_service = TokenRevocationService()
