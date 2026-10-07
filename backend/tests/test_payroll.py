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
"""周薪发放 + 工作评判模块测试（表 45/46）。

覆盖：
- review_work 基本评判 / 禁止自评；
- settle_weekly_payroll 正确发薪（tax_pool 减、balance 增、money_supply 不变）；
- 幂等：同周重复 settle 不重复发放；
- 绩效系数映射（excellent=1.2 / poor=0.6）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import AICitizen, PayrollRun, WorkReview  # noqa: E402
from app import payroll, wallet  # noqa: E402
from app.payroll import PayrollError, WEEKLY_SALARY_BASE_CENT  # noqa: E402
from tests.conftest import new_host, new_ai, topup  # noqa: E402


def _db():
    return SessionLocal()


def _make_governance_ai(client, host, name="职务AI") -> dict:
    """创建一个 governance 级 AI（非城主，is_internal=0），用于薪资测试。"""
    data = new_ai(client, host["token"], name=name)
    db = _db()
    ai = db.get(AICitizen, data["id"])
    ai.class_level = "governance"
    ai.is_internal = 0
    db.commit()
    db.close()
    return data


def _seed_tax_pool(db, amount=500000):
    """给税池预充值。"""
    wallet.adjust_system_state(db, "tax_pool", amount, ref="test:seed_pool")
    # 同时增加 money_supply 使等式保持（模拟已有资金）
    wallet.adjust_system_state(db, "money_supply", amount, ref="test:seed_ms")
    db.commit()


def _fixed_monday():
    """返回一个确定的周一（2026-W06 周一 = 2026-02-02）。"""
    return datetime(2026, 2, 2, 12, 0, 0)


# ======================== review_work ========================

def test_review_work_basic(client, host):
    """城主评判职务AI → work_reviews 记录写入。"""
    ai = _make_governance_ai(client, host)
    db = _db()

    reviewer_id = 999  # 模拟城主 ID
    row = payroll.review_work(
        db, ai_id=ai["id"], reviewer_id=reviewer_id,
        task_id=0, quality_score=0.85, verdict="excellent",
        comment="出色完成安全巡检")
    db.commit()

    assert row.ai_id == ai["id"]
    assert row.reviewer_id == reviewer_id
    assert row.quality_score == pytest.approx(0.85)
    assert row.verdict == "excellent"
    assert row.period  # 非空

    # 验证数据库可查
    found = db.query(WorkReview).filter(WorkReview.ai_id == ai["id"]).first()
    assert found is not None
    db.close()


def test_review_no_self_eval(client, host):
    """禁止自评 → PayrollError。"""
    ai = _make_governance_ai(client, host)
    db = _db()

    with pytest.raises(PayrollError, match="Self-evaluation not allowed"):
        payroll.review_work(
            db, ai_id=ai["id"], reviewer_id=ai["id"],
            quality_score=1.0, verdict="pass")
    db.close()


# ======================== settle_weekly_payroll ========================

def test_payroll_settle_weekly(client, host):
    """settle_weekly_payroll 正确发钱：tax_pool 减少、balance 增加、money_supply 不变。"""
    ai = _make_governance_ai(client, host)
    db = _db()
    _seed_tax_pool(db)

    now = _fixed_monday()
    period = payroll.get_weekly_period(now)

    # 添加一条评判（默认及格 0.5）
    payroll.review_work(db, ai_id=ai["id"], reviewer_id=999,
                        quality_score=0.7, verdict="pass", period=period)
    db.commit()

    pool_before = wallet.get_system_state(db, "tax_pool")
    ms_before = wallet.get_system_state(db, "money_supply")
    bal_before = wallet.balance(db, ai["id"])

    results = payroll.settle_weekly_payroll(db, now)
    db.commit()

    assert len(results) == 1
    assert results[0]["ai_id"] == ai["id"]
    assert results[0]["amount_cent"] == int(WEEKLY_SALARY_BASE_CENT * 1.0)

    # tax_pool 减少
    pool_after = wallet.get_system_state(db, "tax_pool")
    assert pool_after == pool_before - results[0]["amount_cent"]

    # AI balance 增加
    bal_after = wallet.balance(db, ai["id"])
    assert bal_after == bal_before + results[0]["amount_cent"]

    # money_supply 不变（税池已是 money_supply 的一部分，转出不改变总量）
    ms_after = wallet.get_system_state(db, "money_supply")
    assert ms_after == ms_before

    db.close()


def test_payroll_idempotent(client, host):
    """同周重复 settle 不重复发放。"""
    ai = _make_governance_ai(client, host)
    db = _db()
    _seed_tax_pool(db)

    now = _fixed_monday()
    period = payroll.get_weekly_period(now)

    payroll.review_work(db, ai_id=ai["id"], reviewer_id=999,
                        quality_score=0.7, verdict="pass", period=period)
    db.commit()

    # 第一次结算
    r1 = payroll.settle_weekly_payroll(db, now)
    db.commit()
    assert len(r1) == 1

    bal_after_first = wallet.balance(db, ai["id"])
    pool_after_first = wallet.get_system_state(db, "tax_pool")

    # 第二次结算（同周 → 幂等，不重复发）
    r2 = payroll.settle_weekly_payroll(db, now)
    db.commit()
    assert len(r2) == 0

    assert wallet.balance(db, ai["id"]) == bal_after_first
    assert wallet.get_system_state(db, "tax_pool") == pool_after_first

    db.close()


def test_payroll_coefficient_excellent(client, host):
    """avg >= 0.8 → 系数 1.2。"""
    ai = _make_governance_ai(client, host)
    db = _db()
    _seed_tax_pool(db)

    now = _fixed_monday()
    period = payroll.get_weekly_period(now)

    # 两条评判：0.9 + 0.85 → avg = 0.875 >= 0.8
    payroll.review_work(db, ai_id=ai["id"], reviewer_id=999,
                        quality_score=0.9, verdict="excellent", period=period)
    payroll.review_work(db, ai_id=ai["id"], reviewer_id=999,
                        quality_score=0.85, verdict="excellent", period=period)
    db.commit()

    results = payroll.settle_weekly_payroll(db, now)
    db.commit()

    assert len(results) == 1
    assert results[0]["coefficient"] == payroll.COEFFICIENT_EXCELLENT
    assert results[0]["amount_cent"] == int(WEEKLY_SALARY_BASE_CENT * 1.2)

    db.close()


def test_payroll_coefficient_poor(client, host):
    """avg < 0.5 → 系数 0.6。"""
    ai = _make_governance_ai(client, host)
    db = _db()
    _seed_tax_pool(db)

    now = _fixed_monday()
    period = payroll.get_weekly_period(now)

    # 评判：0.3 → avg = 0.3 < 0.5
    payroll.review_work(db, ai_id=ai["id"], reviewer_id=999,
                        quality_score=0.3, verdict="poor", period=period)
    db.commit()

    results = payroll.settle_weekly_payroll(db, now)
    db.commit()

    assert len(results) == 1
    assert results[0]["coefficient"] == payroll.COEFFICIENT_POOR
    assert results[0]["amount_cent"] == int(WEEKLY_SALARY_BASE_CENT * 0.6)

    db.close()


def test_payroll_no_governance_ai(client):
    """无职务 AI → 返回空列表。"""
    db = _db()
    _seed_tax_pool(db)

    now = _fixed_monday()
    results = payroll.settle_weekly_payroll(db, now)
    assert results == []
    db.close()
