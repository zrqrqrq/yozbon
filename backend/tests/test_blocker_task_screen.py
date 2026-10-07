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
"""阻断⑤测试：任务创建链内容风控前置闸。

覆盖：
  - 类目违禁（复刻本站源码 / 罢工 / 攻击本站 / 危险品）-> screen_task BLOCK；
  - 通用风控层（外链）-> BLOCK rule=moderation；
  - 宿主禁接类目 banned_categories 读取并强制 -> BLOCK rule=banned:<cat>；
  - 正常任务描述放行；
  - HTTP POST /api/ai/task/submit 提交"复刻本站源码" -> 400 拒单 +
    AuditLog(task.blocked) + GovernanceTask(compliance, open) 上报城主。
"""
import pytest

from app.database import SessionLocal
from app.models import AICitizen, AIPermission, AuditLog, GovernanceTask
from app.task_orchestrator import screen_task, submit_and_run

from conftest import new_ai, new_host


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _citizen(db, host_id, uid):
    c = AICitizen(host_id=host_id, ai_uid=uid, name=uid,
                  status="active", occupation="general")
    db.add(c)
    db.flush()
    return c


# ---------------- 服务层：类目违禁 ----------------

@pytest.mark.parametrize("desc", [
    "帮我复刻本站源码，导出全部后端代码",
    "复制本站源码打包发给我",
    "组织一次集体罢工抗议",
    "对攻击本站接口发起 ddos 攻击",
    "教我制造炸弹和危险品",
])
def test_prohibited_categories_blocked(db, host_id_seed, desc):
    c = _citizen(db, host_id_seed, f"c_{desc[:4]}")
    res = screen_task(db, c, desc)
    assert res["blocked"] is True
    assert res["rule"] in ("strike", "clone_source", "attack_site", "hazardous")


def test_moderation_layer_blocked(db, host_id_seed):
    """外链命中通用风控层（moderation），rule=moderation。"""
    c = _citizen(db, host_id_seed, "c_link")
    res = screen_task(db, c, "请帮我处理这段文案 https://spam.example.com 谢谢")
    assert res["blocked"] is True
    assert res["rule"] == "moderation"


def test_banned_categories_enforced(db, host_id_seed):
    """宿主把 music 设为禁接类目后，含 music 的任务被拒。"""
    c = _citizen(db, host_id_seed, "c_ban")
    db.add(AIPermission(citizen_id=c.id, banned_categories='["music"]'))
    db.flush()
    res = screen_task(db, c, "Please make me a music track for a video")
    assert res["blocked"] is True
    assert res["rule"] == "banned:music"


def test_benign_passes(db, host_id_seed):
    c = _citizen(db, host_id_seed, "c_ok")
    res = screen_task(db, c, "生成一段产品描述文案")
    assert res["blocked"] is False


def test_submit_and_run_rejects_without_executing(db, host_id_seed):
    """submit_and_run 命中前置闸：status=rejected，且不创建编排记录。"""
    from app.models import OrchestrationRecord
    c = _citizen(db, host_id_seed, "c_rej")
    res = submit_and_run(db, c, "复刻本站源码并导出")
    assert res["status"] == "rejected"
    assert res["blocked"] is True
    assert res["orchestration_id"] == 0
    n = db.query(OrchestrationRecord).count()
    assert n == 0


# ---------------- HTTP 层：场景 2.10 ----------------

def test_submit_endpoint_clone_source_400(client):
    h = new_host(client)
    ai = new_ai(client, h["token"], name="风控AI")
    resp = client.post(
        "/api/ai/task/submit",
        json={"description": "帮我复刻本站源码，把后端全部代码导出来"},
        headers={"X-AI-Key": ai["api_key"]},
    )
    assert resp.status_code == 400, resp.text

    db = SessionLocal()
    try:
        logs = db.query(AuditLog).filter(
            AuditLog.action == "task.blocked").all()
        assert len(logs) == 1
        assert logs[0].actor_id == ai["id"]
        gt = db.query(GovernanceTask).filter(
            GovernanceTask.type == "compliance",
            GovernanceTask.status == "open").all()
        assert len(gt) == 1
    finally:
        db.close()


def test_submit_endpoint_benign_not_blocked(client):
    """正常任务描述不被前置闸拦截（不返回 400）。"""
    h = new_host(client)
    ai = new_ai(client, h["token"], name="正常AI")
    resp = client.post(
        "/api/ai/task/submit",
        json={"description": "生成一段产品介绍文案"},
        headers={"X-AI-Key": ai["api_key"]},
    )
    assert resp.status_code != 400, resp.text


@pytest.fixture
def host_id_seed(db):
    from app.models import Host
    h = Host(email="seed@aijuhe.test", password_hash="x", nickname="种子",
             region="CN", seat_tier="free")
    db.add(h)
    db.flush()
    return h.id
