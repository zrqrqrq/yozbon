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
"""软件组成分析（SCA）扫描服务。

读取 requirements.txt，使用内置 CVE 数据模拟漏洞检查，
结果写入 DependencyVulnReport。
"""
import logging
import os
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import DependencyVulnReport

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


# 内置示例 CVE 数据（模拟 CVE 数据库）
_KNOWN_CVES = [
    {"package": "requests", "version_lt": "2.31.0", "cve_id": "CVE-2023-32681",
     "severity": "medium", "description": "Requests leaks Proxy-Authorization header",
     "fixed_version": "2.31.0"},
    {"package": "pyyaml", "version_lt": "6.0.1", "cve_id": "CVE-2020-14343",
     "severity": "high", "description": "PyYAML arbitrary code execution via full_load",
     "fixed_version": "5.4"},
    {"package": "cryptography", "version_lt": "41.0.0", "cve_id": "CVE-2023-38325",
     "severity": "high", "description": "OpenSSL NULL pointer dereference",
     "fixed_version": "41.0.0"},
    {"package": "pillow", "version_lt": "10.0.1", "cve_id": "CVE-2023-4863",
     "severity": "critical", "description": "libwebp heap buffer overflow",
     "fixed_version": "10.0.1"},
]


class SCAScanner:
    """软件组成分析扫描器。"""

    def scan(self, db: Session) -> list:
        """读取 requirements.txt，模拟 CVE 检查（注册为 weekly）。"""
        # 查找 requirements.txt
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        req_file = os.path.join(base_dir, "requirements.txt")

        packages = []
        if os.path.exists(req_file):
            with open(req_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # 解析 package==version 格式
                        if "==" in line:
                            pkg, ver = line.split("==", 1)
                            packages.append((pkg.strip().lower(), ver.strip()))
                        elif ">=" in line:
                            pkg = line.split(">=")[0].strip().lower()
                            packages.append((pkg, ""))

        vulnerabilities = []
        for pkg_name, pkg_ver in packages:
            for cve in _KNOWN_CVES:
                if cve["package"].lower() == pkg_name:
                    # 简化版本比较：存在性检查
                    report = DependencyVulnReport(
                        package_name=pkg_name,
                        version=pkg_ver,
                        cve_id=cve["cve_id"],
                        severity=cve["severity"],
                        description=cve["description"],
                        fixed_version=cve["fixed_version"],
                    )
                    db.add(report)
                    db.commit()
                    vulnerabilities.append({
                        "package": pkg_name,
                        "cve_id": cve["cve_id"],
                        "severity": cve["severity"],
                    })

        logger.info("SCA scan complete: %d packages, %d vulnerabilities",
                    len(packages), len(vulnerabilities))
        return vulnerabilities

    def get_vulnerabilities(self, db: Session, min_severity: str = "low") -> list:
        """获取漏洞列表。"""
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        min_val = severity_order.get(min_severity, 0)

        items = db.query(DependencyVulnReport).filter(
            DependencyVulnReport.acknowledged == 0
        ).all()
        return [
            {
                "id": v.id,
                "package_name": v.package_name,
                "version": v.version,
                "cve_id": v.cve_id,
                "severity": v.severity,
                "description": v.description,
                "fixed_version": v.fixed_version,
            }
            for v in items
            if severity_order.get(v.severity, 0) >= min_val
        ]

    def acknowledge(self, db: Session, vuln_id: int):
        """确认（忽略）某漏洞。"""
        vuln = db.get(DependencyVulnReport, vuln_id)
        if vuln is None:
            raise ValueError(f"Vulnerability record {vuln_id} not found")
        vuln.acknowledged = 1
        db.commit()

    def get_summary(self, db: Session) -> dict:
        """获取漏洞摘要。"""
        from sqlalchemy import func
        items = db.query(
            DependencyVulnReport.severity,
            func.count(DependencyVulnReport.id),
        ).filter(
            DependencyVulnReport.acknowledged == 0
        ).group_by(DependencyVulnReport.severity).all()

        by_severity = {s: c for s, c in items}
        total = sum(by_severity.values())
        return {"total": total, "by_severity": by_severity}


sca_scanner = SCAScanner()
