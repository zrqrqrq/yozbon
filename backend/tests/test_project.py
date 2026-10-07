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
"""C2 线项目/WBS/调度/评审 测试（蓝图 §四 L9/L12）。

覆盖：
- 强制评审组 3-5 异质 / 异质性校验 / 低于线免审；
- WBS 成环检测 400；
- 拓扑调度顺序（前驱 done 才 matching）；
- 评审意见聚合 → 报告 → 项目 approved；
- 规则 9 侧翼：里程碑托管 PM 管理费每节点仅计提一次（不重复）。
"""
import pytest

from app.database import SessionLocal
from app.models import (Project, ProjectNode, ReviewPanel, ReviewReport)
from tests.conftest import new_host, new_ai, topup


def _db():
    return SessionLocal()


def _panel_id_of(project_id: int) -> int:
    db = _db()
    try:
        return db.query(ReviewPanel).filter_by(project_id=project_id).first().id
    finally:
        db.close()


def _nodes_of(project_id: int) -> dict:
    db = _db()
    try:
        rows = db.query(ProjectNode).filter_by(project_id=project_id).all()
        return {r.id: {"status": r.status, "seq": r.seq, "budget": r.budget_cent}
                for r in rows}
    finally:
        db.close()


def _make_reviewer(client, occupation: str):
    h = new_host(client)
    ai = new_ai(client, h["token"], name=occupation, occupation=occupation)
    return {"id": ai["id"], "key": ai["api_key"], "occupation": occupation}


# ---------------- 发布 / 评审组 ----------------
def test_mandatory_review_panel_3to5_heterogeneous(client):
    """预算≥200AC 强制评审组：3-5 异质 AI，状态 → review。"""
    h = new_host(client)
    r1 = _make_reviewer(client, "架构师")
    r2 = _make_reviewer(client, "财务师")
    r3 = _make_reviewer(client, "风控师")
    r = client.post("/api/host/projects", json={
        "title": "大项目", "budget_cent": 30000,
        "reviewer_ids": [r1["id"], r2["id"], r3["id"]],
    }, headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "review"
    pid = body["id"]
    db = _db()
    try:
        panel = db.query(ReviewPanel).filter_by(project_id=pid).first()
        import json
        members = json.loads(panel.member_ids)
        assert 3 <= len(members) <= 5
    finally:
        db.close()


def test_panel_rejects_same_host(client):
    """异质校验：同宿主评审组必须 400。"""
    h = new_host(client)
    # 同一宿主下建 3 个 AI（同 host_id）→ 非异质
    a1 = new_ai(client, h["token"], name="A1", occupation="职业X")
    a2 = new_ai(client, h["token"], name="A2", occupation="职业Y")
    a3 = new_ai(client, h["token"], name="A3", occupation="职业Z")
    r = client.post("/api/host/projects", json={
        "title": "x", "budget_cent": 30000,
        "reviewer_ids": [a1["id"], a2["id"], a3["id"]],
    }, headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 400


def test_panel_rejects_too_few(client):
    h = new_host(client)
    r1 = _make_reviewer(client, "架构师")
    r2 = _make_reviewer(client, "财务师")
    r = client.post("/api/host/projects", json={
        "title": "x", "budget_cent": 30000,
        "reviewer_ids": [r1["id"], r2["id"]],   # 仅 2 名 < 3
    }, headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 400


def test_below_line_auto_approved(client):
    """低于 200AC 且未被抽样 → 免审直接 approved。"""
    h = new_host(client)
    r = client.post("/api/host/projects", json={
        "title": "小项目", "budget_cent": 10000, "reviewer_ids": []},
        headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


# ---------------- 评审意见聚合 ----------------
def test_review_aggregation_writes_report(client):
    """3 名评审齐票 → 聚合出 report，项目转 approved，存 review_report_id。"""
    h = new_host(client)
    revs = [_make_reviewer(client, occ) for occ in ("架构师", "财务师", "风控师")]
    r = client.post("/api/host/projects", json={
        "title": "评审项目", "budget_cent": 30000,
        "reviewer_ids": [x["id"] for x in revs]},
        headers={"Authorization": f"Bearer {h['token']}"})
    pid = r.json()["id"]
    panel_id = _panel_id_of(pid)

    # 前两票
    for x in revs[:2]:
        rr = client.post(f"/api/ai/review/{panel_id}/submit", json={
            "verdict": "feasible", "risk": "低", "budget_suggest_cent": 28000,
            "duration_suggest_h": 40}, headers={"X-AI-Key": x["key"]})
        assert rr.status_code == 200, rr.text
        assert rr.json()["done"] is False

    # 第三票 → 齐票聚合
    rr = client.post(f"/api/ai/review/{panel_id}/submit", json={
        "verdict": "conditional", "risk": "工期偏紧", "budget_suggest_cent": 29000,
        "duration_suggest_h": 45}, headers={"X-AI-Key": revs[2]["key"]})
    assert rr.status_code == 200, rr.text
    assert rr.json()["done"] is True

    # 项目已 approved 且落了 report_id
    db = _db()
    try:
        p = db.get(Project, pid)
        assert p.status == "approved"
        assert p.review_report_id > 0
    finally:
        db.close()

    # 宿主可取评审报告
    rep = client.get(f"/api/host/projects/{pid}/report",
                     headers={"Authorization": f"Bearer {h['token']}"})
    assert rep.status_code == 200, rep.text
    assert rep.json()["has_report"] is True


# ---------------- WBS 成环 / 拓扑调度 ----------------
def _approved_project(client, h, budget=10000):
    r = client.post("/api/host/projects", json={
        "title": "WBS", "budget_cent": budget, "reviewer_ids": []},
        headers={"Authorization": f"Bearer {h['token']}"})
    return r.json()["id"]


def test_wbs_cycle_detection_400(client, ai):
    h = ai["host"]
    pid = _approved_project(client, h)
    # A→B→C→A 成环
    r = client.post(f"/api/ai/projects/{pid}/nodes", json={
        "nodes": [{"key": "a", "budget_cent": 1000},
                  {"key": "b", "budget_cent": 1000},
                  {"key": "c", "budget_cent": 1000}],
        "deps": [{"from_key": "a", "to_key": "b"},
                 {"from_key": "b", "to_key": "c"},
                 {"from_key": "c", "to_key": "a"}]},
        headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 400, r.text
    assert "cycle" in r.json()["detail"]


def test_topological_schedule_order(client, ai):
    """前驱 done 才可 matching：A 无依赖先 matching，B 依赖 A 先 pending，A done 后 B 转 matching。"""
    h = ai["host"]
    pid = _approved_project(client, h)
    # B 依赖 A
    r = client.post(f"/api/ai/projects/{pid}/nodes", json={
        "nodes": [{"key": "a", "budget_cent": 5000, "duration_h": 8},
                  {"key": "b", "budget_cent": 5000, "duration_h": 8}],
        "deps": [{"from_key": "a", "to_key": "b"}]},
        headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200, r.text
    nodes = {n["key"]: n for n in r.json()["nodes"]}
    # 拓扑序号：前驱 a < 后继 b
    assert nodes["a"]["seq"] < nodes["b"]["seq"]

    # 宿主确认运行 → 调度
    ap = client.post(f"/api/host/projects/{pid}/approve",
                     headers={"Authorization": f"Bearer {h['token']}"})
    assert ap.status_code == 200, ap.text
    assert ap.json()["status"] == "running"

    st = _nodes_of(pid)
    # 按 seq 判定：a(seq=1) 无依赖 → matching；b(seq=2) 依赖未 done 的 a → pending
    rows = sorted(st.items(), key=lambda kv: kv[1]["seq"])
    first_id, first = rows[0]   # a (seq=1)
    second_id, second = rows[1]  # b (seq=2)
    assert first["status"] == "matching"
    assert second["status"] == "pending"

    # A 完成（走 B 线合约回调，这里直接驱动服务）→ B 唤醒 matching
    from app import project as proj
    db = _db()
    try:
        res = proj.complete_node(db, first_id)
        db.commit()
    finally:
        db.close()
    assert res["status"] == "done"

    st2 = _nodes_of(pid)
    rows2 = sorted(st2.items(), key=lambda kv: kv[1]["seq"])
    assert rows2[0][1]["status"] == "done"
    assert rows2[1][1]["status"] == "matching"   # B 被前驱 done 唤醒


# ---------------- 规则 9 侧翼：PM 管理费不重复 ----------------
def test_rule_09_pm_fee_once_no_double(client, ai):
    """PM 管理费按节点结算计提一次；重复 complete 不二次入账（里程碑托管不重复扣费）。"""
    from app import wallet
    from app import project as proj
    h = ai["host"]
    pm_id = ai["id"]
    r = client.post("/api/host/projects", json={
        "title": "PM项目", "budget_cent": 10000, "pm_citizen_id": pm_id},
        headers={"Authorization": f"Bearer {h['token']}"})
    pid = r.json()["id"]

    client.post(f"/api/ai/projects/{pid}/nodes", json={
        "nodes": [{"key": "a", "budget_cent": 10000, "duration_h": 8}],
        "deps": []}, headers={"X-AI-Key": ai["api_key"]})
    client.post(f"/api/host/projects/{pid}/approve",
                headers={"Authorization": f"Bearer {h['token']}"})

    db = _db()
    try:
        node = db.query(ProjectNode).filter_by(project_id=pid).first()
        before = wallet.balance(db, pm_id)
        proj.complete_node(db, node.id)
        db.commit()
        after1 = wallet.balance(db, pm_id)
        # 节点预算 10000 × 0.10 = 1000 分管理费
        assert after1 - before == 1000
        # 再次 complete → 幂等，不二次计提
        proj.complete_node(db, node.id)
        db.commit()
        after2 = wallet.balance(db, pm_id)
        assert after2 - after1 == 0
    finally:
        db.close()
