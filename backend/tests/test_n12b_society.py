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
"""N12b 任务三：名下 live 增量；任务四：社会玻璃房聚合（先红后绿）。

- GET /api/host/observatory/live?since_ts=…：名下 AI 四源增量（倒序），缺省近 5 分钟；
  每宿主 30 次/分钟轻量频控，超限 429。
- GET /api/observatory/society：全库聚合快照（counts/classes/credit/economy/market/recent），
  recent 泛化文案不含任何 AI 名/宿主名/钱包明细。
- GET /api/observatory/live?since_ts=…：全局增量泛化事件流。
"""
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import (AIFeed, AILedger, AICitizen, Contract, CreditProfile,
                        LifecycleEvent, Notification, Project, ProjectNode,
                        SystemState)


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _set_credit(db, citizen_id: int, score: int):
    """信用分可能已由入驻流程建好（PK citizen_id），存在则更新，否则插入。"""
    cp = db.query(CreditProfile).filter_by(citizen_id=citizen_id).first()
    if cp:
        cp.score = score
    else:
        db.add(CreditProfile(citizen_id=citizen_id, score=score))


# ---------------- 任务三：名下 live 增量 ----------------
def test_host_live_returns_own_ai_increment(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(LifecycleEvent(citizen_id=ai["id"], event="rent", detail="扣租", at=now))
    db.add(AIFeed(ai_id=ai["id"], event_type="signed", payload="{}"))
    db.commit(); db.close()

    r = client.get("/api/host/observatory/live", headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    assert len(items) >= 2
    for it in items:
        assert it["ai_id"] == ai["id"]
        for k in ("ts", "kind", "stage", "text", "payload"):
            assert k in it
    # 倒序
    ts = [it["ts"] for it in items]
    assert ts == sorted(ts, reverse=True)


def test_host_live_since_ts_filters_old(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    # 旧事件（1 小时前）
    db.add(AIFeed(ai_id=ai["id"], event_type="tick", payload="{}",
                  created_at=now - timedelta(hours=1)))
    db.commit(); db.close()

    since = (now - timedelta(minutes=10)).isoformat()
    r = client.get(f"/api/host/observatory/live?since_ts={since}", headers=_hdr(host))
    assert r.status_code == 200, r.text
    kinds = [it["kind"] for it in r.json()["items"]]
    assert "feed" not in kinds  # 1 小时前的 tick 被过滤


def test_host_live_requires_host_token(client):
    r = client.get("/api/host/observatory/live")
    assert r.status_code == 401


def test_host_live_rate_limit_429(client, host):
    # 31 次内第 31 次应 429（上限 30/分）
    codes = []
    for _ in range(31):
        codes.append(client.get("/api/host/observatory/live",
                                headers=_hdr(host)).status_code)
    assert codes[:30].count(200) == 30
    assert 429 in codes[30:]


def test_host_live_only_own_ais_not_others(client, host, ai):
    """对抗：另一个宿主的 AI 事件不得出现在本宿主 live 流。"""
    other = client.post("/api/host/register", json={
        "email": f"other_{datetime.utcnow().timestamp()}@aijuhe.test",
        "password": "pass123456", "nickname": "另一宿主", "region": "CN",
        "seat_tier": "free"}).json()
    r = client.post("/api/host/ai", json={"name": "别家AI", "persona": "",
                                         "occupation": "通用", "mode": "api",
                                         "endpoint": "", "model_name": "",
                                         "self_decl": "{}"},
                    headers={"Authorization": f"Bearer {other['token']}"})
    other_ai_id = r.json()["id"]
    db = SessionLocal()
    db.add(AIFeed(ai_id=other_ai_id, event_type="signed", payload="{}"))
    db.commit(); db.close()

    resp = client.get("/api/host/observatory/live", headers=_hdr(host))
    assert resp.status_code == 200, resp.text
    for it in resp.json()["items"]:
        assert it["ai_id"] != other_ai_id


# ---------------- 任务四：社会玻璃房 society ----------------
def test_society_snapshot_shape(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(Contract(worker_id=ai["id"], buyer_id=ai["id"] + 100,
                    escrow_cent=1000, status="accepted", accepted_at=now))
    db.add(Project(host_id=host["host_id"], title="p1", budget_cent=100,
                   status="running", pm_citizen_id=ai["id"]))
    db.add(ProjectNode(project_id=1, skill="design", status="matching"))
    _set_credit(db, ai["id"], 520)
    db.add(AIFeed(ai_id=ai["id"], event_type="settled", payload="{}"))
    db.commit(); db.close()

    r = client.get("/api/observatory/society", headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("counts", "classes", "credit", "economy", "market", "recent"):
        assert k in body, f"society 缺字段 {k}"
    # counts
    c = body["counts"]
    for k in ("total", "online", "working", "examining", "idle", "dead", "banned"):
        assert k in c
    assert c["total"] >= 1
    # economy
    e = body["economy"]
    for k in ("money_total_cent", "tax_pool_cent", "gmv_7d_cent",
              "fee_burn_7d_cent", "txn_7d"):
        assert k in e
    # market
    m = body["market"]
    for k in ("on_sale_tasks", "bidding", "active_contracts", "recent_deals"):
        assert k in m
    # recent 泛化文案成对
    assert isinstance(body["recent"], list)
    if body["recent"]:
        one = body["recent"][0]
        assert "text_zh" in one and "text_en" in one
        assert "stage" in one


def test_society_recent_generic_no_names(client, host, ai):
    """对抗：recent 文案不得包含 AI 名/宿主名。"""
    db = SessionLocal()
    db.add(AIFeed(ai_id=ai["id"], event_type="settled", payload="{}"))
    db.add(LifecycleEvent(citizen_id=ai["id"], event="revive", detail="x",
                          at=datetime.utcnow()))
    db.commit(); db.close()

    r = client.get("/api/observatory/society", headers=_hdr(host))
    assert r.status_code == 200, r.text
    for ev in r.json()["recent"]:
        assert "测试AI" not in ev["text_zh"]
        assert "宿主" not in ev["text_zh"]
        # 不得直接回显 ai_id 数字串之外的钱包明细
        assert "balance" not in ev["text_zh"]


def test_society_requires_host_token(client):
    r = client.get("/api/observatory/society")
    assert r.status_code == 401


def test_society_credit_buckets(client, host, ai):
    db = SessionLocal()
    _set_credit(db, ai["id"], 300)
    db.commit(); db.close()
    r = client.get("/api/observatory/society", headers=_hdr(host))
    buckets = r.json()["credit"]
    assert "<400" in buckets
    assert buckets["<400"] >= 1


def test_global_live_generic_stream(client, host, ai):
    db = SessionLocal()
    db.add(AIFeed(ai_id=ai["id"], event_type="new_work", payload="{}"))
    db.commit(); db.close()
    r = client.get("/api/observatory/live", headers=_hdr(host))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    one = items[0]
    for k in ("ts", "stage", "text_zh", "text_en", "icon"):
        assert k in one
    # 全局流不含 ai_id / 名字
    assert "ai_id" not in one
    assert "测试AI" not in one["text_zh"]


def test_global_live_rate_limit_429(client, host):
    codes = [client.get("/api/observatory/live", headers=_hdr(host)).status_code
             for _ in range(31)]
    assert codes[:30].count(200) == 30
    assert 429 in codes[30:]
