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
"""N17 收藏/心愿单 —— 业务测试（设计 §4 N17）。

双主体：
  - 人类 = host JWT（user_type=human, user_id=host_id）
  - AI   = workflow key（user_type=ai, user_id=citizen_id）
目标三类：task(ProjectNode) / gallery_item(GalleryItem) / ai(AICitizen)。
口径（写明）：AI 收藏列表不做复杂可见性过滤——目标存在即返回。
对抗用例见 test_adversarial_n17.py。
"""
import pytest


def _seed_task(host_id: int) -> int:
    from app.database import SessionLocal
    from app.models import Project, ProjectNode
    db = SessionLocal()
    try:
        p = Project(host_id=host_id, title="收藏测试项目")
        db.add(p)
        db.flush()
        node = ProjectNode(project_id=p.id, skill="code", spec="收藏一个任务",
                           status="open")
        db.add(node)
        db.commit()
        return node.id
    finally:
        db.close()


def _seed_gallery_item(ai_id: int) -> int:
    from app.database import SessionLocal
    from app.models import GalleryItem
    db = SessionLocal()
    try:
        g = GalleryItem(ai_id=ai_id, title_zh="收藏测试作品", category="image",
                        status="on_sale")
        db.add(g)
        db.commit()
        return g.id
    finally:
        db.close()


# ---------------- 人类收藏 ----------------
def test_human_favorite_task(client, host, ai):
    task_id = _seed_task(host["host_id"])
    r = client.post("/api/favorites",
                    headers={"Authorization": f"Bearer {host['token']}"},
                    json={"target_type": "task", "target_id": task_id})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["created"] is True
    assert b["target_type"] == "task" and b["target_id"] == task_id


# ---------------- AI 收藏 ----------------
def test_ai_favorite_gallery_item(client, host, ai):
    gid = _seed_gallery_item(ai["id"])
    r = client.post("/api/favorites", headers={"X-AI-Key": ai["api_key"]},
                    json={"target_type": "gallery_item", "target_id": gid})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["created"] is True and b["target_type"] == "gallery_item"


def test_ai_favorite_ai_target(client, host, ai):
    from tests.conftest import new_ai
    peer = new_ai(client, host["token"], name="被收藏的AI")
    r = client.post("/api/favorites", headers={"X-AI-Key": ai["api_key"]},
                    json={"target_type": "ai", "target_id": peer["id"]})
    assert r.status_code == 200, r.text


# ---------------- 列表（自己的） ----------------
def test_list_own_favorites(client, host, ai):
    task_id = _seed_task(host["host_id"])
    gid = _seed_gallery_item(ai["id"])
    # 人类收藏 task
    client.post("/api/favorites",
                headers={"Authorization": f"Bearer {host['token']}"},
                json={"target_type": "task", "target_id": task_id})
    r = client.get("/api/favorites",
                   headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["target_type"] == "task" and items[0]["target_id"] == task_id


# ---------------- 幂等：重复收藏同一目标 → 200 + 已有收藏，不产生第二行 ----------------
def test_idempotent_double_favorite(client, host, ai):
    task_id = _seed_task(host["host_id"])
    h = {"Authorization": f"Bearer {host['token']}"}
    r1 = client.post("/api/favorites", headers=h,
                     json={"target_type": "task", "target_id": task_id})
    r2 = client.post("/api/favorites", headers=h,
                     json={"target_type": "task", "target_id": task_id})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert r1.json()["id"] == r2.json()["id"]
    # DB 只有一行
    from app.database import SessionLocal
    from app.models import Favorite
    db = SessionLocal()
    try:
        n = db.query(Favorite).filter(
            Favorite.user_type == "human", Favorite.user_id == host["host_id"],
            Favorite.target_type == "task", Favorite.target_id == task_id).count()
        assert n == 1
    finally:
        db.close()


# ---------------- 删除本人收藏 ----------------
def test_delete_own_favorite(client, host, ai):
    task_id = _seed_task(host["host_id"])
    h = {"Authorization": f"Bearer {host['token']}"}
    created = client.post("/api/favorites", headers=h,
                          json={"target_type": "task", "target_id": task_id}).json()
    r = client.delete(f"/api/favorites/{created['id']}", headers=h)
    assert r.status_code in (200, 204), r.text
    after = client.get("/api/favorites", headers=h).json()["items"]
    assert all(i["id"] != created["id"] for i in after)
