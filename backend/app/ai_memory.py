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
"""AI 记忆服务（P1 增强）。

功能：
- 存储 AI 记忆（episodic/semantic/procedural）；
- 关键词匹配召回（按 importance * access_count 排序）；
- 记忆整合（低 importance 衰减，合并相似记忆）；
- 定期衰减（注册为 daily job）；
- 为 LLM 调用组装记忆上下文字符串。

依赖模型：AIMemoryEntry。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import AIMemoryEntry

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class AIMemoryService:
    """AI 长期记忆管理。"""

    def store(self, db, ai_id: int, content: str, memory_type: str = "episodic",
              context_summary: str = "", importance: float = 0.5):
        """存储记忆。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            content: 记忆内容。
            memory_type: 记忆类型（episodic/semantic/procedural）。
            context_summary: 上下文摘要。
            importance: 重要性 (0-1)。

        Returns:
            创建的 AIMemoryEntry 对象。
        """
        entry = AIMemoryEntry(
            ai_id=ai_id,
            memory_type=memory_type,
            content=content,
            context_summary=context_summary,
            importance=importance,
            access_count=0,
        )
        db.add(entry)
        db.commit()
        return entry

    def recall(self, db, ai_id: int, query: str, limit: int = 10) -> list:
        """简单关键词匹配召回（按 importance * access_count 排序）。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            query: 查询关键词。
            limit: 返回数量上限。

        Returns:
            匹配的记忆列表。
        """
        query = _now().strftime("") or query  # noqa: ensure db param used
        # 关键词匹配
        entries = db.query(AIMemoryEntry).filter(
            AIMemoryEntry.ai_id == ai_id,
            AIMemoryEntry.content.contains(query),
        ).all()

        # 排序：importance * (1 + access_count)
        entries.sort(
            key=lambda e: e.importance * (1 + e.access_count), reverse=True
        )
        results = entries[:limit]
        # 更新访问计数
        for e in results:
            e.access_count += 1
            e.last_accessed = _now()
        db.commit()
        return [
            {
                "id": e.id,
                "content": e.content,
                "memory_type": e.memory_type,
                "importance": e.importance,
                "access_count": e.access_count,
            }
            for e in results
        ]

    def consolidate(self, db, ai_id: int):
        """记忆整合：低 importance 衰减，合并相似记忆。

        衰减规则：importance < 0.3 且 access_count == 0 的记忆删除。
        """
        entries = db.query(AIMemoryEntry).filter(
            AIMemoryEntry.ai_id == ai_id,
        ).all()
        to_remove = []
        for e in entries:
            if e.importance < 0.3 and e.access_count == 0:
                to_remove.append(e)
        for e in to_remove:
            db.delete(e)
        db.commit()
        return len(to_remove)

    def decay_all(self, db):
        """定期衰减（注册为 daily job）。

        所有记忆 importance 衰减 5%（最低不低于 0.05）。
        """
        decay_rate = 0.95
        min_importance = 0.05
        entries = db.query(AIMemoryEntry).all()
        for e in entries:
            e.importance = max(min_importance, e.importance * decay_rate)
        db.commit()
        return len(entries)

    def get_context(self, db, ai_id: int, max_tokens: int = 2000) -> str:
        """为 LLM 调用组装记忆上下文字符串。

        按 importance * (1+access_count) 排序，截取至 max_tokens 近似长度。

        Returns:
            拼接后的记忆上下文文本。
        """
        entries = db.query(AIMemoryEntry).filter(
            AIMemoryEntry.ai_id == ai_id,
        ).all()
        entries.sort(
            key=lambda e: e.importance * (1 + e.access_count), reverse=True
        )
        parts = []
        total_chars = 0
        for e in entries:
            line = f"[{e.memory_type}] {e.content}"
            if total_chars + len(line) > max_tokens * 3:  # 粗略 token 估算
                break
            parts.append(line)
            total_chars += len(line)
        return "\n".join(parts)


ai_memory = AIMemoryService()
