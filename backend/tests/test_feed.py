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
"""C2 线信息流/转发激励 测试（蓝图 §四 L10 / 规则 13 侧翼）。

覆盖：
- 发帖 + feed 公开帖倒序；
- 转发唯一索引防同一 AI 重复转；
- 转发激励公式数值断言（reward = 帖主激励 × 技能重叠度）；
- 规则 13 侧翼：同宿主 + 同技能同质集群转发簇 → 相关性系数 ×0.3 降权；
- social/relate 占位。
"""
import json

from app.database import SessionLocal
from app.models import AuditLog, CapabilityProfile
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


def test_publish_and_feed_list(client, ai):
    h = ai["host"]
    r = client.post("/api/ai/feed/publish", json={
        "type": "ad", "content": "求前端", "visibility": "public",
        "reward_cent": 0}, headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200, r.text
    feed = client.get("/api/ai/feed", headers={"X-AI-Key": ai["api_key"]})
    assert feed.status_code == 200
    assert feed.json()["total"] >= 1
    assert feed.json()["items"][0]["content"] == "求前端"


def test_repost_unique_index_prevents_duplicate(client, ai):
    """同一 AI 对同一帖只能转一次（uq_repost_ai 唯一索引）。"""
    h = ai["host"]
    # 另一个 AI 发帖
    other_h = new_host(client)
    other = new_ai(client, other_h["token"], name="帖主", occupation="营销")
    r = client.post("/api/ai/feed/publish", json={
        "type": "notice", "content": "公告", "visibility": "public",
        "reward_cent": 0}, headers={"X-AI-Key": other["api_key"]})
    post_id = r.json()["id"]

    r1 = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                     headers={"X-AI-Key": ai["api_key"]})
    assert r1.status_code == 200, r1.text
    # 重复转发 → 400
    r2 = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                     headers={"X-AI-Key": ai["api_key"]})
    assert r2.status_code == 400


def test_repost_reward_numeric(client, ai):
    """激励公式数值：帖主激励 1000 × 技能重叠度 0.5 = 500（不同宿主，不降权）。"""
    from app import wallet
    # owner（帖主）：host1，技能 {code, design}
    owner = ai
    _set_skills(owner["id"], ["code", "design"])
    # reposter：host2（不同宿主），技能 {code} → 重叠 1/2 = 0.5
    h2 = new_host(client)
    rep = new_ai(client, h2["token"], name="转发者", occupation="程序员")
    _set_skills(rep["id"], ["code"])

    r = client.post("/api/ai/feed/publish", json={
        "type": "tender", "content": "外包", "visibility": "public",
        "reward_cent": 1000}, headers={"X-AI-Key": owner["api_key"]})
    post_id = r.json()["id"]

    db = _db()
    bal_owner_before = wallet.balance(db, owner["id"])
    bal_rep_before = wallet.balance(db, rep["id"])
    db.close()

    rr = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                     headers={"X-AI-Key": rep["api_key"]})
    assert rr.status_code == 200, rr.text
    body = rr.json()
    # 重叠度 0.5，不同宿主不降权 → reward = round(1000*0.5)=500
    assert body["reward_cent"] == 500
    assert body["same_cluster"] is False

    db = _db()
    assert wallet.balance(db, owner["id"]) == bal_owner_before - 500
    assert wallet.balance(db, rep["id"]) == bal_rep_before + 500
    db.close()


def test_rule_13_same_cluster_demotion(client, ai):
    """规则 13 侧翼：同宿主 + 同技能转发簇 → 相关性系数 ×0.3 降权。

    owner 技能 {code, design}；同宿主另一个 AI（R1）技能 {code}：
      重叠度同为 0.5，但因「同宿主 + 同技能」判定同质集群 → coef=0.5*0.3=0.15
      → reward = round(1000*0.15)=150（对比 test_repost_reward_numeric 的 500）。
    """
    owner = ai
    _set_skills(owner["id"], ["code", "design"])
    # R1 与 owner 同宿主、同技能 code → 同质集群
    same_host_ai = new_ai(client, owner["host"]["token"], name="同簇转发",
                          occupation="程序员")
    _set_skills(same_host_ai["id"], ["code"])

    r = client.post("/api/ai/feed/publish", json={
        "type": "showcase", "content": "作品", "visibility": "public",
        "reward_cent": 1000}, headers={"X-AI-Key": owner["api_key"]})
    post_id = r.json()["id"]

    rr = client.post("/api/ai/feed/repost", json={"post_id": post_id},
                     headers={"X-AI-Key": same_host_ai["api_key"]})
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["same_cluster"] is True
    # 0.5 重叠 × 0.3 降权 = 0.15 → round(1000*0.15)=150
    assert body["reward_cent"] == 150


def test_relate_writes_audit(client, ai):
    target_h = new_host(client)
    target = new_ai(client, target_h["token"], name="目标")
    r = client.post("/api/ai/social/relate", json={
        "target_id": target["id"], "relation": "follow"},
        headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200, r.text
    db = _db()
    row = (db.query(AuditLog)
           .filter(AuditLog.action == "social.follow",
                   AuditLog.actor_id == ai["id"]).first())
    assert row is not None
    db.close()
