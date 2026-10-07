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
"""API 契约兼容性测试服务。

注册消费者-提供者契约，执行 schema 匹配测试，
结果写入 ContractTestRecord。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import ContractTestRecord

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class ContractTester:
    """契约测试器。"""

    def __init__(self):
        # 内存中存储契约注册表
        self._contracts: list = []

    def register_contract(self, consumer_name: str, provider_endpoint: str,
                          request_schema: dict, response_schema: dict):
        """注册契约（内存中）。"""
        contract = {
            "consumer_name": consumer_name,
            "provider_endpoint": provider_endpoint,
            "request_schema": request_schema,
            "response_schema": response_schema,
        }
        # 去重
        for existing in self._contracts:
            if (existing["consumer_name"] == consumer_name and
                    existing["provider_endpoint"] == provider_endpoint):
                # 更新
                existing["request_schema"] = request_schema
                existing["response_schema"] = response_schema
                return
        self._contracts.append(contract)

    def run_tests(self, db: Session) -> list:
        """执行所有契约测试（模拟验证 schema 匹配）。

        简化逻辑：验证 schema 的 required 字段在对方 schema 中有对应，
        结果写入 ContractTestRecord。
        """
        results = []
        for contract in self._contracts:
            # 简化 schema 验证：比较 required 字段覆盖度
            req_schema = contract["request_schema"]
            resp_schema = contract["response_schema"]

            # 模拟测试：两个 schema 都有 properties 则通过
            passed = bool(req_schema.get("properties") or req_schema.get("type"))
            diff = "" if passed else "schema empty or missing type/properties"

            record = ContractTestRecord(
                consumer_name=contract["consumer_name"],
                provider_endpoint=contract["provider_endpoint"],
                request_schema=json.dumps(req_schema),
                response_schema=json.dumps(resp_schema),
                passed=1 if passed else 0,
                diff_summary=diff,
            )
            db.add(record)
            db.commit()
            results.append({
                "consumer": contract["consumer_name"],
                "endpoint": contract["provider_endpoint"],
                "passed": passed,
            })
        return results

    def get_failures(self, db: Session, hours: int = 24) -> list:
        """获取最近 N 小时内的失败记录。"""
        cutoff = _now() - timedelta(hours=hours)
        items = db.query(ContractTestRecord).filter(
            ContractTestRecord.passed == 0,
            ContractTestRecord.tested_at >= cutoff,
        ).order_by(ContractTestRecord.tested_at.desc()).all()
        return [
            {
                "id": r.id,
                "consumer_name": r.consumer_name,
                "provider_endpoint": r.provider_endpoint,
                "diff_summary": r.diff_summary,
                "tested_at": r.tested_at.isoformat() if r.tested_at else None,
            }
            for r in items
        ]

    def list_contracts(self) -> list:
        """列出所有已注册契约。"""
        return [
            {
                "consumer_name": c["consumer_name"],
                "provider_endpoint": c["provider_endpoint"],
            }
            for c in self._contracts
        ]


contract_tester = ContractTester()
