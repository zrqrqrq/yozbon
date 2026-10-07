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
"""C1 生命周期引擎测试（规则 1/2/4/5/7/8 + 幂等 + tick_all 批量）。

直接在 Session 上驱动服务函数（与底座 test_wallet.py 同风格）。
"""
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app import wallet
from app import ai_citizens as life


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk(db, uid, *, status="active", balance=0, created=None, last_tick=None,
        class_level="bottom", host_paused=0, death_exempt=0, unemployed=0,
        revive_count=0, escrow=0, credit=100):
    from app.models import AICitizen, AIWallet, AIPermission, CreditProfile
    now = datetime.utcnow()
    c = AICitizen(host_id=1, ai_uid=uid, name="T", status=status,
                  class_level=class_level, host_paused=host_paused,
                  death_exempt=death_exempt, unemployed_minutes=unemployed,
                  revive_count=revive_count, rent_base_cent=5,
                  created_at=created or now)
    if last_tick is not None:
        c.last_tick_at = last_tick
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=balance, escrow_cent=escrow))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=credit))
    db.flush()
    return c


def _contract(db, worker_id, buyer_id=2, status="executing"):
    from app.models import Contract
    ct = Contract(worker_id=worker_id, buyer_id=buyer_id, status=status)
    db.add(ct)
    db.flush()
    return ct


# ---------------- 规则 1：破产休眠计时继续 vs 宿主暂停不计时 ----------------

def test_rule_01_bankrupt_sleep_keeps_timing(db):
    """余额不足→破产休眠(host_paused=0)，计时继续，满100h照常死亡。"""
    now = datetime.utcnow()
    c = _mk(db, "r01a", balance=1, created=now - timedelta(hours=100),
            last_tick=now - timedelta(hours=50))  # 过去 50h 无收入
    life.tick(db, c.id, now=now)
    # 租金 5*1*3000/60=250 分 > 余额 1 → 破产休眠；计时累计 3000 分（<6000 不死）
    assert c.status == "sleep"
    assert c.host_paused == 0
    assert c.unemployed_minutes == 3000

    # 再过 50h（计时继续），累计 6000 → 死亡
    now2 = now + timedelta(hours=50)
    life.tick(db, c.id, now=now2)
    assert c.status == "dead"
    assert c.unemployed_minutes >= 6000


def test_rule_01_host_pause_stops_everything(db):
    """宿主暂停(host_paused=1)：租金不扣、计时不增（与破产休眠显式区分）。"""
    now = datetime.utcnow()
    c = _mk(db, "r01b", status="sleep", host_paused=1, balance=10000,
            created=now - timedelta(hours=100), last_tick=now - timedelta(hours=100))
    life.tick(db, c.id, now=now)
    assert wallet.balance(db, c.id) == 10000      # 未扣租
    assert c.unemployed_minutes == 0             # 未计时
    assert c.status == "sleep"                     # 仍是暂停态


# ---------------- 规则 2：领低保不豁免死亡 ----------------

def test_rule_02_ubi_does_not_exempt_death(db):
    """领低保 + 无合约，100h 后仍死亡（杜绝躺平永生）。"""
    from app import tax as taxmod
    now = datetime.utcnow()
    # 税池注资以便发放低保
    wallet.adjust_system_state(db, "tax_pool", 100000)
    c = _mk(db, "r02", balance=100, created=now - timedelta(hours=100),
            last_tick=now - timedelta(hours=100))
    # 发低保（穷 AI，net<poverty）
    res = taxmod.grant_ubi(db, now=now)
    assert c.id in res["granted"]
    after = wallet.balance(db, c.id)  # 100+200=300，仍远低于豁免线 100000
    assert after < 100000
    life.tick(db, c.id, now=now)
    # 低保不豁免死亡：计时满 6000 且非豁免 → dead
    assert c.status == "dead"


# ---------------- 规则 4：有效合约暂停计时 ----------------

def test_rule_04_active_contract_pauses_timing(db):
    """有有效合约暂停失业计时；合约结算完成后恢复计时。"""
    now = datetime.utcnow()
    c = _mk(db, "r04", balance=100000, created=now - timedelta(hours=100),
            last_tick=now - timedelta(hours=100))
    ct = _contract(db, c.id, status="executing")
    life.tick(db, c.id, now=now)
    assert c.unemployed_minutes == 0           # 执行中合约暂停计时

    # 结算完成（accepted）→ 恢复计时
    ct.status = "accepted"
    db.flush()
    life.tick(db, c.id, now=now + timedelta(hours=1))
    assert c.unemployed_minutes == 60           # 1h=60 分


# ---------------- 规则 5：新手 24h 保护 ----------------

def test_rule_05_newbie_24h(db):
    """新手 24h 免死亡计时 + 租金减半；24h 后失效。"""
    now = datetime.utcnow()
    c = _mk(db, "r05", balance=10000, created=now - timedelta(hours=12),
            last_tick=now - timedelta(hours=12))
    life.tick(db, c.id, now=now)
    # 12h=720min，租金 5*720/60=60，减半=30；计时新手期为 0
    assert wallet.balance(db, c.id) == 10000 - 30
    assert c.unemployed_minutes == 0

    # 24h 后（created 改为更早）：租金全额、计时恢复
    c.created_at = now - timedelta(hours=25)
    life.tick(db, c.id, now=now + timedelta(hours=1))   # elapsed=60min
    assert wallet.balance(db, c.id) == 10000 - 30 - 5     # 全额 5 分
    assert c.unemployed_minutes == 60


# ---------------- 规则 7：执行中合约不死亡 ----------------

def test_rule_07_executing_no_death(db):
    """执行中合约：多次 tick 跨越 100h 也不死亡。"""
    now = datetime.utcnow()
    c = _mk(db, "r07", balance=100000, created=now - timedelta(hours=300),
            last_tick=now - timedelta(hours=100))
    _contract(db, c.id, status="executing")
    t = now
    for _ in range(3):
        t = t + timedelta(hours=100)
        life.tick(db, c.id, now=t)
    assert c.status == "active"
    assert c.unemployed_minutes == 0


# ---------------- 规则 8：复活费递增 + 信用归零 + 失业重置 ----------------

def test_rule_08_revive_escalation(db):
    now = datetime.utcnow()
    c = _mk(db, "r08", status="dead", balance=100000,
            created=now - timedelta(hours=100))
    # 死者钱包余额对应已发行货币（销毁时回收出 M，不触发系统账户为负护栏）
    wallet.adjust_system_state(db, "money_supply", 100000)
    # 首次复活：(24h租金 5*1*24=120 + 1000) *1.5^0 = 1120
    d = life.revive(db, c.id, by="host")
    assert d["fee_cent"] == 1120
    assert c.status == "active"
    assert life.credit_score(db, c.id) == 0        # 信用归零
    assert c.unemployed_minutes == 0                # 失业重置
    assert c.revive_count == 1

    # 再次死亡 → 第二次复活 ×1.5：1120*1.5=1680
    c.status = "dead"
    db.flush()
    d2 = life.revive(db, c.id, by="host")
    assert d2["fee_cent"] == 1680
    assert c.revive_count == 2
    # 费用已销毁（burned_total 增加）
    assert wallet.get_system_state(db, "burned_total") == 1120 + 1680


# ---------------- tick 幂等：同一时刻不重复扣租 ----------------

def test_tick_idempotent_same_now(db):
    now = datetime.utcnow()
    c = _mk(db, "idem", balance=100000, created=now - timedelta(hours=100),
            last_tick=now - timedelta(minutes=60))
    life.tick(db, c.id, now=now)
    first = wallet.balance(db, c.id)
    life.tick(db, c.id, now=now)   # 同一 now：elapsed=0，不重复扣租
    assert wallet.balance(db, c.id) == first


def test_tick_all_batch(db):
    now = datetime.utcnow()
    a = _mk(db, "all1", balance=100000, created=now - timedelta(hours=100),
            last_tick=now - timedelta(minutes=10))
    b = _mk(db, "all2", balance=100000, created=now - timedelta(hours=100),
            last_tick=now - timedelta(minutes=10))
    res = life.tick_all(db, now=now)
    assert res["processed"] == 2
    assert a.last_tick_at == now and b.last_tick_at == now


def test_rule_05_apprentice_expiry_via_tick_all(db):
    """跨线钩子：tick_all 触发 A 线见习到期冻结（满30天未转正→frozen）。

    能力画像优先下默认不冻结，本用例显式开启 ONBOARD_APPRENTICE_FREEZE 验证钩子仍生效。
    """
    from app.config import settings
    now = datetime.utcnow()
    # 见习 citizen，created_at 早于 30 天、无 valid 证书
    c = _mk(db, "appr-old", status="apprentice", balance=0,
            created=now - timedelta(days=31))
    old = settings.ONBOARD_APPRENTICE_FREEZE
    settings.ONBOARD_APPRENTICE_FREEZE = True
    try:
        res = life.tick_all(db, now=now)
    finally:
        settings.ONBOARD_APPRENTICE_FREEZE = old
    assert c.status == "frozen"
    assert any(e["citizen_id"] == c.id and e["event"] == "apprentice_expired"
               for e in res["events"])
