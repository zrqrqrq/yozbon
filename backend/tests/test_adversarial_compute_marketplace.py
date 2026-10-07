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
"""M1 攻击测试（契约 §2.3 清单 + 边界登记册 §一 10 类视角）。

对 M1 实现做逆向攻击：超钱包 / 委托过期 / 委托撤销 / scope 越权 / 超限额 /
自买自卖(C-17) / 非合约双方下载 / 跨宿主委托 / 伪造 delegation_id。
金额 integer 分。
"""
from datetime import datetime, timedelta

import pytest

from app import wallet, project as proj
from app.database import SessionLocal
from app.models import AICitizen, Contract, Deliverable, Delegation
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


def _activate(citizen_id: int):
    db = _db()
    c = db.get(AICitizen, citizen_id)
    c.status = "active"
    db.commit()
    db.close()


def _precharge_pool():
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="m1adv:pool")
    db.commit()
    db.close()


def _hkey(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _host_h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _delivered_contract(client, host_token, buyer, worker, P=5000):
    """发包→运行→拆节点→投标→签约→交付，返回 (project_id, contract_id, deliverable_id)。"""
    topup(client, host_token, buyer["id"], 100_000)
    r = client.post("/api/ai/projects", json={"title": "外包", "budget_cent": P},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(buyer))
    db = _db()
    proj.submit_nodes(db, pid, [{"key": "n1", "skill": "文案", "spec": "s",
                                  "budget_cent": P}], deps=[])
    db.commit()
    node = db.query(proj.ProjectNode).filter_by(project_id=pid).first()
    nid = node.id
    db.close()
    r = client.post(f"/api/ai/jobs/{nid}/bid", json={"offer_cent": P},
                    headers=_hkey(worker))
    cid = r.json()["contract_id"]
    client.post(f"/api/ai/contracts/{cid}/accept",
                json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    client.post(f"/api/ai/contracts/{cid}/deliver",
                json={"file_ref": "s3://x", "fingerprint": "fp-adv"},
                headers=_hkey(worker))
    db = _db()
    did = db.query(Deliverable).filter_by(contract_id=cid).first().id
    db.close()
    return pid, cid, did


# ---- A1. 超钱包发包 → 409 ----
def test_adv_1_over_wallet_publish_409(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="穷AI", occupation="文案")
    topup(client, host["token"], ai["id"], 50)          # 只有 50 分
    _activate(ai["id"])
    r = client.post("/api/ai/projects",
                   json={"title": "超钱包", "budget_cent": 100},   # 要 100 分
                   headers=_hkey(ai))
    assert r.status_code == 409, r.text
    # 未产生项目
    r2 = client.get("/api/ai/projects", headers=_hkey(ai))
    assert r2.json()["total"] == 0


# ---- A2. 委托过期后操作 → 403，且委托状态翻 expired ----
def test_adv_2_expired_delegation_403_and_flips(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="代发AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"], "scope": ["publish_task"],
                          "expires_at": past},
                    headers=_host_h(host["token"]))
    did = r.json()["id"]
    # 过期委托发包 → 403
    r = client.post("/api/ai/projects",
                    json={"title": "代发", "budget_cent": 500, "delegation_id": did},
                    headers=_hkey(ai))
    assert r.status_code == 403, r.text
    # 状态翻 expired（即使操作被拒也持久化）
    r = client.get("/api/host/delegations", headers=_host_h(host["token"]))
    row = [x for x in r.json()["items"] if x["id"] == did][0]
    assert row["status"] == "expired"


# ---- A3. 委托撤销后操作 → 403 ----
def test_adv_3_revoked_delegation_403(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="撤销AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"], "scope": ["publish_task"]},
                    headers=_host_h(host["token"]))
    did = r.json()["id"]
    # 撤销
    client.delete(f"/api/host/delegations/{did}", headers=_host_h(host["token"]))
    # 撤销后发包 → 403
    r = client.post("/api/ai/projects",
                    json={"title": "代发", "budget_cent": 500, "delegation_id": did},
                    headers=_hkey(ai))
    assert r.status_code == 403, r.text


# ---- A4. scope 越权：只有 download 的委托去 publish_task → 403 ----
def test_adv_4_scope_privilege_escalation_403(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="越权AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"], "scope": ["download"]},
                    headers=_host_h(host["token"]))
    did = r.json()["id"]
    # 委托只授 download，却用来发包(publish_task) → 403
    r = client.post("/api/ai/projects",
                    json={"title": "越权发", "budget_cent": 500, "delegation_id": did},
                    headers=_hkey(ai))
    assert r.status_code == 403, r.text


# ---- A5. 超委托单笔限额 → 403 ----
def test_adv_5_over_delegation_max_amount_403(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="限额AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"], "scope": ["publish_task"],
                          "max_amount_cent": 100},
                    headers=_host_h(host["token"]))
    did = r.json()["id"]
    # 限额 100 分，发 101 分项目 → 403
    r = client.post("/api/ai/projects",
                    json={"title": "超限", "budget_cent": 101, "delegation_id": did},
                    headers=_hkey(ai))
    assert r.status_code == 403, r.text
    # 限额内可用（边界值 100 通过）
    r = client.post("/api/ai/projects",
                    json={"title": "限额内", "budget_cent": 100, "delegation_id": did},
                    headers=_hkey(ai))
    assert r.status_code == 200, r.text


# ---- A6. 自买自卖仍被拦（C-17 验证）→ 签约 400 ----
def test_adv_6_self_deal_blocked_c17(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="自炒AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    _precharge_pool()
    P = 5000
    r = client.post("/api/ai/projects", json={"title": "自炒", "budget_cent": P},
                    headers=_hkey(ai))
    pid = r.json()["id"]
    client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(ai))
    db = _db()
    proj.submit_nodes(db, pid, [{"key": "n1", "skill": "文案", "spec": "s",
                                  "budget_cent": P}], deps=[])
    db.commit()
    nid = db.query(proj.ProjectNode).filter_by(project_id=pid).first().id
    db.close()
    # 同一 AI 对自己发布的节点投标（worker==buyer）
    r = client.post(f"/api/ai/jobs/{nid}/bid", json={"offer_cent": P},
                    headers=_hkey(ai))
    cid = r.json()["contract_id"]
    # 签约 → C-17 自买自卖拦截
    r = client.post(f"/api/ai/contracts/{cid}/accept",
                    json={"disclaimer_accepted": True}, headers=_hkey(ai))
    assert r.status_code == 400, r.text


# ---- A7. 非合约双方下载 → 403 ----
def test_adv_7_non_party_download_403(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="买方", occupation="项目管理")
    worker = new_ai(client, host["token"], name="工人", occupation="文案")
    intruder = new_ai(client, host["token"], name="闯入者", occupation="文案")
    for a in (buyer, worker, intruder):
        _activate(a["id"])
    _precharge_pool()
    _, _, did = _delivered_contract(client, host["token"], buyer, worker)
    # 闯入者既非 worker 也非 buyer → 403
    r = client.get(f"/api/ai/deliverables/{did}/download", headers=_hkey(intruder))
    assert r.status_code == 403, r.text


# ---- A8. 跨宿主委托（委托他宿主 AI）→ 404 ----
def test_adv_8_delegate_other_host_ai_404(client):
    host_a = new_host(client)
    host_b = new_host(client)
    ai_b = new_ai(client, host_b["token"], name="B宿主AI", occupation="文案")
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai_b["id"], "scope": ["download"]},
                    headers=_host_h(host_a["token"]))
    assert r.status_code == 404, r.text


# ---- A9. 无委托 AI 传伪造 delegation_id → 404 ----
def test_adv_9_fake_delegation_id_404(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="伪造AI", occupation="文案")
    topup(client, host["token"], ai["id"], 100_000)
    _activate(ai["id"])
    r = client.post("/api/ai/projects",
                    json={"title": "伪造", "budget_cent": 500, "delegation_id": 999999},
                    headers=_hkey(ai))
    assert r.status_code == 404, r.text


# ---- A10. 非项目总管 AI 做项目级验收 → 403 ----
def test_adv_10_non_pm_acceptance_403(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="总管", occupation="项目管理")
    worker = new_ai(client, host["token"], name="工人", occupation="文案")
    outsider = new_ai(client, host["token"], name="外人", occupation="文案")
    for a in (buyer, worker, outsider):
        _activate(a["id"])
    _precharge_pool()
    pid, cid, did = _delivered_contract(client, host["token"], buyer, worker)
    # 外人既非总管也无委托 → 403
    r = client.post(f"/api/ai/projects/{pid}/acceptance",
                   json={"result": "accept"}, headers=_hkey(outsider))
    assert r.status_code == 403, r.text


# ---- A11. 非法 scope 创建委托 → 400 ----
def test_adv_11_invalid_scope_400(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="坏scope", occupation="文案")
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"], "scope": ["hack_all"]},
                    headers=_host_h(host["token"]))
    assert r.status_code == 400, r.text
