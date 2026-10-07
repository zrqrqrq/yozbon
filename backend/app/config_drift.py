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
"""配置漂移检测服务。

启动时快照当前配置为 baseline，定期检查当前配置与 baseline 是否一致，
发现差异则写入 ConfigDriftAlert。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import ConfigDriftAlert

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class ConfigDriftDetector:
    """配置漂移检测器。"""

    def __init__(self):
        self._baseline: dict | None = None

    def take_baseline(self):
        """当前 config 快照作为 baseline。"""
        self._baseline = {}
        for attr in dir(settings):
            if attr.isupper() and not attr.startswith("_"):
                val = getattr(settings, attr, None)
                if val is not None and not callable(val):
                    self._baseline[attr] = str(val)
        logger.info("config_drift baseline taken: %d keys", len(self._baseline))

    def check_drift(self, db: Session) -> list:
        """比较当前 config 与 baseline，发现差异写入 ConfigDriftAlert。"""
        if self._baseline is None:
            self.take_baseline()
            return []

        drifts = []
        for key, expected in self._baseline.items():
            current = getattr(settings, key, None)
            current_str = str(current) if current is not None else ""
            if current_str != expected:
                # 检查是否已有未解决的同 key 告警
                existing = db.query(ConfigDriftAlert).filter(
                    ConfigDriftAlert.key == key,
                    ConfigDriftAlert.resolved == 0,
                ).first()
                if existing is None:
                    alert = ConfigDriftAlert(
                        key=key,
                        expected_value=expected,
                        actual_value=current_str,
                    )
                    db.add(alert)
                    db.commit()
                    drifts.append({
                        "id": alert.id,
                        "key": key,
                        "expected": expected,
                        "actual": current_str,
                    })
        return drifts

    def resolve(self, db: Session, alert_id: int):
        """标记告警为已解决。"""
        alert = db.get(ConfigDriftAlert, alert_id)
        if alert is None:
            raise ValueError(f"Alert {alert_id} not found")
        alert.resolved = 1
        alert.resolved_at = _now()
        db.commit()

    def get_active_alerts(self, db: Session) -> list:
        """获取未解决的配置漂移告警。"""
        alerts = db.query(ConfigDriftAlert).filter(
            ConfigDriftAlert.resolved == 0
        ).order_by(ConfigDriftAlert.detected_at.desc()).all()
        return [
            {
                "id": a.id,
                "key": a.key,
                "expected_value": a.expected_value,
                "actual_value": a.actual_value,
                "detected_at": a.detected_at.isoformat() if a.detected_at else None,
            }
            for a in alerts
        ]


config_drift = ConfigDriftDetector()
