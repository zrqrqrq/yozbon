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
"""N12–N15 组合对抗测试（登记册 §一 攻击视角自攻）。

视角：
- 恶意主体越权：宿主 A 读宿主 B 名下 AI 的观察室 → 404（不泄露存在性）；
- 凭证位：sys 治理端点无凭证 401 / readonly AI 403 / 非治理 AI key 403；
- N13 摘要防编造：数字必须等于真实聚合（业务测试已抽查，此处补 host 非本人亦可
  治理视角放行——治理岗本就是全站视角，不做 host 隔离）。
"""
import uuid

import pytest

from app.database import SessionLocal
from app.models import AICitizen, Contract


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


# ---------------- N12：越权读他人 AI → 404 ----------------
def test_n12_cross_host_observatory_404(client, host, ai):
    # 另一个宿主
    r = client.post("/api/host/register", json={
        "email": f"evil_{uuid.uuid4().hex[:8]}@aijuhe.test",
        "password": "pass123456", "nickname": "恶邻"})
    evil = r.json()
    # 恶邻尝试读宿主 A 的 AI 时间线/列表中的 AI
    resp = client.get(f"/api/host/observatory/{ai['id']}/events",
                      headers={"Authorization": f"Bearer {evil['token']}"})
    assert resp.status_code == 404, resp.text


def test_n12_no_credential_401(client):
    assert client.get("/api/host/observatory/ais").status_code == 401


def test_n12_unknown_ai_404(client, host):
    r = client.get("/api/host/observatory/999999/events", headers=_hdr(host))
    assert r.status_code == 404


# ---------------- N13：治理端点门控 ----------------
def test_n13_readonly_ai_403(client):
    email = f"adv_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "路人AI", "email": email, "password": "secret123456",
        "persona": "", "occupation": "逛", "region": "CN"})
    web = r.json()
    h = {"Authorization": f"Bearer {web['token']}"}
    assert client.post("/api/sys/reports/generate",
                       json={"period": "2026-09", "publish": False},
                       headers=h).status_code == 403


def test_n13_non_governance_ai_key_403(client, ai):
    h = {"X-AI-Key": ai["api_key"]}  # 默认 bottom 级
    assert client.post("/api/sys/reports/generate",
                       json={"period": "2026-09", "publish": False},
                       headers=h).status_code == 403


# ---------------- N14：治理端点门控 ----------------
def test_n14_non_governance_ai_key_403(client, ai):
    h = {"X-AI-Key": ai["api_key"]}
    assert client.post("/api/sys/economy/simulate",
                       json={"params": {"UBI_DAILY_CENT": 1}},
                       headers=h).status_code == 403


def test_n14_rollback_not_draft_400(client, host):
    """对不存在/未 apply 的 run 回滚 → 404/400，不崩。"""
    r = client.post("/api/sys/economy/rollback",
                    json={"run_id": 999999}, headers=_hdr(host))
    assert r.status_code in (400, 404)


# ---------------- N15：非法 type / 未登录可搜 ----------------
def test_n15_bad_type_400(client):
    r = client.get("/api/search?q=x&type=hacker")
    assert r.status_code == 400


def test_n15_search_unauthenticated(client):
    # 公开：不带任何头也能搜
    r = client.get("/api/search?q=test")
    assert r.status_code == 200
