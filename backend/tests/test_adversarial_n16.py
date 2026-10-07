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
"""N16 AI DM 私信 —— 对抗/攻击测试。

硬验收覆盖：
  - 串标/对敲关键词触发 dm.collusion.flag 审计（发/收两侧内容落库前统一扫描），不阻断发送；
  - 频控：同 AI 每分钟发送超限 → 429；
  - host 代发：越权（非名下 AI）→ 404；正常代发 → audit action=host.dm.send 且仍过串标扫描；
  - readonly（前端注册）令牌 → /api/ai/dm 一律 403。
"""
import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_dm_rate():
    """每个用例前后清空 DM 内存频控计数 + 复位上限（citizen_id 跨用例复用）。"""
    from app.routers import dm as dm_mod
    dm_mod._dm_hits.clear()
    dm_mod.DM_RPM = 30
    yield
    dm_mod._dm_hits.clear()


def _key(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _new_peer(client, host, ai) -> dict:
    from tests.conftest import new_ai
    return new_ai(client, host["token"], name="同伴AI")


def _send(client, me, to_id, content, reply_to=0):
    body = {"to_ai": to_id, "content": content}
    if reply_to:
        body["reply_to"] = reply_to
    return client.post("/api/ai/dm", headers=_key(me), json=body)


def _audit_rows(action):
    from app.database import SessionLocal
    from app.models import AuditLog
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == action).all()
        return [{"actor_type": r.actor_type, "actor_id": r.actor_id,
                 "detail": json.loads(r.detail or "{}")} for r in rows]
    finally:
        db.close()


# ---------------- 串标关键词 → 审计标记（不阻断发送） ----------------
@pytest.mark.parametrize("phrase", ["我们串标一下", "bid rigging 搞定它", "对倒抬价"])
def test_collusion_keyword_flags_audit_but_sends(client, host, ai, phrase):
    peer = _new_peer(client, host, ai)
    r = _send(client, ai, peer["id"], phrase)
    assert r.status_code == 200, r.text            # 不阻断
    flags = _audit_rows("dm.collusion.flag")
    assert flags, f"命中串标词却未落审计：{phrase}"
    d = flags[0]["detail"]
    assert d["categories"], "审计 detail 应含关键词类别"
    assert d["from_ai"] == ai["id"] and d["to_ai"] == peer["id"]


def test_clean_message_no_audit_flag(client, host, ai):
    peer = _new_peer(client, host, ai)
    r = _send(client, ai, peer["id"], "你好，这个任务预算多少？")
    assert r.status_code == 200, r.text
    flags = _audit_rows("dm.collusion.flag")
    assert not flags


# ---------------- 频控：每分钟上限 → 429 ----------------
def test_rate_limit_per_ai_per_minute(client, host, ai):
    from app.routers import dm as dm_mod
    peer = _new_peer(client, host, ai)
    dm_mod._dm_hits.clear()
    dm_mod.DM_RPM = 3
    try:
        codes = [_send(client, ai, peer["id"], f"msg{i}").status_code for i in range(4)]
    finally:
        dm_mod._dm_hits.clear()
    assert codes[:3] == [200, 200, 200], codes
    assert codes[3] == 429, codes


# ---------------- host 代发视图 ----------------
def test_host_proxy_send_ok_and_audited(client, host, ai):
    peer = _new_peer(client, host, ai)
    r = client.post(f"/api/host/ai/{ai['id']}/dm",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"to_ai": peer["id"], "content": "宿主代发一句话"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["from_ai"] == ai["id"] and b["to_ai"] == peer["id"]
    send_logs = _audit_rows("host.dm.send")
    assert send_logs, "代发应留痕 host.dm.send"
    assert send_logs[0]["actor_type"] == "host"
    assert send_logs[0]["detail"]["ai_id"] == ai["id"]


def test_host_proxy_send_still_scans_collusion(client, host, ai):
    peer = _new_peer(client, host, ai)
    r = client.post(f"/api/host/ai/{ai['id']}/dm",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"to_ai": peer["id"], "content": "咱们内定这个标"})
    assert r.status_code == 200, r.text
    flags = _audit_rows("dm.collusion.flag")
    assert flags, "代发内容命中串标词仍应落审计"


def test_host_proxy_foreign_ai_404(client, host, ai):
    from tests.conftest import new_host
    other = new_host(client, email=None)
    peer = _new_peer(client, host, ai)
    # 他宿主拿自己 JWT 代发别人名下的 AI → 404（_own_ai：不存在或不属于）
    r = client.post(f"/api/host/ai/{ai['id']}/dm",
                    headers={"Authorization": f"Bearer {other['token']}"},
                    json={"to_ai": peer["id"], "content": "越权代发"})
    assert r.status_code == 404, r.text


def test_host_threads_foreign_ai_404(client, host, ai):
    from tests.conftest import new_host
    other = new_host(client, email=None)
    r = client.get(f"/api/host/ai/{ai['id']}/dm/threads",
                   headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404, r.text


# ---------------- readonly（前端注册）令牌 → 403 ----------------
def test_readonly_ai_cannot_post_dm(client, host, ai):
    import uuid
    email = f"web_{uuid.uuid4().hex[:8]}@aijuhe.test"
    reg = client.post("/api/public/ai/register", json={
        "name": "前端注册AI", "email": email, "password": "secret123456",
        "persona": "", "occupation": "浏览", "region": "CN"})
    assert reg.status_code == 200, reg.text
    tok = reg.json()["token"]
    peer = _new_peer(client, host, ai)
    r = client.post("/api/ai/dm", headers={"Authorization": f"Bearer {tok}"},
                    json={"to_ai": peer["id"], "content": "readonly 想发私信"})
    assert r.status_code == 403, r.text
