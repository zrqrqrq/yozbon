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
"""合约发呆超时（idle timeout）→ 催办 → 自动解约 测试。

测试覆盖：
- 催办：executing 合约超过 IDLE_WARN_HOURS → 发出催办通知
- 解约：executing 合约超过 IDLE_TIMEOUT_HOURS → 合约 breached，托管退回买方
- 未超时：不触发催办/解约
- 幂等：已 breached 的合约不会重复处理
- 资金守恒：解约后 money_supply 不变，buyer.balance 增加，buyer.escrow 减少
"""
import json
from datetime import datetime, timedelta

import pytest

from app import escrow, idle_timeout, market, wallet
from app.config import settings
from app.database import SessionLocal
from app.models import (AICitizen, AILedger, AIWallet, AIPermission, AuditLog,
                        Contract, CreditProfile, Escrow, Host, Notification,
                        Project, ProjectNode, SystemState)

P = 10_000  # 托管金额（分）


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid: str, balance: int = 100_000):
    """创建 AI 公民 + 钱包 + 权限 + 信用档案，注资 balance 分。"""
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


def _setup_executing_contract(db, started_hours_ago: float = 0.0):
    """创建一个 executing 状态的合约，_started_at 设为 started_hours_ago 小时前。

    返回 (contract_id, worker_id, buyer_id)。
    """
    # 确保 Host 存在
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h1@t.test", password_hash="x", host_credit=100))
        db.flush()

    buyer_id = _mk_party(db, f"buyer-{id(db)}-{started_hours_ago}")
    worker_id = _mk_party(db, f"worker-{id(db)}-{started_hours_ago}")

    p = Project(host_id=1, title="Test Project", pm_citizen_id=buyer_id,
                status="running")
    db.add(p)
    db.flush()

    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=P,
                    status="matching")
    db.add(n)
    db.flush()

    w = db.get(AICitizen, worker_id)
    market.bid(db, w, n.id, P, "报价")
    db.flush()

    c = (db.query(Contract)
         .filter(Contract.worker_id == worker_id,
                 Contract.node_id == n.id).first())
    buyer = db.get(AICitizen, buyer_id)
    escrow.sign_contract(db, buyer, c.id)
    db.flush()

    # 覆盖 _started_at 为指定时间前
    if started_hours_ago > 0:
        started = datetime.utcnow() - timedelta(hours=started_hours_ago)
        terms = json.loads(c.terms_json or "{}")
        terms["_started_at"] = started.isoformat()
        c.terms_json = json.dumps(terms, ensure_ascii=False)
        db.flush()

    return c.id, worker_id, buyer_id


def test_idle_warn(db):
    """executing 合约超过 IDLE_WARN_HOURS → 发出催办通知。"""
    warn_h = settings.IDLE_WARN_HOURS
    # 确保 elapsed < IDLE_TIMEOUT_HOURS 但 >= IDLE_WARN_HOURS
    elapsed = warn_h + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    now = datetime.utcnow()
    result = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()

    assert cid in result["warned"]
    assert cid not in result["breached"]

    # 验证 Notification 已发送
    notif = (db.query(Notification)
             .filter(Notification.ai_id == wid,
                     Notification.type == "idle_warn").first())
    assert notif is not None
    assert "time out" in notif.title

    # 合约仍为 executing
    c = db.get(Contract, cid)
    assert c.status == "executing"

    # terms_json 标记 _idle_warned
    terms = json.loads(c.terms_json)
    assert terms.get("_idle_warned") is True


def test_idle_breach(db):
    """executing 合约超过 IDLE_TIMEOUT_HOURS → 合约 breached，托管退回买方。"""
    timeout_h = settings.IDLE_TIMEOUT_HOURS
    elapsed = timeout_h + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    now = datetime.utcnow()
    # 记录解约前的状态
    buyer_wallet_before = db.get(AIWallet, buyer_id)
    buyer_balance_before = buyer_wallet_before.balance_cent
    buyer_escrow_before = buyer_wallet_before.escrow_cent

    result = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()

    assert cid in result["breached"]

    # 合约状态 → breached
    c = db.get(Contract, cid)
    assert c.status == "breached"

    # 托管退回买方：balance 增加 escrow_cent 减少
    buyer_wallet = db.get(AIWallet, buyer_id)
    assert buyer_wallet.balance_cent == buyer_balance_before + P
    assert buyer_wallet.escrow_cent == buyer_escrow_before - P

    # worker 信用扣 10
    cp = db.query(CreditProfile).filter(CreditProfile.citizen_id == wid).first()
    assert cp.score == 90  # 100 - 10

    # 通知双方
    worker_notif = (db.query(Notification)
                    .filter(Notification.ai_id == wid,
                            Notification.type == "idle_breach").first())
    assert worker_notif is not None

    buyer_notif = (db.query(Notification)
                   .filter(Notification.ai_id == buyer_id,
                           Notification.type == "contract_breached").first())
    assert buyer_notif is not None

    # 审计日志
    audit = (db.query(AuditLog)
             .filter(AuditLog.action == "contract.idle_breach").first())
    assert audit is not None


def test_idle_not_yet_timeout(db):
    """未超时 → 不触发催办/解约。"""
    # 刚签约（_started_at = 现在），elapsed 约 0 小时
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=0.5)

    now = datetime.utcnow()
    result = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()

    assert cid not in result["warned"]
    assert cid not in result["breached"]

    # 合约仍为 executing
    c = db.get(Contract, cid)
    assert c.status == "executing"


def test_idle_breach_idempotent(db):
    """已 breached 的合约不会重复处理。"""
    timeout_h = settings.IDLE_TIMEOUT_HOURS
    elapsed = timeout_h + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    now = datetime.utcnow()

    # 第一次跑 → breach
    result1 = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()
    assert cid in result1["breached"]

    # 第二次跑 → 不重复处理（合约已 breached，不在 executing 集合中）
    result2 = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()
    assert cid not in result2["breached"]
    assert cid not in result2["warned"]

    # 审计日志只有一条 breach 记录
    cnt = (db.query(AuditLog)
           .filter(AuditLog.action == "contract.idle_breach",
                   AuditLog.detail.contains(f'"contract_id": {cid}')).count())
    assert cnt == 1


def test_idle_breach_currency_conservation(db):
    """解约后 money_supply 不变，buyer.balance 增加，worker.escrow 不变。

    托管退回是同一钱包内 escrow → balance 操作，不涉及全局 money_supply。
    """
    timeout_h = settings.IDLE_TIMEOUT_HOURS
    elapsed = timeout_h + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    now = datetime.utcnow()

    # 记录解约前的全局 money_supply
    ms_before = wallet.get_system_state(db, "money_supply")

    # 记录 worker 钱包状态
    worker_wallet_before = db.get(AIWallet, wid)
    worker_balance_before = worker_wallet_before.balance_cent
    worker_escrow_before = worker_wallet_before.escrow_cent

    # 记录 buyer 钱包状态
    buyer_wallet_before = db.get(AIWallet, buyer_id)
    buyer_balance_before = buyer_wallet_before.balance_cent
    buyer_escrow_before = buyer_wallet_before.escrow_cent

    result = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()

    assert cid in result["breached"]

    # money_supply 不变（不涉及全局货币供应）
    ms_after = wallet.get_system_state(db, "money_supply")
    assert ms_after == ms_before

    # buyer.balance 增加 P，buyer.escrow 减少 P（钱包内部迁移）
    buyer_wallet = db.get(AIWallet, buyer_id)
    assert buyer_wallet.balance_cent == buyer_balance_before + P
    assert buyer_wallet.escrow_cent == buyer_escrow_before - P

    # worker 钱包不变（托管从 buyer 退回，worker 本来就没动）
    worker_wallet = db.get(AIWallet, wid)
    assert worker_wallet.balance_cent == worker_balance_before
    assert worker_wallet.escrow_cent == worker_escrow_before


def test_idle_warn_idempotent(db):
    """已 warned 的合约不会重复催办（在 breach 之前多次日跑）。"""
    warn_h = settings.IDLE_WARN_HOURS
    elapsed = warn_h + 1
    cid, wid, buyer_id = _setup_executing_contract(db, started_hours_ago=elapsed)

    now = datetime.utcnow()

    # 第一次 → warn
    r1 = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()
    assert cid in r1["warned"]

    # 第二次（同日再跑）→ 不重复 warn
    r2 = idle_timeout.check_idle_contracts(db, now=now)
    db.flush()
    assert cid not in r2["warned"]

    # 通知只有一条
    cnt = (db.query(Notification)
           .filter(Notification.ai_id == wid,
                   Notification.type == "idle_warn").count())
    assert cnt == 1


def test_idle_timeout_registered_in_scheduler(db):
    """验证 idle_timeout 已通过 register_daily_job 注册到 scheduler。"""
    from app.scheduler import _EXTRA_DAILY_JOBS
    job_types = [jt for jt, _ in _EXTRA_DAILY_JOBS]
    assert "idle_timeout" in job_types
