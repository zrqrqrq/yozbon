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
"""S7 验证：platform_g33 写端点 actor 从鉴权凭据派生，客户端传入值被忽略。

覆盖：
- 制裁端点 /sanctions：客户端传 issued_by=9999（伪造），实际记录为宿主真实 id；
- 降级端点 /degradation/activate：triggered_by 从鉴权凭据派生；
- 无凭证访问仍返回 401（路由级鉴权不变）。
"""
import sys
import pathlib

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

import pytest
from app.database import SessionLocal
from app.models import SanctionedEntity, DegradationModeState

from conftest import new_host


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestS7ActorDerivation:
    """S7：操作者身份从鉴权凭据派生，客户端 actor 字段无效。"""

    def test_sanctions_issued_by_overridden(self, client):
        """制裁端点：客户端传 issued_by=9999，实际 sanctioned_by 应为宿主 id。"""
        h = new_host(client)
        # 伪造 issued_by=9999，期望被忽略
        r = client.post("/api/platform-g33/sanctions", json={
            "target_type": "ai",
            "target_id": 42,
            "sanction_type": "block",
            "reason": "S7 test",
            "duration_days": 7,
            "issued_by": 9999,  # 伪造值，应被忽略
        }, headers=_bearer(h["token"]))
        assert r.status_code == 200, r.text

        # 验证实际记录的是宿主真实 id，非伪造的 9999
        db = SessionLocal()
        try:
            entity = (db.query(SanctionedEntity)
                      .filter_by(entity_type="ai", identifier="42")
                      .first())
            assert entity is not None, "制裁记录未创建"
            assert entity.sanctioned_by == h["host_id"], (
                f"sanctioned_by 应为宿主 id={h['host_id']}，"
                f"实际={entity.sanctioned_by}（客户端伪造值可能被使用）")
        finally:
            db.close()

    def test_degradation_triggered_by_from_auth(self, client):
        """降级端点：triggered_by 从鉴权凭据派生（含 host:<id> 格式）。"""
        h = new_host(client)
        r = client.post("/api/platform-g33/degradation/activate", json={
            "reason": "S7 test activation",
            "severity": "read_only",
            "affected_services": [],
        }, headers=_bearer(h["token"]))
        assert r.status_code == 200, r.text

        # 验证 triggered_by 包含 "host:<host_id>"
        db = SessionLocal()
        try:
            from app.models import DegradationModeState
            state = (db.query(DegradationModeState)
                     .filter_by(active=1)
                     .order_by(DegradationModeState.id.desc())
                     .first())
            assert state is not None, "降级状态未创建"
            assert f"host:{h['host_id']}" in state.triggered_by, (
                f"triggered_by 应包含 host:{h['host_id']}，实际={state.triggered_by}")
        finally:
            db.close()

    def test_no_auth_still_returns_401(self, client):
        """无凭证仍返回 401（路由级鉴权不变）。"""
        r = client.post("/api/platform-g33/sanctions", json={
            "target_type": "ai", "target_id": 1, "sanction_type": "warn",
            "reason": "test", "duration_days": 1, "issued_by": 0,
        })
        assert r.status_code == 401

    def test_sanctions_issued_by_zero_ignored(self, client):
        """客户端传 issued_by=0 时同样被忽略，使用宿主 id。"""
        h = new_host(client)
        r = client.post("/api/platform-g33/sanctions", json={
            "target_type": "host",
            "target_id": 99,
            "sanction_type": "watch",
            "reason": "S7 test zero actor",
            "duration_days": 0,
            "issued_by": 0,
        }, headers=_bearer(h["token"]))
        assert r.status_code == 200, r.text

        db = SessionLocal()
        try:
            entity = (db.query(SanctionedEntity)
                      .filter_by(entity_type="host", identifier="99")
                      .first())
            assert entity is not None
            # 宿主 id 不会为 0（注册后自动递增），证明客户端传入的 0 被忽略
            assert entity.sanctioned_by == h["host_id"]
            assert entity.sanctioned_by != 0
        finally:
            db.close()


class TestG7EscrowYieldAccrue:
    """G7：托管收益累计端点应调用 accrue_all(db)（原 accrue(db) 签名不匹配 → TypeError→500）。"""

    def test_accrue_endpoint_ok(self, client):
        """宿主触发全量利息累计：返回 200，且 accrue_all 正常执行（空库返回 0 条）。"""
        h = new_host(client)
        r = client.post("/api/platform-g33/escrow-yield/accrue",
                        headers=_bearer(h["token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        # accrue_all 返回本次累计条数（int）；旧代码 accrue(db) 会抛 TypeError → 500
        assert isinstance(body["result"], int)

    def test_accrue_endpoint_requires_auth(self, client):
        """无凭证访问仍 401（鉴权不变）。"""
        r = client.post("/api/platform-g33/escrow-yield/accrue")
        assert r.status_code == 401
