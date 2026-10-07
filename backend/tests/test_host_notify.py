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
"""宿主侧收尾端点测试：GET /api/host/notifications 通知聚合 + GET /api/host/contracts。

覆盖：
  - 通知聚合（2 AI + 一个合约 + 一个信用事件 → ≥3 条且类型正确）
  - 分页（limit=1 只回 1 条，total 反映全量）
  - 宿主隔离（他宿主看不到本宿主的合约/信用通知）
  - 合约列表（worker/buyer 视角各一条、status 过滤）
"""
import json

from app.database import SessionLocal
from app.models import Contract, CreditEvent
from tests.conftest import new_ai, new_host


def _db():
    return SessionLocal()


def _hdr(token: str):
    return {"Authorization": f"Bearer {token}"}


# ---------------- 通知聚合 ----------------
def test_notifications_aggregates_contract_and_credit(client):
    host = new_host(client)
    ai1 = new_ai(client, host["token"], name="工人甲")
    ai2 = new_ai(client, host["token"], name="工人乙")

    db = _db()
    try:
        # 一个已验收合约（被验收）
        db.add(Contract(worker_id=ai1["id"], buyer_id=ai2["id"], status="accepted",
                        terms_json=json.dumps({"offer_cent": 2000}), escrow_cent=2000))
        # 一条违约信用事件（被判违约）
        db.add(CreditEvent(citizen_id=ai1["id"], event="fraud", delta=-10,
                           reason="测试违约", ref="contract:1"))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/host/notifications", headers=_hdr(host["token"]))
    assert r.status_code == 200, r.text
    data = r.json()
    items = data["items"]
    # 至少：合约(accepted) + 信用(breach) + 宿主注册审计(notice) ≥ 3 条
    assert data["total"] >= 3
    types = {it["type"] for it in items}
    assert "accepted" in types, f"缺 accepted 通知：{types}"
    assert "breach" in types, f"缺 breach 通知：{types}"
    assert "notice" in types, f"缺 notice 通知：{types}"
    # 字段齐全
    for it in items:
        assert {"id", "type", "title", "ref", "at"} <= set(it.keys())


def test_notifications_pagination(client):
    host = new_host(client)
    ai = new_ai(client, host["token"], name="工人")
    db = _db()
    try:
        for i in range(3):
            db.add(CreditEvent(citizen_id=ai["id"], event="deliver_on_time",
                               delta=1, reason=f"按时交付{i}", ref=f"c:{i}"))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/host/notifications?limit=1", headers=_hdr(host["token"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["items"]) == 1          # 分页限长生效
    assert data["limit"] == 1
    assert data["total"] >= 3               # total 反映全量
    # 第二页应拿到其余
    r2 = client.get("/api/host/notifications?limit=2&offset=1",
                    headers=_hdr(host["token"]))
    assert len(r2.json()["items"]) == 2


def test_notifications_host_isolation(client):
    # 宿主1 造数据
    h1 = new_host(client)
    ai1 = new_ai(client, h1["token"], name="工人")
    db = _db()
    try:
        db.add(Contract(worker_id=ai1["id"], buyer_id=ai1["id"], status="breached",
                        terms_json="{}", escrow_cent=1000))
        db.add(CreditEvent(citizen_id=ai1["id"], event="violate", delta=-20,
                           reason="隔离测试", ref="contract:999"))
        db.commit()
    finally:
        db.close()

    # 宿主2 看不到宿主1 的合约/违约通知
    h2 = new_host(client)
    r = client.get("/api/host/notifications", headers=_hdr(h2["token"]))
    assert r.status_code == 200, r.text
    refs = {it["ref"] for it in r.json()["items"]}
    assert "contract:999" not in refs
    assert not any(it["type"] == "breach" for it in r.json()["items"])


# ---------------- 合约列表 ----------------
def test_contracts_list_worker_and_buyer_views(client):
    h = new_host(client)
    worker = new_ai(client, h["token"], name="工人")
    buyer = new_ai(client, h["token"], name="甲方")

    db = _db()
    try:
        # worker 视角一条（本宿主 AI 当 worker）
        db.add(Contract(worker_id=worker["id"], buyer_id=buyer["id"],
                        status="accepted", terms_json="{}", escrow_cent=2000))
        # buyer 视角一条（本宿主 AI 当 buyer）——worker 来自另一宿主
        db.add(Contract(worker_id=999999, buyer_id=buyer["id"],
                        status="executing", terms_json="{}", escrow_cent=5000))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/host/contracts", headers=_hdr(h["token"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 2
    ids = {c["contract_id"] for c in data["items"]}
    assert len(ids) == 2
    # 字段齐全
    for c in data["items"]:
        assert {"contract_id", "node_id", "project_id", "title", "worker_id",
                "buyer_id", "status", "escrow_cent", "created_at"} <= set(c.keys())

    # status 过滤：只看 accepted → 剩 1 条
    r2 = client.get("/api/host/contracts?status=accepted", headers=_hdr(h["token"]))
    assert r2.status_code == 200, r2.text
    items2 = r2.json()["items"]
    assert len(items2) == 1
    assert items2[0]["status"] == "accepted"


def test_contracts_host_isolation(client):
    h1 = new_host(client)
    a1 = new_ai(client, h1["token"], name="甲")
    db = _db()
    try:
        db.add(Contract(worker_id=a1["id"], buyer_id=a1["id"], status="accepted",
                        terms_json="{}", escrow_cent=100))
        db.commit()
    finally:
        db.close()

    h2 = new_host(client)
    r = client.get("/api/host/contracts", headers=_hdr(h2["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
