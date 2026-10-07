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
"""N19 AI 成长体系测试。

覆盖：
- 惰性建级 / 公开视图 / 自身视图 / 宿主视图（仅名下）
- XP 入账 + 升级连动 + level.up→feed
- XP 幂等（同 ref 不重复加）
- 特权默认零加成（无 AiLevel 行 → sort_weight=1/fee_discount=0/quota=0）
- 特权生效：手续费折扣进结算；广场配额加成
"""
import json

import pytest

from app import ai_feeds  # noqa: F401  确保 level.up→feed handler 注册
from app import escrow, levels, market, plaza, wallet
from app.database import SessionLocal
from app.models import (AICitizen, AIFeed, AIWallet, AIPermission, Contract,
                        CreditProfile, LevelRule, Project, ProjectNode)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _seed_rules(db):
    rows = [
        LevelRule(level=1, xp_threshold=0, title_zh="见习", title_en="Novice",
                  privileges=json.dumps({"sort_weight": 1.0, "fee_discount": 0.0, "plaza_quota": 0})),
        LevelRule(level=2, xp_threshold=100, title_zh="熟练", title_en="Skilled",
                  privileges=json.dumps({"sort_weight": 1.0, "fee_discount": 0.0, "plaza_quota": 0})),
        LevelRule(level=3, xp_threshold=300, title_zh="骨干", title_en="Specialist",
                  privileges=json.dumps({"sort_weight": 1.5, "fee_discount": 0.2, "plaza_quota": 5})),
    ]
    for r in rows:
        db.add(r)
    db.flush()


def _mk_ai(db, host_id=1, uid="lvl-ai"):
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid, status="active")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    return c


# ---------------- 惰性建级 / 视图 ----------------
def test_lazy_create_and_view(db):
    ai = _mk_ai(db)
    v = levels.view(db, ai.id)
    assert v["level"] == 1 and v["xp"] == 0
    assert v["badges"] == [] and v["xp_needed"] >= 0


def test_default_zero_privileges_without_rules(db):
    """无规则无 AiLevel 行 → 特权零加成（既有行为不变）。"""
    ai = _mk_ai(db, uid="zero-priv")
    assert levels.sort_weight_for_ai(db, ai.id) == 1.0
    assert levels.fee_discount_for_ai(db, ai.id) == 0.0
    assert levels.plaza_quota_bonus(db, ai.id) == 0


# ---------------- XP 升级连动 ----------------
def test_xp_upgrade_emits_level_up_feed(db):
    _seed_rules(db)
    ai = _mk_ai(db, uid="xp-up")
    # 三次交付（每次 +20 XP）→ 60 XP 不到 L2(100)；再 +50 → 跨 L2
    levels.award_xp(db, ai.id, 20, ref="contract:1")
    levels.award_xp(db, ai.id, 20, ref="contract:2")
    assert levels.view(db, ai.id)["level"] == 1
    levels.award_xp(db, ai.id, 70, ref="contract:3")   # 累计 110 ≥ 100 → L2
    v = levels.view(db, ai.id)
    assert v["level"] == 2 and v["title_zh"] == "熟练"
    # level.up 已落动态流
    feeds = db.query(AIFeed).filter(AIFeed.ai_id == ai.id,
                                    AIFeed.event_type == "level_up").all()
    assert len(feeds) >= 1


def test_xp_idempotent_by_ref(db):
    _seed_rules(db)
    ai = _mk_ai(db, uid="xp-idem")
    levels.award_xp(db, ai.id, 50, ref="contract:9")
    xp1 = levels.view(db, ai.id)["xp"]
    levels.award_xp(db, ai.id, 50, ref="contract:9")   # 同 ref 重放
    xp2 = levels.view(db, ai.id)["xp"]
    assert xp1 == xp2


# ---------------- 特权生效：手续费折扣进结算 ----------------
def _settle(db, worker_id, buyer_id, P=10_000):
    worker = db.get(AICitizen, worker_id)
    buyer = db.get(AICitizen, buyer_id)
    proj = Project(host_id=worker.host_id, title="P", pm_citizen_id=buyer_id,
                  status="running", budget_cent=P)
    db.add(proj); db.flush()
    node = ProjectNode(project_id=proj.id, skill="文案", spec="s", budget_cent=P,
                       status="matching")
    db.add(node); db.flush()
    c = market.bid(db, worker, node.id, P, "o")
    escrow.sign_contract(db, buyer, c.id)
    escrow.deliver(db, worker, c.id, "r", "fp")
    escrow.acceptance(db, buyer, c.id, "accept")
    return db.get(Contract, c.id)


def test_fee_discount_applied_when_leveled(db):
    _seed_rules(db)
    wallet.adjust_system_state(db, "tax_pool", 200_000, ref="s:p")
    wallet.adjust_system_state(db, "money_supply", 200_000, ref="s:m")
    buyer = _mk_ai(db, host_id=1, uid="fb-buyer")
    wallet.credit(db, buyer.id, 100_000, "充值", ref="order:fb")
    # worker A：无 AiLevel 行 → fee 无折扣
    wa = _mk_ai(db, host_id=1, uid="fb-workerA")
    wallet.credit(db, wa.id, 100_000, "充值", ref="order:wA")
    ca = _settle(db, wa.id, buyer.id)
    fee_a = ca.fee_cent
    # worker B：直接造 L3 AiLevel（fee_discount=0.2）→ fee 应比 A 低
    wb = _mk_ai(db, host_id=1, uid="fb-workerB")
    wallet.credit(db, wb.id, 100_000, "充值", ref="order:wB")
    from app.models import AiLevel
    db.add(AiLevel(ai_id=wb.id, level=3, xp=300, xp_needed=600,
                   title_zh="骨干", badges="[]"))
    db.flush()
    assert levels.fee_discount_for_ai(db, wb.id) == pytest.approx(0.2)
    cb = _settle(db, wb.id, buyer.id)
    assert cb.fee_cent < fee_a
    # 折扣默认零：无行 worker 的 fee 与无折扣口径一致（>0）
    assert fee_a > 0


# ---------------- 特权生效：广场配额加成 ----------------
def test_plaza_quota_bonus(db):
    from app.models import AiLevel
    _seed_rules(db)
    a = _mk_ai(db, host_id=1, uid="pq-a")   # 无 AiLevel → bonus 0
    base = plaza._daily_quota(db, "ai", a.id)
    b = _mk_ai(db, host_id=1, uid="pq-b")
    db.add(AiLevel(ai_id=b.id, level=3, xp=300, xp_needed=600, badges="[]"))
    db.flush()
    boosted = plaza._daily_quota(db, "ai", b.id)
    assert boosted == base + 5   # L3 plaza_quota=5


# ---------------- HTTP：宿主视图仅名下 ----------------
def test_host_level_view_only_own(client, host, db):
    ai = _mk_ai(db, host_id=host["host_id"], uid="view-ai")
    db.commit()
    r = client.get(f"/api/host/ai/{ai.id}/level",
                   headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["level"] == 1
    # 别人的宿主看不到
    other = client.post("/api/host/register", json={
        "email": "otherlv@t.test", "password": "pass123456"}).json()
    r2 = client.get(f"/api/host/ai/{ai.id}/level",
                    headers={"Authorization": f"Bearer {other['token']}"})
    assert r2.status_code == 404
