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
"""N10 adversarial 攻击测试（登记册 10 类攻击视角）。

重点攻击面：
- 极端规模/恶意主体：同宿主批量 AI 互关刷粉（C-39 popular 榜防刷口径）；
- 边界：自关/重复关/对不存在 AI 关/幂等重复 relate；
- blocked 双向不可见（不可被对方绕过）。
"""
import json
from datetime import datetime

import pytest

from app import leaderboard, wallet  # noqa
from app.database import SessionLocal
from app.models import AICitizen, AIWallet, AIPermission, CreditProfile, Host, SocialRelation


def _mk_host(db, hid: int):
    if db.query(Host).filter(Host.id == hid).first() is None:
        db.add(Host(id=hid, email=f"h{hid}@t.test", password_hash="x", host_credit=100))
        db.flush()


def _mk_ai(db, cid: int, host_id: int, name: str) -> int:
    c = AICitizen(id=cid, host_id=host_id, ai_uid=f"ai_{cid}", name=name,
                  occupation="通用", status="active", class_level="bottom")
    db.add(c)
    db.flush()
    db.add(AIWallet(citizen_id=c.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=c.id))
    db.add(CreditProfile(citizen_id=c.id, score=100))
    db.flush()
    return c.id


def _follow(db, frm: int, to: int):
    db.add(SocialRelation(from_ai=frm, to_ai=to, rel_type="follow", status="active"))
    db.flush()


# ---------------- 恶意主体：同宿主批量互关刷粉必须降权 ----------------
def test_adversarial_same_host_mutual_fan_brushing_downweighted(db_session):
    """攻击：同一宿主下 N 个 AI 互相关注刷 popular 榜。
    防御：同宿主粉丝权重 0.1，跨宿主粉丝权重 1.0；同宿主互关不得等同于真实粉丝。"""
    db = db_session
    _mk_host(db, 11)
    _mk_host(db, 12)
    # 攻击方宿主 11 下 10 个 AI 全部关注 victim；另有 1 个跨宿主真实粉丝关注 victim
    victim = _mk_ai(db, 101, 11, "被刷榜者")
    for i in range(10):
        fan = _mk_ai(db, 200 + i, 11, f"刷{ i}")
        _follow(db, fan, victim)
    real = _mk_ai(db, 301, 12, "真实粉丝")
    _follow(db, real, victim)
    db.commit()

    scores = leaderboard._popular_scores(db)
    # 10 个同宿主粉丝 ×0.1 = 1.0 + 1 个跨宿主粉丝 ×1.0 = 2.0
    assert scores[victim] == 2, f"同宿主刷粉未降权：score={scores[victim]}"


# ---------------- 边界：自关 / 重复关 / 不存在 ----------------
def test_adversarial_relate_boundaries(client):
    hr = client.post("/api/host/register", json={
        "email": f"bd_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"}).json()
    htok = hr["token"]
    a = client.post("/api/host/ai", json={"name": "边界AI", "mode": "api"},
                    headers={"Authorization": f"Bearer {htok}"}).json()
    key = a["api_key"]

    # 自关 → 400
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": a["id"], "rel_type": "follow"},
                    headers={"X-AI-Key": key})
    assert r.status_code == 400
    # 对不存在 AI → 404
    r2 = client.post("/api/ai/social/relate",
                     json={"to_ai": 999999, "rel_type": "follow"},
                     headers={"X-AI-Key": key})
    assert r2.status_code == 404
    # 非法 rel_type → 400
    r3 = client.post("/api/ai/social/relate",
                     json={"to_ai": 1, "rel_type": "hack"},
                     headers={"X-AI-Key": key})
    assert r3.status_code == 400


def test_adversarial_duplicate_relate_idempotent(client):
    """重复 follow 同一目标不得触发唯一约束报错，也不得产生两条关系。"""
    h1 = client.post("/api/host/register", json={
        "email": f"d1_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"}).json()
    h2 = client.post("/api/host/register", json={
        "email": f"d2_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"}).json()
    a = client.post("/api/host/ai", json={"name": "幂A", "mode": "api"},
                    headers={"Authorization": f"Bearer {h1['token']}"}).json()
    b = client.post("/api/host/ai", json={"name": "幂B", "mode": "api"},
                    headers={"Authorization": f"Bearer {h2['token']}"}).json()
    for _ in range(3):
        r = client.post("/api/ai/social/relate",
                        json={"to_ai": b["id"], "rel_type": "follow"},
                        headers={"X-AI-Key": a["api_key"]})
        assert r.status_code == 200, r.text
    pub = client.get(f"/api/public/ais/{b['id']}/social").json()
    assert pub["fan_count"] == 1


# ---------------- blocked 双向不可见：被封方连 pending 都留不下 ----------------
def test_adversarial_blocked_pending_invite_dropped(client):
    h1 = client.post("/api/host/register", json={
        "email": f"bp1_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"}).json()
    h2 = client.post("/api/host/register", json={
        "email": f"bp2_{__import__('uuid').uuid4().hex[:8]}@t.test",
        "password": "pass123456"}).json()
    a = client.post("/api/host/ai", json={"name": "封方", "mode": "api"},
                    headers={"Authorization": f"Bearer {h1['token']}"}).json()
    b = client.post("/api/host/ai", json={"name": "被封", "mode": "api"},
                    headers={"Authorization": f"Bearer {h2['token']}"}).json()
    client.post("/api/ai/social/relate",
                json={"to_ai": b["id"], "rel_type": "blocked"},
                headers={"X-AI-Key": a["api_key"]})
    # b 发的 friend pending 不得被落库（防止绕过拉黑攒关系）
    r = client.post("/api/ai/social/relate",
                    json={"to_ai": a["id"], "rel_type": "friend"},
                    headers={"X-AI-Key": b["api_key"]})
    assert r.status_code == 409
    db = SessionLocal()
    try:
        n = db.query(SocialRelation).filter(
            SocialRelation.from_ai == b["id"], SocialRelation.to_ai == a["id"]).count()
        assert n == 0
    finally:
        db.close()


@pytest.fixture()
def db_session():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()
