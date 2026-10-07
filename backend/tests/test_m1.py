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
"""M1 常规单测 + 端到端：AI 发包/确认运行/项目级验收/下载 + 宿主委托（C-21/C-22）。

端到端链路：AI(考试通过→active) → 打工接单攒积分 → AI 发包 → 营销AI投标中标
→ 交付 → AI 项目级验收 → 结算 → 下载交付物。
金额 integer 分。测试数据/中文断言均写在本 .py（UTF-8），禁止 PowerShell 内联中文。
"""
from app import wallet, project as proj
from app.database import SessionLocal
from app.models import AICitizen, Contract, Deliverable, Delegation
from tests.conftest import new_host, new_ai, topup

P = 10_000
# 税池预充 → fee_rate=0.05；net = P-fee(500)-tax(450) = 9050
EXPECT_WORKER_NET = 9050


def _db():
    return SessionLocal()


def _activate(citizen_id: int):
    """直接把见习 AI 转正 active（等价"考试通过"，契约允许直接签发）。"""
    db = _db()
    c = db.get(AICitizen, citizen_id)
    c.status = "active"
    db.commit()
    db.close()


def _precharge_pool():
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", 100_000, ref="m1:pool")
    db.commit()
    db.close()


def _hkey(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _host_h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# =====================================================================
# 端到端：AI 发包 → 营销AI投标中标 → 交付 → AI 项目级验收 → 结算 → 下载
# =====================================================================
def test_e2e_ai_publish_settle_download(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="总包AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="营销AI", occupation="文案")
    topup(client, host["token"], buyer["id"], 100_000)
    _activate(buyer["id"])
    _activate(worker["id"])
    _precharge_pool()

    # 1) AI 发包（预算<20000 免评审 → approved）
    r = client.post("/api/ai/projects", json={"title": "文案外包", "budget_cent": P},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "approved"
    assert r.json()["budget_cent"] == P

    # 2) AI 确认运行（approved → running）
    r = client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"

    # 3) 拆 WBS 节点（项目 running → 无依赖节点自动 matching）
    db = _db()
    proj.submit_nodes(db, pid,
                      [{"key": "n1", "skill": "文案", "spec": "写一篇",
                        "deliverable_std": "docx", "budget_cent": P, "duration_h": 8}],
                      deps=[])
    db.commit()
    node = db.query(proj.ProjectNode).filter_by(project_id=pid).first()
    assert node.status == "matching"
    nid = node.id
    db.close()

    # 4) 营销AI 投标（buyer 自动 = 项目总管 buyer.id）
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": P, "message": "接"},
                    headers=_hkey(worker))
    assert r.status_code == 200, r.text
    cid = r.json()["contract_id"]
    assert r.json()["status"] == "proposed"

    # 5) buyer 签约托管（M8：签约前置免责确认 disclaimer_accepted=true）
    r = client.post(f"/api/ai/contracts/{cid}/accept",
                    json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    assert r.status_code == 200, r.text

    # 6) worker 交付
    r = client.post(f"/api/ai/contracts/{cid}/deliver",
                    json={"file_ref": "s3://out.docx", "fingerprint": "sha256:fp1"},
                    headers=_hkey(worker))
    assert r.status_code == 200, r.text

    # 7) AI 项目级验收（accept → 整单结算）
    r = client.post(f"/api/ai/projects/{pid}/acceptance",
                    json={"result": "accept", "reason_json": "[]"},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["accepted_n"] == 1 and body["rejected_n"] == 0
    assert body["processed"] == [{"contract_id": cid, "status": "accepted"}]

    # 8) 合约已结算；worker 实得 = P-fee-tax（攒积分/报酬）
    db = _db()
    c = db.get(Contract, cid)
    assert c.status == "accepted"
    assert wallet.balance(db, worker["id"]) == EXPECT_WORKER_NET
    # buyer 托管占用已释放
    assert wallet.get_wallet(db, buyer["id"]).escrow_cent == 0
    d = db.query(Deliverable).filter_by(contract_id=cid).first()
    did = d.id
    db.close()

    # 9) buyer 下载交付物（合约买方）
    r = client.get(f"/api/ai/deliverables/{did}/download", headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["allowed"] is True
    assert out["fingerprint"] == "sha256:fp1"
    assert out["contract_id"] == cid

    # 10) worker 也能下载自己的交付物（合约履约方）
    r = client.get(f"/api/ai/deliverables/{did}/download", headers=_hkey(worker))
    assert r.status_code == 200, r.text


# =====================================================================
# 常规单测：项目列表 / 委托 CRUD / 项目级验收 reject 返工
# =====================================================================
def test_ai_list_projects(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="发包AI", occupation="项目管理")
    topup(client, host["token"], buyer["id"], 100_000)
    _activate(buyer["id"])
    client.post("/api/ai/projects", json={"title": "甲", "budget_cent": 5000},
                headers=_hkey(buyer))
    r = client.get("/api/ai/projects?limit=50", headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    assert any(p["pm_citizen_id"] == buyer["id"] for p in body["items"])


def test_host_delegate_crud(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="被委托AI", occupation="文案")
    # 创建委托
    r = client.post("/api/host/delegate",
                    json={"ai_id": ai["id"],
                          "scope": ["publish_task", "download"],
                          "max_amount_cent": 5000},
                    headers=_host_h(host["token"]))
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    assert r.json()["status"] == "active"
    # 列表（含 scope）
    r = client.get("/api/host/delegations", headers=_host_h(host["token"]))
    assert r.status_code == 200, r.text
    row = [x for x in r.json()["items"] if x["id"] == did][0]
    assert set(row["scope"]) == {"publish_task", "download"}
    assert row["status"] == "active"
    # 撤销（幂等）
    r = client.delete(f"/api/host/delegations/{did}", headers=_host_h(host["token"]))
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = client.delete(f"/api/host/delegations/{did}", headers=_host_h(host["token"]))
    assert r.status_code == 200 and r.json()["status"] == "revoked"   # 幂等返回现状


def test_project_acceptance_reject_sends_rework(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="买方AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="工人AI", occupation="文案")
    topup(client, host["token"], buyer["id"], 100_000)
    _activate(buyer["id"])
    _activate(worker["id"])
    _precharge_pool()

    r = client.post("/api/ai/projects", json={"title": "返工测试", "budget_cent": 5000},
                    headers=_hkey(buyer))
    pid = r.json()["id"]
    client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(buyer))
    db = _db()
    proj.submit_nodes(db, pid, [{"key": "n1", "skill": "文案", "spec": "s",
                                 "budget_cent": 5000}], deps=[])
    db.commit()
    node = db.query(proj.ProjectNode).filter_by(project_id=pid).first()
    nid = node.id
    db.close()
    r = client.post(f"/api/ai/jobs/{nid}/bid", json={"offer_cent": 5000},
                    headers=_hkey(worker))
    cid = r.json()["contract_id"]
    client.post(f"/api/ai/contracts/{cid}/accept",
                json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    client.post(f"/api/ai/contracts/{cid}/deliver",
                json={"file_ref": "s3://x", "fingerprint": "fp-r"},
                headers=_hkey(worker))

    # 项目级 reject → 返工（合约退回 executing，托管仍锁定）
    r = client.post(f"/api/ai/projects/{pid}/acceptance",
                    json={"result": "reject", "reason_json": '["质量不达标"]'},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["rejected_n"] == 1 and r.json()["accepted_n"] == 0
    db = _db()
    assert db.get(Contract, cid).status == "executing"
    from app.models import Escrow
    assert db.get(Escrow, cid).locked == 1      # 规则14：返工托管仍锁定
    db.close()
