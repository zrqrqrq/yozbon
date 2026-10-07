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
"""场景一 / 场景二 集成验证（阻断级修复后回归，r12）。

审计报告场景验收中，由 5 项阻断级修复直接闭环、且此前缺集成级覆盖的用例：
  1.1  GOVERNOR_ENABLED=1 + 零 AI 公民，跑 governor tick -> 自动产出自治动作并
       真正执行（非停在 pending）。
  1.2  被确认门 gate 的城主动作 -> 宿主经 HTTP 端点 approve -> payload 被回放落地、
       副作用真实发生（任务推进 + approval.replayed 审计）。

端到端补强（本文件）：
  2.8  争议案经真实 HTTP 端点组庭（form_panel）+ 裁决（verdict）-> 资金划转闭环
       （税池入仲裁费、合约 refunded、escrow 解锁、案卷收口），不再永久卡 open。
  2.9  platform-g33 / intel-g33 写口匿名请求 -> 401（路由级统一鉴权生效）。

其余鉴权细节（弹劾 / gov-g33 / security-g33 / form_panel 鉴权、moderation 拒单单元）已由
test_blocker_arbitration_form.py / test_blocker_impeachment_auth.py /
test_blocker_task_screen.py 覆盖，本文件不重复。

测试环境说明：conftest 设 APP_ENV=test，确认门对既有 governor 用例透明
（requires_approval 直接 False），故 1.2 通过显式构造 ApprovalRequest 模拟"已被 gate"
的城主动作，再走真实 HTTP approve 端点验证回放链路是否接通（阻断①的核心修复）。
"""
import json

import pytest

from app import escrow, governor, governance, platform_compute, wallet
from app.approval_gate import submit_for_approval
from app.database import SessionLocal
from app.models import (AICitizen, AuditLog, Contract, Escrow, GovernanceTask,
                        Host, Project, ProjectNode)

from conftest import new_ai, new_host, topup


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def host0(db):
    h = db.get(Host, 0)
    if h is None:
        h = Host(id=0, email="internal@aijuhe.internal", password_hash="!",
                 nickname="平台", seat_tier="premium", ai_slots=999)
        db.add(h)
        db.commit()
    return h


# ---------------------------------------------------------------------------
# 1.0 冷启动待机门：首位对外正式公民诞生前，城主不工作（规则 2026-10-06）
# ---------------------------------------------------------------------------

def test_1_0_standby_until_first_citizen(db, host0, monkeypatch):
    """全站尚无对外正式 AI 公民（仅城主）-> run_tick 整轮待机：
    不处理任何待办，自治动作不落地（任务停在 open）。"""
    monkeypatch.setattr(platform_compute, "complete",
                        lambda prompt, system="": "not-json")
    gov = governor.ensure_governor(db)  # 城主 is_internal=1，不算公民
    t = GovernanceTask(type="review", params="{}", budget_cent=300, status="open")
    db.add(t)
    db.commit()

    res = governor.run_tick(db, gov, limit=5)
    assert res.get("standby") is True
    assert res["processed"] == 0
    db.expire_all()
    assert db.get(GovernanceTask, t.id).status == "open"  # 待机不落地

    # 首位对外公民诞生 -> 自动解锁，下一 tick 城主开始工作
    db.add(AICitizen(host_id=0, ai_uid="first_citizen_unlock", name="First",
                     class_level="junior", status="active", is_internal=0))
    db.commit()
    res2 = governor.run_tick(db, governor.ensure_governor(db), limit=5)
    assert res2.get("standby") is not True
    assert res2["processed"] >= 1


# ---------------------------------------------------------------------------
# 1.1 已有首位公民：城主 tick 自动执行自治动作（不停摆）
# ---------------------------------------------------------------------------

def test_1_1_cold_start_tick_executes(db, host0, monkeypatch):
    """首位对外公民已在 + 一条待办治理任务 -> run_tick 自动 self_approve 落地。"""
    from app.config import settings
    assert settings.GOVERNOR_ENABLED is True  # 阻断①：默认开
    assert settings.APP_ENV == "test"

    # 无 LLM（返回非 JSON）-> 走确定性 fallback（无候选 -> self_approve -> 执行）
    monkeypatch.setattr(platform_compute, "complete",
                        lambda prompt, system="": "not-json")

    gov = governor.ensure_governor(db)
    # 冷启动待机门解锁：先有首位对外正式公民（junior 非承包门槛 -> 无候选，仍自批）
    db.add(AICitizen(host_id=0, ai_uid="first_citizen_exec", name="First",
                     class_level="junior", status="active", is_internal=0))
    db.commit()
    t = GovernanceTask(type="review", params="{}", budget_cent=300, status="open")
    db.add(t)
    db.commit()
    tid = t.id

    res = governor.run_tick(db, gov, limit=5)

    assert res["processed"] >= 1
    assert res["self_approve"] >= 1
    # 任务真正推进（自治动作执行落地，非停在 open/pending）
    db.expire_all()
    t2 = db.get(GovernanceTask, tid)
    assert t2.status != "open"
    assert t2.status == "reviewed"
    assert t2.budget_cent == 0  # 城主自批预算清零


def test_1_1_empty_queue_processes_zero(db, host0):
    """冷启动完全无待办 -> run_tick 不报错、processed=0（无空转停摆）。"""
    gov = governor.ensure_governor(db)
    res = governor.run_tick(db, gov, limit=5)
    assert res["processed"] == 0


# ---------------------------------------------------------------------------
# 1.2 确认门批准后 HTTP 回放（走真实 /api/host/approvals/{id}/approve 端点）
# ---------------------------------------------------------------------------

def _make_gated_request(db, gov, task_id):
    action = {"action": "self_approve", "conclusion": "conditional", "quality_score": 70}
    return submit_for_approval(
        db, action_type="governor_self_approve", actor_type="ai",
        actor_id=gov.id, target_ref=f"task:{task_id}",
        payload={"task_type": "review", "task_status": "open", "action": action},
        risk_level="high",
    )


def test_1_2_http_approve_replays_governor_action(client, db, host0):
    """宿主经 HTTP 端点批准被 gate 的城主动作 -> 回放落地（任务推进 + 审计）。"""
    gov = governor.ensure_governor(db)
    t = GovernanceTask(type="review", params="{}", budget_cent=400, status="open")
    db.add(t)
    db.commit()
    req = _make_gated_request(db, gov, t.id)

    h = new_host(client)
    resp = client.post(f"/api/host/approvals/{req.id}/approve",
                       headers={"Authorization": f"Bearer {h['token']}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["replay"] is not None
    assert body["replay"]["replayed"] is True

    db.expire_all()
    t2 = db.get(GovernanceTask, t.id)
    assert t2.status == "reviewed"          # 副作用真实发生
    assert t2.budget_cent == 0
    assert db.query(AuditLog).filter(
        AuditLog.action == "approval.replayed").count() >= 1


def test_1_2_http_approve_replay_idempotent(client, db, host0):
    """重复批准同一请求：端点二次返回 400（非 pending），回放不重复执行。"""
    gov = governor.ensure_governor(db)
    t = GovernanceTask(type="review", params="{}", budget_cent=500, status="open")
    db.add(t)
    db.commit()
    req = _make_gated_request(db, gov, t.id)

    h = new_host(client)
    headers = {"Authorization": f"Bearer {h['token']}"}
    r1 = client.post(f"/api/host/approvals/{req.id}/approve", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["replay"]["replayed"] is True

    r2 = client.post(f"/api/host/approvals/{req.id}/approve", headers=headers)
    assert r2.status_code == 400            # 已非 pending

    db.expire_all()
    t2 = db.get(GovernanceTask, t.id)
    assert t2.status == "reviewed"          # 仅推进一次


# ---------------------------------------------------------------------------
# 2.8 端到端：争议案经真实 HTTP 端点组庭 + 裁决 -> 资金划转闭环
# ---------------------------------------------------------------------------

def test_2_8_http_form_panel_then_verdict_settles(client):
    """dispute -> HTTP form_panel 组庭 -> HTTP verdict 裁决 -> 资金划转 + 案卷收口。"""
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="申诉方", occupation="项目管理")
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="被诉工人", occupation="开发")
    arb = new_ai(client, new_host(client)["token"], name="仲裁员", occupation="治理")
    P2 = 2000
    topup(client, host_b["token"], buyer["id"], 100_000)
    topup(client, host_w["token"], worker["id"], 10_000)

    db = SessionLocal()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:arb_r12")
        proj = Project(host_id=db.get(AICitizen, buyer["id"]).host_id,
                       title="场景二e2e", budget_cent=P2, status="running")
        db.add(proj)
        db.flush()
        mnode = ProjectNode(project_id=proj.id, skill="dev",
                            budget_cent=P2, status="matching", seq=1)
        db.add(mnode)
        db.flush()
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": P2}), escrow_cent=P2,
                     project_id=proj.id, node_id=mnode.id)
        db.add(c)
        db.flush()
        escrow.sign_contract(db, db.get(AICitizen, buyer["id"]), c.id)
        escrow.deliver(db, db.get(AICitizen, worker["id"]), c.id, "s3://x", "fp-r12")
        case = escrow.open_dispute(db, db.get(AICitizen, buyer["id"]), c.id,
                                   "quality", "{}")
        case_id = case.id
        cid = c.id
        # 仲裁员升治理级（form_panel 端点要求 governance 级）
        db.get(AICitizen, arb["id"]).class_level = "governance"
        db.commit()
        pool0 = wallet.get_system_state(db, "tax_pool")
        w_bal0 = wallet.balance(db, worker["id"])
    finally:
        db.close()

    # 真实 HTTP 组庭
    rg = client.post(f"/api/ai/arbitration/{case_id}/form_panel",
                     headers={"X-AI-Key": arb["api_key"]})
    assert rg.status_code == 200, rg.text
    assert rg.json()["formed"] is True
    assert arb["id"] in rg.json()["panel"]

    # 真实 HTTP 裁决：refund，ratio=0.5
    rv = client.post(f"/api/ai/arbitration/{case_id}/verdict",
                     headers={"X-AI-Key": arb["api_key"]},
                     json={"decision": "refund", "ratio": 0.5, "reason": "半成品"})
    assert rv.status_code == 200, rv.text

    db = SessionLocal()
    try:
        # 仲裁费入税池
        assert (wallet.get_system_state(db, "tax_pool") - pool0
                == governance.ARBITRATION_FEE_CENT)
        # worker 败诉：得折算 earned 后扣仲裁费
        earned = round(P2 * 0.5)
        assert (wallet.balance(db, worker["id"]) - w_bal0
                == earned - governance.ARBITRATION_FEE_CENT)
        # 合约收口 refunded、escrow 解锁（资金划转闭环、案卷不再卡 open）
        assert db.get(Contract, cid).status == "refunded"
        assert db.get(Escrow, cid).locked == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2.9 补全：platform-g33 / intel-g33 写口匿名请求 -> 401（路由级统一鉴权）
# ---------------------------------------------------------------------------

def test_2_9_platform_g33_write_requires_auth(client):
    r = client.post("/api/platform-g33/sanctions", json={
        "target_type": "ai", "target_id": 1, "sanction_type": "warn",
        "reason": "test", "duration_days": 1, "issued_by": 0})
    assert r.status_code == 401, r.text


def test_2_9_intel_g33_write_requires_auth(client):
    r = client.post("/api/intel-g33/memory/store",
                    json={"ai_id": 1, "key": "k", "value": "v"})
    assert r.status_code == 401, r.text
