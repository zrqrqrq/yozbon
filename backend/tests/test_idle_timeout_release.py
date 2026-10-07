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
"""S6 严重级修复测试：自动解约「账本旁路 + 双重释放 TOCTOU」。

覆盖：
- 托管退回改走 wallet.escrow_release 正规路径 → 写 AILedger（可回溯，不再旁路）；
- escrow_release 原子抢权：第二次释放返回 False（幂等/防双重释放）；
- 并发抢先场景：托管已被（正常结算/手工）抢先释放后，idle 解约跳过，
  不二次退款、不标 breached，并写 contract.idle_breach_skip 留痕。
"""
import json
from datetime import datetime, timedelta

import pytest

from app import escrow, idle_timeout, market, wallet
from app.config import settings
from app.database import SessionLocal
from app.models import (AICitizen, AILedger, AIWallet, AIPermission, AuditLog,
                        Contract, CreditProfile, Host, Project, ProjectNode)

P = 10_000  # 托管金额（分）


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid, balance=100_000):
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status="active",
                  class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c.id


def _setup_executing_contract(db, started_hours_ago=0.0):
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h1@t.test", password_hash="x", host_credit=100))
        db.flush()

    buyer_id = _mk_party(db, f"buyer-{id(db)}-{started_hours_ago}")
    worker_id = _mk_party(db, f"worker-{id(db)}-{started_hours_ago}")

    p = Project(host_id=1, title="S6 Project", pm_citizen_id=buyer_id, status="running")
    db.add(p)
    db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=P, status="matching")
    db.add(n)
    db.flush()

    w = db.get(AICitizen, worker_id)
    market.bid(db, w, n.id, P, "报价")
    db.flush()

    c = (db.query(Contract)
           .filter(Contract.worker_id == worker_id, Contract.node_id == n.id).first())
    buyer = db.get(AICitizen, buyer_id)
    escrow.sign_contract(db, buyer, c.id)
    db.flush()

    if started_hours_ago > 0:
        started = datetime.utcnow() - timedelta(hours=started_hours_ago)
        terms = json.loads(c.terms_json or "{}")
        terms["_started_at"] = started.isoformat()
        c.terms_json = json.dumps(terms, ensure_ascii=False)
        db.flush()

    return c.id, worker_id, buyer_id


def test_breach_writes_escrow_ledger(db):
    """解约退款应写 AILedger（正规路径，不再旁路直接改 balance_cent）。"""
    elapsed = settings.IDLE_TIMEOUT_HOURS + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    bal_before = db.get(AIWallet, buyer_id).balance_cent
    idle_timeout.check_idle_contracts(db, now=datetime.utcnow())
    db.flush()

    # 存在指向本合约解约的入账流水，且金额=托管额、balance_after 正确。
    led = (db.query(AILedger)
             .filter(AILedger.citizen_id == buyer_id,
                     AILedger.ref.contains(f"contract:{cid}:idle_breach")).first())
    assert led is not None
    assert led.amount_cent == P
    assert led.balance_after == bal_before + P
    # 钱包余额确实增加了 P（正规入账）
    assert db.get(AIWallet, buyer_id).balance_cent == bal_before + P


def test_escrow_release_is_atomic_no_double_refund(db):
    """wallet.escrow_release 第二次释放（locked 已为 0）返回 False，杜绝双重释放。"""
    elapsed = settings.IDLE_TIMEOUT_HOURS + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    bal_before = db.get(AIWallet, buyer_id).balance_cent

    first = wallet.escrow_release(db, citizen_id=buyer_id, contract_id=cid,
                                  amount_cent=P, ref="manual:rel1")
    db.flush()
    assert first is True
    assert db.get(AIWallet, buyer_id).balance_cent == bal_before + P

    second = wallet.escrow_release(db, citizen_id=buyer_id, contract_id=cid,
                                   amount_cent=P, ref="manual:rel2")
    db.flush()
    assert second is False
    # 第二次未做任何资金改动
    assert db.get(AIWallet, buyer_id).balance_cent == bal_before + P


def test_idle_skip_when_escrow_already_released(db):
    """并发抢先：托管已被正常结算/手工释放后，idle 解约跳过，不二次退款、不标 breached。"""
    elapsed = settings.IDLE_TIMEOUT_HOURS + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    # 模拟并发正常结算抢先释放托管
    assert wallet.escrow_release(db, citizen_id=buyer_id, contract_id=cid,
                                 amount_cent=P, ref="normal:settle") is True
    db.flush()
    bal_after_release = db.get(AIWallet, buyer_id).balance_cent

    result = idle_timeout.check_idle_contracts(db, now=datetime.utcnow())
    db.flush()

    # 未计入 breached；合约仍 executing（解约被安全跳过）
    assert cid not in result["breached"]
    assert db.get(Contract, cid).status == "executing"

    # 没有二次退款：余额停留在抢先释放后的值
    assert db.get(AIWallet, buyer_id).balance_cent == bal_after_release
    # 不存在 idle_breach 的退款流水
    assert (db.query(AILedger)
              .filter(AILedger.citizen_id == buyer_id,
                      AILedger.ref.contains(f"contract:{cid}:idle_breach")).first()) is None

    # 写了跳过留痕
    skip = (db.query(AuditLog)
              .filter(AuditLog.action == "contract.idle_breach_skip").first())
    assert skip is not None
    # 没有正常的 idle_breach 审计
    assert (db.query(AuditLog)
              .filter(AuditLog.action == "contract.idle_breach",
                      AuditLog.detail.contains(f'"contract_id": {cid}')).first()) is None
