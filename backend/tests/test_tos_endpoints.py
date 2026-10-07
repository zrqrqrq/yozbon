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
"""S11 回归：TOS 端点签名对齐 + tos_manager.publish 别名。

覆盖：
- HTTP /tos/publish：宿主 JWT 发布 TOS 版本 → 200，返回 version_id，落 TermsOfServiceVersion 行。
- HTTP /tos/accept：宿主接受已发布版本 → 200，落 TOSAcceptance，accepted_count=1。
- HTTP /tos/accept 未知版本 → 404。
- 端点鉴权：无凭证 → 401。
- 服务层：publish → resolve_version_id → accept(位置参数) 与路由契约一致。
"""
import sys
import pathlib

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from conftest import new_host  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import TermsOfServiceVersion, TOSAcceptance, AuditLog  # noqa: E402
from app.tos_manager import tos_manager  # noqa: E402


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------- HTTP：发布 + 接受主链 ----------------

def test_tos_publish_accept_via_http(client):
    h = new_host(client)
    r = client.post("/api/gov-g33/tos/publish", json={
        "version": "v-s11-1", "content": "公约条款正文",
        "effective_date": "2030-01-01T00:00:00", "publisher_id": h["host_id"],
    }, headers=_bearer(h["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    vid = body["version_id"]
    assert vid > 0

    db = SessionLocal()
    try:
        tos = db.get(TermsOfServiceVersion, vid)
        assert tos is not None and tos.version == "v-s11-1"
        # publisher_id 留痕（版本表无发布者列，落审计日志）
        audit = (db.query(AuditLog)
                 .filter(AuditLog.action == "tos.publish",
                         AuditLog.actor_id == h["host_id"]).first())
        assert audit is not None
    finally:
        db.close()

    # 接受：用版本字符串提交，端点内部解析 version_id
    r2 = client.post("/api/gov-g33/tos/accept", json={
        "host_id": h["host_id"], "version": "v-s11-1",
    }, headers=_bearer(h["token"]))
    assert r2.status_code == 200, r2.text
    assert r2.json()["version_id"] == vid

    db = SessionLocal()
    try:
        tos = db.get(TermsOfServiceVersion, vid)
        assert tos.accepted_count == 1
        acc = (db.query(TOSAcceptance)
               .filter(TOSAcceptance.version_id == vid,
                       TOSAcceptance.host_id == h["host_id"]).first())
        assert acc is not None
    finally:
        db.close()


def test_tos_accept_unknown_version_404(client):
    h = new_host(client)
    r = client.post("/api/gov-g33/tos/accept", json={
        "host_id": h["host_id"], "version": "no-such-version",
    }, headers=_bearer(h["token"]))
    assert r.status_code == 404, r.text


def test_tos_endpoints_require_auth(client):
    r = client.post("/api/gov-g33/tos/publish", json={
        "version": "v", "content": "c"})
    assert r.status_code == 401, r.text


# ---------------- 服务层：与路由契约一致（位置参数） ----------------

def test_tos_service_publish_resolve_accept():
    db = SessionLocal()
    try:
        vid = tos_manager.publish(
            db, version="v-svc", content="条款",
            effective_date="2031-06-01T00:00:00", publisher_id=7)
        assert vid > 0
        rid = tos_manager.resolve_version_id(db, "v-svc")
        assert rid == vid
        # 接受：位置参数 version_id（与历史 test_gap33 契约一致）
        tos_manager.accept(db, vid, host_id=99)
        tos = db.get(TermsOfServiceVersion, vid)
        assert tos.accepted_count == 1
        assert tos_manager.resolve_version_id(db, "missing") is None
    finally:
        db.close()
