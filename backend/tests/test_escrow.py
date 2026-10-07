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
"""B 线合约/托管/结算单测（规则 4/6/12/14 + 结算并发幂等 + 双保险）。

数值约定（P=10000 分=100 AC，税池预充至 ≥ 储备线使费率保持 5%）：
  fee=500(=5%)，burn=300(=3%)，taxpool=200(=2%)；
  net=P-fee=9500；月累计 cum=0 → tax=(9500-5000)*10%=450；
  worker 实得 = P-fee-tax = 9050。
"""
import json
import threading

import pytest

from app import escrow, market, wallet
from app.database import SessionLocal
from app.models import (AICitizen, AIWallet, AIPermission, ArbitrationCase, Contract,
                        CreditProfile, Escrow, Host, Project, ProjectNode, ReworkOrder,
                        TaxRecord)

P = 10_000
FEE, BURN, TAXPOOL, TAX, NET = 500, 300, 200, 450, 9050  # 见模块头注释


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid: str, balance: int = 100_000):
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status="active", class_level="bottom")
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c.id


def _setup(db, worker_class="bottom", P=P):
    """造 buyer/worker + 项目节点，完成 投标→签约→交付(delivered)，返回 (cid, worker_id, buyer_id)。"""
    # 预充税池 ≥ 储备线(UBI_DAILY*30=6000)，使平衡阀不触发，费率保持 5%
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")
    if db.query(Host).filter(Host.id == 1).first() is None:
        db.add(Host(id=1, email="h1@t.test", password_hash="x", host_credit=100))
    buyer = _mk_party(db, "sc-buyer")
    wid = _mk_party(db, "sc-worker")
    w = db.get(AICitizen, wid)
    w.class_level = worker_class
    p = Project(host_id=1, title="P", pm_citizen_id=buyer, status="running")
    db.add(p); db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=P,
                     status="matching")
    db.add(n); db.flush()
    c = market.bid(db, w, n.id, P, "报价")
    b = db.get(AICitizen, buyer)
    escrow.sign_contract(db, b, c.id)
    escrow.deliver(db, w, c.id, "s3://x/v1", "fp-v1")
    return c.id, wid, buyer


def test_rule_06_settlement_same_txn(db):
    """规则6：结算后 worker=P-fee-tax；tax_pool+=taxpool+tax；burned_total+=burn；
    money_supply-=burn；tax_records 有 income 行（同事务四账一致）。"""
    cid, wid, buyer = _setup(db)
    w_before = wallet.balance(db, wid)
    pool_before = wallet.get_system_state(db, "tax_pool")
    money_before = wallet.get_system_state(db, "money_supply")
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    # worker 实得
    assert wallet.balance(db, wid) == w_before + NET
    # 税池 = 原 + taxpool(200) + tax(450)
    assert wallet.get_system_state(db, "tax_pool") == pool_before + TAXPOOL + TAX
    # 销毁累计 += burn
    assert wallet.get_system_state(db, "burned_total") == BURN
    # 货币供应 -= burn
    assert wallet.get_system_state(db, "money_supply") == money_before - BURN
    # tax_records 有 income 行（worker，本月，税额=450）
    rows = db.query(TaxRecord).filter(TaxRecord.citizen_id == wid,
                                     TaxRecord.type == "income").all()
    assert len(rows) == 1 and rows[0].amount_cent == TAX
    c = db.get(Contract, cid)
    assert c.status == "accepted" and c.fee_cent == FEE and c.tax_cent == TAX


def test_rule_12_signed_class_frozen(db):
    """规则12：签约时冻结 worker 等级；结算时 worker 已升级也按签约等级/锁定金额，不多付。"""
    cid, wid, buyer = _setup(db, worker_class="bottom")
    c = db.get(Contract, cid)
    terms = json.loads(c.terms_json)
    assert terms["signed_class"] == "bottom"     # 签约时冻结
    # 模拟 worker 结算前升级
    w = db.get(AICitizen, wid)
    w.class_level = "middle"
    db.flush()
    w_before = wallet.balance(db, wid)
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    # 金额仍按签约锁定的 P 结算，不因升级多付
    assert wallet.balance(db, wid) == w_before + NET
    c2 = db.get(Contract, cid)
    assert json.loads(c2.terms_json)["signed_class"] == "bottom"


def test_rule_14_escrow_lock_state_machine(db):
    """规则14：delivered 前 escrow locked；reject→仍 locked+rework round++；accept→释放；
    仲裁中→locked；解除→按折算释放。"""
    cid, wid, buyer = _setup(db)
    esc = db.get(Escrow, cid)
    assert esc.locked == 1                       # delivered 前恒锁定
    b = db.get(AICitizen, buyer)
    w = db.get(AICitizen, wid)
    # reject → 仍锁定 + round=1 + 退回 executing
    escrow.acceptance(db, b, cid, "reject", "[]")
    db.commit()
    esc = db.get(Escrow, cid)
    assert esc.locked == 1
    ro = db.query(ReworkOrder).filter(ReworkOrder.contract_id == cid).all()
    assert len(ro) == 1 and ro[0].round == 1
    assert db.get(Contract, cid).status == "executing"
    # 返工再交付 v2 → delivered，仍锁定
    escrow.deliver(db, w, cid, "s3://x/v2", "fp-v2")
    db.commit()
    assert db.get(Escrow, cid).locked == 1
    # 仲裁中 → 锁定（dispute 不改 escrow）
    escrow.open_dispute(db, w, cid, "delivery", "{}")
    db.commit()
    assert db.get(Escrow, cid).locked == 1
    assert db.get(Contract, cid).status == "disputed"
    # 解除折算释放：worker 完成 60%
    escrow.release_refund(db, cid, ratio=0.6)
    db.commit()
    esc = db.get(Escrow, cid)
    assert esc.locked == 0 and esc.released_cent == P
    assert db.get(Contract, cid).status == "refunded"
    # earned=6000 归 worker，4000 退回买方（worker 本金 100000 不变）
    assert wallet.balance(db, wid) == 100_000 + 6000


def test_rule_11_malicious_reject_3x(db):
    """规则14/11 侧翼：同一合约连续 reject≥3 → 买方信用扣分事件（恶意拒收 -30）。"""
    cid, wid, buyer = _setup(db)
    b = db.get(AICitizen, buyer)
    w = db.get(AICitizen, wid)
    from app.models import CreditEvent
    for i in range(3):
        # reject → 退回 executing → 再交付 → delivered
        escrow.acceptance(db, b, cid, "reject", "[]")
        db.flush()
        escrow.deliver(db, w, cid, f"s3://x/v{i+2}", f"fp-v{i+2}")
    db.commit()
    evs = db.query(CreditEvent).filter(CreditEvent.citizen_id == buyer,
                                       CreditEvent.event == "malicious_reject").all()
    assert len(evs) == 1 and evs[0].delta == -30
    # 买方信用档案已扣分（初始100-30=70）
    from app.models import CreditProfile
    prof = db.get(CreditProfile, buyer)
    assert prof.score == 70


def test_rule_14_rework_cap_escalates_to_arbitration(db):
    """G4 返工上限硬出口：连续 reject 超过 MAX_REWORK_ROUNDS → 自动开仲裁案(disputed)，托管仍锁定。

    历史缺陷：验收 reject 只把合约退回 executing、无限返工，无硬出口 → 死循环。
    """
    from app.config import settings
    cid, wid, buyer = _setup(db)
    b = db.get(AICitizen, buyer)
    w = db.get(AICitizen, wid)
    limit = settings.MAX_REWORK_ROUNDS
    # 前 limit 次 reject：每次都退回 executing 并再交付 → 不触发升级
    for i in range(limit):
        escrow.acceptance(db, b, cid, "reject", "[]")
        db.flush()
        assert db.get(Contract, cid).status == "executing"
        escrow.deliver(db, w, cid, f"s3://x/v{i+2}", f"fp-v{i+2}")
        db.flush()
    # 第 limit+1 次 reject：超过上限 → 自动开仲裁案，状态 disputed
    escrow.acceptance(db, b, cid, "reject", "[]")
    db.commit()
    assert db.get(Contract, cid).status == "disputed"
    # 托管保持锁定（规则14）
    assert db.get(Escrow, cid).locked == 1
    # 生成 open 仲裁案，申请人为买方
    case = (db.query(ArbitrationCase)
            .filter(ArbitrationCase.contract_id == cid,
                    ArbitrationCase.status == "open").first())
    assert case is not None and case.applicant_id == buyer
    # 该返工轮 ReworkOrder 标记 escalated
    ro = (db.query(ReworkOrder)
          .filter(ReworkOrder.contract_id == cid)
          .order_by(ReworkOrder.round.desc()).first())
    assert ro.status == "escalated"


def test_settlement_concurrency_idempotent(db):
    """多线程同时 fulfill 同一合约：仅一次成功（条件 UPDATE 抢权），其余抛'已结算'。"""
    cid, wid, buyer = _setup(db)
    db.commit()
    results = []
    lock = threading.Lock()

    def _worker():
        s = SessionLocal()
        try:
            escrow.fulfill_contract(s, cid)
            s.commit()
            with lock:
                results.append("ok")
        except Exception as e:  # noqa: BLE001
            s.rollback()
            with lock:
                results.append(("err", str(e)))
        finally:
            s.close()

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    oks = [r for r in results if r == "ok"]
    errs = [r for r in results if r != "ok"]
    assert len(oks) == 1, f"成功数≠1: {results}"
    assert len(errs) == 7
    assert all("already settled" in r[1] for r in errs), results
    # 最终 worker 只结算一次（实得 NET，不重复）
    assert wallet.balance(db, wid) == 100_000 + NET


def test_dup_ledger_double_insurance(db):
    """第二道保险：手动重新上锁模拟绕过抢权重入 → ai_ledger 唯一索引兜底拒绝重复入账。"""
    cid, wid, buyer = _setup(db)
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    # 模拟异常重入：手动把 escrow 重新上锁，让抢权再次 rowcount==1
    esc = db.get(Escrow, cid)
    esc.locked = 1
    db.commit()
    with pytest.raises(wallet.WalletError) as ei:
        escrow.fulfill_contract(db, cid)
    assert "dup ledger" in str(ei.value)


def test_rule_04_valid_contract_status_set(db):
    """规则4：有效合约（暂停失业计时）= status ∈ {escrowed, executing, delivered}。
    B 线只需把状态机走对：签约后 executing、交付后 delivered 均属有效集合；accepted 后退出。"""
    cid, wid, buyer = _setup(db)   # 已 delivered
    VALID = {"escrowed", "executing", "delivered"}
    c = db.get(Contract, cid)
    assert c.status in VALID        # delivered → 有效（就业中，C1 tick 据此暂停计时）
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    assert db.get(Contract, cid).status not in VALID   # accepted → 计时恢复


def test_deliver_versioning(db):
    """交付版本化：v1/v2 递增，fingerprint 必填。"""
    cid, wid, buyer = _setup(db)
    w = db.get(AICitizen, wid)
    # 第一次 deliver 已在 setup 内（v1）
    from app.models import Deliverable
    escrow.acceptance(db, db.get(AICitizen, buyer), cid, "reject", "[]")
    db.flush()
    d2 = escrow.deliver(db, w, cid, "s3://x/v2", "fp-v2")
    assert d2.version == 2
    # fingerprint 必填
    with pytest.raises(escrow.EscrowError):
        escrow.deliver(db, w, cid, "s3://x/v3", "")


def test_full_accept_path(db):
    """验收 accept 全路径：签约托管→交付→accept→结算→买方托管释放。"""
    cid, wid, buyer = _setup(db)
    bw_before = wallet.get_wallet(db, buyer).escrow_cent
    assert bw_before == P            # 签约后买方托管占用 = P
    b = db.get(AICitizen, buyer)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    assert wallet.get_wallet(db, buyer).escrow_cent == 0   # 结算后释放占用
    assert db.get(Contract, cid).status == "accepted"
