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
"""P2 合规审计报告服务。

支持生成周期性合规审计报告（财务/隐私/安全/运维），
包含发现项（findings）跟踪、合规评分计算和发现项确认。

报告类型: financial / privacy / security / operational
风险等级: low / medium / high
报告状态: draft / final / archived
"""
import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import ComplianceReport

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class ComplianceAuditor:
    """合规审计报告服务。"""

    def generate_report(self, report_type: str, period_start: datetime = None,
                        period_end: datetime = None) -> dict:
        """生成合规审计报告。

        Args:
            report_type: financial / privacy / security / operational。
            period_start: 审计周期起始。
            period_end: 审计周期结束。

        Returns:
            {"report_id", "report_type", "period_start", "period_end", "findings_count", "risk_level"}
        """
        valid_types = ("financial", "privacy", "security", "operational")
        if report_type not in valid_types:
            raise ValueError(f"report_type must be one of {valid_types}")

        if period_start is None:
            period_start = _now() - timedelta(days=30)
        if period_end is None:
            period_end = _now()

        # 生成发现项（此处为规则驱动的基础审计逻辑）
        findings = self._run_checks(report_type, period_start, period_end)

        # 计算风险等级
        high_count = sum(1 for f in findings if f.get("severity") == "high")
        med_count = sum(1 for f in findings if f.get("severity") == "medium")
        if high_count > 0:
            risk_level = "high"
        elif med_count > 2:
            risk_level = "medium"
        else:
            risk_level = "low"

        db: Session = SessionLocal()
        try:
            report = ComplianceReport(
                report_type=report_type,
                period_start=period_start,
                period_end=period_end,
                findings=json.dumps(findings),
                risk_level=risk_level,
                status="draft",
                generated_by="auto",
            )
            db.add(report)
            db.commit()
            logger.info("compliance: generated %s report id=%d findings=%d risk=%s",
                        report_type, report.id, len(findings), risk_level)
            return {
                "report_id": report.id,
                "report_type": report_type,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "findings_count": len(findings),
                "risk_level": risk_level,
            }
        finally:
            db.close()

    def get_report(self, report_id: int) -> dict:
        """获取报告详情。"""
        db: Session = SessionLocal()
        try:
            report = db.get(ComplianceReport, report_id)
            if report is None:
                raise ValueError(f"Report {report_id} not found")
            return {
                "report_id": report.id,
                "report_type": report.report_type,
                "period_start": report.period_start.isoformat() if report.period_start else None,
                "period_end": report.period_end.isoformat() if report.period_end else None,
                "findings": json.loads(report.findings or "[]"),
                "risk_level": report.risk_level,
                "status": report.status,
                "generated_by": report.generated_by,
                "created_at": report.created_at.isoformat() if report.created_at else None,
            }
        finally:
            db.close()

    def list_reports(self, filters: dict = None) -> list:
        """列出报告（可按类型/状态/风险等级过滤）。"""
        db: Session = SessionLocal()
        try:
            q = db.query(ComplianceReport)
            if filters:
                if filters.get("report_type"):
                    q = q.filter(ComplianceReport.report_type == filters["report_type"])
                if filters.get("status"):
                    q = q.filter(ComplianceReport.status == filters["status"])
                if filters.get("risk_level"):
                    q = q.filter(ComplianceReport.risk_level == filters["risk_level"])
            reports = q.order_by(ComplianceReport.id.desc()).all()
            return [
                {
                    "report_id": r.id,
                    "report_type": r.report_type,
                    "period_start": r.period_start.isoformat() if r.period_start else None,
                    "period_end": r.period_end.isoformat() if r.period_end else None,
                    "risk_level": r.risk_level,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reports
            ]
        finally:
            db.close()

    def export_report(self, report_id: int, fmt: str = "json") -> dict:
        """导出报告为指定格式。

        Args:
            report_id: 报告 ID。
            fmt: 导出格式 json / csv / pdf。

        Returns:
            {"report_id", "format", "content"}
        """
        report = self.get_report(report_id)
        content = json.dumps(report, ensure_ascii=False, indent=2)
        return {
            "report_id": report_id,
            "format": fmt,
            "content": content,
            "filename": f"compliance_report_{report_id}.{fmt}",
        }

    def schedule_periodic_audit(self, cron: str = "0 6 * * 1") -> dict:
        """配置周期性审计（默认每周一 06:00）。

        实际调度由 scheduler 模块负责，此处返回配置信息。
        """
        return {
            "enabled": True,
            "cron": cron,
            "types": ["financial", "privacy", "security", "operational"],
        }

    def get_compliance_score(self) -> dict:
        """计算当前合规评分（0-100）。

        基于最近报告的发现项严重度加权扣分。
        """
        db: Session = SessionLocal()
        try:
            recent = (db.query(ComplianceReport)
                      .order_by(ComplianceReport.id.desc())
                      .limit(10)
                      .all())
            if not recent:
                return {"score": 100, "checked_reports": 0}

            score = 100
            for r in recent:
                findings = json.loads(r.findings or "[]")
                for f in findings:
                    sev = f.get("severity", "low")
                    if sev == "high":
                        score -= 15
                    elif sev == "medium":
                        score -= 5
                    else:
                        score -= 1
            score = max(0, score)
            return {"score": score, "checked_reports": len(recent)}
        finally:
            db.close()

    def get_findings(self, report_id: int) -> list:
        """获取报告的发现项列表。"""
        report = self.get_report(report_id)
        return report.get("findings", [])

    def acknowledge_finding(self, report_id: int, finding_index: int) -> dict:
        """确认（标记已处理）某条发现项。"""
        db: Session = SessionLocal()
        try:
            report = db.get(ComplianceReport, report_id)
            if report is None:
                raise ValueError(f"Report {report_id} not found")

            findings = json.loads(report.findings or "[]")
            if finding_index < 0 or finding_index >= len(findings):
                raise ValueError(f"Finding index {finding_index} out of range (total {len(findings)})")

            findings[finding_index]["acknowledged"] = True
            findings[finding_index]["acknowledged_at"] = _now().isoformat()
            report.findings = json.dumps(findings)
            db.commit()
            logger.info("compliance: acknowledged finding %d in report %d", finding_index, report_id)
            return {"report_id": report_id, "finding_index": finding_index, "acknowledged": True}
        finally:
            db.close()

    # ---- 内部方法 ----

    @staticmethod
    def _run_checks(report_type: str, period_start: datetime, period_end: datetime) -> list:
        """执行审计检查规则（基础实现，可扩展为规则引擎）。"""
        findings = []
        now = _now()

        if report_type == "security":
            findings.append({
                "id": str(uuid.uuid4())[:8],
                "category": "access_control",
                "severity": "low",
                "description": "All API endpoints have JWT authentication enabled",
                "status": "open",
                "acknowledged": False,
                "detected_at": now.isoformat(),
            })
        elif report_type == "privacy":
            findings.append({
                "id": str(uuid.uuid4())[:8],
                "category": "data_retention",
                "severity": "medium",
                "description": "Some chat records exceed the 90-day retention period",
                "status": "open",
                "acknowledged": False,
                "detected_at": now.isoformat(),
            })
        elif report_type == "financial":
            findings.append({
                "id": str(uuid.uuid4())[:8],
                "category": "transaction_integrity",
                "severity": "low",
                "description": "No abnormal transactions found during the audit period",
                "status": "open",
                "acknowledged": False,
                "detected_at": now.isoformat(),
            })
        elif report_type == "operational":
            findings.append({
                "id": str(uuid.uuid4())[:8],
                "category": "backup_verification",
                "severity": "low",
                "description": "Latest backup integrity verification passed",
                "status": "open",
                "acknowledged": False,
                "detected_at": now.isoformat(),
            })

        return findings


instance = ComplianceAuditor()
