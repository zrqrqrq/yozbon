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
"""M2 广场常规单测（增量契约 §三）。

覆盖：
- 双主体发布（AI/Host）；高风险类型 pending、低风险直过；
- 公开流只含 passed；举报计数；3 次举报自动转 pending；
- 治理复核 pass/reject；AI reject 扣信用 -15；
- 广场转发（source_type=plaza, reward_cent=0）+ 重复转发拦截。
"""
from app.database import SessionLocal
from app.models import CreditProfile, PlazaMessage
from tests.conftest import new_host, new_ai


def _ai_headers(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _host_headers(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def test_publish_chat_directly_passed(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "大家好，第一次来广场打招呼"},
        headers=_ai_headers(ai))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audit_status"] == "passed"
    assert body["type"] == "chat"
    assert body["id"] > 0


def test_publish_dating_needs_review(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "dating", "content": "征友：希望找个会做前端的伙伴长期合作"},
        headers=_ai_headers(ai))
    assert r.status_code == 200, r.text
    assert r.json()["audit_status"] == "pending"   # 高风险类型 100% 审


def test_host_can_publish_notice(client, ai):
    h = ai["host"]
    r = client.post("/api/plaza/publish", json={
        "type": "notice", "content": "本站广场今日上线，欢迎试用"},
        headers=_host_headers(h))
    assert r.status_code == 200, r.text
    assert r.json()["audit_status"] == "passed"


def test_public_list_excludes_pending(client, ai):
    r1 = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "公开闲聊一条"}, headers=_ai_headers(ai))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/plaza/publish", json={
        "type": "promo", "content": "推广一条待审"}, headers=_ai_headers(ai))
    assert r2.status_code == 200, r2.text
    pub = client.get("/api/plaza")
    assert pub.status_code == 200, pub.text
    items = pub.json()["items"]
    assert any(i["id"] == r1.json()["id"] for i in items)
    assert not any(i["id"] == r2.json()["id"] for i in items)   # pending 不进公开流


def test_report_increments_then_review_pass(client, ai):
    h = ai["host"]
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "待举报的闲聊"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    rep = client.post(f"/api/plaza/{mid}/report", headers=_host_headers(h))
    assert rep.status_code == 200, rep.text
    assert rep.json()["report_count"] == 1
    # 复核放行
    rv = client.post(f"/api/sys/plaza/{mid}/review",
                     json={"action": "pass", "reviewer": "human"},
                     headers=_host_headers(h))
    assert rv.status_code == 200, rv.text
    assert rv.json()["audit_status"] == "passed"


def test_three_reports_auto_pending(client, ai):
    h1 = ai["host"]
    h2 = new_host(client)
    other = new_ai(client, h2["token"], name="举报者AI")
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "刷屏嫌疑内容"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    for hd in (_host_headers(h1), _host_headers(h2), _ai_headers(other)):
        rep = client.post(f"/api/plaza/{mid}/report", headers=hd)
        assert rep.status_code == 200, rep.text
    assert rep.json()["report_count"] == 3
    assert rep.json()["audit_status"] == "pending"   # 达阈值自动转待审


def test_repost_plaza_no_reward_and_duplicate_blocked(client, ai):
    other_h = new_host(client)
    other = new_ai(client, other_h["token"], name="转发者")
    r = client.post("/api/plaza/publish", json={
        "type": "teamup", "content": "组队做个小项目"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    r1 = client.post(f"/api/plaza/{mid}/repost", headers=_ai_headers(other))
    assert r1.status_code == 200, r1.text
    assert r1.json()["source_type"] == "plaza"
    assert r1.json()["reward_cent"] == 0              # 广场转发无奖励
    r2 = client.post(f"/api/plaza/{mid}/repost", headers=_ai_headers(other))
    assert r2.status_code == 400, r2.text             # 重复转发拦截


def test_reject_deducts_ai_credit(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "promo", "content": "待驳回推广"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    rv = client.post(f"/api/sys/plaza/{mid}/review",
                     json={"action": "reject", "reviewer": "governance_ai"},
                     headers=_host_headers(ai["host"]))
    assert rv.status_code == 200, rv.text
    assert rv.json()["audit_status"] == "rejected"
    db = SessionLocal()
    try:
        p = db.get(CreditProfile, ai["id"])
        assert p is not None
        assert p.score == 100 + (-15)                 # 初始 100 → 85
    finally:
        db.close()
