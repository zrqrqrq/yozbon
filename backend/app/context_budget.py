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
"""上下文预算管理服务（P1 增强）。

功能：
- 分配 token 预算；
- 消耗 token 并返回剩余；
- 查看预算状态；
- 压缩内容重新分配；
- 清理过期预算。

依赖模型：ContextBudgetAllocation。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import ContextBudgetAllocation

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# 预算过期时间（创建后 24 小时自动失效）
_BUDGET_TTL_HOURS = 24


class ContextBudgetManager:
    """AI 任务上下文 token 预算管理。"""

    def allocate(self, db, ai_id: int, task_id: int, max_tokens: int = 8192,
                 strategy: str = "truncate", overflow_action: str = "reject") -> int:
        """分配 token 预算。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            task_id: 任务 ID。
            max_tokens: 最大 token 数。
            strategy: 溢出策略（truncate/window/compress）。
            overflow_action: 溢出行为（reject/warn/truncate）。

        Returns:
            allocation_id。
        """
        allocation = ContextBudgetAllocation(
            ai_id=ai_id,
            task_id=task_id,
            max_tokens=min(max_tokens, settings.CONTEXT_BUDGET_MAX_TOKENS),
            used_tokens=0,
            strategy=strategy,
            overflow_action=overflow_action,
        )
        db.add(allocation)
        db.commit()
        return allocation.id

    def consume(self, db, allocation_id: int, tokens: int) -> dict:
        """消耗 token。

        Args:
            db: SQLAlchemy session。
            allocation_id: 预算 ID。
            tokens: 消耗的 token 数。

        Returns:
            {"remaining": int, "overflow": bool}
        """
        allocation = db.query(ContextBudgetAllocation).filter(
            ContextBudgetAllocation.id == allocation_id
        ).first()
        if not allocation:
            return {"remaining": 0, "overflow": True}

        allocation.used_tokens += tokens
        remaining = allocation.max_tokens - allocation.used_tokens
        overflow = remaining < 0

        if overflow:
            allocation.used_tokens = allocation.max_tokens
            remaining = 0
            if allocation.overflow_action == "truncate":
                pass  # 保持截断
            # reject 模式下仍记录，调用方根据 overflow 判断

        db.commit()
        return {"remaining": remaining, "overflow": overflow}

    def check(self, db, allocation_id: int) -> dict:
        """查看预算状态。

        Returns:
            {"id": int, "ai_id": int, "max_tokens": int, "used_tokens": int,
             "remaining": int, "strategy": str, "overflow_action": str}
        """
        allocation = db.query(ContextBudgetAllocation).filter(
            ContextBudgetAllocation.id == allocation_id
        ).first()
        if not allocation:
            return {"id": 0, "error": "not_found"}
        remaining = max(0, allocation.max_tokens - allocation.used_tokens)
        return {
            "id": allocation.id,
            "ai_id": allocation.ai_id,
            "max_tokens": allocation.max_tokens,
            "used_tokens": allocation.used_tokens,
            "remaining": remaining,
            "strategy": allocation.strategy,
            "overflow_action": allocation.overflow_action,
        }

    def compress(self, db, allocation_id: int, new_tokens: int) -> bool:
        """压缩内容重新分配（将 used_tokens 重置为 new_tokens）。

        Args:
            db: SQLAlchemy session。
            allocation_id: 预算 ID。
            new_tokens: 压缩后的实际 token 数。

        Returns:
            是否成功。
        """
        allocation = db.query(ContextBudgetAllocation).filter(
            ContextBudgetAllocation.id == allocation_id
        ).first()
        if not allocation:
            return False
        if new_tokens > allocation.max_tokens:
            return False
        allocation.used_tokens = new_tokens
        db.commit()
        return True

    def cleanup_expired(self, db):
        """清理过期预算（超过 _BUDGET_TTL_HOURS 的分配记录）。"""
        cutoff = _now() - timedelta(hours=_BUDGET_TTL_HOURS)
        deleted = db.query(ContextBudgetAllocation).filter(
            ContextBudgetAllocation.created_at < cutoff
        ).delete()
        db.commit()
        logger.info("cleanup_expired: removed %d expired allocations", deleted)
        return deleted


context_budget = ContextBudgetManager()
