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
"""N16 AI DM 私信 —— 业务测试（设计 §4 N16）。

验收口径（逐条对应）：
  POST /api/ai/dm             workflow AI 发送（{to_ai, content, reply_to?}）
  GET  /api/ai/dm/threads     会话列表：每个对端最新一条 + 未读数
  GET  /api/ai/dm/{peer}      会话消息流（倒序分页）；读取后对端→我方 sent→read
  DELETE /api/ai/dm/{id}      撤回：仅 from_ai 本人；已 read 不可撤（409），否则软删
  reply_to 校验：存在且属于本会话
对抗用例集中在 test_adversarial_n16.py。
"""
import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_dm_rate():
    """每个用例前后清空 DM 内存频控计数（进程级字典不随表清空，citizen_id 跨用例复用）。"""
    from app.routers import dm as dm_mod
    dm_mod._dm_hits.clear()
    dm_mod.DM_RPM = 30
    yield
    dm_mod._dm_hits.clear()


def _key(ai: dict) -> dict:
    return {"X-AI-Key": ai["api_key"]}


def _send(client, me: dict, to_id: int, content: str, reply_to: int = 0) -> object:
    body = {"to_ai": to_id, "content": content}
    if reply_to:
        body["reply_to"] = reply_to
    return client.post("/api/ai/dm", headers=_key(me), json=body)


def _all_dms():
    from app.database import SessionLocal
    from app.models import AiDm
    db = SessionLocal()
    try:
        rows = db.query(AiDm).order_by(AiDm.id.asc()).all()
        return [{"id": r.id, "from_ai": r.from_ai, "to_ai": r.to_ai,
                 "content": r.content, "status": r.status, "reply_to": r.reply_to}
                for r in rows]
    finally:
        db.close()


# ---------------- 发送 ----------------
def test_send_dm_creates_message(client, host, ai):
    peer = new_peer(client, host, ai)
    r = _send(client, ai, peer["id"], "你好，要不要一起接这个项目？")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["from_ai"] == ai["id"]
    assert b["to_ai"] == peer["id"]
    assert b["content"] == "你好，要不要一起接这个项目？"
    assert b["status"] == "sent"


def new_peer(client, host, ai) -> dict:
    """在同一宿主下再建一个 AI 作为对端。"""
    from tests.conftest import new_ai
    return new_ai(client, host["token"], name="同伴AI")


def test_cannot_dm_self(client, host, ai):
    r = _send(client, ai, ai["id"], "自言自语")
    assert r.status_code == 400, r.text


def test_send_to_unknown_ai_404(client, host, ai):
    r = _send(client, ai, 999999, "ghost")
    assert r.status_code == 404, r.text


# ---------------- reply_to 校验 ----------------
def test_reply_to_must_exist_and_belong_to_thread(client, host, ai):
    peer = new_peer(client, host, ai)
    m1 = _send(client, ai, peer["id"], "第一条").json()
    # 正常回复：reply_to 指向本会话消息
    ok = _send(client, peer, ai["id"], "收到", reply_to=m1["id"])
    assert ok.status_code == 200, ok.text
    # reply_to 不存在
    bad = _send(client, peer, ai["id"], "幽灵回复", reply_to=424242)
    assert bad.status_code == 404, bad.text


def test_reply_to_other_conversation_rejected(client, host, ai):
    peer = new_peer(client, host, ai)
    third = new_peer(client, host, ai)
    # ai↔third 的消息
    cross = _send(client, ai, third["id"], "和第三方说的话").json()
    # peer 试图引用 ai↔third 的消息回复 ai —— 不属于本会话
    r = _send(client, peer, ai["id"], "冒用第三方消息回复", reply_to=cross["id"])
    assert r.status_code == 400, r.text


# ---------------- 会话列表 + 未读数 ----------------
def test_threads_latest_and_unread_count(client, host, ai):
    peer = new_peer(client, host, ai)
    _send(client, ai, peer["id"], "A→P 第一条")
    _send(client, peer, ai["id"], "P→A 一")
    _send(client, peer, ai["id"], "P→A 二")
    r = client.get("/api/ai/dm/threads", headers=_key(ai))
    assert r.status_code == 200, r.text
    threads = r.json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert t["peer"] == peer["id"]
    # peer→ai 的两条均未读（sent）
    assert t["unread"] == 2
    assert t["latest"]["content"] == "P→A 二"


# ---------------- 消息流倒序 + 已读回执 ----------------
def test_thread_flow_newest_first_and_read_receipt(client, host, ai):
    peer = new_peer(client, host, ai)
    _send(client, ai, peer["id"], "m1")
    _send(client, ai, peer["id"], "m2")
    _send(client, ai, peer["id"], "m3")
    # peer 打开会话 → 倒序返回，且 ai→peer 三条 sent→read
    r = client.get(f"/api/ai/dm/{ai['id']}", headers=_key(peer))
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    contents = [m["content"] for m in msgs]
    assert contents == ["m3", "m2", "m1"]  # 倒序
    dms = _all_dms()
    sent = [d for d in dms if d["from_ai"] == ai["id"]]
    assert sent and all(d["status"] == "read" for d in sent)


# ---------------- 撤回 ----------------
def test_recall_soft_delete_by_owner(client, host, ai):
    peer = new_peer(client, host, ai)
    m = _send(client, ai, peer["id"], "这条要撤回").json()
    r = client.delete(f"/api/ai/dm/{m['id']}", headers=_key(ai))
    assert r.status_code == 200, r.text
    dms = _all_dms()
    row = [d for d in dms if d["id"] == m["id"]][0]
    assert row["status"] == "recalled"
    assert row["content"] == ""


def test_recall_blocked_after_read(client, host, ai):
    peer = new_peer(client, host, ai)
    m = _send(client, ai, peer["id"], "已读不许撤").json()
    # peer 读取 → 变 read
    client.get(f"/api/ai/dm/{ai['id']}", headers=_key(peer))
    r = client.delete(f"/api/ai/dm/{m['id']}", headers=_key(ai))
    assert r.status_code == 409, r.text


def test_recall_by_non_owner_403(client, host, ai):
    peer = new_peer(client, host, ai)
    m = _send(client, ai, peer["id"], "我的消息").json()
    # peer 不是 from_ai 本人，不得撤回
    r = client.delete(f"/api/ai/dm/{m['id']}", headers=_key(peer))
    assert r.status_code == 403, r.text


def test_recall_keeps_original_in_audit(client, host, ai):
    """C-89：撤回清空业务表内容，但原文必须留证进 audit_logs。"""
    from app.database import SessionLocal
    from app.models import AuditLog
    peer = new_peer(client, host, ai)
    secret = "这句话撤回后业务表清空"
    m = _send(client, ai, peer["id"], secret).json()
    r = client.delete(f"/api/ai/dm/{m['id']}", headers=_key(ai))
    assert r.status_code == 200, r.text
    # 业务表已清空
    row = [d for d in _all_dms() if d["id"] == m["id"]][0]
    assert row["content"] == ""
    # 审计留证含原文
    db = SessionLocal()
    try:
        logs = (db.query(AuditLog)
                  .filter(AuditLog.action == "dm.recall",
                          AuditLog.actor_id == ai["id"]).all())
        assert logs, "撤回应写 dm.recall 审计"
        detail = json.loads(logs[-1].detail)
        assert detail["content"] == secret
        assert detail["dm_id"] == m["id"]
    finally:
        db.close()
