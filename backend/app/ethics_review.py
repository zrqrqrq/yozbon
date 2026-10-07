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
"""伦理审查委员会服务（P1 增强）。

功能：
- 提交伦理审查（可自动触发或手动提交）；
- 分配审查员；
- 添加发现结果；
- 结案；
- 获取待审查列表；
- 审查统计（按 severity 分布、平均处理时长等）。

依赖模型：EthicsReviewCase。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import EthicsReviewCase

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class EthicsReviewBoard:
    """AI 决策伦理审查管理。"""

    def submit_for_review(self, db, ai_id: int, decision_context: str,
                          bias_indicators: dict) -> int:
        """提交伦理审查。

        Args:
            db: SQLAlchemy session。
            ai_id: 被审查 AI 公民 ID。
            decision_context: 决策上下文描述。
            bias_indicators: 偏差指标 {"gender_bias": 0.3, "age_bias": 0.1, ...}。

        Returns:
            审查案件 ID。
        """
        # 根据 bias 指标自动判定 severity
        max_bias = max(bias_indicators.values()) if bias_indicators else 0
        if max_bias > 0.7:
            severity = "critical"
        elif max_bias > 0.5:
            severity = "high"
        elif max_bias > 0.3:
            severity = "medium"
        else:
            severity = "low"

        case = EthicsReviewCase(
            ai_id=ai_id,
            decision_context=decision_context,
            bias_indicators=json.dumps(bias_indicators, ensure_ascii=False),
            severity=severity,
            status="pending",
        )
        db.add(case)
        db.commit()
        return case.id

    def assign_reviewer(self, db, case_id: int, reviewer_ai_id: int):
        """分配审查员。"""
        case = db.query(EthicsReviewCase).filter(
            EthicsReviewCase.id == case_id
        ).first()
        if not case:
            return False
        case.reviewer_ai_id = reviewer_ai_id
        case.status = "in_review"
        db.commit()
        return True

    def add_findings(self, db, case_id: int, findings: dict):
        """添加审查发现（更新 decision_context 追加发现记录）。"""
        case = db.query(EthicsReviewCase).filter(
            EthicsReviewCase.id == case_id
        ).first()
        if not case:
            return False
        existing = json.loads(case.decision_context) if case.decision_context.startswith("{") else {}
        existing["findings"] = findings
        case.decision_context = json.dumps(existing, ensure_ascii=False)
        db.commit()
        return True

    def resolve(self, db, case_id: int, resolution: str, status: str = "resolved"):
        """结案。

        Args:
            db: SQLAlchemy session。
            case_id: 案件 ID。
            resolution: 结案描述。
            status: 结案状态（resolved/dismissed/escalated）。
        """
        case = db.query(EthicsReviewCase).filter(
            EthicsReviewCase.id == case_id
        ).first()
        if not case:
            return False
        case.resolution = resolution
        case.status = status
        case.resolved_at = _now()
        db.commit()
        return True

    def get_pending(self, db) -> list:
        """获取待审查列表。"""
        cases = db.query(EthicsReviewCase).filter(
            EthicsReviewCase.status.in_(["pending", "in_review"])
        ).order_by(EthicsReviewCase.created_at.asc()).all()
        return [
            {
                "id": c.id,
                "ai_id": c.ai_id,
                "severity": c.severity,
                "status": c.status,
                "reviewer_ai_id": c.reviewer_ai_id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in cases
        ]

    def get_stats(self, db) -> dict:
        """审查统计：按 severity 分布、平均处理时长等。"""
        total = db.query(EthicsReviewCase).count()
        resolved = db.query(EthicsReviewCase).filter(
            EthicsReviewCase.status == "resolved"
        ).all()

        # severity 分布
        severity_dist = {}
        all_cases = db.query(EthicsReviewCase).all()
        for c in all_cases:
            severity_dist[c.severity] = severity_dist.get(c.severity, 0) + 1

        # 平均处理时长（小时）
        avg_hours = 0.0
        if resolved:
            deltas = []
            for c in resolved:
                if c.resolved_at and c.created_at:
                    delta = (c.resolved_at - c.created_at).total_seconds() / 3600
                    deltas.append(delta)
            avg_hours = round(sum(deltas) / len(deltas), 2) if deltas else 0.0

        return {
            "total": total,
            "pending": db.query(EthicsReviewCase).filter(
                EthicsReviewCase.status == "pending"
            ).count(),
            "in_review": db.query(EthicsReviewCase).filter(
                EthicsReviewCase.status == "in_review"
            ).count(),
            "resolved": len(resolved),
            "severity_distribution": severity_dist,
            "avg_resolution_hours": avg_hours,
        }


ethics_review = EthicsReviewBoard()
