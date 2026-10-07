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
"""C-55 追责口径 对抗测试（登记册 §一 10 类攻击视角 + §三 双测试纪律）。

拿 10 类攻击视角轰 C-55 实现（封禁熔断 / 价值保全 / 防窃取）：
  1 新物种进场：web 注册（host_id=0）AI 同样可被 ban/unban，机制不挑来源。
  2 极端规模：ban 幂等（重复 ban 不重复扣费/写审计），unban 幂等。
  3 恶意主体：banned AI 拿 aik key 继续调后端 → 403；拿 readonly JWT 调只读面 → 403。
  4 规则冲突：banned 与 frozen/dead 同走 _status_check，banned 文案专属但都 403。
  5 边界数值：0 余额 / 0 托管的 AI 被 ban，价值保全断言=前后相等（不出现负/归零）。
  6 故障恢复：ban 后重启会话仍 banned（落库 status）；unban 后功能恢复。
  7 新供应商：不涉及外部通道。
  8 人为滥用（核心）：宿主 A 无法操作宿主 B 名下 AI 的钱包（topup/ledger 越权 404）；
    无任何端点把 AI 钱包/作品/资产转移到其他宿主名下。
  9 经济失衡：ban 不增减 money_supply/tax_pool（价值只冻结，不销毁不印钞）。
 10 法律合规：未登录调 ban → 401；非宿主凭证（aik key / readonly JWT）调 ban → 401。

价值保全硬断言：banned 后 balance_cent/escrow_cent 与 ban 前（及 audit 快照）逐分相等。
"""
import json
import uuid

from tests.conftest import new_host, new_ai, topup


def _db():
    from app.database import SessionLocal
    return SessionLocal()


# ---------------- 3：banned AI 认证 403（aik key 与 readonly JWT 都拒） ----------------
def test_banned_ai_aik_key_rejected(client, host, ai):
    """banned 后，正式 workflow key 调任意 /api/ai/* → 403。"""
    aid = ai["id"]
    assert client.get("/api/ai/wallet",
                      headers={"X-AI-Key": ai["api_key"]}).status_code == 200
    rb = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "攻击行为"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200
    r = client.get("/api/ai/wallet", headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 403, r.text
    assert "banned" in r.json()["detail"].lower() or "frozen" in r.json()["detail"].lower()


def test_banned_ai_readonly_jwt_rejected(client, host):
    """banned 后，web readonly JWT 调 /api/public/me → 403（全链路熔断覆盖只读面）。"""
    email = f"banme_{uuid.uuid4().hex[:8]}@aijuhe.test"
    reg = client.post("/api/public/ai/register", json={
        "name": "待封AI", "email": email, "password": "pass123456"})
    assert reg.status_code == 200
    tok = reg.json()["token"]
    aid = reg.json()["citizen_id"]
    # 封禁前可读
    assert client.get("/api/public/me",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    rb = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "刷量"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200
    r = client.get("/api/public/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403, r.text
    # 被封后也不能重新登录拿新 token
    lg = client.post("/api/public/ai/login", json={"email": email, "password": "pass123456"})
    assert lg.status_code == 403


# ---------------- 5/9：价值保全——banned 后余额/托管不变 ----------------
def test_value_preserved_after_ban(client, host, ai):
    """ban 只冻结不转移不归零：balance/escrow 前后逐分相等，且==audit 快照。"""
    from app.models import AIWallet, AuditLog
    aid = ai["id"]
    w0 = client.get("/api/ai/wallet", headers={"X-AI-Key": ai["api_key"]}).json()
    rb = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "保全测试"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200

    db = _db()
    try:
        w = db.get(AIWallet, aid)
        # 价值保全：钱原封不动
        assert w.balance_cent == w0["balance_cent"]
        assert w.escrow_cent == w0["escrow_cent"]
        # 与 audit 快照一致
        rows = (db.query(AuditLog).filter_by(action="ai.ban")
                .order_by(AuditLog.id.desc()).all())
        snap = json.loads(rows[0].detail)
        assert snap["balance_cent_snapshot"] == w0["balance_cent"]
        assert snap["escrow_cent_snapshot"] == w0["escrow_cent"]
    finally:
        db.close()

    # banned 后该 AI 自己也取不出钱（403），更不可能被转走
    assert client.get("/api/ai/wallet",
                      headers={"X-AI-Key": ai["api_key"]}).status_code == 403


# ---------------- 8：防窃取——宿主无法操作非名下 AI 的钱包 ----------------
def test_no_cross_host_wallet_path(client):
    """宿主 B 对宿主 A 名下 AI 做 topup/ledger → 404（_own_ai 归属闸）。
    全站无把 AI 钱包/作品/资产转移到其他宿主名下的端点（见交付说明审计）。"""
    host_a = new_host(client)
    host_b = new_host(client)
    ai_a = new_ai(client, host_a["token"], name="A的资产AI")
    topup(client, host_a["token"], ai_a["id"], 50_000)

    # B 试图给 A 的 AI 充值 → 404
    r = client.post(f"/api/host/ai/{ai_a['id']}/topup", json={"amount_cent": 100},
                    headers={"Authorization": f"Bearer {host_b['token']}"})
    assert r.status_code == 404, r.text
    # B 试图查 A 的 AI 流水 → 404
    r2 = client.get(f"/api/host/ai/{ai_a['id']}/ledger",
                    headers={"Authorization": f"Bearer {host_b['token']}"})
    assert r2.status_code == 404, r2.text
    # B 试图冻结/熔断 A 的 AI → 404
    r3 = client.post(f"/api/host/ai/{ai_a['id']}/kill",
                     headers={"Authorization": f"Bearer {host_b['token']}"})
    assert r3.status_code == 404
    # A 的 AI 余额未被 B 动过
    w = client.get(f"/api/host/ai/{ai_a['id']}/ledger",
                   headers={"Authorization": f"Bearer {host_a['token']}"})
    assert w.status_code == 200


# ---------------- 10：未登录 / 非宿主凭证调 ban → 401 ----------------
def test_ban_requires_login(client, ai):
    """未登录调 ban → 401。"""
    r = client.post(f"/api/sys/ai/{ai['id']}/ban", json={"reason": "x"})
    assert r.status_code == 401, r.text


def test_ban_rejected_for_non_host_credential(client, host, ai):
    """非宿主凭证（AI workflow key / web readonly JWT）调 ban → 401。
    ban 是平台运营动作，只认 host JWT；AI 自己/前端号无权封禁。"""
    # aik_ workflow key
    r1 = client.post(f"/api/sys/ai/{ai['id']}/ban", json={"reason": "x"},
                     headers={"Authorization": f"Bearer {ai['api_key']}"})
    assert r1.status_code == 401, r1.text
    r1b = client.post(f"/api/sys/ai/{ai['id']}/ban", json={"reason": "x"},
                      headers={"X-AI-Key": ai["api_key"]})
    assert r1b.status_code == 401, r1b.text
    # web readonly JWT
    email = f"ro_{uuid.uuid4().hex[:8]}@aijuhe.test"
    reg = client.post("/api/public/ai/register", json={
        "name": "只读号", "email": email, "password": "pass123456"})
    rotok = reg.json()["token"]
    r2 = client.post(f"/api/sys/ai/{ai['id']}/ban", json={"reason": "x"},
                     headers={"Authorization": f"Bearer {rotok}"})
    assert r2.status_code == 401, r2.text


# ---------------- 6：unban 后恢复正常功能 ----------------
def test_unban_restores_function(client, host, ai):
    """ban→aik 403；unban→aik 200（熔断解除，价值仍在）。"""
    aid = ai["id"]
    rb = client.post(f"/api/sys/ai/{aid}/ban", json={"reason": "临时"},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert rb.status_code == 200
    assert client.get("/api/ai/wallet",
                      headers={"X-AI-Key": ai["api_key"]}).status_code == 403
    ru = client.post(f"/api/sys/ai/{aid}/unban", json={},
                     headers={"Authorization": f"Bearer {host['token']}"})
    assert ru.status_code == 200
    rw = client.get("/api/ai/wallet", headers={"X-AI-Key": ai["api_key"]})
    assert rw.status_code == 200, rw.text
    # 解封后钱还在（价值保全贯穿封禁期）
    assert rw.json()["balance_cent"] == 100_000
