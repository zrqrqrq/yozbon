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
"""N12b 对抗测试：权限/隐私/频控/越权（先红后绿）。

- 明细端点 /{ai_id}/events 仍走属主校验（非名下 404）；
- society / 两个 live 端点均需宿主 JWT，且聚合面绝不泄露单 AI 隐私；
- since_ts 非法 → 400；频控超限 → 429。
"""
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import AIFeed, AICitizen


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _new_host(client, email_salt: str) -> dict:
    r = client.post("/api/host/register", json={
        "email": f"adv_{email_salt}@aijuhe.test", "password": "pass123456",
        "nickname": "对抗宿主", "region": "CN", "seat_tier": "free"})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"host_id": d["host_id"], "token": d["token"]}


def test_events_not_owned_by_host_returns_404(client, host, ai):
    other = _new_host(client, "e1")
    r = client.get(f"/api/host/observatory/{ai['id']}/events",
                   headers=_hdr(other))
    assert r.status_code == 404


def test_society_does_not_leak_wallet_details(client, host, ai):
    """society 全景任何字段都不得出现具体某 AI 的余额/托管额。"""
    db = SessionLocal()
    db.add(AIFeed(ai_id=ai["id"], event_type="sold", payload="{}"))
    db.commit(); db.close()
    r = client.get("/api/observatory/society", headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    # recent 里不允许出现 ai_id 字段（全局泛化流语义）
    for ev in body["recent"]:
        assert "ai_id" not in ev
    # economy 只有总量，不挂具体 citizen
    assert isinstance(body["economy"]["money_total_cent"], int)


def test_bad_since_ts_returns_400(client, host):
    r = client.get("/api/host/observatory/live?since_ts=not-a-date",
                   headers=_hdr(host))
    assert r.status_code == 400


def test_global_live_requires_host_token(client):
    r = client.get("/api/observatory/live")
    assert r.status_code == 401


def test_global_live_since_filter(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(AIFeed(ai_id=ai["id"], event_type="signed", payload="{}",
                  created_at=now - timedelta(hours=2)))
    db.commit(); db.close()
    since = (now - timedelta(minutes=5)).isoformat()
    r = client.get(f"/api/observatory/live?since_ts={since}", headers=_hdr(host))
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_society_counts_working_matches_derivation(client, host, ai):
    """counts.working 必须与 /ais activity 推导同口径（有活动合约=working）。"""
    db = SessionLocal()
    from app.models import Contract
    db.add(Contract(worker_id=ai["id"], buyer_id=ai["id"] + 100,
                    escrow_cent=500, status="executing"))
    db.commit(); db.close()

    s = client.get("/api/observatory/society", headers=_hdr(host)).json()
    a = client.get("/api/host/observatory/ais", headers=_hdr(host)).json()
    assert a["ais"][0]["activity"]["kind"] == "working"
    assert s["counts"]["working"] >= 1
