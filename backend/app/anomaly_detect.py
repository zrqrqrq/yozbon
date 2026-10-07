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
"""AI 行为异常检测服务。

功能：
- 过度消费检测（overspend）：消费金额超近期均值 N 倍；
- 空转循环检测（idle_loop）：无产出行为数超阈值；
- 攻击性行为检测（aggression）：近期冲突事件数异常；
- 垃圾信息检测（spam）：消息频率异常；
- 异常记录与自动处置。

依赖模型：AnomalyEvent。
"""
import json
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import AnomalyEvent

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class AnomalyDetector:
    """AI 行为异常检测业务逻辑。"""

    def check_overspend(self, citizen_id: int, amount_cent: int,
                        avg_recent_cent: int = 0) -> dict:
        """检测过度消费。

        Args:
            amount_cent: 本次消费金额。
            avg_recent_cent: 近期平均消费（若为 0 则从 DB 计算）。

        Returns:
            {"anomaly": bool, "severity": str, "ratio": float}
        """
        multiplier = settings.ANOMALY_OVERSPEND_MULT

        # 若无传入均值，从异常历史推断（简化：使用阈值判断）
        if avg_recent_cent <= 0:
            avg_recent_cent = 10000  # 默认基线 100 AC

        ratio = amount_cent / avg_recent_cent if avg_recent_cent > 0 else 999

        if ratio >= multiplier * 2:
            severity = "critical"
        elif ratio >= multiplier:
            severity = "high"
        elif ratio >= multiplier * 0.6:
            severity = "medium"
        else:
            return {"anomaly": False, "severity": "low", "ratio": round(ratio, 2)}

        self.record_anomaly(citizen_id, "overspend", severity, {
            "amount_cent": amount_cent,
            "avg_recent_cent": avg_recent_cent,
            "ratio": round(ratio, 2),
        })
        return {"anomaly": True, "severity": severity, "ratio": round(ratio, 2)}

    def check_idle_loop(self, citizen_id: int, action_count: int) -> dict:
        """检测空转循环（无实质产出的重复行为）。"""
        threshold = settings.ANOMALY_IDLE_LOOP_COUNT

        if action_count >= threshold * 2:
            severity = "critical"
        elif action_count >= threshold:
            severity = "high"
        else:
            return {"anomaly": False, "severity": "low", "count": action_count}

        self.record_anomaly(citizen_id, "idle_loop", severity, {
            "action_count": action_count,
            "threshold": threshold,
        })
        return {"anomaly": True, "severity": severity, "count": action_count}

    def check_aggression(self, citizen_id: int, incidents_recent: int) -> dict:
        """检测攻击性行为。"""
        # 24h 内冲突次数阈值
        threshold = 5

        if incidents_recent >= threshold * 2:
            severity = "critical"
        elif incidents_recent >= threshold:
            severity = "high"
        elif incidents_recent >= threshold // 2:
            severity = "medium"
        else:
            return {"anomaly": False, "severity": "low", "incidents": incidents_recent}

        self.record_anomaly(citizen_id, "aggression", severity, {
            "incidents_recent": incidents_recent,
            "threshold": threshold,
        })
        return {"anomaly": True, "severity": severity, "incidents": incidents_recent}

    def check_spam(self, citizen_id: int, messages_per_min: int) -> dict:
        """检测垃圾信息。"""
        threshold = 30  # 每分钟 30 条

        if messages_per_min >= threshold * 3:
            severity = "critical"
        elif messages_per_min >= threshold:
            severity = "high"
        elif messages_per_min >= threshold // 2:
            severity = "medium"
        else:
            return {"anomaly": False, "severity": "low", "rpm": messages_per_min}

        self.record_anomaly(citizen_id, "spam", severity, {
            "messages_per_min": messages_per_min,
            "threshold": threshold,
        })
        return {"anomaly": True, "severity": severity, "rpm": messages_per_min}

    def record_anomaly(self, citizen_id: int, anomaly_type: str,
                       severity: str, indicators: dict) -> dict:
        """记录异常事件。"""
        db = SessionLocal()
        try:
            event = AnomalyEvent(
                citizen_id=citizen_id,
                anomaly_type=anomaly_type,
                severity=severity,
                indicators=json.dumps(indicators, ensure_ascii=False, default=str),
                action_taken="",
                resolved=0,
            )
            db.add(event)
            db.commit()
            logger.warning("Anomaly detected: citizen=%d type=%s severity=%s",
                           citizen_id, anomaly_type, severity)
            return {"event_id": event.id}
        finally:
            db.close()

    def get_anomaly_history(self, citizen_id: int, limit: int = 50) -> list[dict]:
        """查询异常历史。"""
        db = SessionLocal()
        try:
            rows = (db.query(AnomalyEvent)
                    .filter(AnomalyEvent.citizen_id == citizen_id)
                    .order_by(AnomalyEvent.id.desc())
                    .limit(min(limit, 200))
                    .all())
            return [
                {
                    "id": r.id,
                    "anomaly_type": r.anomaly_type,
                    "severity": r.severity,
                    "indicators": json.loads(r.indicators or "{}"),
                    "action_taken": r.action_taken,
                    "resolved": bool(r.resolved),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()

    def auto_resolve(self, citizen_id: int) -> dict:
        """自动处置：对 low/medium 级别且超过 24h 的异常自动标记 resolved。"""
        db = SessionLocal()
        try:
            cutoff = _now() - timedelta(hours=24)
            count = (db.query(AnomalyEvent)
                     .filter(AnomalyEvent.citizen_id == citizen_id,
                             AnomalyEvent.resolved == 0,
                             AnomalyEvent.severity.in_(["low", "medium"]),
                             AnomalyEvent.created_at < cutoff)
                     .update({"resolved": 1, "action_taken": "auto_resolved"},
                             synchronize_session=False))
            db.commit()
            if count:
                logger.info("Auto-resolved %d anomalies for citizen=%d", count, citizen_id)
            return {"resolved": count}
        finally:
            db.close()


instance = AnomalyDetector()
