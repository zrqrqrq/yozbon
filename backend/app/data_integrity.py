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
"""数据完整性检查服务。

注册并运行各类不变量检查（钱包余额对账、负余额检测、孤立引用检测等），
结果写入 DataIntegrityCheck。
"""
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import (AIWallet, AILedger, AICitizen, DataIntegrityCheck)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class DataIntegrityService:
    """数据完整性检查服务。"""

    def __init__(self):
        self._checks: list = []  # [(name, func, severity)]

    def register_check(self, name: str, check_func, severity: str = "warning"):
        """注册完整性检查函数。"""
        self._checks.append((name, check_func, severity))

    def run_all(self, db: Session) -> list:
        """运行所有检查，结果写入 DataIntegrityCheck（注册为 periodic）。"""
        results = []

        # 内置检查
        built_in = [
            ("wallet_balance_consistency", self.check_wallet_balance, "critical"),
            ("no_negative_balances", self.check_negative_balances, "critical"),
            ("no_orphan_references", self.check_orphan_references, "warning"),
        ]

        all_checks = built_in + self._checks
        for name, func, severity in all_checks:
            try:
                result = func(db)
                record = DataIntegrityCheck(
                    check_name=name,
                    invariant=result.get("invariant", name),
                    passed=1 if result.get("passed", False) else 0,
                    expected=str(result.get("expected", "")),
                    actual=str(result.get("actual", "")),
                    severity=severity,
                )
                db.add(record)
                db.commit()
                results.append({
                    "check_name": name,
                    "passed": result.get("passed", False),
                    "severity": severity,
                })
            except Exception as exc:
                logger.error("integrity check %s failed: %s", name, exc)
                results.append({
                    "check_name": name,
                    "passed": False,
                    "severity": severity,
                    "error": str(exc),
                })
        return results

    def check_wallet_balance(self, db: Session) -> dict:
        """wallet.balance == SUM(ledger amounts)。"""
        wallets = db.query(AIWallet).all()
        mismatches = 0
        details = []

        for w in wallets:
            ledger_sum = db.query(func.coalesce(func.sum(AILedger.amount_cent), 0)).filter(
                AILedger.citizen_id == w.citizen_id
            ).scalar() or 0
            if w.balance_cent != ledger_sum:
                mismatches += 1
                details.append(f"citizen {w.citizen_id}: wallet={w.balance_cent} ledger={ledger_sum}")

        passed = mismatches == 0
        return {
            "invariant": "wallet.balance_cent == SUM(ai_ledger.amount_cent)",
            "passed": passed,
            "expected": "0 mismatches",
            "actual": f"{mismatches} mismatches: {details[:5]}" if details else "0",
        }

    def check_negative_balances(self, db: Session) -> dict:
        """检测负余额。"""
        negatives = db.query(AIWallet).filter(AIWallet.balance_cent < 0).count()
        return {
            "invariant": "no wallet has negative balance",
            "passed": negatives == 0,
            "expected": "0 negative wallets",
            "actual": f"{negatives} negative wallets",
        }

    def check_orphan_references(self, db: Session) -> dict:
        """检测孤立引用（ledger 引用了不存在的 citizen）。"""
        # 查找 ledger 中引用了不存在 citizen_id 的记录
        orphan_count = db.query(AILedger).outerjoin(
            AICitizen, AILedger.citizen_id == AICitizen.id
        ).filter(AICitizen.id.is_(None)).count()

        return {
            "invariant": "all ledger citizen_id reference valid citizen",
            "passed": orphan_count == 0,
            "expected": "0 orphans",
            "actual": f"{orphan_count} orphans",
        }

    def get_latest_results(self, db: Session) -> list:
        """获取最近一次各检查结果。"""
        from sqlalchemy import text
        subq = db.query(
            DataIntegrityCheck.check_name,
            func.max(DataIntegrityCheck.checked_at).label("latest"),
        ).group_by(DataIntegrityCheck.check_name).subquery()

        results = db.query(DataIntegrityCheck).join(
            subq,
            (DataIntegrityCheck.check_name == subq.c.check_name) &
            (DataIntegrityCheck.checked_at == subq.c.latest),
        ).all()

        return [
            {
                "check_name": r.check_name,
                "passed": bool(r.passed),
                "severity": r.severity,
                "expected": r.expected,
                "actual": r.actual,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in results
        ]


data_integrity = DataIntegrityService()
