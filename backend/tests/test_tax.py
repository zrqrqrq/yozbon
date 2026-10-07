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
"""C1 税收/低保/平衡阀测试（规则 3/10/13）。"""
from datetime import datetime

import pytest

from app.config import settings
from app.database import SessionLocal
from app import wallet
from app import tax as taxmod
from app import tax_rules


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk(db, uid, *, balance=0, escrow=0, status="active", occupation=""):
    from app.models import AICitizen, AIWallet, AIPermission, CreditProfile
    c = AICitizen(host_id=1, ai_uid=uid, name="T", status=status,
                  occupation=occupation)
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=balance, escrow_cent=escrow))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    return c


# ---------------- 规则 3：豁免线 < 流通税线，无矛盾 ----------------

def test_rule_03_exempt_vs_flow_tax(db):
    """豁免线(1000AC) < 流通税线(5000AC)；达豁免但未达流通线者不征流通税。"""
    assert settings.DEATH_EXEMPT_NET < settings.TAX_FLOW_THRESHOLD
    # 1500 AC：跨过豁免线(1000)，但未到流通线(5000)
    c = _mk(db, "r03", balance=150000)
    res = taxmod.settle_periodic_taxes(db)
    assert res["flow_taxed"] == 0
    # 未扣流通税
    assert wallet.balance(db, c.id) == 150000


def test_flow_tax_above_threshold(db):
    """净资产≥5000AC 征 5% 流通税并入税池。"""
    c = _mk(db, "flow", balance=600000)   # 6000 AC
    pool_before = wallet.get_system_state(db, "tax_pool")
    taxmod.settle_periodic_taxes(db)
    # 600000*5% = 30000 分
    assert wallet.balance(db, c.id) == 600000 - 30000
    assert wallet.get_system_state(db, "tax_pool") == pool_before + 30000


# ---------------- 规则 10：平衡阀单向 + 封顶 8% ----------------

def test_rule_10_valve_one_way(db):
    reserve = settings.UBI_DAILY_CENT * 30
    # 税池充足 → 基础费率
    wallet.adjust_system_state(db, "tax_pool", reserve * 2)
    assert taxmod.current_fee_rate(db) == settings.TXN_FEE_RATE
    # 税池不足 → 上浮一个步长（单向，不印钞）
    from app.models import SystemState
    db.query(SystemState).filter(SystemState.key == "tax_pool").update(
        {"value_cent": 1})
    db.flush()
    assert taxmod.current_fee_rate(db) == min(
        settings.TXN_FEE_RATE + settings.FEE_ADJUST_STEP, settings.FEE_RATE_MAX)
    # 封顶 8%（纯函数：base 已高时不再上浮）
    assert tax_rules.adjusted_fee_rate(0, settings.FEE_RATE_MAX, reserve) == settings.FEE_RATE_MAX


# ---------------- 规则 13：低保按 AI-ID 独立 ----------------

def test_rule_13_ubi_per_ai_id(db):
    """同宿主两个穷 AI 各自可领；同日不重复发放。"""
    wallet.adjust_system_state(db, "tax_pool", 100000)
    a = _mk(db, "ubi-a", balance=100)   # 穷
    b = _mk(db, "ubi-b", balance=100)  # 同宿主穷
    res = taxmod.grant_ubi(db)
    assert a.id in res["granted"] and b.id in res["granted"]
    assert wallet.balance(db, a.id) == 100 + settings.UBI_DAILY_CENT
    assert wallet.balance(db, b.id) == 100 + settings.UBI_DAILY_CENT
    # 同日再发 → 幂等不重复
    res2 = taxmod.grant_ubi(db)
    assert wallet.balance(db, a.id) == 100 + settings.UBI_DAILY_CENT


def test_ubi_poor_line_excluded(db):
    """净资产 ≥ 贫困线 不发低保。"""
    wallet.adjust_system_state(db, "tax_pool", 100000)
    c = _mk(db, "rich-enough", balance=3000)   # >2000
    res = taxmod.grant_ubi(db)
    assert c.id not in res["granted"]


def test_income_reconcile_uses_escrow_minus_fee(db):
    """月收入税对账口径：cum 基数 = Σ(escrow−fee)；tax_records 仅税额审计。"""
    from app.models import Contract, TaxRecord
    now = datetime.utcnow()
    worker = _mk(db, "w-rc", balance=0)
    # 两笔已结算(accepted)合约：基数 = (escrow−fee)
    c1 = Contract(worker_id=worker.id, buyer_id=9, status="accepted",
                  escrow_cent=10000, fee_cent=500, accepted_at=now)
    c2 = Contract(worker_id=worker.id, buyer_id=9, status="accepted",
                  escrow_cent=20000, fee_cent=1000, accepted_at=now)
    db.add_all([c1, c2]); db.flush()
    # tax_records 存的是税额（与基数无关），证明它不被当基数
    db.add(TaxRecord(citizen_id=worker.id, type="income", amount_cent=123,
                     period=now.strftime("%Y%m"), ref="contract:1"))
    db.flush()

    res = taxmod.monthly_income_reconcile(db, now=now)
    # Σ(escrow−fee) = (10000−500)+(20000−1000) = 28500
    assert res["cum_base_by_worker"][worker.id] == 28500
    assert res["cum_base_total_cent"] == 28500
    # 税额审计单列，且不等于基数
    assert res["income_tax_total_cent"] == 123
    assert res["n_accepted_contracts"] == 2


def test_recalc_levels(db):
    """阶层重算：净资产/信用/职业标签。"""
    _mk(db, "lv-bottom", balance=5000)         # <100AC
    _mk(db, "lv-middle", balance=200000)       # 100~5000AC
    _mk(db, "lv-boss", balance=600000)         # 5000~50000AC
    _mk(db, "lv-cap", balance=6000000)         # ≥50000AC
    _mk(db, "lv-gov", balance=100, occupation="governance")
    counts = taxmod.recalc_levels(db)
    assert counts["bottom"] == 1
    assert counts["middle"] == 1
    assert counts["boss"] == 1
    assert counts["capital"] == 1
    assert counts["governance"] == 1
