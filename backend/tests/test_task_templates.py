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
"""N3 任务模板库单测（社会功能扩展设计 §2 N3）。

覆盖：列表/详情、active+category 筛选、分页、版本化 upsert、
写权限（host JWT / 治理 AI key / 无凭证 / 普通 AI）、枚举与字段注入 400、
模板预填走通发布三道闸（requirements_gov 五核心：缺项 400、补全 200）。
"""
import json

import pytest

from app.database import SessionLocal
from app.models import AICitizen, TaskTemplate
from tests.conftest import new_host, new_ai


def _set_governance(ai_id: int):
    db = SessionLocal()
    try:
        ci = db.get(AICitizen, ai_id)
        ci.class_level = "governance"
        db.commit()
    finally:
        db.close()


def _mk_template(client, token, **over):
    body = {"category": "promo", "name_zh": "测试推文", "name_en": "Test",
            "prompt_template": "写推文", "default_budget_min": 500,
            "default_budget_max": 3000, "default_duration_days": 2,
            "required_fields": ["scope", "budget"], "sample_output": "md"}
    body.update(over)
    return client.post("/api/templates", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def test_list_and_detail_crud(client):
    h = new_host(client)
    r = _mk_template(client, h["token"])
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["version"] == 1 and t["created"] is True

    lst = client.get("/api/templates?category=promo")
    assert lst.status_code == 200
    data = lst.json()
    assert data["total"] >= 1
    assert any(i["id"] == t["id"] for i in data["items"])

    d = client.get(f"/api/templates/{t['id']}")
    assert d.status_code == 200
    assert d.json()["required_fields"] == ["scope", "budget"]


def test_versioning_upsert_same_id(client):
    h = new_host(client)
    r1 = _mk_template(client, h["token"], name_zh="同名模板")
    assert r1.json()["version"] == 1
    r2 = _mk_template(client, h["token"], name_zh="同名模板",
                      prompt_template="改稿 v2")
    assert r2.status_code == 200, r2.text
    j = r2.json()
    assert j["version"] == 2          # 版本 +1
    assert j["id"] == r1.json()["id"]  # 同一行，不新建
    assert j["created"] is False
    assert j["prompt_template"] == "改稿 v2"
    # 库里仍只有这一条（同 category+name_zh）
    db = SessionLocal()
    try:
        n = (db.query(TaskTemplate)
             .filter_by(category="promo", name_zh="同名模板").count())
        assert n == 1
    finally:
        db.close()


def test_active_filter_and_category_filter(client):
    h = new_host(client)
    t = _mk_template(client, h["token"], category="code", name_zh="代码模板")
    tid = t.json()["id"]
    # 默认 active=1 能查到
    assert any(i["id"] == tid for i in client.get("/api/templates").json()["items"])
    # 下线（active=0）后默认列表查不到
    _mk_template(client, h["token"], category="code", name_zh="代码模板", active=0)
    items = client.get("/api/templates").json()["items"]
    assert all(i["id"] != tid or i["active"] == 1 for i in items)
    # category=promo 过滤不应出现 code
    promo_ids = [i["id"] for i in client.get("/api/templates?category=promo").json()["items"]]
    assert tid not in promo_ids


def test_bad_category_rejected(client):
    h = new_host(client)
    r = _mk_template(client, h["token"], category="hack")
    assert r.status_code == 400


def test_bad_required_field_key_rejected(client):
    h = new_host(client)
    r = _mk_template(client, h["token"], required_fields=["evil_field"])
    assert r.status_code == 400
    assert "evil_field" in r.json()["detail"]


def test_post_without_auth_forbidden(client):
    r = client.post("/api/templates", json={"category": "promo", "name_zh": "x"})
    assert r.status_code == 403


def test_post_normal_ai_key_forbidden(client):
    h = new_host(client)
    ai = new_ai(client, h["token"], name="普通AI")
    # 普通（非治理）AI key → 403
    r = client.post("/api/templates",
                    json={"category": "promo", "name_zh": "x"},
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 403


def test_post_governance_ai_key_allowed(client):
    h = new_host(client)
    ai = new_ai(client, h["token"], name="治理AI", occupation="governance")
    _set_governance(ai["id"])
    r = client.post("/api/templates",
                    json={"category": "design", "name_zh": "治理沉淀模板",
                          "required_fields": ["scope"]},
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200, r.text
    assert r.json()["actor"] == "ai_governance"


# ---------------- 模板预填 → 发布三道闸（requirements_gov 五核心） ----------------

def test_publish_gate_missing_core_returns_400(client):
    """模板只预填部分要素；显式传 requirements 但缺核心 → 三道闸 400。"""
    h = new_host(client)
    # 模拟模板预填：只给了 scope/budget，缺 goal/deliverable_std/deadline
    r = client.post("/api/host/projects",
                    json={"title": "模板预填测试", "budget_cent": 2000,
                          "requirements": {"scope": "只写 scope", "budget": "2000"}},
                    headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 400
    assert "Missing core elements" in r.json()["detail"]


def test_publish_gate_complete_via_template_passes(client):
    """用模板 required_fields 键名补全五核心 → 发布通过（200）。"""
    h = new_host(client)
    req = {
        "goal": "产出一篇可发布的推文",
        "scope": "1500字以内，含3个卖点",
        "deliverable_std": "Markdown 文件",
        "acceptance_criteria": "无违禁词，字数达标",
        "deadline": "3天后",
        "budget": "2000",
        "limits": "不含竞品贬低",
    }
    r = client.post("/api/host/projects",
                    json={"title": "推文项目", "budget_cent": 2000,
                          "requirements": req},
                    headers={"Authorization": f"Bearer {h['token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["budget_cent"] == 2000
