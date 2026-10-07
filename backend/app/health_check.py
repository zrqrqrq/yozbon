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
"""健康检查服务（P0 安全）。

功能：
- 注册各服务的健康检查函数；
- 执行全量/单项探针并记录到 HealthProbeRecord；
- 获取最新状态；
- 计算 N 小时内可用率。

依赖模型：HealthProbeRecord。
"""
import logging
import time
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import HealthProbeRecord

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class HealthCheckService:
    """服务健康检查与探针管理。"""

    def __init__(self):
        self._checks: dict = {}  # service_name -> check_func

    def register_check(self, name: str, check_func):
        """注册健康检查函数。

        Args:
            name: 服务名称（如 "database", "redis", "llm"）。
            check_func: 无参数可调用对象，返回 (status: str, detail: str)。
        """
        self._checks[name] = check_func

    def probe_all(self, db) -> list:
        """执行所有注册的检查，记录到 HealthProbeRecord，返回结果列表。

        Returns:
            [{"service": str, "status": str, "latency_ms": int, "detail": str}, ...]
        """
        results = []
        for name, func in self._checks.items():
            start = time.time()
            try:
                status, detail = func()
            except Exception as exc:
                status, detail = "error", str(exc)
            latency_ms = int((time.time() - start) * 1000)
            record = HealthProbeRecord(
                service=name,
                status=status,
                latency_ms=latency_ms,
                detail=detail,
            )
            db.add(record)
            results.append({
                "service": name,
                "status": status,
                "latency_ms": latency_ms,
                "detail": detail,
            })
        db.commit()
        return results

    def probe(self, db, service_name: str) -> dict:
        """单个服务健康检查。

        Args:
            db: SQLAlchemy session。
            service_name: 服务名称。

        Returns:
            {"service": str, "status": str, "latency_ms": int, "detail": str}
        """
        func = self._checks.get(service_name)
        if func is None:
            return {"service": service_name, "status": "unknown", "latency_ms": 0,
                    "detail": "no check registered"}
        start = time.time()
        try:
            status, detail = func()
        except Exception as exc:
            status, detail = "error", str(exc)
        latency_ms = int((time.time() - start) * 1000)
        record = HealthProbeRecord(
            service=service_name,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
        )
        db.add(record)
        db.commit()
        return {
            "service": service_name,
            "status": status,
            "latency_ms": latency_ms,
            "detail": detail,
        }

    def get_status(self, db, service_name: str = None) -> dict:
        """获取最新状态；无指定则返回所有。

        Args:
            db: SQLAlchemy session。
            service_name: 可选服务名，None 则返回全部。

        Returns:
            单个服务或所有服务的最新状态 dict。
        """
        query = db.query(HealthProbeRecord)
        if service_name:
            query = query.filter(HealthProbeRecord.service == service_name)
        records = query.order_by(HealthProbeRecord.probed_at.desc()).all()

        if service_name:
            latest = records[0] if records else None
            if latest:
                return {
                    "service": latest.service,
                    "status": latest.status,
                    "latency_ms": latest.latency_ms,
                    "detail": latest.detail,
                    "probed_at": latest.probed_at.isoformat() if latest.probed_at else None,
                }
            return {"service": service_name, "status": "unknown", "latency_ms": 0,
                    "detail": "no records"}

        # 返回每个服务的最新记录
        seen = {}
        for rec in records:
            if rec.service not in seen:
                seen[rec.service] = {
                    "service": rec.service,
                    "status": rec.status,
                    "latency_ms": rec.latency_ms,
                    "detail": rec.detail,
                    "probed_at": rec.probed_at.isoformat() if rec.probed_at else None,
                }
        return seen

    def get_uptime(self, db, service_name: str, hours: int = 24) -> float:
        """计算 N 小时内的可用率百分比。

        Args:
            db: SQLAlchemy session。
            service_name: 服务名称。
            hours: 回溯小时数。

        Returns:
            可用率百分比 (0-100)。
        """
        since = _now() - timedelta(hours=hours)
        total = db.query(HealthProbeRecord).filter(
            HealthProbeRecord.service == service_name,
            HealthProbeRecord.probed_at >= since,
        ).count()
        if total == 0:
            return 100.0
        healthy = db.query(HealthProbeRecord).filter(
            HealthProbeRecord.service == service_name,
            HealthProbeRecord.probed_at >= since,
            HealthProbeRecord.status.in_(["healthy", "ok"]),
        ).count()
        return round(healthy / total * 100, 2)


health_check = HealthCheckService()
