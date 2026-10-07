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
"""N15 全局搜索 业务+对抗测试（先红后绿）。

口径（设计 §3 N15）：GET /api/search?q=&type=task|ai|gallery|plaza&page=
- 公开无登录；SQLite FTS5；索引 tasks 标题+描述 / ai_profiles 名+技能 /
  gallery 标题 / plaza 内容；中文可用；分页正确。
"""
from app.database import SessionLocal
from app.models import (AICitizen, CapabilityProfile, GalleryItem,
                        PlazaMessage, Project, ProjectNode)
from app.routers import search as search_mod


def _seed_all():
    db = SessionLocal()
    db.add(Project(host_id=1, title="量子海报设计", budget_cent=5000))
    db.flush()
    db.add(ProjectNode(project_id=db.query(Project).first().id,
                       skill="image", spec="需要生成量子主题海报",
                       budget_cent=5000))
    ai = AICitizen(host_id=1, ai_uid="ai_1_99", name="水墨画师",
                   occupation="插画师")
    db.add(ai)
    db.flush()
    db.add(CapabilityProfile(citizen_id=ai.id, skill="国画",
                            profile_json="{}"))
    db.add(GalleryItem(ai_id=ai.id, title_zh="星河夜景", category="image"))
    db.add(PlazaMessage(actor_type="ai", actor_id=ai.id, type="teamup",
                        content="寻找队友组队做视频剪辑", audit_status="passed"))
    db.commit()
    db.close()


def _rebuild():
    db = SessionLocal()
    search_mod.ensure_index()
    search_mod.rebuild_index(db)
    db.close()


# ---------------- 四类检索均命中（中文） ----------------
def test_n15_search_four_categories(client):
    _seed_all()
    _rebuild()

    cases = [
        ("量子", "task"),
        ("水墨", "ai"),
        ("星河", "gallery"),
        ("组队", "plaza"),
    ]
    for q, kind in cases:
        r = client.get(f"/api/search?q={q}")
        assert r.status_code == 200, r.text
        body = r.json()
        kinds = {it["kind"] for it in body["items"]}
        assert kind in kinds, f"搜 {q} 未命中 {kind}: {body}"
        assert body["total"] >= 1


def test_n15_type_filter(client):
    _seed_all()
    _rebuild()
    r = client.get("/api/search?q=海报&type=task")
    assert r.status_code == 200
    body = r.json()
    assert all(it["kind"] == "task" for it in body["items"])
    assert body["total"] >= 1


# ---------------- 公开无登录 / 分页 ----------------
def test_n15_public_no_auth(client):
    r = client.get("/api/search?q=量子")
    assert r.status_code == 200  # 无任何凭证


def test_n15_pagination(client):
    _seed_all()
    _rebuild()
    r1 = client.get("/api/search?q=量子&page=1&limit=1")
    b1 = r1.json()
    assert b1["limit"] == 1
    assert len(b1["items"]) <= 1
    # 越界页 → 空列表但 total 正确
    r2 = client.get("/api/search?q=量子&page=999&limit=10")
    assert r2.json()["items"] == []


# ---------------- 对抗：注入 / 超长 q / 空 q ----------------
def test_n15_fts_injection_safe(client):
    _seed_all()
    _rebuild()
    evil = '" OR 1=1 -- '
    r = client.get(f"/api/search?q={evil}")
    # 不得 500；空匹配返回空列表
    assert r.status_code == 200, r.text


def test_n15_overlong_q_clamped(client):
    _seed_all()
    _rebuild()
    r = client.get("/api/search?q=" + "海报" * 500)
    assert r.status_code == 200  # 不崩，截断后查询


def test_n15_empty_q_returns_empty(client):
    r = client.get("/api/search?q=")
    assert r.status_code == 200
    assert r.json()["items"] == []
