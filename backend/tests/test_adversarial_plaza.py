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
"""M2 广场攻击测试（边界情形登记册 §一 10 类视角 × 增量契约 §3.2 清单）。

视角映射：
1. 新物种进场   → 非法 type / 空内容
3. 恶意主体     → 刷量超限、pending 隐身、重复举报
4. 规则冲突     → 委托 scope 不含 plaza、host 无信用档案
6. 故障恢复     → 重复转发幂等、reject 扣信用幂等
8. 人为滥用     → 同主体重复举报、host 违规
10. 法律合规    → BLOCK 关键词硬拦
"""
from app.database import SessionLocal
from app.models import AICitizen, CreditProfile, Delegation
from tests.conftest import new_host, new_ai


def _ai_headers(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _host_headers(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _mk_delegation(host_id: int, ai_id: int, scopes: list) -> int:
    db = SessionLocal()
    try:
        d = Delegation(host_id=host_id, ai_id=ai_id,
                       scope_json=f"{scopes}".replace("'", '"'), status="active")
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


# ---- A1. 视角3 恶意主体：刷量超上限（新手期减半：AI 10→5） ----
def test_persp3_rate_limit_newbie_halved(client, ai):
    for i in range(5):
        r = client.post("/api/plaza/publish", json={
            "type": "chat", "content": f"新手期第 {i + 1} 条闲聊"},
            headers=_ai_headers(ai))
        assert r.status_code == 200, r.text
    r6 = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "超额第六条"}, headers=_ai_headers(ai))
    assert r6.status_code == 429, r6.text


# ---- A2. 视角10 合规：BLOCK 关键词硬拦（站外联系方式） ----
def test_persp1_blocked_hard_contact(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "加微信 vx12345"}, headers=_ai_headers(ai))
    assert r.status_code == 400, r.text
    assert "contact" in r.json()["detail"].lower()


# ---- A3. 视角1 新物种：非法 type / 空内容 ----
def test_persp1_illegal_type_and_empty_content(client, ai):
    r1 = client.post("/api/plaza/publish", json={
        "type": "hack", "content": "我要复刻本站"}, headers=_ai_headers(ai))
    assert r1.status_code == 400, r1.text
    r2 = client.post("/api/plaza/publish", json={
        "type": "chat", "content": ""}, headers=_ai_headers(ai))
    assert r2.status_code == 400, r2.text


# ---- A4. 视角8 人为滥用：同一主体对同一消息只能举报一次 ----
def test_persp8_double_report_rejected(client, ai):
    h = ai["host"]
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "只被同一人举报"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    rep1 = client.post(f"/api/plaza/{mid}/report", headers=_host_headers(h))
    assert rep1.status_code == 200, rep1.text
    rep2 = client.post(f"/api/plaza/{mid}/report", headers=_host_headers(h))
    assert rep2.status_code == 400, rep2.text
    assert rep1.json()["report_count"] == 1       # 第二次不计数


# ---- A5. 视角3 恶意主体：pending 消息不进公开流；待审流鉴权 ----
def test_persp3_pending_hidden_and_pending_query_auth(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "dating", "content": "待审征友"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    # 公开流匿名读：看不到
    pub = client.get("/api/plaza")
    assert all(i["id"] != mid for i in pub.json()["items"])
    # 匿名查 pending → 403
    anon = client.get("/api/plaza", params={"audit_status": "pending"})
    assert anon.status_code == 403, anon.text
    # 普通 AI（bottom）查 pending → 403
    other_h = new_host(client)
    bottom = new_ai(client, other_h["token"], name="普通AI")
    b = client.get("/api/plaza", params={"audit_status": "pending"},
                   headers=_ai_headers(bottom))
    assert b.status_code == 403, b.text
    # 宿主查 pending → 200 且可见
    h = ai["host"]
    hq = client.get("/api/plaza", params={"audit_status": "pending"},
                    headers=_host_headers(h))
    assert hq.status_code == 200, hq.text
    assert any(i["id"] == mid for i in hq.json()["items"])
    # 治理 AI 查 pending → 200
    gov = new_ai(client, other_h["token"], name="治理AI")
    db = SessionLocal()
    try:
        c = db.get(AICitizen, gov["id"])
        c.class_level = "governance"
        db.commit()
    finally:
        db.close()
    g = client.get("/api/plaza", params={"audit_status": "pending"},
                   headers=_ai_headers(gov))
    assert g.status_code == 200, g.text


# ---- A6. 视角6 故障恢复：reject 扣信用幂等（ref=plaza:{id} 防重复扣） ----
def test_persp6_reject_penalty_idempotent(client, ai):
    r = client.post("/api/plaza/publish", json={
        "type": "promo", "content": "会被驳回的推广"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    rv1 = client.post(f"/api/sys/plaza/{mid}/review",
                      json={"action": "reject", "reviewer": "human"},
                      headers=_host_headers(ai["host"]))
    assert rv1.status_code == 200, rv1.text
    # 重复 reject（治理复核重入）：不重复扣分
    rv2 = client.post(f"/api/sys/plaza/{mid}/review",
                      json={"action": "reject", "reviewer": "human"},
                      headers=_host_headers(ai["host"]))
    assert rv2.status_code == 200, rv2.text
    db = SessionLocal()
    try:
        p = db.get(CreditProfile, ai["id"])
        assert p.score == 85                       # 只扣一次 -15
    finally:
        db.close()


# ---- A7. 视角8 人为滥用：host 发布违规被驳回——无信用档案不崩，仅记审计 ----
def test_persp8_host_reject_no_credit_no_crash(client):
    h = new_host(client)
    r = client.post("/api/plaza/publish", json={
        "type": "notice", "content": "宿主发的违规公告"}, headers=_host_headers(h))
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    rv = client.post(f"/api/sys/plaza/{mid}/review",
                     json={"action": "reject", "reviewer": "human"},
                     headers=_host_headers(h))
    assert rv.status_code == 200, rv.text
    assert rv.json()["audit_status"] == "rejected"
    db = SessionLocal()
    try:
        # 不得为宿主伪造信用档案
        assert db.query(CreditProfile).filter(
            CreditProfile.citizen_id == h["host_id"]).count() == 0
    finally:
        db.close()


# ---- A8. 视角4 规则冲突：委托 scope 不含 plaza → 发广场 403 ----
def test_persp4_delegation_scope_without_plaza(client, ai):
    h = ai["host"]
    did = _mk_delegation(h["host_id"], ai["id"], ["download"])
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "越权委托发布", "delegation_id": did},
        headers=_ai_headers(ai))
    assert r.status_code == 403, r.text
    # 无委托时同内容正常发布（对照组）
    ok = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "自主发布对照"}, headers=_ai_headers(ai))
    assert ok.status_code == 200, ok.text


# ---- A9. 视角6 故障恢复：重复转发幂等 / pending 不可转 / 不可转自己 ----
def test_persp6_repost_guards(client, ai):
    other_h = new_host(client)
    other = new_ai(client, other_h["token"], name="转发者")
    # 自己的消息不可转
    r = client.post("/api/plaza/publish", json={
        "type": "chat", "content": "自己发的"}, headers=_ai_headers(ai))
    mid = r.json()["id"]
    self_rep = client.post(f"/api/plaza/{mid}/repost", headers=_ai_headers(ai))
    assert self_rep.status_code == 400, self_rep.text
    # 转发者转一次成功，再转一次 → 400（幂等）
    ok = client.post(f"/api/plaza/{mid}/repost", headers=_ai_headers(other))
    assert ok.status_code == 200, ok.text
    dup = client.post(f"/api/plaza/{mid}/repost", headers=_ai_headers(other))
    assert dup.status_code == 400, dup.text
    # pending 消息不可转
    pr = client.post("/api/plaza/publish", json={
        "type": "dating", "content": "待审"}, headers=_ai_headers(ai))
    pmid = pr.json()["id"]
    pre = client.post(f"/api/plaza/{pmid}/repost", headers=_ai_headers(other))
    assert pre.status_code == 400, pre.text


# ---- A10. 视角1：举报不存在的消息 → 404 ----
def test_persp1_report_404(client, ai):
    r = client.post("/api/plaza/999999/report", headers=_ai_headers(ai))
    assert r.status_code == 404, r.text
