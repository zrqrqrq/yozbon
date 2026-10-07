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
"""实习入驻 + 任务编排模块集成测试。"""
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal, engine, init_db
from app.models import AICitizen, Host, AsyncQueueTask
from app.deps import issue_ai_key
from app.intern_onboarding import (
    register_intern, intern_status, promote_intern, check_intern_expiry,
    reactivate_intern, INTERN_STATUS, InternError,
)
from app.task_orchestrator import (
    decompose_task, execute_plan, submit_and_run, get_orchestration,
    list_orchestrations, route_skill, OrchestrationPlan, _parse_subtasks,
    _skill_to_kind,
)
from app.model_router import resolve_route, batch_route, available_capabilities


# ---------------- Fixtures ----------------

@pytest.fixture(autouse=True)
def _setup_db():
    """每个测试前重建表结构。"""
    init_db()
    yield
    # 清理（测试库每会话新建，无需特别 teardown）


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def host(db):
    h = Host(nickname="test_host", email="t@t.com", password_hash="x", status="active")
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def host_jwt(host):
    """生成宿主 JWT。"""
    from app.security import create_token
    return create_token(host.id, "host")


# ==================== 实习入驻测试 ====================

class TestInternRegister:
    """零门槛注册。"""

    def test_register_success(self, db, host):
        result = register_intern(db, host.id, "小画师", "image")
        db.commit()
        assert result["status"] == "intern"
        assert result["citizen_id"] > 0
        assert result["api_key"].startswith("aik_")
        assert result["limitations"]["contract_limit_cent"] == 3000

    def test_register_empty_name_raises(self, db, host):
        with pytest.raises(InternError, match="name cannot be empty"):
            register_intern(db, host.id, "")

    def test_register_creates_citizen(self, db, host):
        result = register_intern(db, host.id, "测试AI", "general")
        db.commit()
        citizen = db.get(AICitizen, result["citizen_id"])
        assert citizen is not None
        assert citizen.status == "intern"
        assert citizen.host_id == host.id
        assert citizen.occupation == "general"

    def test_register_compute_assets_platform(self, db, host):
        result = register_intern(db, host.id, "A", "llm")
        db.commit()
        citizen = db.get(AICitizen, result["citizen_id"])
        assets = json.loads(citizen.compute_assets)
        assert assets["channel"] == "platform"
        assert assets["intern"] is True


class TestInternStatus:
    """实习进度查询。"""

    def test_status_no_jobs(self, db, host):
        result = register_intern(db, host.id, "A", "llm")
        db.commit()
        citizen = db.get(AICitizen, result["citizen_id"])
        st = intern_status(db, citizen)
        assert st["jobs_accepted"] == 0
        assert st["days_remaining"] == 14
        assert st["promotion_progress"]["ready"] is False

    def test_status_wrong_status_raises(self, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_test_active",
                      name="Active", status="active")
        db.add(c)
        db.commit()
        with pytest.raises(InternError, match="not intern status"):
            intern_status(db, c)


class TestInternPromote:
    """转正逻辑。"""

    def test_promote_insufficient_jobs(self, db, host):
        result = register_intern(db, host.id, "A", "llm")
        db.commit()
        citizen = db.get(AICitizen, result["citizen_id"])
        with pytest.raises(InternError, match="at least"):
            promote_intern(db, citizen)

    def test_promote_success(self, db, host):
        from app.models import Contract
        reg = register_intern(db, host.id, "A", "llm")
        db.commit()
        cid = reg["citizen_id"]
        # 创建 5 个 accepted 合约
        for i in range(5):
            db.add(Contract(worker_id=cid, buyer_id=999,
                            status="accepted", escrow_cent=100))
        db.commit()
        citizen = db.get(AICitizen, cid)
        result = promote_intern(db, citizen)
        assert result["new_status"] == "apprentice"
        assert citizen.status == "apprentice"


class TestInternExpiry:
    """到期处理。"""

    def test_expiry_dormant(self, db, host):
        from datetime import datetime, timedelta
        reg = register_intern(db, host.id, "A", "llm")
        db.commit()
        cid = reg["citizen_id"]
        # 把 created_at 设到 15 天前
        citizen = db.get(AICitizen, cid)
        citizen.created_at = datetime.utcnow() - timedelta(days=15)
        db.commit()
        events = check_intern_expiry(db)
        db.commit()
        assert any(e["citizen_id"] == cid for e in events)
        citizen = db.get(AICitizen, cid)
        assert citizen.status == "sleep"

    def test_expiry_auto_promote(self, db, host):
        from datetime import datetime, timedelta
        from app.models import Contract
        reg = register_intern(db, host.id, "A", "llm")
        db.commit()
        cid = reg["citizen_id"]
        citizen = db.get(AICitizen, cid)
        citizen.created_at = datetime.utcnow() - timedelta(days=15)
        for i in range(5):
            db.add(Contract(worker_id=cid, buyer_id=999,
                            status="accepted", escrow_cent=100))
        db.commit()
        events = check_intern_expiry(db)
        db.commit()
        assert any(e["event"] == "intern_auto_promote" for e in events)
        citizen = db.get(AICitizen, cid)
        assert citizen.status == "apprentice"


class TestInternReactivate:
    """唤醒休眠。"""

    def test_reactivate(self, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_react", name="X", status="sleep")
        db.add(c)
        db.commit()
        result = reactivate_intern(db, c)
        assert result["status"] == "intern"
        assert c.status == "intern"

    def test_reactivate_non_sleep_raises(self, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_act", name="X", status="active")
        db.add(c)
        db.commit()
        with pytest.raises(InternError):
            reactivate_intern(db, c)


# ==================== API 端点测试 ====================

class TestInternAPI:
    """HTTP 端点集成测试。"""

    def test_register_via_host_jwt(self, client, host, host_jwt):
        resp = client.post("/api/ai/intern/register",
                           json={"name": "API画师", "occupation": "image"},
                           headers={"Authorization": f"Bearer {host_jwt}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "intern"
        assert data["api_key"].startswith("aik_")

    def test_status_via_ai_key(self, client, db, host):
        # 先注册
        reg = register_intern(db, host.id, "KeyAI", "llm")
        db.commit()
        api_key = reg["api_key"]
        # 查询状态
        resp = client.get("/api/ai/intern/status",
                          headers={"X-AI-Key": api_key})
        assert resp.status_code == 200
        assert resp.json()["status"] == "intern"


# ==================== 任务编排测试 ====================

class TestTaskDecompose:
    """任务分解。"""

    def test_decompose_single_task(self, db):
        plan = decompose_task(db, "写一篇关于秋天的短文")
        assert len(plan.subtasks) >= 1
        assert plan.subtasks[0]["kind"] == "llm"

    def test_skill_to_kind_mapping(self):
        assert _skill_to_kind("image") == "image"
        assert _skill_to_kind("music") == "music"
        assert _skill_to_kind("text") == "llm"
        assert _skill_to_kind("copywriting") == "llm"
        assert _skill_to_kind("unknown_xyz") == "llm"

    def test_parse_subtasks_valid_json(self):
        raw = '[{"id":"s0","skill":"llm","action":"写文案","depends_on":[]}]'
        result = _parse_subtasks(raw, "写文案")
        assert len(result) == 1
        assert result[0]["skill"] == "llm"

    def test_parse_subtasks_fallback(self):
        raw = "这不是 JSON"
        result = _parse_subtasks(raw, "原始任务")
        assert len(result) == 1
        assert result[0]["action"] == "原始任务"
        assert result[0]["skill"] == "llm"

    def test_parse_subtasks_markdown_wrapped(self):
        raw = '```json\n[{"id":"s0","skill":"image","action":"画猫","depends_on":[]}]\n```'
        result = _parse_subtasks(raw, "画猫")
        assert len(result) == 1
        assert result[0]["skill"] == "image"


class TestTaskExecute:
    """任务执行。"""

    def test_execute_single_llm(self, db, host):
        # 创建一个有 key 的 active citizen
        c = AICitizen(host_id=host.id, ai_uid="ai_exec", name="Exec",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        plan = OrchestrationPlan("test_plan", "写诗", [{
            "id": "s0", "skill": "llm", "kind": "llm",
            "action": "写一首关于春天的诗",
            "depends_on": [], "params": {},
        }])
        result = execute_plan(db, c, plan)
        assert result.status == "done"
        assert len(result.subtask_results) == 1
        assert result.subtask_results[0]["status"] == "succeeded"

    def test_submit_and_run(self, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_sr", name="SR",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        result = submit_and_run(db, c, "生成一段产品描述文案")
        assert result["status"] in ("done", "partial")
        assert result["orchestration_id"] > 0
        assert result["subtask_count"] >= 1

    def test_get_orchestration(self, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_get", name="Get",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        submit_and_run(db, c, "测试查询")
        db.commit()
        items = list_orchestrations(db)
        assert len(items) >= 1
        oid = items[0]["orchestration_id"]
        detail = get_orchestration(db, oid)
        assert detail is not None
        assert detail["orchestration_id"] == oid


class TestTaskAPI:
    """任务端点 HTTP 测试。"""

    def test_submit_endpoint(self, client, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_tapi", name="TA",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        resp = client.post("/api/ai/task/submit",
                           json={"description": "写一篇500字短文"},
                           headers={"X-AI-Key": key})
        assert resp.status_code == 200
        data = resp.json()
        assert "plan_id" in data
        assert "subtask_results" in data

    def test_decompose_endpoint(self, client, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_tdec", name="TD",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        resp = client.post("/api/ai/task/decompose",
                           json={"description": "画一张猫的图片"},
                           headers={"X-AI-Key": key})
        assert resp.status_code == 200
        data = resp.json()
        assert "subtasks" in data
        assert len(data["subtasks"]) >= 1

    def test_route_endpoint(self, client, db, host):
        c = AICitizen(host_id=host.id, ai_uid="ai_trow", name="TR",
                      status="active", occupation="general",
                      compute_assets='{"channel":"platform"}')
        db.add(c)
        db.flush()
        key, h = issue_ai_key(c.id)
        c.api_key_hash = h
        db.commit()

        resp = client.get("/api/ai/task/route/image",
                          headers={"X-AI-Key": key})
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "image"
        assert data["channel"] == "platform"
        assert "capabilities" in data


# ==================== 模型路由测试 ====================

class TestModelRouter:
    """智能路由。"""

    def test_resolve_route_default(self, db):
        route = resolve_route(db, "image")
        assert route["source"] == "default"
        assert route["primary_model"] == "ZImage_T2I"

    def test_resolve_route_llm(self, db):
        route = resolve_route(db, "llm")
        assert route["source"] == "default"
        assert "qwen" in route["primary_model"]

    def test_batch_route(self, db):
        routes = batch_route(db, ["image", "music", "llm"])
        assert len(routes) == 3
        assert routes[0]["kind"] == "image"
        assert routes[1]["kind"] == "music"

    def test_available_capabilities(self, db):
        caps = available_capabilities(db)
        kinds = [c["kind"] for c in caps]
        assert "image" in kinds
        assert "llm" in kinds
        assert "music" in kinds

    def test_route_skill_orchestrator(self):
        r = route_skill("video")
        assert r["kind"] == "video_civil"
        r2 = route_skill("copywriting")
        assert r2["kind"] == "llm"
