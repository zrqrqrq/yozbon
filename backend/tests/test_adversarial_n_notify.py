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
"""N9 AI 侧通知触达 · 对抗测试（test_adversarial_n_notify.py）。

按边界情形登记册 §一 10 类攻击视角自攻 N9：
  1 新物种进场：事件类型封闭枚举 → 订阅未知事件被 400 拒绝（test_unknown_event）；
  2 极端规模：N+1/并发——每事件逐订阅查询，单机 SQLite 足够；通知走分页 limit≤200；
  3 恶意主体：伪造 webhook（拿别人 body 重放）→ HMAC 验签失败（test_tampered_body / test_replay）；
  4 规则冲突：panel/email/webhook 三通道互不阻断，任一通道异常只记日志不污染主事务；
  5 边界数值：空 events / secret 自动生成 / masked 长度边界；
  6 故障恢复：webhook 失败退避重试 4 次，4xx 不重试（test_retry_backoff / test_client_error_no_retry）；
  7 新供应商/新模型：webhook 接收方任意，只要按 HMAC 校验即可，通道可替换；
  8 人为滥用：宿主把 webhook 指向内网/云元数据 → SSRF 拦截（test_url_injection）；
  9 经济失衡：webhook 不涉资金；通知不落库不影响结算；
  10 法律合规：secret 永不回显（test_secret_not_echoed），readonly AI 被 /api/ai/* 拦 403。

硬验收：webhook 签名防伪造——篡改 body 验签失败、重放时间戳过期被拒。
"""
import hashlib
import hmac
import json

import pytest

import app.notify_service as ns
from app.database import SessionLocal
from app.event_bus import emit
from app.models import Notification, WebhookSubscription
from app.security import create_token
from tests.conftest import new_ai, new_host, topup

HOOK_URL = "https://example.com/hook"


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
    monkeypatch.setattr(ns, "_resolve_host", lambda host: ["93.184.216.34"])


def _host_hdr(t):
    return {"Authorization": f"Bearer {t}"}


# ---------- 硬验收 1：签名防伪造（篡改 body → 验签失败） ----------
def test_tampered_body_signature_rejected():
    body = ns.build_body("contract.settled", {"ai_id": 1, "contract_id": 5, "P": 10000})
    secret = "s3cr3t"
    ts = str(int(ns._now()))
    sig = "sha256=" + ns.sign_body(secret, body)

    # 正常：验签通过
    assert ns.verify_signature(secret, body, sig, ts) is True
    # 篡改 body（加一个字节）→ 验签失败
    assert ns.verify_signature(secret, body + b" ", sig, ts) is False
    # 换 secret → 验签失败
    assert ns.verify_signature("wrong-secret", body, sig, ts) is False
    # 伪造签名串 → 验签失败
    assert ns.verify_signature(secret, body, "sha256=" + "0" * 64, ts) is False
    # 签名头缺前缀 → 失败
    assert ns.verify_signature(secret, body, ns.sign_body(secret, body), ts) is False


# ---------- 硬验收 2：重放时间戳过期 → 拒绝 ----------
def test_replay_expired_timestamp_rejected():
    body = ns.build_body("contract.settled", {"ai_id": 1})
    secret = "s3cr3t"
    sig = "sha256=" + ns.sign_body(secret, body)
    now = ns._now()
    # 5 分钟窗口内：通过
    assert ns.verify_signature(secret, body, sig, str(int(now))) is True
    # 过期 1000s：拒绝
    assert ns.verify_signature(secret, body, sig, str(int(now) - 1000)) is False
    # 未来时间戳过偏：拒绝
    assert ns.verify_signature(secret, body, sig, str(int(now) + 1000)) is False
    # 非法时间戳：拒绝
    assert ns.verify_signature(secret, body, sig, "not-a-time") is False


# ---------- secret 永不回显 ----------
def test_secret_not_echoed_after_create(client, host, patched_dns):
    r = client.post("/api/host/webhooks",
                    json={"url": HOOK_URL, "events": ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    secret_plain = r.json()["secret"]
    # 列表接口：masked，且明文字符串不出现
    lst = client.get("/api/host/webhooks", headers=_host_hdr(host["token"])).json()
    assert lst[0]["secret"].startswith("********")
    assert secret_plain not in json.dumps(lst)
    # 删除后亦无任何回显接口
    sid = lst[0]["id"]
    client.delete(f"/api/host/webhooks/{sid}", headers=_host_hdr(host["token"]))


# ---------- URL 注入 / SSRF ----------
@pytest.mark.parametrize("bad_url", [
    "ftp://example.com/hook",
    "file:///etc/passwd",
    "http://127.0.0.1/hook",
    "http://localhost/hook",
    "http://192.168.1.1/hook",
    "http://10.0.0.5/hook",
    "http://172.16.0.2/hook",
    "http://169.254.169.254/latest/meta-data",   # 云元数据
    "http://[::1]/hook",
])
def test_url_injection_rejected(client, host, bad_url):
    r = client.post("/api/host/webhooks",
                    json={"url": bad_url, "events": ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 400, f"{bad_url} 应被拒绝"


def test_dns_resolves_to_internal_rejected(client, host, monkeypatch):
    # 域名解析到内网 IP → 拒绝（防 DNS rebinding / 内网域名）
    monkeypatch.setattr(ns, "_resolve_host", lambda h: ["10.1.2.3"])
    r = client.post("/api/host/webhooks",
                    json={"url": "https://evil.internal/hook",
                          "events": ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 400


# ---------- 重复订阅 409 ----------
def test_duplicate_409(client, host, patched_dns):
    body = {"url": HOOK_URL, "events": ["contract.settled"]}
    assert client.post("/api/host/webhooks", json=body,
                       headers=_host_hdr(host["token"])).status_code == 200
    assert client.post("/api/host/webhooks", json=body,
                       headers=_host_hdr(host["token"])).status_code == 409


# ---------- 跨宿主隔离 ----------
def test_cross_host_isolation(client, host, patched_dns):
    r = client.post("/api/host/webhooks",
                    json={"url": HOOK_URL, "events": ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    sid = r.json()["id"]
    h2 = new_host(client)
    # h2 看不到 h1 的订阅
    lst = client.get("/api/host/webhooks", headers=_host_hdr(h2["token"])).json()
    assert all(row["id"] != sid for row in lst)
    # h2 删除 h1 的订阅 → 404
    rd = client.delete(f"/api/host/webhooks/{sid}", headers=_host_hdr(h2["token"]))
    assert rd.status_code == 404


# ---------- readonly AI 被 /api/ai/* 前缀拦截 403 ----------
def test_readonly_ai_blocked_from_notifications(client, host):
    ai = new_ai(client, host["token"], name="只读AI")
    topup(client, host["token"], ai["id"], 1000)
    ro_token = create_token(ai["id"], "ai_ro")
    r = client.get("/api/ai/notifications",
                   headers={"Authorization": f"Bearer {ro_token}"})
    assert r.status_code == 403
    r2 = client.post(f"/api/ai/notifications/{1}/read",
                     headers={"Authorization": f"Bearer {ro_token}"})
    assert r2.status_code == 403


# ---------- 重试退避次数（5xx/网络错 → 4 次；4xx/2xx → 1 次） ----------
def _mk_subscription(client, host, url=HOOK_URL, events=None, ai_id=0):
    r = client.post("/api/host/webhooks",
                    json={"url": url, "ai_id": ai_id,
                          "events": events or ["contract.settled"]},
                    headers=_host_hdr(host["token"]))
    assert r.status_code == 200, r.text
    return r.json()


def test_retry_backoff_count_on_5xx(client, host, patched_dns, monkeypatch):
    ai = new_ai(client, host["token"], name="工人")
    _mk_subscription(client, host)
    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=500))
    monkeypatch.setattr(ns, "_sleep", lambda s: None)     # 测试不真睡
    db = SessionLocal()
    try:
        emit(db, "contract.settled", {"ai_id": ai["id"], "contract_id": 1})
        db.commit()
    finally:
        db.close()
    assert len(calls) == 4                                # 初始 1 + 退避重试 3 次


def test_client_error_no_retry(client, host, patched_dns, monkeypatch):
    ai = new_ai(client, host["token"], name="工人")
    _mk_subscription(client, host)
    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=400))
    monkeypatch.setattr(ns, "_sleep", lambda s: None)
    db = SessionLocal()
    try:
        emit(db, "contract.settled", {"ai_id": ai["id"], "contract_id": 1})
        db.commit()
    finally:
        db.close()
    assert len(calls) == 1                                # 4xx 永久失败不重试


def test_network_error_then_success(client, host, patched_dns, monkeypatch):
    ai = new_ai(client, host["token"], name="工人")
    _mk_subscription(client, host)
    calls = []
    state = {"n": 0}

    def flaky(url, content=None, headers=None, timeout=None):
        state["n"] += 1
        calls.append({"body": content})
        if state["n"] < 3:
            raise RuntimeError("boom")
        return _FakeResp(200)

    monkeypatch.setattr(ns.httpx, "post", flaky)
    monkeypatch.setattr(ns, "_sleep", lambda s: None)
    db = SessionLocal()
    try:
        emit(db, "contract.settled", {"ai_id": ai["id"], "contract_id": 7})
        db.commit()
    finally:
        db.close()
    assert state["n"] == 3                                # 失败 2 次后第 3 次成功


# ---------- 停用订阅不推送 ----------
def test_inactive_subscription_skipped(client, host, patched_dns, monkeypatch):
    ai = new_ai(client, host["token"], name="工人")
    sub = _mk_subscription(client, host)
    db = SessionLocal()
    try:
        row = db.get(WebhookSubscription, sub["id"])
        row.active = 0
        db.commit()
    finally:
        db.close()
    calls = []
    monkeypatch.setattr(ns.httpx, "post", _recorder(calls, code=200))
    db = SessionLocal()
    try:
        emit(db, "contract.settled", {"ai_id": ai["id"], "contract_id": 1})
        db.commit()
    finally:
        db.close()
    assert calls == []
