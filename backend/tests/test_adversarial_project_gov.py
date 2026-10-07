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
"""C2 线 project/feed/governance 对抗攻击测试（独立第三方视角）。

视角号（对齐 docs/边界情形登记册.md §一）：
  视角3 恶意主体 ：评审组非成员投票、同成员重复投票、空报告、仲裁重复 verdict
  视角4 规则冲突 ：评审否决后宿主仍 approve、仲裁中合约被验收（escrow 锁定）
  视角13 侧翼女巫：同宿主同质集群批量转发同一帖 → 相关性 ×0.3 降权（数值断言）
  视角1 新物种  ：未知 governance task type / 未知 post type → 拒绝不崩

已知洞（C-19 空报告不校验 / C-20 评审不防重复投票）用 xfail 标注，
不自己改 C2 代码，只登记+对照报告写明待修建议。
"""
import json

import pytest

from app import escrow, feed, governance, project as proj, wallet
from app.database import SessionLocal
from app.models import (AIWallet, AICitizen, AIPermission, ArbitrationCase,
                        CapabilityProfile, Contract, CreditProfile, Project,
                        ProjectNode, ReviewPanel)
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


def _set_skills(ai_id: int, skills: list):
    db = _db()
    try:
        for s in skills:
            db.add(CapabilityProfile(citizen_id=ai_id, skill=s,
                                     profile_json="{}", declared=1))
        db.commit()
    finally:
        db.close()


def _matching_node(db, buyer_id: int, budget: int = 2000):
    """B 线 C-16：合约须绑定 matching 节点才可签约。返回 (project_id, node_id)。"""
    host_id = db.get(AICitizen, buyer_id).host_id
    proj_row = Project(host_id=host_id, title="对抗项目", budget_cent=budget,
                       status="running")
    db.add(proj_row)
    db.flush()
    node = ProjectNode(project_id=proj_row.id, skill="dev", budget_cent=budget,
                       status="matching", seq=1)
    db.add(node)
    db.flush()
    return proj_row.id, node.id


def _mk_reviewer(client, occ: str):
    h = new_host(client)
    ai = new_ai(client, h["token"], name=occ, occupation=occ)
    return {"id": ai["id"], "key": ai["api_key"], "occ": occ}


def _mandatory_project_with_panel(client, reviewers):
    """发一个预算≥20000 的强制评审项目，返回 (project_id, panel_id)，panel=voting。"""
    h = new_host(client)
    r = client.post("/api/host/projects", json={
        "title": "攻击项目", "budget_cent": 30000,
        "reviewer_ids": [r["id"] for r in reviewers]},
        headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    db = _db()
    try:
        panel = db.query(ReviewPanel).filter_by(project_id=pid).first()
        panel_id = panel.id
    finally:
        db.close()
    return h, pid, panel_id


# =====================================================================
# 视角 3：恶意主体
# =====================================================================
def test_persp3_non_member_review_vote_rejected(client):
    """视角3：非评审组成员对 panel 提交意见 → 拒绝。"""
    revs = [_mk_reviewer(client, o) for o in ("架构师", "财务师", "风控师")]
    _, _, panel_id = _mandatory_project_with_panel(client, revs)
    # 一个与项目无关的 AI（非成员）试图投票
    outsider = new_ai(client, new_host(client)["token"], name="外人", occupation="法务")
    db = _db()
    try:
        with pytest.raises(governance.GovError) as ei:
            governance.submit_review_opinion(db, outsider["id"], panel_id, "feasible")
        assert "not a member" in str(ei.value)
    finally:
        db.close()


def test_persp3_same_member_double_vote_rejected(client):
    """视角3（C-20 已修）：同一评审组成员重复投票 → 拒绝（防一人多票凑齐）。"""
    revs = [_mk_reviewer(client, o) for o in ("架构师", "财务师", "风控师")]
    _, _, panel_id = _mandatory_project_with_panel(client, revs)
    db = _db()
    try:
        governance.submit_review_opinion(db, revs[0]["id"], panel_id, "feasible")
        # 期望：第二次同一成员投票被拒；现状：放行 → xfail
        with pytest.raises(governance.GovError):
            governance.submit_review_opinion(db, revs[0]["id"], panel_id, "infeasible")
    finally:
        db.close()


def test_persp3_empty_task_report_rejected(client, ai):
    """视角3（C-19 已修）：治理任务竞标者提交空结论报告 → 拒绝。"""
    db = _db()
    try:
        t = governance.publish_task(db, "audit", {"scope": "女巫"},
                                   budget_cent=500, deadline=None)
        db.commit()
        # 期望：空结论被拒；现状：放行 → xfail
        with pytest.raises(governance.GovError):
            governance.submit_task_report(db, ai["id"], t.id, conclusion="", evidence={})
    finally:
        db.close()


def test_persp3_arbitration_double_verdict_rejected(client, ai):
    """视角3：仲裁 panel 对同一案重复 verdict → 第二次拒绝（案已 verdict）。"""
    buyer = ai
    wh = new_host(client)
    worker = new_ai(client, wh["token"], name="工人", occupation="开发")
    topup(client, buyer["host"]["token"], buyer["id"], 100000)
    arb = new_ai(client, new_host(client)["token"], name="仲裁", occupation="治理")

    db = _db()
    try:
        # 造真实签约托管案（绑定 matching 节点，对齐 B 线 C-16）
        pid, nid = _matching_node(db, buyer["id"], 2000)
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": 2000}), escrow_cent=2000,
                     project_id=pid, node_id=nid)
        db.add(c); db.flush()
        buyer_obj = db.get(AICitizen, buyer["id"])
        escrow.sign_contract(db, buyer_obj, c.id)
        buyer_obj = db.get(AICitizen, buyer["id"])
        case = escrow.open_dispute(db, buyer_obj, c.id, "delivery", "{}")
        case_id = case.id
        governance.form_arbitration_panel(db, case_id, [arb["id"]])
        db.commit()

        governance.submit_verdict(db, arb["id"], case_id, "support_worker",
                                 ratio=1.0, reason="申诉不成立")
        db.commit()
        # 第二次 verdict → 案已 verdict，拒绝
        with pytest.raises(governance.GovError) as ei:
            governance.submit_verdict(db, arb["id"], case_id, "refund",
                                       ratio=0.5, reason="改判")
        assert "cannot be judged twice" in str(ei.value)
    finally:
        db.close()


# =====================================================================
# 视角 4：规则冲突
# =====================================================================
def test_persp4_rejected_project_host_approve_rejected(client):
    """视角4：项目评审组全票否决（→draft）后，宿主仍 approve 上线 → 拒绝。"""
    revs = [_mk_reviewer(client, o) for o in ("架构师", "财务师", "风控师")]
    h, pid, panel_id = _mandatory_project_with_panel(client, revs)
    # 三名评审一致投不可行 → 项目回 draft
    db = _db()
    try:
        for r in revs:
            governance.submit_review_opinion(db, r["id"], panel_id, "infeasible")
        db.commit()
    finally:
        db.close()
    db = _db()
    try:
        p = db.get(proj.Project, pid)
        hid = p.host_id
        assert p.status == "draft"
    finally:
        db.close()
    # 宿主试图 approve running → 项目不是 approved，拒绝
    with pytest.raises(proj.ProjectError):
        db = _db()
        try:
            proj.approve_running(db, host_id=hid, project_id=pid)
        finally:
            db.close()


def test_persp4_disputed_contract_acceptance_rejected(client, ai):
    """视角4：合约进入仲裁（disputed）后，买方仍走验收 → 拒绝；escrow 仲裁中保持锁定。"""
    buyer = ai
    wh = new_host(client)
    worker = new_ai(client, wh["token"], name="工人", occupation="开发")
    topup(client, buyer["host"]["token"], buyer["id"], 100000)
    db = _db()
    try:
        pid, nid = _matching_node(db, buyer["id"], 2000)
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": 2000}), escrow_cent=2000,
                     project_id=pid, node_id=nid)
        db.add(c); db.flush()
        buyer_obj = db.get(AICitizen, buyer["id"])
        worker_obj = db.get(AICitizen, worker["id"])
        escrow.sign_contract(db, buyer_obj, c.id)
        escrow.deliver(db, worker_obj, c.id, "s3://x", "fp-disp")
        buyer_obj = db.get(AICitizen, buyer["id"])
        escrow.open_dispute(db, buyer_obj, c.id, "quality", "{}")
        db.commit()
        assert db.get(Contract, c.id).status == "disputed"
        # 仲裁中买方试图验收 → 状态非 delivered，拒绝
        with pytest.raises(escrow.EscrowError) as ei:
            escrow.acceptance(db, buyer_obj, c.id, "accept", "[]")
        assert "delivered" in str(ei.value)
        # escrow 仍锁定（仲裁中不释放）
        esc = db.query(escrow.Escrow).filter_by(contract_id=c.id).first()
        assert esc.locked == 1
    finally:
        db.close()


# =====================================================================
# 视角 13：侧翼女巫（同质集群转发刷激励，数值断言）
# =====================================================================
def test_persp13_same_host_cluster_repost_demotion_numeric(client, ai):
    """视角13：同宿主 2 个同质 AI 批量转发同一帖 → 各自相关性 ×0.3 降权（数值断言）。

    owner 技能 {code,design}；同宿主 R1/R2 技能 {code}：
      重叠度 = 1/2 = 0.5；同宿主+同技能 → 命中同质集群 coef = 0.5*0.3 = 0.15
      reward = round(1000*0.15) = 150（对比不同宿主应为 500）。
    """
    owner = ai
    _set_skills(owner["id"], ["code", "design"])
    # 同宿主两个同质转发者（女巫簇）
    r1 = new_ai(client, owner["host"]["token"], name="簇1", occupation="程序员")
    r2 = new_ai(client, owner["host"]["token"], name="簇2", occupation="程序员")
    _set_skills(r1["id"], ["code"])
    _set_skills(r2["id"], ["code"])
    # 不同宿主对照组（不降权）
    h2 = new_host(client)
    r3 = new_ai(client, h2["token"], name="异簇", occupation="程序员")
    _set_skills(r3["id"], ["code"])

    r = client.post("/api/ai/feed/publish", json={
        "type": "tender", "content": "外包", "visibility": "public",
        "reward_cent": 1000}, headers={"X-AI-Key": owner["api_key"]})
    post_id = r.json()["id"]

    rr1 = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                      headers={"X-AI-Key": r1["api_key"]})
    rr2 = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                      headers={"X-AI-Key": r2["api_key"]})
    rr3 = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                      headers={"X-AI-Key": r3["api_key"]})
    assert rr1.status_code == 200, rr1.text
    assert rr2.status_code == 200, rr2.text
    assert rr3.status_code == 200, rr3.text
    # 同宿主同质 → 降权到 150；不同宿主 → 500
    assert rr1.json()["reward_cent"] == 150
    assert rr2.json()["reward_cent"] == 150
    assert rr1.json()["same_cluster"] is True
    assert rr3.json()["reward_cent"] == 500
    assert rr3.json()["same_cluster"] is False


# =====================================================================
# 视角 1：新物种（未知类型拒绝不崩）
# =====================================================================
def test_persp1_unknown_gov_task_type_rejected(client, ai):
    """视角1：未知 governance task type → 拒绝，不崩（不进未知执行体）。"""
    db = _db()
    try:
        with pytest.raises(governance.GovError) as ei:
            governance.publish_task(db, "mind_control", {"x": 1}, budget_cent=100)
        assert "type" in str(ei.value)
    finally:
        db.close()


def test_persp1_unknown_post_type_rejected(client, ai):
    """视角1：未知 post type → 拒绝不崩。"""
    db = _db()
    try:
        with pytest.raises(feed.FeedError) as ei:
            feed.publish_post(db, ai["id"], "deepfake_video", "违规内容")
        assert "type" in str(ei.value)
    finally:
        db.close()
