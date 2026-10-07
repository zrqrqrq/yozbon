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
"""S8 验证：platform_facts.collect_security_facts 接入真实数据源，不再返回空 mock。

覆盖：
- 插入 DependencyVulnReport 后 dep_vulns 非空；
- 插入 CORSPolicy（通配+credentials）后 services 含风险标记；
- 无任何数据时 dep_vulns/login_anomalies 为空（正常态安全，非恒 safe mock）。
"""
import sys
import pathlib

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import pytest
from datetime import datetime
from app.database import SessionLocal
from app.models import DependencyVulnReport, CORSPolicy


class TestS8SecurityFacts:
    """S8：安全事实采集返回真实数据，非空 mock。"""

    def test_dep_vulns_populated_from_report(self):
        """有未确认漏洞时 dep_vulns 非空。"""
        db = SessionLocal()
        try:
            v = DependencyVulnReport(
                package_name="requests", version="2.28.0",
                cve_id="CVE-2023-32681", severity="medium",
                description="Proxy leak", fixed_version="2.31.0",
                acknowledged=0,
            )
            db.add(v)
            db.commit()

            from app.platform_facts import collect_security_facts
            facts = collect_security_facts(db)
            assert "dep_vulns" in facts
            assert len(facts["dep_vulns"]) >= 1, "dep_vulns 不应为空（已注入漏洞记录）"
            vuln = facts["dep_vulns"][0]
            assert vuln["package"] == "requests"
            assert vuln["cve_id"] == "CVE-2023-32681"
        finally:
            db.rollback()
            db.close()

    def test_cors_risk_flagged(self):
        """通配 origin + credentials=True 的 CORS 策略被标记为风险。"""
        db = SessionLocal()
        try:
            p = CORSPolicy(
                origin_pattern="*", methods="GET,POST",
                headers="*", credentials=1,
                max_age=3600, tenant_code="default",
            )
            db.add(p)
            db.commit()

            from app.platform_facts import collect_security_facts
            facts = collect_security_facts(db)
            assert "services" in facts
            risky = [s for s in facts["services"] if s.get("risk")]
            assert len(risky) >= 1, "通配+credentials 的 CORS 应被标记风险"
            assert risky[0]["risk"] == "wildcard_origin_with_credentials"
        finally:
            db.rollback()
            db.close()

    def test_clean_db_returns_empty_lists(self):
        """无漏洞、无异常时 dep_vulns 和 login_anomalies 为空（真实无风险，非恒 safe mock）。"""
        db = SessionLocal()
        try:
            # 确认无漏洞记录（先清理）
            db.query(DependencyVulnReport).delete()
            db.query(CORSPolicy).delete()
            db.commit()

            from app.platform_facts import collect_security_facts
            facts = collect_security_facts(db)
            # 无数据时返回空列表（函数逻辑真实，不是硬编码空）
            assert facts["dep_vulns"] == []
            assert facts["login_anomalies"] == []
            assert "ports" in facts
            assert "services" in facts
        finally:
            db.close()

    def test_return_keys_stable(self):
        """返回键固定为 ports/services/dep_vulns/login_anomalies。"""
        db = SessionLocal()
        try:
            from app.platform_facts import collect_security_facts
            facts = collect_security_facts(db)
            assert set(facts.keys()) == {"ports", "services", "dep_vulns", "login_anomalies"}
            assert isinstance(facts["ports"], list)
            assert isinstance(facts["services"], list)
            assert isinstance(facts["dep_vulns"], list)
            assert isinstance(facts["login_anomalies"], list)
        finally:
            db.close()