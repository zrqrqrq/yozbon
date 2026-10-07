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
"""N11 worker_bridge 业务测试（设计 §3 N11：宿主自备算力真算力接入）。

全链路：注册节点（host JWT，token 单次明文）→ 心跳 → 签约分派 → pull →
回传 delivered → 驱动 escrow.deliver → 买方验收结算；failed 回传连动信用违约。
"""
import json
from datetime import datetime, timedelta

import pytest

from app import escrow, market, wallet, worker_service  # noqa: F401
from app.database import SessionLocal
from app.models import (AICitizen, AIWallet, AIPermission, Contract,
                        CreditEvent, CreditProfile, Host, Project, ProjectNode,
                        WorkerNode, WorkerTask)


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
        db.add(Host(id=hid, email=email, password_hash="x", host_credit=100,
                    status="active"))
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
    wallet.credit(db, c.id, balance, "充值", ref=f"order:w{cid}")
    wallet.adjust_system_state(db, "money_supply", balance, ref=f"order:w{cid}")
    return c.id


def _sign_contract(db, worker_id: int, buyer_id: int, P: int = 10_000) -> int:
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool11")
    p = Project(host_id=1, title="N11项目", pm_citizen_id=buyer_id, status="running")
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
    """节点上线（模拟一次心跳）：分派钩子只选非离线节点。"""
    from datetime import datetime
    node.status = "idle"
    node.heartbeat_at = datetime.utcnow()
    node.offline_since = None


def _api_host_token(client, email: str) -> str:
    r = client.post("/api/host/register", json={"email": email, "password": "pass123456"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------------- 注册：token 单次明文，库中只存哈希 ----------------
def test_register_returns_token_once(client):
    tok = _api_host_token(client, f"reg_{__import__('uuid').uuid4().hex[:8]}@t.test")
    r = client.post("/api/workers/register",
                    json={"name": "本机4090", "node_type": "local_gpu",
                          "base_url": "http://127.0.0.1:9000",
                          "capabilities": ["image"], "max_concurrency": 2},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node_token"].startswith("wn_")
    node_id = body["node_id"]
    db = SessionLocal()
    try:
        node = db.get(WorkerNode, node_id)
        assert node.api_key_hash and node.api_key_hash != body["node_token"]
        assert node.max_concurrency == 2
    finally:
        db.close()


# ---------------- 全链路：签约分派 → 心跳 → pull → 回传 → 结算 ----------------
def test_full_chain_register_heartbeat_pull_deliver_settle(client, db):
    _mk_host(db, 21, "h21@t.test")
    worker_id = _mk_ai(db, 501, 21, "算力工")
    buyer_id = _mk_ai(db, 502, 99, "甲方")
    db.commit()

    # 宿主 API 注册节点（用宿主 21 的 JWT——直接走 DB 建节点并签 token）
    node, token = worker_service.register_node(
        db, 21, "算力A", "local_gpu", "http://x", ["image"], 1)
    _online(node)
    db.commit()

    # 签约 → handler 应把合约分派为 pending WorkerTask
    cid = _sign_contract(db, worker_id, buyer_id)
    db.commit()
    wt = db.query(WorkerTask).filter(WorkerTask.task_id == cid).first()
    assert wt is not None and wt.status == "pending"

    # 节点心跳
    hb = client.post("/api/workers/heartbeat", json={"load": 0},
                     headers={"Authorization": f"Bearer {token}"})
    assert hb.status_code == 200, hb.text

    # pull 拿到任务
    pull = client.get("/api/workers/pull",
                      headers={"Authorization": f"Bearer {token}"})
    assert pull.status_code == 200
    pspec = pull.json()
    assert pspec["contract_id"] == cid and pspec["status"] == "running"
    wt_id = pspec["worker_task_id"]

    # 回传 delivered → 驱动 escrow.deliver
    res = client.post(f"/api/workers/{wt_id}/result",
                      json={"result_ref": "s3://deliver/v1.png", "status": "delivered"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    c = db.get(Contract, cid)
    assert c.status == "delivered"

    # 买方验收 → 结算
    b = db.get(AICitizen, buyer_id)
    escrow.acceptance(db, b, cid, "accept")
    db.commit()
    assert db.get(Contract, cid).status == "accepted"


# ---------------- failed 回传 → 连动信用违约 ----------------
def test_failed_result_records_breach_credit(client, db):
    _mk_host(db, 22, "h22@t.test")
    worker_id = _mk_ai(db, 601, 22, "违约工")
    buyer_id = _mk_ai(db, 602, 99, "甲方2")
    node, token = worker_service.register_node(
        db, 22, "坏节点", "rh_api", "http://x", ["text"], 1)
    _online(node)
    db.commit()
    cid = _sign_contract(db, worker_id, buyer_id)
    db.commit()
    wt = db.query(WorkerTask).filter(WorkerTask.task_id == cid).first()
    pull = client.get("/api/workers/pull",
                      headers={"Authorization": f"Bearer {token}"})
    wt_id = pull.json()["worker_task_id"]
    res = client.post(f"/api/workers/{wt_id}/result",
                      json={"result_ref": "", "status": "failed"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    db.expire_all()
    ev = db.query(CreditEvent).filter(CreditEvent.citizen_id == worker_id,
                                      CreditEvent.event == "breach").first()
    assert ev is not None and ev.ref == f"contract:{cid}"
    assert db.get(WorkerTask, wt_id).status == "failed"


# ---------------- 无可用节点 → RH fallback（不分派） ----------------
def test_rh_fallback_when_no_node(client, db):
    _mk_host(db, 23, "h23@t.test")
    worker_id = _mk_ai(db, 701, 23, "无节点工")
    buyer_id = _mk_ai(db, 702, 99, "甲方3")
    # 注册一个节点但保持离线（无心跳）
    node, token = worker_service.register_node(
        db, 23, "离线节点", "local_gpu", "http://x", [], 1)
    db.commit()
    cid = _sign_contract(db, worker_id, buyer_id)
    db.commit()
    # 节点离线 → 不应分派 WorkerTask（合约保持 executing，平台通道承接）
    wt = db.query(WorkerTask).filter(WorkerTask.task_id == cid).first()
    assert wt is None
    assert db.get(Contract, cid).status == "executing"
