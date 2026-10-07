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
"""N11 adversarial 攻击测试（登记册 10 类攻击视角）。

必覆盖（用户硬验收）：
- 假节点骗任务：伪造/篡改 token、越权操作他节点任务；
- 任务幂等：节点崩溃重拉、重复回传 delivered 不得重复结算；
- 节点离线违约：心跳超时巡检 → offline → 在途任务 timeout → 连动信用；
- 极端规模：超 max_concurrency 不得多派发。
"""
from datetime import datetime, timedelta

import pytest

from app import escrow, market, wallet, worker_service  # noqa
from app.database import SessionLocal
from app.models import (AICitizen, AIWallet, AIPermission, Contract, CreditEvent,
                        CreditProfile, Host, Project, ProjectNode, WorkerNode,
                        WorkerTask)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_host(db, hid: int, email: str):
    if db.query(Host).filter(Host.id == hid).first() is None:
        db.add(Host(id=hid, email=email, password_hash="x", host_credit=100))
        db.flush()


def _mk_ai(db, cid: int, host_id: int, name: str, balance: int = 100_000) -> int:
    c = AICitizen(id=cid, host_id=host_id, ai_uid=f"ai_{cid}", name=name,
                  occupation="通用", status="active", class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    wallet.credit(db, c.id, balance, "充值", ref=f"order:a{cid}")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:a{cid}")
    return c.id


def _sign(db, worker_id, buyer_id, P=10_000) -> int:
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:a11")
    p = Project(host_id=1, title="攻防", pm_citizen_id=buyer_id, status="running")
    db.add(p); db.flush()
    n = ProjectNode(project_id=p.id, skill="图文", spec="s", budget_cent=P,
                    status="matching")
    db.add(n); db.flush()
    w = db.get(AICitizen, worker_id)
    c = market.bid(db, w, n.id, P, "报价")
    b = db.get(AICitizen, buyer_id)
    escrow.sign_contract(db, b, c.id)
    return c.id


def _online(node):
    from datetime import datetime
    node.status = "idle"
    node.heartbeat_at = datetime.utcnow()
    node.offline_since = None


# ---------------- 攻击1：假节点（伪造 token / 改 id / 改 secret） ----------------
def test_adversarial_fake_node_cannot_pull(client, db):
    _mk_host(db, 31, "h31@t.test")
    worker_id = _mk_ai(db, 801, 31, "真工")
    buyer_id = _mk_ai(db, 802, 99, "甲方")
    node, token = worker_service.register_node(db, 31, "真节点", "local_gpu",
                                               "http://x", [], 1)
    _online(node)
    db.commit()
    cid = _sign(db, worker_id, buyer_id)
    db.commit()

    # 篡改 secret
    tampered = token[:-4] + "dead"
    r1 = client.get("/api/workers/pull",
                    headers={"Authorization": f"Bearer {tampered}"})
    assert r1.status_code == 401
    # 篡改 node id（冒牌别的节点）
    parts = token.split("_")
    forged = f"wn_999_{parts[2]}"
    r2 = client.get("/api/workers/pull",
                    headers={"Authorization": f"Bearer {forged}"})
    assert r2.status_code == 401
    # 无 token
    r3 = client.get("/api/workers/pull")
    assert r3.status_code == 401


# ---------------- 攻击2：越权回传他节点的任务 ----------------
def test_adversarial_cannot_submit_other_nodes_task(client, db):
    _mk_host(db, 32, "h32@t.test")
    _mk_host(db, 33, "h33@t.test")
    w1 = _mk_ai(db, 811, 32, "甲工")
    w2 = _mk_ai(db, 812, 33, "乙工")
    buyer = _mk_ai(db, 813, 99, "甲方")
    n1, t1 = worker_service.register_node(db, 32, "甲节点", "local_gpu", "http://x", [], 1)
    n2, t2 = worker_service.register_node(db, 33, "乙节点", "local_gpu", "http://x", [], 1)
    _online(n1)
    db.commit()
    cid1 = _sign(db, w1, buyer)
    db.commit()
    wt_id = db.query(WorkerTask).filter(WorkerTask.task_id == cid1).first().id
    # 乙节点拿甲节点的 worker_task_id 回传 → 403
    r = client.post(f"/api/workers/{wt_id}/result",
                    json={"result_ref": "s3://x", "status": "delivered"},
                    headers={"Authorization": f"Bearer {t2}"})
    assert r.status_code in (403, 404), r.text


# ---------------- 攻击3：任务幂等——重复回传不重复结算 ----------------
def test_adversarial_idempotent_double_deliver(client, db):
    _mk_host(db, 34, "h34@t.test")
    worker_id = _mk_ai(db, 821, 34, "幂等工")
    buyer_id = _mk_ai(db, 822, 99, "甲方")
    node, token = worker_service.register_node(db, 34, "幂节点", "local_gpu",
                                               "http://x", [], 1)
    _online(node)
    db.commit()
    cid = _sign(db, worker_id, buyer_id)
    db.commit()
    wt_id = db.query(WorkerTask).filter(WorkerTask.task_id == cid).first().id
    pull = client.get("/api/workers/pull",
                      headers={"Authorization": f"Bearer {token}"})
    assert pull.json()["worker_task_id"] == wt_id
    r1 = client.post(f"/api/workers/{wt_id}/result",
                     json={"result_ref": "s3://v1", "status": "delivered"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200 and r1.json()["duplicate"] is False
    # 节点崩溃重拉（running 任务再发一次，不报错不重复）
    repull = client.get("/api/workers/pull",
                        headers={"Authorization": f"Bearer {token}"})
    assert repull.json() == {}   # 已 delivered 无任务
    # 重复回传同一 task → 幂等 duplicate，不触发第二次交付/结算
    r2 = client.post(f"/api/workers/{wt_id}/result",
                     json={"result_ref": "s3://v1", "status": "delivered"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    # 合约只 deliver 一次（deliverables 版本=1）
    from app.models import Deliverable
    dels = db.query(Deliverable).filter(Deliverable.contract_id == cid).all()
    assert len(dels) == 1


# ---------------- 攻击4：节点离线超时 → offline + 在途任务 timeout + 违约 ----------------
def test_adversarial_offline_sweep_breach(client, db):
    _mk_host(db, 35, "h35@t.test")
    worker_id = _mk_ai(db, 831, 35, "掉线工")
    buyer_id = _mk_ai(db, 832, 99, "甲方")
    node, token = worker_service.register_node(db, 35, "掉线节点", "local_gpu",
                                               "http://x", [], 1)
    _online(node)
    db.commit()
    cid = _sign(db, worker_id, buyer_id)
    db.commit()
    wt = db.query(WorkerTask).filter(WorkerTask.task_id == cid).first()
    # 节点 pull 后再也不心跳（模拟崩溃）
    pull = client.get("/api/workers/pull",
                      headers={"Authorization": f"Bearer {token}"})
    assert pull.status_code == 200
    # 把心跳拨到 10 分钟前（超过 3 分钟超时）
    node.heartbeat_at = datetime.utcnow() - timedelta(minutes=10)
    node.status = "busy"
    db.commit()

    out = worker_service.sweep_offline(db)
    db.commit()
    assert isinstance(out, int) and out >= 1   # 日任务契约：返回 int
    node = db.get(WorkerNode, node.id)
    assert node.status == "offline"
    wt = db.get(WorkerTask, wt.id)
    assert wt.status == "timeout"
    ev = db.query(CreditEvent).filter(CreditEvent.citizen_id == worker_id,
                                      CreditEvent.event == "breach",
                                      CreditEvent.ref == f"contract:{cid}").first()
    assert ev is not None
    # 幂等：再扫一次不重复记违约
    worker_service.sweep_offline(db)
    db.commit()
    n_breach = db.query(CreditEvent).filter(CreditEvent.citizen_id == worker_id,
                                            CreditEvent.event == "breach",
                                            CreditEvent.ref == f"contract:{cid}").count()
    assert n_breach == 1


# ---------------- 攻击5：并发上限保护（极端规模） ----------------
def test_adversarial_concurrency_cap(client, db):
    _mk_host(db, 36, "h36@t.test")
    worker_id = _mk_ai(db, 841, 36, "单槽工")
    buyer_id = _mk_ai(db, 842, 99, "甲方")
    node, token = worker_service.register_node(db, 36, "单槽", "local_gpu",
                                               "http://x", [], 1)
    _online(node)
    db.commit()
    c1 = _sign(db, worker_id, buyer_id)
    # 直接再造第二个合约（第二节点任务）
    c2 = _sign(db, worker_id, buyer_id)
    db.commit()
    # 第一次 pull 拿到 c1
    p1 = client.get("/api/workers/pull", headers={"Authorization": f"Bearer {token}"})
    assert p1.json()["contract_id"] == c1
    # 并发已满（running=1 >= max=1）→ 第二次 pull 返回空
    p2 = client.get("/api/workers/pull", headers={"Authorization": f"Bearer {token}"})
    assert p2.json() == {}
