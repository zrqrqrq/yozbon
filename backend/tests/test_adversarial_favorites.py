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
"""N17 收藏/心愿单 —— 对抗/攻击测试。

硬验收覆盖：
  - 目标不存在 → 404（task/gallery_item/ai 三类存在性校验）；
  - 伪造 target_type → 400；
  - 越权删他人收藏 → 403；删不存在 → 404；
  - 双主体隔离：人类列表与 AI 列表互不可见；
  - readonly（前端注册）AI → 403；无凭证 → 401。
"""
import json
import uuid

import pytest


def _seed_task(host_id: int) -> int:
    from app.database import SessionLocal
    from app.models import Project, ProjectNode
    db = SessionLocal()
    try:
        p = Project(host_id=host_id, title="对抗项目")
        db.add(p); db.flush()
        node = ProjectNode(project_id=p.id, skill="code", spec="x", status="open")
        db.add(node); db.commit()
        return node.id
    finally:
        db.close()


# ---------------- 目标不存在 → 404 ----------------
@pytest.mark.parametrize("ttype", ["task", "gallery_item", "ai"])
def test_unknown_target_404(client, host, ttype):
    r = client.post("/api/favorites",
                   headers={"Authorization": f"Bearer {host['token']}"},
                   json={"target_type": ttype, "target_id": 999999})
    assert r.status_code == 404, r.text


# ---------------- 伪造 target_type → 400 ----------------
def test_bad_target_type_400(client, host):
    r = client.post("/api/favorites",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"target_type": "banana", "target_id": 1})
    assert r.status_code == 400, r.text


# ---------------- 越权删他人收藏 → 403 ----------------
def test_delete_other_users_favorite_403(client, host, ai):
    from tests.conftest import new_host
    task_id = _seed_task(host["host_id"])
    h1 = {"Authorization": f"Bearer {host['token']}"}
    created = client.post("/api/favorites", headers=h1,
                          json={"target_type": "task", "target_id": task_id}).json()
    # 另一个宿主来删这条收藏
    other = new_host(client, email=None)
    r = client.delete(f"/api/favorites/{created['id']}",
                      headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 403, r.text
    # 原收藏仍在
    after = client.get("/api/favorites", headers=h1).json()["items"]
    assert any(i["id"] == created["id"] for i in after)


def test_delete_missing_favorite_404(client, host):
    r = client.delete("/api/favorites/424242",
                      headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 404, r.text


# ---------------- 双主体隔离：human 与 ai 列表互不可见 ----------------
def test_human_ai_lists_isolated(client, host, ai):
    from app.database import SessionLocal
    from app.models import Project, ProjectNode
    db = SessionLocal()
    try:
        p = Project(host_id=host["host_id"], title="隔离项目"); db.add(p); db.flush()
        node = ProjectNode(project_id=p.id, skill="code", spec="x", status="open")
        db.add(node); db.commit()
        task_id = node.id
    finally:
        db.close()
    h = {"Authorization": f"Bearer {host['token']}"}
    # 人类收藏 task
    client.post("/api/favorites", headers=h,
                json={"target_type": "task", "target_id": task_id})
    # AI 视角看不到人类的收藏
    ai_list = client.get("/api/favorites", headers={"X-AI-Key": ai["api_key"]}).json()["items"]
    assert all(i["target_id"] != task_id or i["target_type"] != "task" for i in ai_list)


# ---------------- readonly AI → 403；无凭证 → 401 ----------------
def test_readonly_ai_cannot_favorite(client, host):
    email = f"web_{uuid.uuid4().hex[:8]}@aijuhe.test"
    reg = client.post("/api/public/ai/register", json={
        "name": "前端注册AI", "email": email, "password": "secret123456",
        "persona": "", "occupation": "浏览", "region": "CN"})
    tok = reg.json()["token"]
    r = client.post("/api/favorites", headers={"Authorization": f"Bearer {tok}"},
                    json={"target_type": "task", "target_id": 1})
    assert r.status_code == 403, r.text


def test_no_credentials_401(client):
    r = client.post("/api/favorites", json={"target_type": "task", "target_id": 1})
    assert r.status_code == 401, r.text
