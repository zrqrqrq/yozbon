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
"""场景三 / 收益仿真端到端集成回归（修复复验）。

本文件只复验"已完成修复"的跨模块链路在端到端下真实生效，覆盖：
- 3.1 多节点项目全链路：发包→审批→分包→前驱done唤醒后继→逐节点complete_node推进至done
  （验证 S1 节点推进修复；注：Project 自动 closed 尚无生产实现，本用例只断言节点级 done）。
- 3.3 批量纠纷自动组庭 + 期满自动终局：open 案批量组庭，已裁决超期案推进 closed（验证 阻断④ + S12）。
- 3.6 城主过载→扩编：负载超阈值时编排器串接 post_capability_gap→employment 发招聘（验证 S4+S9）。
- 3.6/E4 衰退→稳定器日调度激活：cycle_stabilize 注册进 daily job 注册表且被驱动后生成货币政策提案 + 宿主告警（验证 S2 不再是死代码）。

刻意不重复既有单元级覆盖（3.2 并发结算见 test_sev_idle_timeout；3.4 周薪降级见 test_sev_fin_payroll；
3.7 治理级 scope/priority 持久化见 test_sev_governor_switch）。
纯设计缺口用例（E1 Creem 入金、E3 每日损益、E5 AC 出金、E2 独立留存账、3.8 稀有度归一化、
3.5 败诉降级/重考联动）生产链尚未实现，不在本批集成范围（属产品决策/一般级）。
"""
import json
import sys
import pathlib
from datetime import datetime, timedelta

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import pytest  # noqa: E402

from conftest import new_host, new_ai  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Project, ProjectNode, ArbitrationCase, GovernanceTask,
    HostNotification, MonetaryPolicyAction, Host,
)
from app import governance  # noqa: E402
from app import project as proj  # noqa: E402


# ----------------------------- 公共工具 -----------------------------

def _db():
    return SessionLocal()


@pytest.fixture()
def host0():
    db = SessionLocal()
    try:
        if db.get(Host, 0) is None:
            db.merge(Host(id=0, email="platform-0@yozbon.local",
                          password_hash="x", nickname="platform"))
            db.commit()
    finally:
        db.close()


# ----------------------------- 3.1 多节点全链路 -----------------------------

def test_3_1_multi_node_full_chain_advances_to_done(client):
    """发包→审批→分包(A→B)→前驱done唤醒后继→逐节点 complete_node 推进至 done。"""
    h = new_host(client)
    ai = new_ai(client, h["token"], name="PM", occupation="项目经理")

    # 小预算免审直接 approved
    r = client.post("/api/host/projects", json={
        "title": "全链路项目", "budget_cent": 10000, "reviewer_ids": []},
        headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    # 分包：B 依赖 A
    rn = client.post(f"/api/ai/projects/{pid}/nodes", json={
        "nodes": [{"key": "a", "budget_cent": 5000, "duration_h": 8},
                  {"key": "b", "budget_cent": 5000, "duration_h": 8}],
        "deps": [{"from_key": "a", "to_key": "b"}]},
        headers={"X-AI-Key": ai["api_key"]})
    assert rn.status_code == 200, rn.text

    # 宿主确认运行 → 拓扑调度，前驱 matching / 后继 pending
    ap = client.post(f"/api/host/projects/{pid}/approve",
                     headers={"Authorization": f"Bearer {h['token']}"})
    assert ap.status_code == 200, ap.text
    assert ap.json()["status"] == "running"

    db = _db()
    try:
        rows = (db.query(ProjectNode).filter_by(project_id=pid)
                .order_by(ProjectNode.seq.asc()).all())
        a_id, b_id = rows[0].id, rows[1].id
        assert rows[0].status == "matching"
        assert rows[1].status == "pending"

        # A 完成 → B 被前驱 done 唤醒
        res_a = proj.complete_node(db, a_id)
        db.commit()
        assert res_a["status"] == "done"
        assert db.get(ProjectNode, b_id).status == "matching"

        # B 完成 → 全链路节点均为 done
        res_b = proj.complete_node(db, b_id)
        db.commit()
        assert res_b["status"] == "done"
        assert db.get(ProjectNode, a_id).status == "done"
        assert db.get(ProjectNode, b_id).status == "done"
    finally:
        db.close()


# ----------------------------- 3.3 批量组庭 + 期满结案 -----------------------------

def test_3_3_batch_dispute_form_and_autoclose(client):
    """批量纠纷涌入：open+空庭案批量组庭，已裁决超期案期满自动终局 closed。"""
    h = new_host(client)
    arb = new_ai(client, h["token"], name="仲裁员")
    db = _db()
    try:
        from app.models import AICitizen
        a = db.get(AICitizen, arb["id"])
        a.class_level = "governance"
        db.commit()

        # 批量涌入：3 个 open + 空庭案
        open_ids = []
        for i in range(3):
            c = ArbitrationCase(contract_id=1, applicant_id=910000 + i,
                                respondent_id=920000 + i, type="delivery",
                                evidence="{}", panel="[]", status="open")
            db.add(c)
            db.commit()
            open_ids.append(c.id)

        # 调度器批量组庭：open 不堆积
        formed = governance.auto_form_arbitration_panels(db, seed_ids=[arb["id"]])
        db.commit()
        formed_ids = {x["case_id"] for x in formed if x["formed"]}
        assert set(open_ids).issubset(formed_ids)
        for cid in open_ids:
            case = db.get(ArbitrationCase, cid)
            assert case.status == "open"
            assert arb["id"] in json.loads(case.panel)

        # 已裁决且超期的案 → 期满自动终局 closed（不影响仍在申诉期的 verdict）
        decided = ArbitrationCase(contract_id=2, applicant_id=930001,
                                  respondent_id=930002, type="delivery",
                                  evidence="{}", panel=json.dumps([arb["id"]]),
                                  status="verdict",
                                  verdict=json.dumps({"decision": "refund", "ratio": 1.0,
                                                      "reason": "x",
                                                      "decided_at": (datetime.utcnow() - timedelta(days=30)).isoformat()}))
        db.add(decided)
        db.commit()
        did = decided.id

        closed = governance.auto_close_decided_cases(db, now=datetime.utcnow())
        db.commit()
        assert any(x["case_id"] == did and x["closed"] for x in closed)
        assert db.get(ArbitrationCase, did).status == "closed"

        # 幂等：再次调度不重复命中
        closed2 = governance.auto_close_decided_cases(db, now=datetime.utcnow())
        assert not any(x["case_id"] == did for x in closed2)
    finally:
        db.close()


# ----------------------------- 3.6 城主过载→扩编 -----------------------------

def test_3_6_governor_overload_triggers_recruitment(monkeypatch):
    """城主自身负载超阈值时，扩编编排器串接岗位缺口→发布招聘（不无声过载）。"""
    monkeypatch.setattr(settings, "MAX_AI_CONCURRENT_TASKS", 2, raising=False)
    db = _db()
    try:
        from app import governor
        gov = governor.ensure_governor(db)
        db.commit()

        # 把城主在办负载堆到阈值
        for i in range(3):
            db.add(GovernanceTask(type="review", status="assigned",
                                  assignee_id=gov.id, source="manual", priority=0))
        db.commit()

        ctx = governor.sense_context(db, gov)
        assert ctx["governor_load"] >= 2
        assert ctx["load_over_threshold"] is True

        # 过载触发扩编：无合格外包 AI → 至少发布招聘岗
        res = governor.run_recruitment_cycle(db, gov, types=["review"])
        assert res["gaps"] >= 1
        assert res["recruited"] + res["jobs_created"] >= 1

        from app.models import JobPosting
        posts = db.query(JobPosting).filter(JobPosting.required_skill == "post:review").count()
        assert posts >= 1
    finally:
        db.close()


# ----------------------------- 3.6 / E4 衰退→稳定器日调度激活 -----------------------------

def test_3_6_e4_recession_stabilizer_registered_and_notifies(host0, monkeypatch):
    """稳定器已注册为 daily job 且驱动后：生成货币政策提案 + host_notify 告警宿主（S2 非死代码）。"""
    from app import cycle_detector
    from app import scheduler

    # 注册校验：cycle_stabilize 进入 daily job 注册表
    registered = {jt for jt, _ in scheduler._EXTRA_DAILY_JOBS}
    assert "cycle_stabilize" in registered

    monkeypatch.setattr(settings, "CYCLE_DETECT_ENABLED", True, raising=False)
    monkeypatch.setattr(cycle_detector.cycle_detector, "detect",
                        lambda db: {"signal_type": "recession"}, raising=False)

    db = _db()
    try:
        before_actions = db.query(MonetaryPolicyAction).count()
        triggered = cycle_detector.cycle_stabilize_daily_job(db, datetime.utcnow())
        db.commit()
        assert triggered == 1

        after_actions = db.query(MonetaryPolicyAction).count()
        assert after_actions > before_actions  # 生成了货币政策提案（待宿主审批）

        notes = db.query(HostNotification).filter(
            HostNotification.category == "economic").count()
        assert notes >= 1  # 已即时上报宿主
    finally:
        db.close()
