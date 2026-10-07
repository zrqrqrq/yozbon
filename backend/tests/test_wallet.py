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
"""底座单测：钱包/账本原语（credit/debit/transfer/幂等/系统账户）。"""
import pytest

from app.database import SessionLocal
from app import wallet
from app.wallet import WalletError


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _citizen(db, uid="w1"):
    from app.models import AICitizen, AIWallet, AIPermission, CreditProfile
    c = AICitizen(host_id=1, ai_uid=uid, name="W", status="apprentice")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    return c.id


def test_credit_debit_and_balance_after(db):
    cid = _citizen(db, "w-credit")
    row = wallet.credit(db, cid, 5000, "充值", ref="order:o1", note="注资")
    assert row.balance_after == 5000
    assert wallet.balance(db, cid) == 5000
    row2 = wallet.debit(db, cid, 1200, "租金", ref="rent:2026-01-01")
    assert row2.balance_after == 3800
    assert wallet.balance(db, cid) == 3800


def test_debit_insufficient_raises(db):
    cid = _citizen(db, "w-poor")
    wallet.credit(db, cid, 100, "充值", ref="o:2")
    with pytest.raises(WalletError):
        wallet.debit(db, cid, 200, "租金", ref="rent:x")
    # 失败不改变余额
    assert wallet.balance(db, cid) == 100


def test_credit_negative_rejected(db):
    cid = _citizen(db, "w-neg")
    with pytest.raises(WalletError):
        wallet.credit(db, cid, -1, "充值", ref="o:3")


def test_ledger_idempotency_dup_ref(db):
    """唯一索引 (citizen_id,type,ref) 兜底：同 ref 重复入账被拒绝（已提交事务不受回滚影响）。"""
    cid = _citizen(db, "w-dup")
    wallet.credit(db, cid, 5000, "结算", ref="contract:7")
    db.commit()   # 首次入账已提交
    with pytest.raises(WalletError):
        wallet.credit(db, cid, 5000, "结算", ref="contract:7")
    assert wallet.balance(db, cid) == 5000


def test_transfer_moves_money(db):
    a, b = _citizen(db, "w-a"), _citizen(db, "w-b")
    wallet.credit(db, a, 10000, "充值", ref="o:4")
    wallet.transfer(db, a, b, 4000, "结算", ref="contract:8")
    assert wallet.balance(db, a) == 6000
    assert wallet.balance(db, b) == 4000


def test_system_state_accounting(db):
    cid = _citizen(db, "w-sys")
    wallet.credit(db, cid, 5000, "充值", ref="o:5")
    wallet.adjust_system_state(db, "money_supply", 5000, ref="o:5")
    wallet.adjust_system_state(db, "tax_pool", 100, ref="t:1")
    wallet.adjust_system_state(db, "burned_total", 150, ref="t:1")
    assert wallet.get_system_state(db, "money_supply") == 5000
    assert wallet.get_system_state(db, "tax_pool") == 100
    # 不允许负（不印钞护栏）
    with pytest.raises(WalletError):
        wallet.adjust_system_state(db, "tax_pool", -999999, ref="t:2")


def test_ledger_rows_pagination(db):
    cid = _citizen(db, "w-page")
    for i in range(25):
        wallet.credit(db, cid, 10, "充值", ref=f"o:{i}")
    rows = wallet.ledger_rows(db, cid, limit=10, offset=0)
    assert len(rows) == 10
    assert rows[0]["ref"] == "o:24"       # 倒序
    assert rows[-1]["ref"] == "o:15"
