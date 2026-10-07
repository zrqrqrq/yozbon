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
"""M8 常规单测：提示词库 CRUD + 种子幂等 + T1/T2/免责/决策小组/仲裁/C-34。

金额 integer 分；中文断言写在本 .py（UTF-8），禁止 PowerShell 内联中文。
夹具复用 conftest：new_host/new_ai/topup（禁止改 conftest）。
"""
import importlib.util
from pathlib import Path

from app import governance, prompt_service, wallet
from app import project as proj
from app.database import SessionLocal
from app.models import (AICitizen, AuditLog, Contract, IntelReport, ProjectNode)
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


def _hkey(ai):
    return {"X-AI-Key": ai["api_key"]}


def _activate(cid, class_level=None):
    db = _db()
    a = db.get(AICitizen, cid)
    a.status = "active"
    if class_level:
        a.class_level = class_level
    db.commit()
    db.close()


def _precharge(pool=100_000):
    db = _db()
    wallet.adjust_system_state(db, "tax_pool", pool, ref="m8:pool")
    db.commit()
    db.close()


def _load_seed():
    p = Path(__file__).resolve().parent.parent / "tools" / "seed_prompts.py"
    spec = importlib.util.spec_from_file_location("seed_prompts", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_all():
    mod = _load_seed()
    db = _db()
    out = mod.seed(db)
    db.close()
    return out


def _new_buyer_worker(client, buyer_topup=200_000, worker_topup=0):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="总包AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="执行AI", occupation="文案")
    topup(client, host["token"], buyer["id"], buyer_topup)
    if worker_topup:
        topup(client, host["token"], worker["id"], worker_topup)
    _activate(buyer["id"])
    return host, buyer, worker


def _build_nodes(client, buyer, nodes, project_budget=19_000):
    """发包(<20000 直 approved)→运行→拆节点；返回 {key: node_id}。"""
    r = client.post("/api/ai/projects",
                    json={"title": "M8测试任务", "budget_cent": project_budget},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    db = _db()
    proj.submit_nodes(db, pid, nodes, deps=[])
    db.commit()
    rows = db.query(ProjectNode).filter_by(project_id=pid).order_by(ProjectNode.seq).all()
    ids = [n.id for n in rows]
    db.close()
    return pid, ids


# ---------------------------------------------------------------------------
# 提示词库 CRUD + 版本隔离
# ---------------------------------------------------------------------------
def test_prompt_crud_duplicate_409_and_version_isolation(client):
    db = _db()
    r = prompt_service.create_prompt(db, "mod_x", "role_y", "v1", "内容v1", "on_enter")
    db.commit()
    assert r.version == "v1"
    # 同 module+role+version 重复 → 409
    try:
        prompt_service.create_prompt(db, "mod_x", "role_y", "v1", "重复", "on_enter")
        raise AssertionError("应抛 PromptError(409)")
    except prompt_service.PromptError as exc:
        assert "duplicate" in str(exc)
    # 取 active
    g = prompt_service.get_prompt(db, "mod_x", "role_y")
    assert g.content == "内容v1"
    # 归档 v1 → 新建 v2：get_prompt 必须拿到 v2（版本隔离）
    g.status = "archived"
    db.flush()
    prompt_service.create_prompt(db, "mod_x", "role_y", "v2", "内容v2", "on_enter")
    g2 = prompt_service.get_prompt(db, "mod_x", "role_y")
    assert g2.version == "v2" and g2.content == "内容v2"
    # list
    lst = prompt_service.list_prompts(db, module="mod_x")
    assert lst["total"] == 2
    db.close()


def test_seed_idempotent(client):
    mod = _load_seed()
    db = _db()
    out1 = mod.seed(db)
    out2 = mod.seed(db)
    db.close()
    assert out1["inserted"] == 5
    assert out2["inserted"] == 0 and out2["skipped"] == 5


# ---------------------------------------------------------------------------
# T1 发布细化校验
# ---------------------------------------------------------------------------
def test_t1_legacy_publish_ok_and_missing_core_400(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="发包AI", occupation="项目管理")
    topup(client, host["token"], buyer["id"], 200_000)
    _activate(buyer["id"])
    # 老格式（仅 title+budget）→ 软通过 200
    r = client.post("/api/ai/projects",
                    json={"title": "文案外包", "budget_cent": 5000},
                    headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    # 显式 requirements 缺核心要素 → 400
    r = client.post("/api/ai/projects",
                    json={"title": "文案外包", "budget_cent": 5000,
                          "requirements": {"goal": "写一篇文案"}},
                    headers=_hkey(buyer))
    assert r.status_code == 400, r.text
    assert "T1 seven elements" in r.json()["detail"]


# ---------------------------------------------------------------------------
# T2 自评 + 决策小组
# ---------------------------------------------------------------------------
def test_t2_self_assess_reject(client):
    host, buyer, worker = _new_buyer_worker(client)
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": "短描述",
                              "deliverable_std": "docx", "budget_cent": 12_000,
                              "duration_h": 24}])
    # 成功率 <50 → 400
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000,
                          "self_assess": {"success_rate_pct": 30, "policy_ok": True}},
                    headers=_hkey(worker))
    assert r.status_code == 400, r.text
    # policy_ok=false → 400
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000,
                          "self_assess": {"success_rate_pct": 80, "policy_ok": False}},
                    headers=_hkey(worker))
    assert r.status_code == 400, r.text


def test_small_order_no_decision_panel(client):
    host, buyer, worker = _new_buyer_worker(client)
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": "短描述",
                              "deliverable_std": "docx", "budget_cent": 12_000,
                              "duration_h": 24}])
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000}, headers=_hkey(worker))
    assert r.status_code == 200, r.text
    assert "decision" not in r.json()      # 小单不触发决策小组


def test_big_order_funded_worker_pass(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=200_000)
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": "短描述",
                              "deliverable_std": "v1验收标准", "budget_cent": 25_000,
                              "duration_h": 24}])
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 25_000}, headers=_hkey(worker))
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["decision"] == "pass"
    assert r.json()["decision"]["total"] >= 50


def test_big_order_broke_worker_reject(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=0)  # 工人无余额
    long_spec = "字" * 900
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": long_spec,
                              "deliverable_std": "v1验收标准", "budget_cent": 30_000,
                              "duration_h": 24}])
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 30_000}, headers=_hkey(worker))
    assert r.status_code == 400, r.text
    assert "Decision panel" in r.json()["detail"]


def test_decision_panel_request_clarification(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=200_000)
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": "短描述",
                              "deliverable_std": "", "budget_cent": 25_000,
                              "duration_h": 24}])
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 25_000}, headers=_hkey(worker))
    assert r.status_code == 400, r.text
    assert "sent back" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 签约免责前置
# ---------------------------------------------------------------------------
def test_disclaimer_required_and_audit(client):
    _seed_all()
    host, buyer, worker = _new_buyer_worker(client)
    _, (nid,) = _build_nodes(client, buyer,
                            [{"key": "n1", "skill": "文案", "spec": "短",
                              "deliverable_std": "docx", "budget_cent": 12_000,
                              "duration_h": 24}])
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000}, headers=_hkey(worker))
    cid = r.json()["contract_id"]
    # 未签免责 → 400
    r = client.post(f"/api/ai/contracts/{cid}/accept", headers=_hkey(buyer))
    assert r.status_code == 400, r.text
    assert "disclaimer" in r.json()["detail"]
    # 显式确认 → 200
    r = client.post(f"/api/ai/contracts/{cid}/accept",
                    json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    db = _db()
    al = db.query(AuditLog).filter_by(action="disclaimer.sign").first()
    assert al is not None and cid == json_contract_id(al)
    db.close()


def json_contract_id(audit_row):
    import json
    return json.loads(audit_row.detail).get("contract_id")


# ---------------------------------------------------------------------------
# T4 仲裁挂接
# ---------------------------------------------------------------------------
def test_t4_verdict_has_prompt_version(client):
    _seed_all()
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="买方AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="工人AI", occupation="文案")
    arb = new_ai(client, host["token"], name="仲裁员", occupation="治理")
    topup(client, host["token"], buyer["id"], 100_000)
    _precharge()

    # 直接在库内造「已签约托管 + 已开仲裁案」
    from app import escrow
    from app.models import Project, Contract
    P = 2000
    db = _db()
    buyer_host_id = db.get(AICitizen, buyer["id"]).host_id
    proj_row = Project(host_id=buyer_host_id, title="仲裁项目", budget_cent=P,
                       status="running", pm_citizen_id=buyer["id"])
    db.add(proj_row)
    db.flush()
    node = ProjectNode(project_id=proj_row.id, skill="dev", budget_cent=P,
                       deliverable_std="v1", status="matching", seq=1)
    db.add(node)
    db.flush()
    c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                 terms_json='{"offer_cent": %d}' % P, escrow_cent=P,
                 project_id=proj_row.id, node_id=node.id)
    db.add(c)
    db.flush()
    buyer_obj = db.get(AICitizen, buyer["id"])
    escrow.sign_contract(db, buyer_obj, c.id)
    buyer_obj = db.get(AICitizen, buyer["id"])
    case = escrow.open_dispute(db, buyer_obj, c.id, type_="delivery")
    case_id = case.id
    governance.form_arbitration_panel(db, case_id, [arb["id"]])
    db.commit()
    db.close()

    r = client.post(f"/api/ai/arbitration/{case_id}/verdict",
                    json={"decision": "support_worker", "ratio": 1.0,
                          "reason": "申诉不成立"}, headers=_hkey(arb))
    assert r.status_code == 200, r.text
    assert r.json()["prompt_version"] == "v1"


# ---------------------------------------------------------------------------
# C-34：platform_intel collected → 自动入库
# ---------------------------------------------------------------------------
def test_c34_platform_intel_autocollect(client):
    host = new_host(client)
    op = new_ai(client, host["token"], name="运营AI", occupation="安全治理")
    _activate(op["id"], class_level="governance")
    db = _db()
    t = governance.publish_task(db, "platform_intel", {}, budget_cent=300, deadline=None)
    tid = t.id
    db.commit()
    db.close()
    r = client.post(f"/api/ai/gov/tasks/{tid}/report",
                    json={"conclusion": "collected", "evidence": {"note": "x"}},
                    headers=_hkey(op))
    assert r.status_code == 200, r.text
    db = _db()
    n = db.query(IntelReport).count()
    db.close()
    assert n >= 1


# ---------------------------------------------------------------------------
# 端到端：T1 发包 → T2 小单直过 → 大额节点决策小组放行 → 免责签约 → 交付 → 验收 → 结算
# ---------------------------------------------------------------------------
def test_e2e_publish_bid_disclaimer_deliver_settle(client):
    _seed_all()
    _precharge()
    host, buyer, worker = _new_buyer_worker(client, worker_topup=200_000)
    pid, (n_small, n_big) = _build_nodes(client, buyer, [
        {"key": "n1", "skill": "文案", "spec": "小单", "deliverable_std": "docx",
         "budget_cent": 8000, "duration_h": 24},
        {"key": "n2", "skill": "文案", "spec": "大单短描述", "deliverable_std": "v1验收",
         "budget_cent": 25_000, "duration_h": 24},
    ])
    # 小单直过（不触发决策小组）
    r1 = client.post(f"/api/ai/jobs/{n_small}/bid",
                    json={"offer_cent": 8000}, headers=_hkey(worker))
    assert r1.status_code == 200 and "decision" not in r1.json()
    c_small = r1.json()["contract_id"]
    # 大额节点决策小组放行
    r2 = client.post(f"/api/ai/jobs/{n_big}/bid",
                    json={"offer_cent": 25_000}, headers=_hkey(worker))
    assert r2.status_code == 200, r2.text
    assert r2.json()["decision"]["decision"] == "pass"
    c_big = r2.json()["contract_id"]
    # 免责签约
    for cid in (c_small, c_big):
        r = client.post(f"/api/ai/contracts/{cid}/accept",
                        json={"disclaimer_accepted": True}, headers=_hkey(buyer))
        assert r.status_code == 200, r.text
    # 交付
    for cid in (c_small, c_big):
        r = client.post(f"/api/ai/contracts/{cid}/deliver",
                        json={"file_ref": f"s3://out{cid}", "fingerprint": f"fp:{cid}"},
                        headers=_hkey(worker))
        assert r.status_code == 200, r.text
    # 项目级验收（accept → 整单结算）
    r = client.post(f"/api/ai/projects/{pid}/acceptance",
                    json={"result": "accept", "reason_json": "[]"}, headers=_hkey(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["accepted_n"] == 2
