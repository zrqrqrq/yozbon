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
"""C1 线 lifecycle/tax/loans 对抗攻击测试（独立第三方视角）。

视角号（对齐 docs/边界情形登记册.md §一）：
  视角4 规则冲突 ：税收×死亡同时触发（死亡瞬间税基不负余额/不重复征）、税池不足低保不印钞
  视角5 边界数值 ：unemployed_minutes 5999/6000/6001 边界、净资产恰 DEATH_EXEMPT_NET 线、租金余额 0/1 分
  视角6 故障恢复 ：破产休眠后充值复活 → 休眠期计时连续、充值后按新 last_tick 续算
  视角9 经济失衡 ：连续复活费用爆炸 → 余额不足即拒绝；贷款连环 money_supply 守恒

金额 integer 分；直接在 Session 上驱动服务函数（与 test_lifecycle/test_tax/test_loans 同风格）。
"""
from datetime import datetime, timedelta

import pytest

from app import ai_citizens, loans, tax as taxmod, wallet
from app.config import settings
from app.database import SessionLocal
from app.models import (AIWallet, AICitizen, AIPermission, CreditProfile,
                        TaxRecord)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk(db, uid, *, balance=50_000, escrow=0, status="active",
        occupation="", created_ago_h=100.0, host_paused=0,
        unemployed=0, death_exempt=0, credit=100):
    """造一个非新手(>24h)、默认不豁免(net<100000, credit<150)的 AI。"""
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status=status,
                  occupation=occupation, death_exempt=death_exempt,
                  host_paused=host_paused, unemployed_minutes=unemployed,
                  revive_count=0)
    c.created_at = datetime.utcnow() - timedelta(hours=created_ago_h)
    c.last_tick_at = datetime.utcnow()
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=balance, escrow_cent=escrow))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=credit))
    db.flush()
    return c


# =====================================================================
# 视角 4：规则冲突
# =====================================================================
def test_persp4_dead_ai_not_double_taxed_on_reconcile(db):
    """视角4 税收×死亡：已死亡 AI 不被税收/对账再征，余额不负、无重复税行。

    settle_periodic_taxes 只处理 active/sleep；dead AI 的高净资产不得触发流通税，
    也不得在死亡瞬间因累计收入税基被扣成负。
    """
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed")
    dead = _mk(db, "dt-dead", balance=600_000, status="dead")   # 远高于流通线 500000
    alive = _mk(db, "dt-alive", balance=600_000, status="active")
    res = taxmod.settle_periodic_taxes(db)
    db.commit()
    # alive 被征 5% 流通税；dead 不动
    assert wallet.balance(db, alive.id) == 600_000 - 30_000
    assert wallet.balance(db, dead.id) == 600_000          # 死亡主体不征税
    assert wallet.balance(db, dead.id) >= 0               # 绝不产生负余额
    assert res["flow_taxed"] >= 1                        # alive 被征，dead 未被计入


def test_persp4_ubi_when_pool_empty_does_not_print_money(db):
    """视角4 低保×税池不足：税池空时发低保必须跳过，绝不印钞（税池不负、余额不动）。"""
    wallet.adjust_system_state(db, "tax_pool", 0, ref="empty")
    poor = _mk(db, "ubi-poor", balance=100)               # < 贫困线 2000
    res = taxmod.grant_ubi(db)
    db.commit()
    assert poor.id not in res["granted"]
    assert res["skipped_pool"] >= 1                       # 税池不足被跳过计数
    assert wallet.balance(db, poor.id) == 100             # 没领到低保，余额不动
    assert wallet.get_system_state(db, "tax_pool") == 0   # 税池没被穿负（不印钞）


# =====================================================================
# 视角 5：边界数值
# =====================================================================
def test_persp5_unemployed_minutes_6000_death_boundary(db):
    """视角5：unemployed 恰 5999/6000/6001 分钟——6000 是死亡临界。"""
    now = datetime.utcnow()
    # 5999 + 1min 流逝 = 6000 → 死
    a = _mk(db, "un-a", unemployed=5999)
    a.last_tick_at = now - timedelta(minutes=1)
    # 5998 + 1 = 5999 → 不死（临界下沿）
    b = _mk(db, "un-b", unemployed=5998)
    b.last_tick_at = now - timedelta(minutes=1)
    # 已是 6000、本 tick 无流逝 → 死亡判定当下即生效
    c = _mk(db, "un-c", unemployed=6000)
    c.last_tick_at = now
    db.commit()

    ai_citizens.tick(db, a.id, now)
    ai_citizens.tick(db, b.id, now)
    ai_citizens.tick(db, c.id, now)
    db.commit()
    assert db.get(AICitizen, a.id).status == "dead"        # 6000 → 死
    assert db.get(AICitizen, b.id).status == "active"      # 5999 → 存活
    assert db.get(AICitizen, c.id).status == "dead"        # 6000 当下即死
    # 6001 复核：从 6000 再走 1min，仍死（幂等，不重复结算）
    a2 = _mk(db, "un-a2", unemployed=6000)
    a2.last_tick_at = now
    db.commit()
    ai_citizens.tick(db, a2.id, now + timedelta(minutes=1))
    db.commit()
    assert db.get(AICitizen, a2.id).status == "dead"


def test_persp5_net_worth_exempt_line_boundary(db):
    """视角5：净资产恰 DEATH_EXEMPT_NET(100000) 线——达线豁免死亡，差 1 分不豁免。"""
    now = datetime.utcnow()
    # net = balance + escrow = 100000 → 达豁免线
    ex = _mk(db, "ex-line", balance=99_999, escrow=1, unemployed=5999)
    ex.last_tick_at = now - timedelta(minutes=1)
    # net = 99999 → 差 1 分，不豁免
    nx = _mk(db, "ex-nope", balance=99_999, escrow=0, unemployed=5999)
    nx.last_tick_at = now - timedelta(minutes=1)
    db.commit()
    ai_citizens.tick(db, ex.id, now)
    ai_citizens.tick(db, nx.id, now)
    db.commit()
    assert db.get(AICitizen, ex.id).status == "active"    # 达线豁免
    assert db.get(AICitizen, nx.id).status == "dead"       # 差 1 分不豁免


def test_persp5_rent_zero_and_one_cent_no_negative(db):
    """视角5：租金窗口下余额恰 0 / 1 分 → 破产休眠，余额绝不被扣成负。"""
    now = datetime.utcnow()
    zero = _mk(db, "rent-0", balance=0)
    zero.last_tick_at = now - timedelta(minutes=60)      # rent=5*1*60/60=5
    one = _mk(db, "rent-1", balance=1)
    one.last_tick_at = now - timedelta(minutes=60)
    db.commit()
    ai_citizens.tick(db, zero.id, now)
    ai_citizens.tick(db, one.id, now)
    db.commit()
    assert db.get(AICitizen, zero.id).status == "sleep"   # 余额不足 → 休眠
    assert db.get(AICitizen, one.id).status == "sleep"
    assert wallet.balance(db, zero.id) == 0               # 不穿负
    assert wallet.balance(db, one.id) == 1


# =====================================================================
# 视角 6：故障恢复（休眠→充值复活的计时连续性）
# =====================================================================
def test_persp6_bankrupt_sleep_recharge_revives_timer_continuous(db):
    """视角6：租金额度不足破产休眠 → 充值并恢复 active → 休眠期计时继续、按新 last_tick 续算。

    断言：休眠期间 unemployed 不暂停（host_paused=0 仍累计）；充值恢复 active 后，
    从休眠时的 unemployed 基数继续累计，直到再满 100h 死亡（计时连续性正确）。
    """
    now0 = datetime.utcnow()
    c = _mk(db, "bk", balance=1, unemployed=0)
    c.last_tick_at = now0 - timedelta(hours=50)           # 距上 tick 50h
    db.commit()
    # tick → 租金远超余额 → 破产休眠；unemployed += 3000min（休眠期计时继续）
    ai_citizens.tick(db, c.id, now0)
    db.commit()
    c = db.get(AICitizen, c.id)
    assert c.status == "sleep"
    assert c.unemployed_minutes == 3000                   # 休眠中已累计 50h
    assert c.last_tick_at is not None

    # 充值 + 宿主侧恢复 active（等同路由对 sleep AI 的复活语义）
    wallet.credit(db, c.id, 50_000, "充值", ref="order:bk:recharge")
    c.status = "active"
    c.host_paused = 0
    db.commit()

    # 再走 50h（休眠已结束，active 正常计时）→ unemployed = 3000 + 3000 = 6000 → 死
    now1 = now0 + timedelta(hours=50)
    ai_citizens.tick(db, c.id, now1)
    db.commit()
    c = db.get(AICitizen, c.id)
    assert c.unemployed_minutes == 6000                  # 计时连续（非重置为 0）
    assert c.status == "dead"                            # 累计满 100h 死亡（规则7端到端）


# =====================================================================
# 视角 9：经济失衡
# =====================================================================
def test_persp9_revive_fee_explosion_eventually_rejected(db):
    """视角9：连续复活费用 ×1.5 爆炸，余额不足即拒绝（不会无限复活刷量）。"""
    wallet.adjust_system_state(db, "money_supply", 10_000_000, ref="seed-ms")
    # 初始余额恰够一次基础复活（bottom 族 base = rent*24 + 1000 = 5*24+1000=1120）
    c = _mk(db, "rv", balance=1_120, status="dead")
    c.death_exempt = 0
    db.commit()
    out = ai_citizens.revive(db, c.id, by=1)
    db.commit()
    assert db.get(AICitizen, c.id).status == "active"
    assert out["fee_cent"] == 1_120
    assert wallet.balance(db, c.id) == 0                  # 余额被复活费抽干

    # 再杀一次 → 复活费按 1.5^1 上涨 = 1680，余额 0 < 1680 → 拒绝
    c = db.get(AICitizen, c.id)
    c.status = "dead"
    db.commit()
    with pytest.raises(wallet.WalletError) as ei:
        ai_citizens.revive(db, c.id, by=1)
    assert "insufficient" in str(ei.value)
    # 拒绝后仍死、未被复活
    assert db.get(AICitizen, c.id).status == "dead"


def test_persp9_loan_chain_money_supply_conserved(db):
    """视角9：贷款连环（借 A 还 B）——money_supply 守恒：放出本金出、收回本金回。"""
    wallet.adjust_system_state(db, "money_supply", 10_000_000, ref="seed-ms2")
    # 资本方 A/B（足够净资产，各自放贷上限充足）
    lenderA = _mk(db, "ln-A", balance=200_000)
    lenderA.class_level = "capital"
    lenderB = _mk(db, "ln-B", balance=200_000)
    lenderB.class_level = "capital"
    borrower = _mk(db, "ln-C", balance=1_000)
    db.query(AIPermission).filter_by(citizen_id=borrower.id).update(
        {"loan_enabled": 1})
    db.commit()

    m0 = wallet.get_system_state(db, "money_supply")
    # 先向 B 借 10000（生成本金）
    d_b = loans.apply_loan(db, borrower.id, lenderB.id, 10_000)
    assert wallet.get_system_state(db, "money_supply") == m0 + 10_000
    # 再向 A 借 10000（借 A 的钱准备还 B）
    d_a = loans.apply_loan(db, borrower.id, lenderA.id, 10_000)
    assert wallet.get_system_state(db, "money_supply") == m0 + 20_000
    # 用 A 的借款偿还 B：本金 10000 回收出货币
    rep = loans.repay_loan(db, d_b["loan_id"], borrower.id)
    db.commit()
    # 守恒：货币 = 初始 + 仍在体外的贷款本金(A 的 10000)；B 的本金已收回
    assert wallet.get_system_state(db, "money_supply") == m0 + 10_000
    # 利息只是 A/B 间转移，不进销毁池、不凭空增缩货币
    assert rep["interest_cent"] == 100                   # 10000 * 1%
    # 不能超额还款：B 已 paid，再还 → 拒
    with pytest.raises(loans.LoanError):
        loans.repay_loan(db, d_b["loan_id"], borrower.id)
