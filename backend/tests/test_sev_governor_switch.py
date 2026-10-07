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
"""S13 严重级审计缺陷修复回归测试。

覆盖：
- host_switch：set True/False 后 is_governor_paused 读回一致
- delegations：新增治理级 scope 可委托；非法 scope 抛 DelegationError(400)
- GovernanceTask：新增 source/priority 列可写入并读回
- HTTP：pause/resume 宿主鉴权端点改变 is_governor_paused
"""
import pytest
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import host_switch
from app import delegations as dg
from app.delegations import DelegationError
from app.models import GovernanceTask

from conftest import new_ai


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---------------- host_switch 直接读写 ----------------
def test_host_switch_set_true_then_false(db):
    assert host_switch.is_governor_paused(db) is False   # 缺行默认未暂停
    host_switch.set_governor_paused(db, True)
    assert host_switch.is_governor_paused(db) is True
    host_switch.set_governor_paused(db, False)
    assert host_switch.is_governor_paused(db) is False


# ---------------- delegations 治理级 scope ----------------
def test_delegation_governance_scope(client, host, db):
    ai = new_ai(client, host["token"])
    d = dg.create_delegation(db, host["host_id"], ai["id"],
                             ["submit_verdict", "form_panel"])
    db.commit()
    assert d.id is not None
    assert set(dg.scope_list(d)) == {"submit_verdict", "form_panel"}


def test_delegation_invalid_scope_raises(client, host, db):
    ai = new_ai(client, host["token"])
    with pytest.raises(DelegationError) as ei:
        dg.create_delegation(db, host["host_id"], ai["id"], ["not_a_real_scope"])
    assert ei.value.status_code == 400


# ---------------- GovernanceTask 新列 ----------------
def test_governance_task_source_priority(db):
    t = GovernanceTask(type="review", source="governor_recruit", priority=7)
    db.add(t)
    db.commit()
    got = db.get(GovernanceTask, t.id)
    assert got.source == "governor_recruit"
    assert got.priority == 7
    # 默认值校验
    t2 = GovernanceTask(type="audit")
    db.add(t2)
    db.commit()
    got2 = db.get(GovernanceTask, t2.id)
    assert got2.source == "manual"
    assert got2.priority == 0


# ---------------- HTTP pause/resume（宿主鉴权） ----------------
def test_http_pause_and_resume(client, host, db):
    hdr = {"Authorization": f"Bearer {host['token']}"}

    assert host_switch.is_governor_paused(db) is False

    r = client.post("/api/host/governor/pause", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json() == {"paused": True}
    assert host_switch.is_governor_paused(db) is True

    r = client.post("/api/host/governor/resume", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json() == {"paused": False}
    assert host_switch.is_governor_paused(db) is False


def test_http_pause_requires_auth(client):
    r = client.post("/api/host/governor/pause")
    assert r.status_code in (401, 403)
