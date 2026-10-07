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
"""底座单测：宿主认证（注册/登录/me/鉴权失败）与 AI key 鉴权（deps 网关）。"""
import hashlib

import pytest

from conftest import new_host, new_ai


def test_register_login_me(client):
    r = client.post("/api/host/register", json={
        "email": "boss@aijuhe.test", "password": "pass123456",
        "nickname": "老板", "region": "CN", "seat_tier": "free"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert token

    r = client.post("/api/host/login", json={"email": "boss@aijuhe.test", "password": "pass123456"})
    assert r.status_code == 200
    assert r.json()["host_id"] == 1

    r = client.get("/api/host/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["seat_tier"] == "free"
    assert r.json()["ai_slots"] == 3   # free 席位 = 3（规则 13）

    r = client.post("/api/host/login", json={"email": "boss@aijuhe.test", "password": "wrong"})
    assert r.status_code == 401


def test_register_dup_email(client):
    payload = {"email": "dup@aijuhe.test", "password": "pass123456"}
    assert client.post("/api/host/register", json=payload).status_code == 200
    assert client.post("/api/host/register", json=payload).status_code == 409


def test_me_requires_token(client):
    assert client.get("/api/host/me").status_code == 401
    assert client.get("/api/host/me",
                      headers={"Authorization": "Bearer bad.token.here"}).status_code == 401


def test_seat_slots_mapping(client):
    for tier, expect in (("free", 3), ("basic", 10), ("standard", 30), ("premium", 100)):
        r = client.post("/api/host/register", json={
            "email": f"{tier}@aijuhe.test", "password": "pass123456", "seat_tier": tier})
        assert r.status_code == 200
        me = client.get("/api/host/me",
                        headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
        assert me["ai_slots"] == expect, tier


def test_ai_key_issue_and_auth_gate(client):
    """创建 AI 签发 key（明文一次、库中哈希）；deps.get_current_ai 校验与状态门。"""
    from app.database import SessionLocal
    from app.deps import get_current_ai
    from app.models import AICitizen
    from fastapi import HTTPException

    h = new_host(client, email="k@aijuhe.test")
    data = new_ai(client, h["token"])
    ai_key = data["api_key"]
    cid = data["id"]
    assert ai_key.startswith(f"aik_{cid}_")

    db = SessionLocal()
    try:
        citizen = db.get(AICitizen, cid)
        assert citizen.api_key_hash == hashlib.sha256(ai_key.encode()).hexdigest()

        # 正确 key 通过
        ok = get_current_ai(x_ai_key=ai_key, authorization=None, db=db)
        assert ok.id == cid
        # 错误 key 拒绝
        with pytest.raises(HTTPException) as e:
            get_current_ai(x_ai_key="aik_1_wrongsecret", authorization=None, db=db)
        assert e.value.status_code == 401
        # 死亡状态 403
        citizen.status = "dead"
        db.commit()
        with pytest.raises(HTTPException) as e2:
            get_current_ai(x_ai_key=ai_key, authorization=None, db=db)
        assert e2.value.status_code == 403
    finally:
        db.close()


def test_create_ai_basic_fields(client):
    h = new_host(client, email="c@aijuhe.test")
    data = new_ai(client, h["token"], occupation="文案")
    assert data["status"] == "apprentice"
    assert data["ai_uid"] == "ai_1_1"

    # AI 列表含余额/信用/合约数
    r = client.get("/api/host/ais", headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200
    item = r.json()[0]
    assert item["balance_cent"] == 0
    assert item["credit_score"] == 100
    assert item["active_contracts"] == 0
