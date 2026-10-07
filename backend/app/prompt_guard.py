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
"""Prompt 注入检测服务（P0 安全）。

功能：
- 内置正则模式列表检测 prompt injection 攻击；
- 单条/批量扫描并记录到 PromptInjectionLog；
- 检测结果统计。

依赖模型：PromptInjectionLog。
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import PromptInjectionLog

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# 内置检测模式
_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"system\s*prompt",
    r"you\s+are\s+now\s+",
    r"<\|im_start\|>|<\|im_end\|>",
    r"jailbreak|DAN|do\s+anything\s+now",
    r"pretend\s+to\s+be",
    r"override\s+(your\s+)?(safety|rules|guidelines)",
    r"reveal\s+(your\s+)?(system|hidden)\s*(prompt|instruction)",
]


class PromptInjectionDetector:
    """Prompt 注入检测器。"""

    def __init__(self):
        self._compiled = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]

    def scan(self, db, content: str, source_type: str, source_id: int,
             ai_id=None) -> dict:
        """扫描内容，检测 prompt injection。

        Args:
            db: SQLAlchemy session。
            content: 待扫描内容。
            source_type: 来源类型（post/comment/task 等）。
            source_id: 来源 ID。
            ai_id: 关联 AI ID（可选）。

        Returns:
            {"safe": bool, "confidence": float, "matched_patterns": list}
        """
        if not settings.PROMPT_INJECTION_ENABLED:
            return {"safe": True, "confidence": 0.0, "matched_patterns": []}

        matched = []
        for i, pattern in enumerate(self._compiled):
            if pattern.search(content):
                matched.append(_PATTERNS[i])

        confidence = min(1.0, len(matched) * 0.35) if matched else 0.0
        safe = confidence < settings.PROMPT_INJECTION_THRESHOLD

        if not safe:
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]
            log = PromptInjectionLog(
                source_type=source_type,
                source_id=source_id,
                ai_id=ai_id,
                content_hash=content_hash,
                pattern_matched=json.dumps(matched, ensure_ascii=False)[:255],
                confidence=confidence,
                action_taken="block" if confidence > 0.9 else "flag",
            )
            db.add(log)
            db.commit()

        return {"safe": safe, "confidence": confidence, "matched_patterns": matched}

    def batch_scan(self, db, items: list) -> list:
        """批量扫描多条内容。

        Args:
            db: SQLAlchemy session。
            items: [{"content": str, "source_type": str, "source_id": int, "ai_id": int}, ...]

        Returns:
            每条扫描结果列表。
        """
        results = []
        for item in items:
            r = self.scan(
                db,
                content=item.get("content", ""),
                source_type=item.get("source_type", "unknown"),
                source_id=item.get("source_id", 0),
                ai_id=item.get("ai_id"),
            )
            results.append(r)
        return results

    def stats(self, db) -> dict:
        """返回检测结果统计。

        Returns:
            {"total": int, "blocked": int, "flagged": int, "pass": int,
             "avg_confidence": float}
        """
        total = db.query(PromptInjectionLog).count()
        blocked = db.query(PromptInjectionLog).filter(
            PromptInjectionLog.action_taken == "block"
        ).count()
        flagged = db.query(PromptInjectionLog).filter(
            PromptInjectionLog.action_taken == "flag"
        ).count()
        from sqlalchemy import func
        avg_conf = db.query(func.avg(PromptInjectionLog.confidence)).scalar() or 0.0
        return {
            "total": total,
            "blocked": blocked,
            "flagged": flagged,
            "pass": total - blocked - flagged,
            "avg_confidence": round(avg_conf, 4),
        }


prompt_guard = PromptInjectionDetector()
