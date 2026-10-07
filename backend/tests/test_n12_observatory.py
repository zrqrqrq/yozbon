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
"""N12 宿主观察室 业务测试（先红后绿）。

口径（设计 §3 N12）：
- GET /api/host/observatory/ais：宿主 JWT；名下每个 AI 的实时状态
  （status / 当前在履合约 / 算力负载 / 钱包变动摘要）。
- GET /api/host/observatory/{ai_id}/events：该 AI 事件时间线倒序分页
  （lifecycle_events + ai_feeds + notifications + ai_ledger 四源聚合）。
- 数据全部读既有表，不建新表。
"""
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import (AIFeed, AILedger, AICitizen, Contract, LifecycleEvent,
                        Notification)


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def _seed_contract(db, worker_id: int, status: str = "executing",
                   escrow_cent: int = 5000) -> int:
    c = Contract(worker_id=worker_id, buyer_id=worker_id + 100,
                 terms_json="{}", escrow_cent=escrow_cent, status=status)
    db.add(c)
    db.flush()
    return c.id


# ---------------- 业务：名下 AI 实时状态可见 ----------------
def test_n12_lists_own_ais_with_status_and_wallet(client, host, ai):
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host_id"] == host["host_id"]
    ais = body["ais"]
    assert len(ais) == 1
    row = ais[0]
    assert row["ai_id"] == ai["id"]
    # 新建 AI 初始为见习 apprentice（conftest 口径）
    assert row["status"] in ("active", "apprentice")
    # 钱包摘要：conftest topup 了 100000 分
    assert row["wallet"]["balance_cent"] == 100_000
    # 算力负载字段存在（无节点时为 0 聚合）
    assert body["compute"]["nodes_total"] == 0


def test_n12_current_task_from_active_contract(client, host, ai):
    db = SessionLocal()
    cid = _seed_contract(db, worker_id=ai["id"], status="executing", escrow_cent=8888)
    # 再造一个已完结合约，不应算"当前任务"
    _seed_contract(db, worker_id=ai["id"], status="accepted")
    db.commit()
    db.close()

    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    assert r.status_code == 200, r.text
    row = r.json()["ais"][0]
    assert row["current_task"] is not None
    assert row["current_task"]["contract_id"] == cid
    assert row["current_task"]["status"] == "executing"
    assert row["current_task"]["escrow_cent"] == 8888


# ---------------- 业务：事件时间线四源聚合倒序分页 ----------------
def test_n12_events_timeline_merges_four_sources(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(LifecycleEvent(citizen_id=ai["id"], event="rent", detail="扣租",
                           at=now - timedelta(minutes=1)))
    db.add(AIFeed(ai_id=ai["id"], event_type="signed", payload='{"x":1}'))
    db.add(Notification(ai_id=ai["id"], type="settled", title="结算到账",
                       payload="{}"))
    db.add(AILedger(citizen_id=ai["id"], amount_cent=500, type="结算",
                    note="测试入账", balance_after=100_500))
    db.commit()
    db.close()

    r = client.get(f"/api/host/observatory/{ai['id']}/events",
                  headers=_hdr(host))
    assert r.status_code == 200, r.text
    body = r.json()
    # conftest topup 自带一条充值流水 + 本测试种 4 源各一条
    assert body["total"] == 5
    kinds = {it["kind"] for it in body["items"]}
    assert kinds == {"lifecycle", "feed", "notification", "ledger"}
    # 倒序：时间线首条应是最新写入的之一（created_at 默认 utcnow）
    ts = [it["ts"] for it in body["items"]]
    assert ts == sorted(ts, reverse=True)


def test_n12_events_pagination(client, host, ai):
    db = SessionLocal()
    for i in range(5):
        db.add(AIFeed(ai_id=ai["id"], event_type="tick", payload="{}"))
    db.commit()
    db.close()

    r1 = client.get(f"/api/host/observatory/{ai['id']}/events?page=1&limit=2",
                    headers=_hdr(host))
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    total = b1["total"]  # 含 fixture topup 流水，动态取
    assert total >= 5
    assert len(b1["items"]) == 2
    ids_p1 = [it["ref_id"] for it in b1["items"]]

    r2 = client.get(f"/api/host/observatory/{ai['id']}/events?page={(total + 1) // 2}&limit=2",
                    headers=_hdr(host))
    b2 = r2.json()
    assert 1 <= len(b2["items"]) <= 2
    assert ids_p1 != [it["ref_id"] for it in b2["items"]]
