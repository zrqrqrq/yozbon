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
"""M8 攻击测试（契约 §9.5 清单 1-10 全覆盖；边界登记册 C-31/C-32/C-33/C-34）。

每条用例显式标注攻击视角与断言。中文断言写在本 .py（UTF-8）。
"""
import importlib.util
import json
from pathlib import Path

from app import governance, prompt_service, wallet
from app import project as proj
from app.database import SessionLocal
from app.models import (AICitizen, AuditLog, Contract, IntelReport, Project,
                        ProjectNode, PromptLibrary)
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


def _load_seed():
    p = Path(__file__).resolve().parent.parent / "tools" / "seed_prompts.py"
    spec = importlib.util.spec_from_file_location("seed_prompts", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_all():
    mod = _load_seed()
    db = _db()
    mod.seed(db)
    db.close()


def _new_buyer_worker(client, worker_topup=0):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="总包AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="执行AI", occupation="文案")
    topup(client, host["token"], buyer["id"], 200_000)
    if worker_topup:
        topup(client, host["token"], worker["id"], worker_topup)
    _activate(buyer["id"])
    return host, buyer, worker


def _one_node(client, buyer, *, node_budget, spec, deliverable_std, duration_h=24,
              project_budget=19_000):
    r = client.post("/api/ai/projects",
                    json={"title": "攻击任务", "budget_cent": project_budget},
                    headers=_hkey(buyer))
    pid = r.json()["id"]
    client.post(f"/api/ai/projects/{pid}/approve", headers=_hkey(buyer))
    db = _db()
    proj.submit_nodes(db, pid,
                      [{"key": "n1", "skill": "文案", "spec": spec,
                        "deliverable_std": deliverable_std,
                        "budget_cent": node_budget, "duration_h": duration_h}],
                      deps=[])
    db.commit()
    node = db.query(ProjectNode).filter_by(project_id=pid).first()
    nid = node.id
    db.close()
    return pid, nid


# ---------------------------------------------------------------------------
# 攻击1：T1 缺核心要素发包 → 400（显式 requirements 不完整 = 老格式推导失败场景）
# ---------------------------------------------------------------------------
def test_a1_t1_missing_core_400(client):
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="发包AI", occupation="项目管理")
    topup(client, host["token"], buyer["id"], 200_000)
    _activate(buyer["id"])
    # 显式 requirements 只给 goal，缺 scope/deliverable_std/deadline → 400
    r = client.post("/api/ai/projects",
                    json={"title": "文案", "budget_cent": 5000,
                          "requirements": {"goal": "写文案"}},
                    headers=_hkey(buyer))
    assert r.status_code == 400
    assert "T1 seven elements" in r.json()["detail"]
    # 老格式（无 requirements）不被误伤 → 200
    r2 = client.post("/api/ai/projects",
                     json={"title": "文案", "budget_cent": 5000},
                     headers=_hkey(buyer))
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 攻击2：T2 success_rate<50 投标 → 400（自评拒单硬拦）
# ---------------------------------------------------------------------------
def test_a2_t2_low_success_rate_400(client):
    host, buyer, worker = _new_buyer_worker(client)
    _, nid = _one_node(client, buyer, node_budget=12_000, spec="短",
                       deliverable_std="docx")
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000,
                          "self_assess": {"success_rate_pct": 49, "policy_ok": True}},
                    headers=_hkey(worker))
    assert r.status_code == 400
    assert "success rate" in r.json()["detail"]
    # 未创建任何 proposed 要约
    db = _db()
    n = db.query(Contract).filter_by(node_id=nid, status="proposed").count()
    db.close()
    assert n == 0


# ---------------------------------------------------------------------------
# 攻击3：大额节点决策小组评分<50 → 400（资源不足 bottom AI 投 30000 节点）
# ---------------------------------------------------------------------------
def test_a3_big_order_underresourced_reject(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=0)  # 无余额
    _, nid = _one_node(client, buyer, node_budget=30_000, spec="字" * 900,
                       deliverable_std="v1验收")
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 30_000}, headers=_hkey(worker))
    assert r.status_code == 400
    assert "Decision panel" in r.json()["detail"]
    # 被拒不得留下 proposed 要约
    db = _db()
    n = db.query(Contract).filter_by(node_id=nid, status="proposed").count()
    db.close()
    assert n == 0


# ---------------------------------------------------------------------------
# 攻击4：决策小组打回（node deliverable_std 空）→ 400
# ---------------------------------------------------------------------------
def test_a4_request_clarification(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=200_000)
    _, nid = _one_node(client, buyer, node_budget=25_000, spec="短",
                       deliverable_std="")
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 25_000}, headers=_hkey(worker))
    assert r.status_code == 400
    assert "sent back" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 攻击5：免责未确认签约 → 400；确认后成功且 audit 留痕
# ---------------------------------------------------------------------------
def test_a5_disclaimer_required_and_audit(client):
    _seed_all()
    host, buyer, worker = _new_buyer_worker(client)
    _, nid = _one_node(client, buyer, node_budget=12_000, spec="短",
                       deliverable_std="docx")
    cid = client.post(f"/api/ai/jobs/{nid}/bid",
                     json={"offer_cent": 12_000}, headers=_hkey(worker)).json()["contract_id"]
    # 未确认 → 400
    r = client.post(f"/api/ai/contracts/{cid}/accept", headers=_hkey(buyer))
    assert r.status_code == 400 and "disclaimer" in r.json()["detail"]
    # 确认 → 200
    r = client.post(f"/api/ai/contracts/{cid}/accept",
                    json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    assert r.status_code == 200
    db = _db()
    al = db.query(AuditLog).filter_by(action="disclaimer.sign").first()
    assert al is not None
    db.close()


# ---------------------------------------------------------------------------
# 攻击6：prompt_library 同 module/role/version 重复创建 → 409
# ---------------------------------------------------------------------------
def test_a6_duplicate_prompt_409(client):
    db = _db()
    prompt_service.create_prompt(db, "mod_z", "role_z", "v1", "x", "on_enter")
    db.commit()
    try:
        prompt_service.create_prompt(db, "mod_z", "role_z", "v1", "x2", "on_enter")
        raise AssertionError("应抛 409")
    except prompt_service.PromptError:
        pass
    db.close()


# ---------------------------------------------------------------------------
# 攻击7：版本隔离——归档 v1 后新建 v2，签约留痕按当前 active(v2) 版本
# ---------------------------------------------------------------------------
def test_a7_version_isolation_on_sign(client):
    _seed_all()
    host, buyer, worker = _new_buyer_worker(client)
    _, nid = _one_node(client, buyer, node_budget=12_000, spec="短",
                       deliverable_std="docx")
    cid = client.post(f"/api/ai/jobs/{nid}/bid",
                     json={"offer_cent": 12_000}, headers=_hkey(worker)).json()["contract_id"]
    # 归档 v1，新建 v2（t1_buyer）
    db = _db()
    v1 = prompt_service.get_prompt(db, "task_publish", "t1_buyer")
    v1.status = "archived"
    db.flush()
    prompt_service.create_prompt(db, "task_publish", "t1_buyer", "v2", "新版免责",
                                "on_enter")
    db.commit()
    db.close()
    # 签约留痕应记 v2（不追溯到 archived v1）
    client.post(f"/api/ai/contracts/{cid}/accept",
                json={"disclaimer_accepted": True}, headers=_hkey(buyer))
    db = _db()
    al = db.query(AuditLog).filter_by(action="disclaimer.sign").order_by(AuditLog.id.desc()).first()
    ver = json.loads(al.detail)["prompt_version"]
    db.close()
    assert ver == "v2"


# ---------------------------------------------------------------------------
# 攻击8：C-34 platform_intel collected → 自动入库；重复报告不重复入
# ---------------------------------------------------------------------------
def test_a8_c34_collect_idempotent(client):
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
    assert r.status_code == 200
    db = _db()
    n1 = db.query(IntelReport).count()
    db.close()
    assert n1 >= 1
    # 再次 collected 报告 → 情报去重，不重复入库
    r = client.post(f"/api/ai/gov/tasks/{tid}/report",
                    json={"conclusion": "collected", "evidence": {"note": "x2"}},
                    headers=_hkey(op))
    assert r.status_code == 200
    db = _db()
    n2 = db.query(IntelReport).count()
    db.close()
    assert n2 == n1


# ---------------------------------------------------------------------------
# 攻击9：T4 仲裁响应含 prompt_version
# ---------------------------------------------------------------------------
def test_a9_t4_verdict_prompt_version(client):
    _seed_all()
    host = new_host(client)
    buyer = new_ai(client, host["token"], name="买方AI", occupation="项目管理")
    worker = new_ai(client, host["token"], name="工人AI", occupation="文案")
    arb = new_ai(client, host["token"], name="仲裁员", occupation="治理")
    topup(client, host["token"], buyer["id"], 100_000)
    from app import escrow
    P = 2000
    db = _db()
    host_id = db.get(AICitizen, buyer["id"]).host_id
    proj_row = Project(host_id=host_id, title="仲裁项目", budget_cent=P,
                       status="running", pm_citizen_id=buyer["id"])
    db.add(proj_row); db.flush()
    node = ProjectNode(project_id=proj_row.id, skill="dev", budget_cent=P,
                       deliverable_std="v1", status="matching", seq=1)
    db.add(node); db.flush()
    c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                 terms_json='{"offer_cent": %d}' % P, escrow_cent=P,
                 project_id=proj_row.id, node_id=node.id)
    db.add(c); db.flush()
    escrow.sign_contract(db, db.get(AICitizen, buyer["id"]), c.id)
    case = escrow.open_dispute(db, db.get(AICitizen, buyer["id"]), c.id, type_="delivery")
    governance.form_arbitration_panel(db, case.id, [arb["id"]])
    db.commit(); db.close()
    r = client.post(f"/api/ai/arbitration/{case.id}/verdict",
                    json={"decision": "support_worker", "ratio": 1.0, "reason": "x"},
                    headers=_hkey(arb))
    assert r.status_code == 200
    assert r.json()["prompt_version"] == "v1"


# ---------------------------------------------------------------------------
# 攻击10：小单投标不触发决策小组（不误伤，正常投标成功）
# ---------------------------------------------------------------------------
def test_a10_small_order_no_panel_no_false_reject(client):
    host, buyer, worker = _new_buyer_worker(client, worker_topup=0)  # 无余额小工
    _, nid = _one_node(client, buyer, node_budget=12_000, spec="短",
                       deliverable_std="docx")
    # 无余额小工投小单：不触发决策小组 → 不因资源不足被拒
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000}, headers=_hkey(worker))
    assert r.status_code == 200, r.text
    assert "decision" not in r.json()


# ---------------------------------------------------------------------------
# 附加攻击：policy_ok=false 拒接豁免 → 400，且不算违约（不写信用负事件）
# ---------------------------------------------------------------------------
def test_a11_policy_ok_false_exempt_no_penalty(client):
    host, buyer, worker = _new_buyer_worker(client)
    _, nid = _one_node(client, buyer, node_budget=12_000, spec="短",
                       deliverable_std="docx")
    r = client.post(f"/api/ai/jobs/{nid}/bid",
                    json={"offer_cent": 12_000,
                          "self_assess": {"success_rate_pct": 90, "policy_ok": False}},
                    headers=_hkey(worker))
    assert r.status_code == 400
    db = _db()
    from app.models import CreditEvent
    n = db.query(CreditEvent).filter_by(citizen_id=worker["id"]).count()
    db.close()
    assert n == 0   # 合规拒接不产生信用处罚
