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
"""S3 严重级修复测试：周薪发放「税池不足批量全有或全无」→ 改为逐个降级。

覆盖：
- 税池不足以覆盖全部待发 AI 时，成功的照常入账留痕（PayrollRun + AILedger），
  税池耗尽后的 AI 被跳过而非整批失败（不再全有或全无）；
- 发放前税池余额低于待发放总额 → 写一条 payroll.tax_pool_shortfall 预警；
- 被跳过的 AI 无 PayrollRun(paid)、无周薪流水（半成品已回滚）；
- 成功 AI 入账金额与税池扣减一致（资金口径守恒）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, AILedger, AuditLog, PayrollRun  # noqa: E402
from app import payroll, wallet  # noqa: E402
from app.payroll import WEEKLY_SALARY_BASE_CENT  # noqa: E402
from tests.conftest import new_host, new_ai, topup  # noqa: E402


def _db():
    return SessionLocal()


def _make_governance_ai(client, host, name):
    """创建 governance 级、非城主的职务 AI（用于薪资发放）。"""
    data = new_ai(client, host["token"], name=name)
    db = _db()
    ai = db.get(AICitizen, data["id"])
    ai.class_level = "governance"
    ai.is_internal = 0
    db.commit()
    db.close()
    return data


def _fixed_monday():
    return datetime(2026, 2, 2, 12, 0, 0)


def test_payroll_degrades_when_tax_pool_insufficient(client, host):
    """税池仅够 1 名 AI：第 1 名成功入账留痕，第 2 名被跳过，整批不再全量失败。"""
    ai1 = _make_governance_ai(client, host, "职务AI-1")
    ai2 = _make_governance_ai(client, host, "职务AI-2")

    db = _db()
    # 税池只预置 7000 分：仅够 1 名及格 AI（5000 分）发放，第 2 名必然不足。
    wallet.adjust_system_state(db, "tax_pool", 7000, ref="test:s3:pool")
    db.commit()

    now = _fixed_monday()

    pool_before = wallet.get_system_state(db, "tax_pool")
    bal1_before = wallet.balance(db, ai1["id"])
    bal2_before = wallet.balance(db, ai2["id"])

    results = payroll.settle_weekly_payroll(db, now)

    # 逐个降级：只成功发放能负担得起的那 1 名（不再整批抛错返回 0 或全量）。
    assert len(results) == 1
    assert results[0]["amount_cent"] == int(WEEKLY_SALARY_BASE_CENT * 1.0)

    # 税池被扣减恰好 1 份周薪，剩余 2000 分。
    assert wallet.get_system_state(db, "tax_pool") == pool_before - results[0]["amount_cent"]
    assert wallet.get_system_state(db, "tax_pool") == 2000

    # 成功的那名 AI：余额 +5000，且写了 PayrollRun(paid) 与周薪流水（入账留痕）。
    funded_id = results[0]["ai_id"]
    assert wallet.balance(db, funded_id) == (bal1_before if funded_id == ai1["id"] else bal2_before) + results[0]["amount_cent"]
    pr = (db.query(PayrollRun)
            .filter(PayrollRun.ai_id == funded_id, PayrollRun.status == "paid").first())
    assert pr is not None
    led = (db.query(AILedger)
             .filter(AILedger.citizen_id == funded_id,
                     AILedger.type == "周薪").first())
    assert led is not None

    # 被跳过的那名 AI：无 paid PayrollRun、无周薪流水、余额未变（半成品已回滚）。
    skipped_id = ai2["id"] if funded_id == ai1["id"] else ai1["id"]
    assert (db.query(PayrollRun)
              .filter(PayrollRun.ai_id == skipped_id,
                      PayrollRun.status == "paid").first()) is None
    assert (db.query(AILedger)
              .filter(AILedger.citizen_id == skipped_id,
                      AILedger.type == "周薪").first()) is None
    assert wallet.balance(db, skipped_id) == (bal2_before if skipped_id == ai2["id"] else bal1_before)

    db.close()


def test_payroll_writes_shortfall_warning(client, host):
    """发放前税池 < 待发放总额 → 写一条 payroll.tax_pool_shortfall 预警审计。"""
    _make_governance_ai(client, host, "职务AI-W1")
    _make_governance_ai(client, host, "职务AI-W2")

    db = _db()
    # 2 名 AI 待发放总额 = 10000 分，税池仅 3000 分 → 触发预警。
    wallet.adjust_system_state(db, "tax_pool", 3000, ref="test:s3:warn_pool")
    db.commit()

    payroll.settle_weekly_payroll(db, _fixed_monday())

    warn = (db.query(AuditLog)
              .filter(AuditLog.action == "payroll.tax_pool_shortfall").first())
    assert warn is not None
    assert "total_needed_cent" in warn.detail
    db.close()


def test_payroll_no_warning_when_pool_sufficient(client, host):
    """税池充足 → 不写预警，正常全额发放（回归护栏：改动不影响健康路径）。"""
    ai = _make_governance_ai(client, host, "职务AI-OK")

    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 500000, ref="test:s3:ok_pool")
    db.commit()

    results = payroll.settle_weekly_payroll(db, _fixed_monday())
    assert len(results) == 1

    warn = (db.query(AuditLog)
              .filter(AuditLog.action == "payroll.tax_pool_shortfall").first())
    assert warn is None
    db.close()


def test_payroll_idempotent_after_partial(client, host):
    """部分发放后，对已 paid 的 AI 同周再次结算应幂等跳过（不重复入账）。"""
    ai1 = _make_governance_ai(client, host, "职务AI-I1")
    ai2 = _make_governance_ai(client, host, "职务AI-I2")

    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 7000, ref="test:s3:idem_pool")
    db.commit()

    now = _fixed_monday()
    r1 = payroll.settle_weekly_payroll(db, now)
    assert len(r1) == 1
    funded_id = r1[0]["ai_id"]
    bal_funded = wallet.balance(db, funded_id)
    pool_after = wallet.get_system_state(db, "tax_pool")

    # 再次结算：已 paid 的那名幂等跳过；未发的另一名仍因税池不足被跳过。
    r2 = payroll.settle_weekly_payroll(db, now)
    assert len(r2) == 0
    assert wallet.balance(db, funded_id) == bal_funded
    assert wallet.get_system_state(db, "tax_pool") == pool_after
    db.close()
