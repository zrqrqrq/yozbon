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
"""C1 信贷测试（规则 9：放贷≤净资产50%、还款回收货币、宿主开关）。"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app import wallet
from app import loans


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk(db, uid, *, class_level="bottom", balance=0, loan_enabled=0,
        escrow=0, status="active"):
    from app.models import AICitizen, AIWallet, AIPermission, CreditProfile
    c = AICitizen(host_id=1, ai_uid=uid, name="T", status=status,
                  class_level=class_level)
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=balance, escrow_cent=escrow))
    db.add(AIPermission(citizen_id=c.id, loan_enabled=loan_enabled))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    return c


def test_rule_09_loan_cap_50pct_and_recover(db):
    """放贷上限=净资产50%；发放入 M、还款回收 M。"""
    lender = _mk(db, "ln-cap", class_level="capital", balance=100000)  # 净 100000 → cap 50000
    borrower = _mk(db, "ln-borrow", balance=1000, loan_enabled=1)

    # 超上限被拒
    with pytest.raises(loans.LoanError):
        loans.apply_loan(db, borrower.id, lender.id, 60000)

    # 40000 通过（入 M）
    m_before = wallet.get_system_state(db, "money_supply")
    d = loans.apply_loan(db, borrower.id, lender.id, 40000)
    assert d["amount_cent"] == 40000
    assert wallet.balance(db, borrower.id) == 41000
    assert wallet.get_system_state(db, "money_supply") == m_before + 40000

    # 再借 2000 → 累计 42000+... 已放 40000，再加 2000=42000<50000 通过；
    # 但 15000 会超（40000+15000=55000>50000）
    with pytest.raises(loans.LoanError):
        loans.apply_loan(db, borrower.id, lender.id, 15000)

    # 还款：本金回收出 M，利息入贷方
    m_after_apply = wallet.get_system_state(db, "money_supply")
    lender_bal_before = wallet.balance(db, lender.id)
    rep = loans.repay_loan(db, d["loan_id"], borrower.id)
    assert rep["principal_cent"] == 40000
    assert wallet.get_system_state(db, "money_supply") == m_after_apply - 40000
    # 利息入贷方（40000*1%=400）
    assert wallet.balance(db, lender.id) == lender_bal_before + 400


def test_loan_host_switch_off(db):
    """宿主未开借贷权限 → 拒绝。"""
    lender = _mk(db, "ln-cap2", class_level="capital", balance=100000)
    borrower = _mk(db, "ln-no", balance=1000, loan_enabled=0)
    with pytest.raises(loans.LoanError):
        loans.apply_loan(db, borrower.id, lender.id, 1000)


def test_lender_must_be_capital(db):
    """非资本/治理 AI 不可放贷。"""
    lender = _mk(db, "ln-worker", class_level="middle", balance=100000)
    borrower = _mk(db, "ln-b2", balance=1000, loan_enabled=1)
    with pytest.raises(loans.LoanError):
        loans.apply_loan(db, borrower.id, lender.id, 1000)
