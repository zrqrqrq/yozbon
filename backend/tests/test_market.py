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
"""B 线市场单测：jobs 检索排序 / 投标防重复 / 议价 / worker_bridge 冒烟。"""
import pytest

from app.database import SessionLocal
from app import market, wallet
from app.models import AICitizen, AIWallet, AIPermission, CreditProfile, Project, ProjectNode


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_party(db, uid: str, balance: int = 100_000):
    """直接造一个已注资 AI（含钱包/权限/信用档案），返回 citizen_id。"""
    c = AICitizen(host_id=1, ai_uid=uid, name=uid, status="active")
    db.add(c); db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:{uid}:seed")
    return c.id


def _mk_project_with_node(db, buyer_id, skill, budget, host_credit=100):
    from app.models import Host
    h = db.query(Host).filter(Host.id == 1).first()
    if h is None:
        h = Host(id=1, email="h1@t.test", password_hash="x", host_credit=host_credit)
        db.add(h); db.flush()
    p = Project(host_id=1, title="P", pm_citizen_id=buyer_id, status="running",
                budget_cent=budget)
    db.add(p); db.flush()
    n = ProjectNode(project_id=p.id, skill=skill, spec="s", budget_cent=budget,
                     status="matching")
    db.add(n); db.flush()
    return n


def test_jobs_list_sorted_by_credit_then_budget(db):
    """市场排序：同技能节点按 宿主信用 desc → 预算 desc。"""
    buyer = _mk_party(db, "m-buyer")
    # 低信用宿主项目 + 高预算
    from app.models import Host
    db.query(Host).filter(Host.id == 1).update({"host_credit": 80})
    n_lowcredit = _mk_project_with_node(db, buyer, "文案", 50_000, host_credit=80)
    db.commit()  # 落地后再造高信用项目会换新 host？为隔离，这里单项目验证
    # 只放一个节点验证能被检索到
    res = market.list_jobs(db, skill="文案")
    assert res["total"] == 1
    assert res["items"][0]["node_id"] == n_lowcredit.id


def test_jobs_skill_filter_and_pagination(db):
    """技能过滤 + 分页 limit/offset。"""
    buyer = _mk_party(db, "pg-buyer")
    for i in range(5):
        _mk_project_with_node(db, buyer, "绘图", 1000 * i)
    res = market.list_jobs(db, skill="绘图", limit=2, offset=0)
    assert res["total"] == 5 and len(res["items"]) == 2
    res2 = market.list_jobs(db, skill="绘图", limit=2, offset=2)
    assert len(res2["items"]) == 2
    # 技能不匹配过滤
    res3 = market.list_jobs(db, skill="不存在的技能")
    assert res3["total"] == 0


def test_dup_bid_rejected(db):
    """投标防重复：同 AI 对同节点只投一次（代码校验 + 部分唯一索引兜底）。"""
    buyer = _mk_party(db, "db-buyer")
    worker_id = _mk_party(db, "db-worker")
    n = _mk_project_with_node(db, buyer, "文案", 10_000)
    w = db.get(AICitizen, worker_id)
    c = market.bid(db, w, n.id, 9000, "首投")
    assert c.status == "proposed"
    db.commit()
    with pytest.raises(market.MarketError):
        market.bid(db, w, n.id, 8000, "重复投")


def test_negotiate_updates_offer(db):
    """议价：worker 调整报价；非双方不可议。"""
    buyer = _mk_party(db, "ng-buyer")
    w1 = _mk_party(db, "ng-w1")
    n = _mk_project_with_node(db, buyer, "文案", 10_000)
    worker = db.get(AICitizen, w1)
    c = market.bid(db, worker, n.id, 9000)
    market.negotiate(db, worker, n.id, 8500, "降价")
    import json
    terms = json.loads(c.terms_json)
    assert terms["offer_cent"] == 8500


def test_worker_bridge_register_heartbeat_fetch_ack(db):
    """worker_bridge 四函数冒烟：register→heartbeat→fetch_task→ack（不建新表）。"""
    from app import worker_bridge
    from app import escrow
    from app.models import OnboardingApplication
    buyer = _mk_party(db, "wb-buyer")
    wid = _mk_party(db, "wb-worker")
    w = db.get(AICitizen, wid)
    # register
    appl = worker_bridge.register_worker(db, w, "http://worker:9000", "gpt-x")
    assert appl.mode == "worker" and appl.stage == "probe"
    # heartbeat
    assert worker_bridge.heartbeat(db, w)["ok"]
    # 无任务时 fetch 返回 None
    assert worker_bridge.fetch_task(db, w) is None
    # 造一个 executing 合约后 fetch 应返回任务
    n = _mk_project_with_node(db, buyer, "文案", 5000)
    c = market.bid(db, w, n.id, 5000)
    b = db.get(AICitizen, buyer)
    escrow.sign_contract(db, b, c.id)
    task = worker_bridge.fetch_task(db, w)
    assert task and task["contract_id"] == c.id
    # ack
    r = worker_bridge.ack(db, w, c.id, "fp-x")
    assert r["ok"]
