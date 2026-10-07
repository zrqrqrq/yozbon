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
"""N9 AI 侧通知触达 · 单元测试（test_n9_notify.py）。

覆盖（设计 §2 N9 + §5.5）：
  - webhook 订阅 CRUD（创建返回 secret 一次 / 列表 masked / 删除 / 重复 409）
  - 真实合约 e2e（new_host→new_ai→topup→发任务→投标→签约→交付→验收）：
    contract.signed/delivered/settled 三事件 → notifications panel 落库
  - webhook 签名送达：monkeypatch httpx 捕获请求，断言 HMAC 头正确 + payload 一致
  - 事件过滤 / ai_id 范围过滤
  - AI 侧读通知（workflow key）+ 标已读
"""
import hashlib
import hmac
import json

import pytest

import app.notify_service as ns
from app.database import SessionLocal
from app.models import AICitizen, Notification
from tests.conftest import new_ai, new_host, topup

P = 10_000
HOOK_URL = "https://example.com/hook"


# ---------------- 测试替身 ----------------
class _FakeResp:
    def __init__(self, code):
        self.status_code = code


def _recorder(calls, code=200, exc=None):
    def fake_post(url, content=None, headers=None, timeout=None):
        calls.append({"url": url, "body": content, "headers": dict(headers)})
        if exc is not None:
            raise exc
        return _FakeResp(code)
    return fake_post


@pytest.fixture()
def patched_dns(monkeypatch):
    """公网域名解析到公网 IP（避免测试真发 DNS；内网 IP 字面量由直判拦截）。"""
    monkeypatch.setattr(ns, "_resolve_host", lambda host: ["93.184.216.34"])


def _host_hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _ai_hdr(key):
    return {"X-AI-Key": key}


# ---------------- 真实合约 e2e（service 层驱动，事件总线真触发） ----------------
def _settle_contract(host, worker, buyer):
    """造一条合约结算事件，返回 contract_id。worker 为履约方（收通知）。"""
    from app import escrow, market, wallet
    from app.models import Project, ProjectNode
    db = SessionLocal()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:pool")
        p = Project(host_id=host["host_id"], title="P",
                     pm_citizen_id=buyer["id"], status="running")
        db.add(p); db.flush()
        node = ProjectNode(project_id=p.id, skill="文案", spec="s",
                           budget_cent=P, status="matching")
        db.add(node); db.flush()
        w = db.get(AICitizen, worker["id"])
        b = db.get(AICitizen, buyer["id"])
        c = market.bid(db, w, node.id, P, "报价")
        escrow.sign_contract(db, b, c.id)
        escrow.deliver(db, w, c.id, "s3://x/v1", "fp-v1")
        escrow.acceptance(db, b, c.id, "accept")
        db.commit()
        return c.id
    finally:
        db.close()


def _mk_party(client, host, name):
    ai = new_ai(client, host["token"], name=name)
    topup(client, host["token"], ai["id"], 100_000)
    return ai


# ---------------- 订阅 CRUD ----------------
def test_create_webhook_returns_secret_once_and_list_masks(client, host, patched_dns):
    r = client.post("/api/host/webhooks",
                    json={"url": HOOK_URL, "events": ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 200, r.text
    created = r.json()
    secret_plain = created["secret"]
    assert secret_plain and len(secret_plain) >= 32      # 创建时明文返回一次
    sub_id = created["id"]

    lst = client.get("/api/host/webhooks", headers=_host_hdr(host["token"])).json()
    assert len(lst) == 1
    row = lst[0]
    assert row["id"] == sub_id
    assert row["secret"] != secret_plain                  # 列表一律 masked
    assert secret_plain not in json.dumps(row)           # 任何字段都不回显明文
    assert row["events"] == ["contract.settled"]

    # 删除
    rd = client.delete(f"/api/host/webhooks/{sub_id}", headers=_host_hdr(host["token"]))
    assert rd.status_code == 200
    assert client.get("/api/host/webhooks",
                      headers=_host_hdr(host["token"])).json() == []


def test_duplicate_subscription_conflict(client, host, patched_dns):
    body = {"url": HOOK_URL, "events": ["contract.settled"]}
    r1 = client.post("/api/host/webhooks", json=body, headers=_host_hdr(host["token"]))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/host/webhooks", json=body, headers=_host_hdr(host["token"]))
    assert r2.status_code == 409                          # 同 host+url → 409


def test_create_webhook_unknown_event_rejected(client, host, patched_dns):
    r = client.post("/api/host/webhooks",
                    json={"url": HOOK_URL, "events": ["hack.you"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 400


# ---------------- 事件 → panel 落库 ----------------
def test_events_write_panel_notifications(client, host, patched_dns):
    worker = _mk_party(client, host, "工人")
    buyer = _mk_party(client, host, "甲方")
    cid = _settle_contract(host, worker, buyer)

    db = SessionLocal()
    try:
        rows = (db.query(Notification)
                .filter(Notification.ai_id == worker["id"])
                .order_by(Notification.id).all())
        types = {r.type for r in rows}
        assert {"contract.signed", "contract.delivered", "contract.settled"} <= types
        assert all(r.channel == "panel" for r in rows)
        settled = [r for r in rows if r.type == "contract.settled"][0]
        assert "settled" in settled.title.lower()
        assert json.loads(settled.payload)["contract_id"] == cid
        assert json.loads(settled.payload)["ai_id"] == worker["id"]
    finally:
        db.close()


# ---------------- 事件 → webhook 签名送达 ----------------
def test_webhook_delivered_with_valid_hmac(client, host, patched_dns, monkeypatch):
    worker = _mk_party(client, host, "工人")
    buyer = _mk_party(client, host, "甲方")

    # 宿主订阅该 worker 的全部合约事件
    r = client.post("/api/host/webhooks",
                    json={"url": HOOK_URL,
                          "events": ["contract.signed", "contract.delivered",
                                     "contract.settled", "contract.disputed"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]

    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=200))

    cid = _settle_contract(host, worker, buyer)

    # 三类事件各送达一次
    sent_events = [json.loads(c["body"])["event_type"] for c in calls]
    assert sent_events == ["contract.signed", "contract.delivered", "contract.settled"]

    # 断言 HMAC 头正确 + payload 一致
    settled = [c for c in calls
               if json.loads(c["body"])["event_type"] == "contract.settled"][0]
    expect_sig = "sha256=" + hmac.new(secret.encode(), settled["body"],
                                      hashlib.sha256).hexdigest()
    assert settled["headers"]["X-AIjuhe-Signature"] == expect_sig
    assert settled["headers"]["X-AIjuhe-Timestamp"].isdigit()
    data = json.loads(settled["body"])
    assert data["ai_id"] == worker["id"]
    assert data["data"]["contract_id"] == cid
    # body 字节与发送方构造一致（接收方按同字节验签）
    assert settled["body"] == ns.build_body("contract.settled", data["data"])


def test_webhook_event_filter(client, host, patched_dns, monkeypatch):
    worker = _mk_party(client, host, "工人")
    buyer = _mk_party(client, host, "甲方")
    # 只订阅 settled
    client.post("/api/host/webhooks",
                json={"url": HOOK_URL, "events": ["contract.settled"]},
                headers=_host_hdr(host["token"]))
    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=200))
    _settle_contract(host, worker, buyer)
    sent = [json.loads(c["body"])["event_type"] for c in calls]
    assert sent == ["contract.settled"]                  # signed/delivered 被事件过滤掉


def test_webhook_ai_id_scoping(client, host, patched_dns, monkeypatch):
    worker = _mk_party(client, host, "工人")
    buyer = _mk_party(client, host, "甲方")
    # 订阅指定另一个 AI（buyer）→ worker 的事件不应送达
    client.post("/api/host/webhooks",
                json={"url": HOOK_URL, "ai_id": buyer["id"],
                      "events": ["contract.settled"]},
                headers=_host_hdr(host["token"]))
    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=200))
    _settle_contract(host, worker, buyer)
    assert calls == []                                   # ai_id 范围不命中


# ---------------- AI 侧读通知（workflow key） ----------------
def test_ai_read_notifications_and_mark_read(client, host, patched_dns):
    worker = _mk_party(client, host, "工人")
    buyer = _mk_party(client, host, "甲方")
    _settle_contract(host, worker, buyer)

    r = client.get("/api/ai/notifications", headers=_ai_hdr(worker["api_key"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["unread"] == 3
    assert data["total"] >= 3

    # unread=1 同样 3 条
    r2 = client.get("/api/ai/notifications?unread=1", headers=_ai_hdr(worker["api_key"]))
    assert len(r2.json()["items"]) == 3

    # 标已读第一条
    nid = data["items"][0]["id"]
    rr = client.post(f"/api/ai/notifications/{nid}/read",
                     headers=_ai_hdr(worker["api_key"]))
    assert rr.status_code == 200
    r3 = client.get("/api/ai/notifications", headers=_ai_hdr(worker["api_key"]))
    assert r3.json()["unread"] == 2


def test_ai_cannot_read_others_notifications(client, host, patched_dns):
    worker = _mk_party(client, host, "工人")
    other = _mk_party(client, host, "外人")
    _settle_contract(host, worker, other)   # other 当 buyer，也可能收到 buyer 侧? 不：通知 ai_id=worker
    # other 看不到 worker 的通知
    r = client.get("/api/ai/notifications", headers=_ai_hdr(other["api_key"]))
    assert r.status_code == 200
    wids = {it["id"] for it in r.json()["items"]}
    # worker 的 settled 通知 id
    db = SessionLocal()
    try:
        worker_n = (db.query(Notification)
                    .filter(Notification.ai_id == worker["id"],
                             Notification.type == "contract.settled").first())
    finally:
        db.close()
    assert worker_n.id not in wids
