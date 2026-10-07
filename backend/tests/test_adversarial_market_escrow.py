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
"""B 线 market/escrow/credit 对抗攻击测试（独立第三方视角，逆向轰实现）。

视角号（对齐 docs/边界情形登记册.md §一 10 类攻击）：
  视角3 恶意主体 ：女巫批量投标同一节点、0 余额 AI 签约托管
  视角5 边界数值 ：合约金额恰 0/1 分下限、fee_rate 封顶 8% 整数闭合、超余额 1 分
  视角6 故障恢复 ：fulfill 在 ledger dup 时整单回滚（余额/税池/合约全不脏）、自买自卖
  视角8 人为滥用 ：拒收后再 accept 同合约（状态机混乱）、executing 直接 dispute 跳步

约定：金额 integer 分；直接在 Session 上驱动服务函数（与 test_escrow.py 同风格）。
已知洞用 @pytest.mark.xfail(strict=False) 标注（登记于 docs/边界情形登记册.md C-11/C-12/C-13），
期望安全行为当前未落地——不自己改 B 线代码，只登记+在对照报告写明待修建议。
"""
import pytest

from app import credit, escrow, market, wallet
from app import tax_rules
from app.database import SessionLocal
from app.models import (AIWallet, AICitizen, AIPermission, Contract, CreditProfile,
                        Escrow, Host, Project, ProjectNode, TaxRecord)

P = 10_000  # 100 AC，常规结算基准（税池预充后费率 5%）


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk(db, uid: str, balance: int = 100_000, host_id: int = 1) -> AICitizen:
    """造一个有足额余额/权限/信用档案的 AI。"""
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status="active",
                  class_level="bottom")
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    if balance > 0:     # 0 余额主体不注资（credit 拒绝非正额，用于测 0 余额攻击面）
        wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
        wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:{uid}:seed")
    return c


def _seed_pool(db):
    """税池预充到 ≥ 储备线(UBI_DAILY*30=6000)，使平衡阀不触发，费率保持 5%。"""
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")


def _proj_node(db, buyer: AICitizen, node_budget: int = P, host_id: int = 1):
    """造 running 项目 + 一个 matching 节点，返回 node。"""
    p = Project(host_id=host_id, title="P", pm_citizen_id=buyer.id, status="running")
    db.add(p); db.flush()
    n = ProjectNode(project_id=p.id, skill="文案", spec="s", budget_cent=node_budget,
                    status="matching")
    db.add(n); db.flush()
    return n


# =====================================================================
# 视角 3：恶意主体
# =====================================================================
def test_persp3_witch_same_host_bid_same_node_characterization(db):
    """视角3：同宿主 3 个 AI 对同一节点投标——投标层不拦女巫（现状登记）。

    规则13 女巫防线在「免费席位 3 个 / 低保按 AI-ID / 转发相关性降权」，
    投标层目前允许同宿主多 worker 各投一条 proposed 要约（uq_bid_proposal
    只按 (node,worker) 去重，不按 host 去重）。本用例刻画该现状：3 条 proposed
    全部落库；真正的收口在「签约时 node 必须仍在 matching」——见 C-11。
    """
    _seed_pool(db)
    buyer = _mk(db, "atk-buyer")
    node = _proj_node(db, buyer)
    w1 = _mk(db, "atk-w1")
    w2 = _mk(db, "atk-w2")
    w3 = _mk(db, "atk-w3")
    c1 = market.bid(db, w1, node.id, P, "投1")
    c2 = market.bid(db, w2, node.id, P, "投2")
    c3 = market.bid(db, w3, node.id, P, "投3")
    db.commit()
    rows = (db.query(Contract)
            .filter(Contract.node_id == node.id,
                    Contract.status == "proposed").all())
    assert len(rows) == 3                       # 3 条要约全部被受理（现状）
    assert {c1.id, c2.id, c3.id} == {r.id for r in rows}


def test_persp3_signed_node_should_reject_repeat_sign(db):
    """视角3（洞 C-11）：node 已被首单签走后，买方再签同节点第二条要约应拒绝。

    期望：sign_contract 应在签约前校验 node.status=='matching'，否则重复托管。
    现状：放行（探针实测 buyer.escrow_cent=2P、产生两条 escrow）。
    """
    _seed_pool(db)
    buyer = _mk(db, "atk2-buyer")
    node = _proj_node(db, buyer)
    w1 = _mk(db, "atk2-w1")
    w2 = _mk(db, "atk2-w2")
    c1 = market.bid(db, w1, node.id, P, "投1")
    c2 = market.bid(db, w2, node.id, P, "投2")
    escrow.sign_contract(db, buyer, c1.id)     # 首单：node→signed
    db.commit()
    node = db.get(ProjectNode, node.id)
    assert node.status == "signed"
    # 期望：再签 c2 应抛错；现状：放行 → 本测试断言抛错，故 xfail
    with pytest.raises((escrow.EscrowError, wallet.WalletError)):
        escrow.sign_contract(db, buyer, c2.id)


def test_persp3_zero_balance_buyer_sign_no_half_escrow(db):
    """视角3：0 余额 AI 当买方签约托管 → 拒绝，且不产生半截 escrow/流水。"""
    _seed_pool(db)
    buyer = _mk(db, "zb-buyer", balance=0)      # 余额 0
    node = _proj_node(db, buyer)
    worker = _mk(db, "zb-worker")
    c = market.bid(db, worker, node.id, P, "投")
    db.commit()
    esc_count_before = db.query(Escrow).count()
    with pytest.raises(wallet.WalletError) as ei:
        escrow.sign_contract(db, buyer, c.id)
    assert "insufficient" in str(ei.value)
    db.rollback()
    # 不脏：无 escrow 行、无 '托管' 流水、合约仍是 proposed、买方余额仍 0
    assert db.query(Escrow).count() == esc_count_before
    assert db.query(TaxRecord).count() == 0
    c = db.get(Contract, c.id)
    assert c.status == "proposed"
    assert wallet.balance(db, buyer.id) == 0
    assert wallet.get_wallet(db, buyer.id).escrow_cent == 0


# =====================================================================
# 视角 5：边界数值
# =====================================================================
def test_persp5_offer_zero_rejected_at_bid(db):
    """视角5：报价恰 0 分 → 投标即拒（offer_cent<=0 下限校验）。"""
    _seed_pool(db)
    buyer = _mk(db, "z0-buyer")
    node = _proj_node(db, buyer)
    worker = _mk(db, "z0-worker")
    with pytest.raises(market.MarketError) as ei:
        market.bid(db, worker, node.id, 0, "零报价")
    assert "positive integer" in str(ei.value)


def test_persp5_offer_one_cent_signs_and_delivers(db):
    """视角5：报价恰 1 分 → 投标/签约/交付路径不崩（结算阶段 fee=0 的洞见 C-13）。"""
    _seed_pool(db)
    buyer = _mk(db, "z1-buyer")
    node = _proj_node(db, buyer, node_budget=1)
    worker = _mk(db, "z1-worker")
    c = market.bid(db, worker, node.id, 1, "一分报价")
    escrow.sign_contract(db, buyer, c.id)       # 托管 1 分
    escrow.deliver(db, worker, c.id, "s3://x", "fp-1")
    db.commit()
    assert db.get(Contract, c.id).status == "delivered"
    assert wallet.get_wallet(db, buyer.id).escrow_cent == 1


def test_persp5_one_cent_settlement_no_float_residue(db):
    """视角5（洞 C-13）：1 分合约结算后 worker 应净得 1 分、账平、无浮点残留。

    现状：fee=round(1*0.05)=0 → debit(0,'手续费') 抛 WalletError，结算中断。
    期望：fee/tax 为 0 时跳过对应 debit 行，worker 净得 = P-fee-tax = 1。
    """
    _seed_pool(db)
    buyer = _mk(db, "z1b-buyer")
    node = _proj_node(db, buyer, node_budget=1)
    worker = _mk(db, "z1b-worker")
    c = market.bid(db, worker, node.id, 1, "一分")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "s3://x", "fp-1")
    db.commit()
    w_before = wallet.balance(db, worker.id)
    escrow.fulfill_contract(db, c.id)          # 期望不抛错
    db.commit()
    assert db.get(Contract, c.id).status == "accepted"
    assert wallet.balance(db, worker.id) == w_before + 1   # 净得恰 1 分（整数）


def test_persp5_fee_rate_cap_8pct_integer_closed_books(db):
    """视角5：fee_rate 封顶 8% 时，fee/burn/taxpool 整数闭合（fee==burn+taxpool，无浮点残留）。"""
    # 纯函数层：多个 P 都要求 fee_split 整数账平
    for Px in (7, 999, 1001, 12345, 99999):
        fee, burn, taxpool = tax_rules.fee_split(Px, 0.08)
        assert fee >= 0 and burn >= 0 and taxpool >= 0
        assert fee == burn + taxpool, f"P={Px} 账不平: fee={fee} burn={burn} taxpool={taxpool}"
    # 端到端：把费率推到 8% 上限，结算后三账联动且无尾差
    wallet.adjust_system_state(db, "tax_pool", 0, ref="empty")   # 触发平衡阀
    buyer = _mk(db, "cap-buyer")
    node = _proj_node(db, buyer, node_budget=1000)
    worker = _mk(db, "cap-worker")
    c = market.bid(db, worker, node.id, 1000, "边界")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "s3://x", "fp-cap")
    db.commit()
    # 池空 → adjusted_fee_rate 应取到 min(0.055, 0.08)（单步长），再人为推到 8% 上限验证闭合
    rate = tax_rules.adjusted_fee_rate(0, 0.08, 6000)
    assert rate == 0.08
    fee, burn, taxpool = tax_rules.fee_split(1000, rate)
    assert fee == burn + taxpool


def test_persp5_escrow_exceeds_balance_by_one_cent_rejected(db):
    """视角5：托管金额恰为买方余额+1 分 → 签约拒绝，无半截状态。"""
    _seed_pool(db)
    buyer = _mk(db, "ex-buyer", balance=P - 1)   # 差 1 分
    node = _proj_node(db, buyer)
    worker = _mk(db, "ex-worker")
    c = market.bid(db, worker, node.id, P, "投")
    db.commit()
    esc_before = db.query(Escrow).count()
    with pytest.raises(wallet.WalletError):
        escrow.sign_contract(db, buyer, c.id)
    db.rollback()
    assert db.query(Escrow).count() == esc_before
    assert db.get(Contract, c.id).status == "proposed"
    assert wallet.get_wallet(db, buyer.id).escrow_cent == 0


# =====================================================================
# 视角 6：故障恢复
# =====================================================================
def test_persp6_fulfill_dup_ledger_rolls_back_everything(db):
    """视角6：fulfill 触发 ledger 唯一索引 dup 时整单回滚——余额/税池/销毁/合约全不脏。

    与 test_escrow.test_dup_ledger_double_insurance 的区别：本用例不仅断言抛错，
    还断言失败的二次 fulfill 没有污染任何账本（worker 余额、tax_pool、burned、
    money_supply、escrow 锁态、合约状态），即「整单回滚」。
    """
    _seed_pool(db)
    buyer = _mk(db, "rb-buyer")
    node = _proj_node(db, buyer)
    worker = _mk(db, "rb-worker")
    c = market.bid(db, worker, node.id, P, "投")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "s3://x", "fp-rb")
    escrow.fulfill_contract(db, c.id)           # 首次结算成功
    db.commit()
    # 快照（首次结算后的真实终态）
    snap_worker = wallet.balance(db, worker.id)
    snap_pool = wallet.get_system_state(db, "tax_pool")
    snap_burned = wallet.get_system_state(db, "burned_total")
    snap_money = wallet.get_system_state(db, "money_supply")
    snap_buyer_esc = wallet.get_wallet(db, buyer.id).escrow_cent
    # 模拟并发/重入：手动重新上锁让抢权再次 rowcount==1
    esc = db.get(Escrow, c.id)
    esc.locked = 1
    db.commit()
    with pytest.raises(wallet.WalletError) as ei:
        escrow.fulfill_contract(db, c.id)       # 应抛 dup ledger
    assert "dup ledger" in str(ei.value)
    # 整单回滚：所有快照不变（worker 不重复入账、税池/销毁/货币不重复动）
    assert wallet.balance(db, worker.id) == snap_worker
    assert wallet.get_system_state(db, "tax_pool") == snap_pool
    assert wallet.get_system_state(db, "burned_total") == snap_burned
    assert wallet.get_system_state(db, "money_supply") == snap_money
    assert wallet.get_wallet(db, buyer.id).escrow_cent == snap_buyer_esc
    # 合约仍是 accepted，未被回滚成脏态
    assert db.get(Contract, c.id).status == "accepted"


def test_persp6_self_deal_worker_equals_buyer_rejected(db):
    """视角6（洞 C-12）：worker 与 buyer 同一 ID 自买自卖 → 应拒绝（对敲护栏）。

    期望：签约/结算侧应禁止 worker_id==buyer_id。现状：放行（探针实测可签约）。
    """
    _seed_pool(db)
    selfai = _mk(db, "selfai")
    node = _proj_node(db, selfai)              # buyer = selfai
    c = market.bid(db, selfai, node.id, 1000, "自投")   # worker = selfai
    db.commit()
    # 期望签约即拒；现状放行 → 断言抛错，故 xfail
    with pytest.raises((escrow.EscrowError, wallet.WalletError)):
        escrow.sign_contract(db, selfai, c.id)


# =====================================================================
# 视角 8：人为滥用
# =====================================================================
def test_persp8_reject_then_buyer_accept_same_contract_rejected(db):
    """视角8：交付后买方拒收（返工退回 executing），买方再直接 accept 同合约 → 拒绝。

    状态机：delivered --reject--> executing（返工期）。此时买方若想绕过返工
    直接 accept 结算，acceptance() 要求 status=='delivered' → 抛错，杜绝状态机混乱。
    """
    _seed_pool(db)
    buyer = _mk(db, "ab-buyer")
    node = _proj_node(db, buyer)
    worker = _mk(db, "ab-worker")
    c = market.bid(db, worker, node.id, P, "投")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "s3://x", "fp-ab")
    escrow.acceptance(db, buyer, c.id, "reject", "[]")     # 拒收 → 回 executing
    db.commit()
    assert db.get(Contract, c.id).status == "executing"
    # 再直接 accept → 拒绝（不在 delivered 态）
    with pytest.raises(escrow.EscrowError) as ei:
        escrow.acceptance(db, buyer, c.id, "accept", "[]")
    assert "delivered" in str(ei.value)
    # 返工期托管仍锁定（规则14）
    assert db.get(Escrow, c.id).locked == 1


def test_persp8_executing_direct_dispute_allowed_escrow_still_locked(db):
    """视角8：合约跳步——未交付（仍 executing）worker 直接 dispute 申诉。

    设计上允许（任何履约期争议都可开案），但必须保证 escrow 仍锁定（规则14），
    不得因跳步申诉而解锁或半截释放。
    """
    _seed_pool(db)
    buyer = _mk(db, "dp-buyer")
    node = _proj_node(db, buyer)
    worker = _mk(db, "dp-worker")
    c = market.bid(db, worker, node.id, P, "投")
    escrow.sign_contract(db, buyer, c.id)       # 只签约托管，未交付
    db.commit()
    assert db.get(Contract, c.id).status == "executing"
    case = escrow.open_dispute(db, worker, c.id, "delivery", "{}")
    db.commit()
    assert db.get(Contract, c.id).status == "disputed"
    assert case.status == "open"
    # 跳步申诉后托管仍锁定（仲裁中不动 escrow）
    assert db.get(Escrow, c.id).locked == 1
