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
"""P3 SLA 仪表盘服务。

记录和展示各服务的 SLA 指标（可用性、延迟、错误率等），
提供实时 SLA 状态、历史趋势、违约检测和报告导出。

指标类型（metric_name）：
- availability: 可用性百分比（目标 99.9%）；
- latency_p50 / latency_p99: 延迟百分位（ms）；
- error_rate: 错误率（%）；
- throughput: 吞吐量（req/s）。

SLA 目标由 settings 提供默认值，可通过 set_targets 按服务自定义。
"""
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import SLAMetricSnapshot

logger = logging.getLogger(__name__)

# 内存中的 SLA 目标配置（服务 -> {metric_name: target_value}）
_targets: dict = {}


def _now() -> datetime:
    return datetime.utcnow()


def _default_target(metric_name: str) -> float:
    """从全局 settings 获取指标默认目标。"""
    if metric_name == "availability":
        return settings.SLA_TARGET_AVAILABILITY * 100  # 转为百分比
    elif metric_name in ("latency_p99", "latency_p95", "latency_p50"):
        return float(settings.SLA_TARGET_LATENCY_P99_MS)
    elif metric_name == "error_rate":
        return 0.1  # 0.1% 以下
    return 0.0


class SLADashboard:
    """SLA 仪表盘服务。"""

    def record_metric(self, service: str, metric_name: str,
                      value: float, unit: str = "") -> dict:
        """记录一次指标快照。

        自动判断是否达标（met = 1/0）。

        Returns:
            {"snapshot_id", "service", "metric_name", "value", "met"}
        """
        target = self._get_target(service, metric_name)
        # 判断达标逻辑
        if metric_name in ("availability",):
            met = 1 if value >= target else 0
        elif metric_name.startswith("latency_") or metric_name == "error_rate":
            met = 1 if value <= target else 0
        else:
            met = 1  # 无目标默认达标

        db: Session = SessionLocal()
        try:
            snapshot = SLAMetricSnapshot(
                service=service,
                metric_name=metric_name,
                value=value,
                unit=unit,
                target=target,
                met=met,
            )
            db.add(snapshot)
            db.commit()

            if met == 0:
                logger.warning("sla_dashboard: BREACH service=%s metric=%s value=%.2f target=%.2f",
                               service, metric_name, value, target)

            return {
                "snapshot_id": snapshot.id,
                "service": service,
                "metric_name": metric_name,
                "value": value,
                "unit": unit,
                "target": target,
                "met": bool(met),
            }
        finally:
            db.close()

    def get_current_sla(self, service: str) -> dict:
        """获取服务当前 SLA 状态（各指标最近一次快照）。"""
        db: Session = SessionLocal()
        try:
            metrics = {}
            # 获取每个 metric_name 的最新一条
            latest_ids = (db.query(SLAMetricSnapshot.id)
                          .filter(SLAMetricSnapshot.service == service)
                          .subquery())

            from sqlalchemy import func
            latest = (db.query(SLAMetricSnapshot)
                      .filter(SLAMetricSnapshot.service == service)
                      .order_by(SLAMetricSnapshot.id.desc())
                      .all())

            seen = set()
            for s in latest:
                if s.metric_name not in seen:
                    seen.add(s.metric_name)
                    metrics[s.metric_name] = {
                        "value": s.value,
                        "unit": s.unit,
                        "target": s.target,
                        "met": bool(s.met),
                        "captured_at": s.captured_at.isoformat() if s.captured_at else None,
                    }

            overall_met = all(m["met"] for m in metrics.values()) if metrics else True
            return {
                "service": service,
                "overall_status": "healthy" if overall_met else "breaching",
                "metrics": metrics,
            }
        finally:
            db.close()

    def get_sla_history(self, service: str, period_days: int = 7) -> dict:
        """获取 SLA 历史趋势数据。"""
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=period_days)
            snapshots = (db.query(SLAMetricSnapshot)
                         .filter(SLAMetricSnapshot.service == service,
                                 SLAMetricSnapshot.captured_at >= cutoff)
                         .order_by(SLAMetricSnapshot.captured_at.asc())
                         .all())

            # 按 metric_name 分组
            history = {}
            for s in snapshots:
                if s.metric_name not in history:
                    history[s.metric_name] = []
                history[s.metric_name].append({
                    "value": s.value,
                    "captured_at": s.captured_at.isoformat() if s.captured_at else None,
                    "met": bool(s.met),
                })

            return {
                "service": service,
                "period_days": period_days,
                "total_data_points": len(snapshots),
                "history": history,
            }
        finally:
            db.close()

    def get_breaches(self, period_days: int = 30) -> list:
        """获取时间范围内的 SLA 违约记录。"""
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=period_days)
            breaches = (db.query(SLAMetricSnapshot)
                        .filter(SLAMetricSnapshot.met == 0,
                                SLAMetricSnapshot.captured_at >= cutoff)
                        .order_by(SLAMetricSnapshot.captured_at.desc())
                        .all())

            # 按服务汇总
            service_counts = {}
            for b in breaches:
                svc = b.service
                if svc not in service_counts:
                    service_counts[svc] = {"service": svc, "breach_count": 0, "metrics": {}}
                service_counts[svc]["breach_count"] += 1
                mn = b.metric_name
                service_counts[svc]["metrics"][mn] = service_counts[svc]["metrics"].get(mn, 0) + 1

            return list(service_counts.values())
        finally:
            db.close()

    def get_uptime(self, service: str, period_days: int = 30) -> dict:
        """计算服务可用率（基于 availability 指标的 met 比例）。"""
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=period_days)
            snapshots = (db.query(SLAMetricSnapshot)
                         .filter(SLAMetricSnapshot.service == service,
                                 SLAMetricSnapshot.metric_name == "availability",
                                 SLAMetricSnapshot.captured_at >= cutoff)
                         .all())

            total = len(snapshots)
            if total == 0:
                return {"service": service, "period_days": period_days, "uptime_pct": None, "samples": 0}

            # 平均可用性
            avg_availability = sum(s.value for s in snapshots) / total
            breach_count = sum(1 for s in snapshots if not s.met)

            return {
                "service": service,
                "period_days": period_days,
                "uptime_pct": round(avg_availability, 4),
                "samples": total,
                "breach_count": breach_count,
            }
        finally:
            db.close()

    def get_latency_percentile(self, service: str, percentile: float = 0.99) -> dict:
        """计算延迟百分位（从最近记录中计算）。"""
        db: Session = SessionLocal()
        try:
            metric_name = f"latency_p{int(percentile * 100)}"
            cutoff = _now() - timedelta(days=7)
            snapshots = (db.query(SLAMetricSnapshot)
                         .filter(SLAMetricSnapshot.service == service,
                                 SLAMetricSnapshot.metric_name == metric_name,
                                 SLAMetricSnapshot.captured_at >= cutoff)
                         .order_by(SLAMetricSnapshot.value.asc())
                         .all())

            values = [s.value for s in snapshots]
            if not values:
                return {"service": service, "percentile": percentile, "value": None, "samples": 0}

            # 从已记录数据中取对应百分位（简化：直接返回最新一条）
            idx = max(0, int(math.ceil(percentile * len(values))) - 1)
            return {
                "service": service,
                "metric_name": metric_name,
                "percentile": percentile,
                "value": values[idx],
                "min": min(values),
                "max": max(values),
                "avg": round(sum(values) / len(values), 2),
                "samples": len(values),
            }
        finally:
            db.close()

    def set_targets(self, service: str, targets: dict) -> dict:
        """设置服务的 SLA 目标。

        Args:
            service: 服务名。
            targets: {metric_name: target_value}，如 {"availability": 99.9, "latency_p99": 200}。

        Returns:
            {"service", "targets"}
        """
        if service not in _targets:
            _targets[service] = {}
        _targets[service].update(targets)
        logger.info("sla_dashboard: set targets for %s: %s", service, targets)
        return {"service": service, "targets": _targets[service]}

    def export_report(self, period_start: datetime, period_end: datetime) -> dict:
        """导出 SLA 报告。

        汇总时间范围内所有服务的 SLA 表现。
        """
        db: Session = SessionLocal()
        try:
            snapshots = (db.query(SLAMetricSnapshot)
                         .filter(SLAMetricSnapshot.captured_at >= period_start,
                                 SLAMetricSnapshot.captured_at <= period_end)
                         .all())

            # 按服务聚合
            services = {}
            for s in snapshots:
                if s.service not in services:
                    services[s.service] = {"metrics": {}, "total": 0, "met": 0}
                services[s.service]["total"] += 1
                if s.met:
                    services[s.service]["met"] += 1

                if s.metric_name not in services[s.service]["metrics"]:
                    services[s.service]["metrics"][s.metric_name] = []
                services[s.service]["metrics"][s.metric_name].append(s.value)

            report = {
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "generated_at": _now().isoformat(),
                "total_services": len(services),
                "services": {},
            }

            for svc, data in services.items():
                compliance = data["met"] / data["total"] if data["total"] > 0 else 1.0
                metrics_summary = {}
                for mn, vals in data["metrics"].items():
                    metrics_summary[mn] = {
                        "avg": round(sum(vals) / len(vals), 2),
                        "min": round(min(vals), 2),
                        "max": round(max(vals), 2),
                        "count": len(vals),
                    }
                report["services"][svc] = {
                    "compliance_rate": round(compliance, 4),
                    "total_data_points": data["total"],
                    "metrics": metrics_summary,
                }

            return report
        finally:
            db.close()

    # ---- 内部方法 ----

    @staticmethod
    def _get_target(service: str, metric_name: str) -> float:
        """获取目标值：自定义 > 默认。"""
        if service in _targets and metric_name in _targets[service]:
            return _targets[service][metric_name]
        return _default_target(metric_name)


instance = SLADashboard()
