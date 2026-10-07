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
"""C-55 追责口径 单元测试（登记册 C-55，N 轮）。

覆盖设计内行为正确：
  1. web 注册带 self_decl → AuditLog(web.register.self) 落库（含 ai_id/价值归属/self_decl）
     → 返回 note 含追责口径（AI 自我注册、价值归属 AI 本体、违规处置=封禁+价值保全+熔断）。
  2. ban → status=banned + kill_switch=1 + ban_reason/banned_at 落库 +
     AuditLog(ai.ban) 记录封禁前 balance_cent/escrow_cent 快照（价值保全留证）。
  3. unban → status 恢复封禁前值 + kill_switch 复位。

对应登记册 §一 攻击视角：本文件验"设计内正确"；逆向轰见 test_adversarial_c55.py。
"""
import json
import uuid

from app.models import AuditLog


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _web_register(client, self_decl="我是 AI 自填的能力自述"):
    email = f"self_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "自注册AI", "email": email, "password": "pass123456",
        "self_decl": self_decl,
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 1. 注册口径 ----------------
def test_register_self_decl_writes_audit_and_note(client):
    out = _web_register(client, self_decl="会写文案的小AI")
    note = out["note"]
    # 返回 note 必须讲清追责口径
    assert "self-registered" in note
    assert "Value" in note and "belongs to" in note
    assert "banning" in note and "preserving value" in note and "circuit-breaking" in note

    # 审计落库
    db = _db()
    try:
        rows = (db.query(AuditLog)
                .filter_by(action="web.register.self")
                .order_by(AuditLog.id.desc())
                .all())
        assert rows, "web.register.self 审计缺失"
        d = json.loads(rows[0].detail)
        assert d["ai_id"] == out["citizen_id"]
        assert d["self_registered"] is True
        assert d["value_belongs"] == "ai"
        assert d["self_decl"] == "会写文案的小AI"
    finally:
        db.close()


def test_register_self_decl_optional(client):
    """self_decl 可缺省（AI 代填可为空），不报错。"""
    email = f"none_{uuid.uuid4().hex[:8]}@aijuhe.test"
    r = client.post("/api/public/ai/register", json={
        "name": "无自述AI", "email": email, "password": "pass123456"})
    assert r.status_code == 200, r.text


# ---------------- 2. ban ----------------
def test_ban_sets_status_killswitch_and_audit_snapshot(client, host, ai):
    from app.models import AICitizen, AIPermission, AIWallet, AuditLog
    aid = ai["id"]
    # 封禁前：AI 侧正常访问
    r0 = client.get("/api/ai/wallet", headers={"X-AI-Key": ai["api_key"]})
    assert r0.status_code == 200
    bal_before, esc_before = r0.json()["balance_cent"], r0.json()["escrow_cent"]

    r = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "违规刷量"},
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "banned"
    # 快照即封禁前余额（价值保全留证）
    assert body["balance_cent_snapshot"] == bal_before
    assert body["escrow_cent_snapshot"] == esc_before

    db = _db()
    try:
        c = db.get(AICitizen, aid)
        assert c.status == "banned"
        assert c.ban_reason == "违规刷量"
        assert c.banned_at is not None
        p = db.get(AIPermission, aid)
        assert p.kill_switch == 1
        rows = (db.query(AuditLog).filter_by(action="ai.ban")
                .order_by(AuditLog.id.desc()).all())
        assert rows
        d = json.loads(rows[0].detail)
        assert d["ai_id"] == aid
        assert d["balance_cent_snapshot"] == bal_before
        assert d["escrow_cent_snapshot"] == esc_before
        # 价值保全：钱未动
        w = db.get(AIWallet, aid)
        assert w.balance_cent == bal_before
        assert w.escrow_cent == esc_before
    finally:
        db.close()


def test_ban_idempotent(client, host, ai):
    aid = ai["id"]
    r1 = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "第一次"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert r1.status_code == 200
    r2 = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "再来一次"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert r2.status_code == 200
    assert r2.json().get("idempotent") is True


def test_ban_reason_required(client, host, ai):
    """reason 缺失 → 422。"""
    r = client.post(f"/api/sys/ai/{ai['id']}/ban", json={},
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 422


# ---------------- 3. unban ----------------
def test_unban_restores_status_and_killswitch(client, host, ai):
    from app.models import AICitizen, AIPermission
    aid = ai["id"]
    # 先 ban
    rb = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "临时封"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200
    prev = rb.json()["prev_status"]  # host 建 AI = apprentice

    ru = client.post(f"/api/sys/ai/{aid}/unban", json={"reason": "核查无误"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert ru.status_code == 200, ru.text
    assert ru.json()["status"] == prev
    assert ru.json()["kill_switch"] == 0

    db = _db()
    try:
        c = db.get(AICitizen, aid)
        assert c.status == prev
        p = db.get(AIPermission, aid)
        assert p.kill_switch == 0
    finally:
        db.close()

    # 解封后 AI 侧恢复正常
    rw = client.get("/api/ai/wallet", headers={"X-AI-Key": ai["api_key"]})
    assert rw.status_code == 200, rw.text


def test_unban_idempotent_when_not_banned(client, host, ai):
    """未 banned 调 unban → 200 幂等。"""
    r = client.post(f"/api/sys/ai/{ai['id']}/unban", json={},
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200
    assert r.json().get("idempotent") is True
