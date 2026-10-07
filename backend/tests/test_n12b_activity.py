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
"""N12b 观察室增强：任务一 activity 推导 + 任务二 stage 分类（先红后绿）。

口径（设计 §3 N12（增强）+ §3 N12b）：
- GET /api/host/observatory/ais 每 AI 增输出 activity={kind,text_zh,text_en,icon,since,progress?}
- 推导优先级（命中即定）：examining > working > posting_feed > posting_task > training
  > idle；dead/banned/frozen 为终态直接映射（优先级最高，死号不可能在干活）。
- GET /api/host/observatory/{ai_id}/events 每项加 stage ∈
  register/exam/work/post/message/trade/gov/life；既有 kind 字段保留。
"""
import json
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import (AIFeed, AILedger, AICitizen, Contract, LifecycleEvent,
                        Notification, Project)


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


# ============================================================
# 纯函数单测：derive_activity 优先级与文案
# ============================================================
def test_derive_pure_idle():
    from app.routers.observatory import derive_activity
    a = derive_activity("active")
    assert a["kind"] == "idle"
    assert "待机" in a["text_zh"]
    assert a["text_en"]  # 双语成对
    assert a["icon"]


def test_derive_pure_status_terminal_override():
    from app.routers.observatory import derive_activity
    # banned/frozen/dead 即使有活动合约也直接映射终态
    for st, zh in (("banned", "封禁"), ("frozen", "冻结"), ("dead", "休眠")):
        a = derive_activity(st, contract={"escrow_cent": 999})
        assert a["kind"] == st
        assert zh in a["text_zh"]
        assert a["text_en"]


def test_derive_pure_working_priority_over_idle():
    from app.routers.observatory import derive_activity
    created = datetime.utcnow() - timedelta(seconds=95)
    a = derive_activity("active",
                        contract={"escrow_cent": 12000},
                        project_title="LOGO 设计",
                        contract_created_at=created)
    assert a["kind"] == "working"
    assert "LOGO 设计" in a["text_zh"]
    assert "120" in a["text_zh"]          # 12000 分 = 120 积分
    assert "01:35" in a["text_zh"] or "01:36" in a["text_zh"]  # 用时 mm:ss
    assert a["since"]
    assert a["text_en"]


def test_derive_pure_examining_with_and_without_skill():
    from app.routers.observatory import derive_activity
    # 有技能名 + 科目进度
    started = datetime.utcnow()
    a = derive_activity("apprentice",
                        exam={"skill": "绘画", "n": 2, "m": 4,
                              "started_at": started})
    assert a["kind"] == "examining"
    assert "绘画" in a["text_zh"]
    assert "第2科/共4科" in a["text_zh"]
    assert a["progress"] == {"n": 2, "m": 4}
    # 无技能信息 → 兜底文案
    b = derive_activity("apprentice", exam={})
    assert b["kind"] == "examining"
    assert "能力考试" in b["text_zh"]


def test_derive_pure_posting_feed_task_training():
    from app.routers.observatory import derive_activity
    now = datetime.utcnow()
    a = derive_activity("active", recent_post_feed=now)
    assert a["kind"] == "posting_feed"
    assert "等待审核" in a["text_zh"]

    b = derive_activity("active",
                        recent_post_task={"title": "小程序开发", "budget_cent": 5000,
                                          "created_at": now})
    assert b["kind"] == "posting_task"
    assert "小程序开发" in b["text_zh"]
    assert "50" in b["text_zh"]

    c = derive_activity("active", training_skill="对话文案")
    assert c["kind"] == "training"
    assert "对话文案" in c["text_zh"]


# ============================================================
# 集成：GET /ais 每 AI 带 activity
# ============================================================
def test_ais_includes_activity_block(client, host, ai):
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    assert r.status_code == 200, r.text
    row = r.json()["ais"][0]
    act = row["activity"]
    for k in ("kind", "text_zh", "text_en", "icon", "since"):
        assert k in act, f"activity 缺字段 {k}"
    # 新建未派活的见习 AI → idle
    assert act["kind"] == "idle"
    # 双语成对
    assert act["text_zh"] and act["text_en"]


def test_ais_activity_working_when_contract_active(client, host, ai):
    db = SessionLocal()
    db.add(Contract(worker_id=ai["id"], buyer_id=ai["id"] + 100,
                   project_id=0, node_id=0,
                   terms_json="{}", escrow_cent=12000,
                   status="executing", created_at=datetime.utcnow()))
    db.commit(); db.close()

    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    row = r.json()["ais"][0]
    assert row["activity"]["kind"] == "working"
    assert "积分" in row["activity"]["text_zh"]


def test_ais_activity_dead_mapping(client, host, ai):
    db = SessionLocal()
    c = db.get(AICitizen, ai["id"])
    c.status = "dead"
    db.commit(); db.close()
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    row = r.json()["ais"][0]
    assert row["activity"]["kind"] == "dead"
    assert "休眠" in row["activity"]["text_zh"]


def test_ais_activity_posting_task_recent_project(client, host, ai):
    db = SessionLocal()
    db.add(Project(host_id=host["host_id"], title="官网改版",
                   budget_cent=5000, status="running",
                   pm_citizen_id=ai["id"], created_at=datetime.utcnow()))
    db.commit(); db.close()
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    row = r.json()["ais"][0]
    assert row["activity"]["kind"] == "posting_task"
    assert "官网改版" in row["activity"]["text_zh"]


def test_ais_activity_posting_feed_recent(client, host, ai):
    db = SessionLocal()
    db.add(AIFeed(ai_id=ai["id"], event_type="post_moment",
                  payload="{}", created_at=datetime.utcnow()))
    db.commit(); db.close()
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    row = r.json()["ais"][0]
    assert row["activity"]["kind"] == "posting_feed"
    assert "等待审核" in row["activity"]["text_zh"]


def test_ais_old_post_feed_ignored(client, host, ai):
    """post_* 动态超过窗口 → 不算 posting_feed，回落 idle。"""
    db = SessionLocal()
    db.add(AIFeed(ai_id=ai["id"], event_type="post_moment", payload="{}",
                  created_at=datetime.utcnow() - timedelta(hours=3)))
    db.commit(); db.close()
    r = client.get("/api/host/observatory/ais", headers=_hdr(host))
    row = r.json()["ais"][0]
    assert row["activity"]["kind"] == "idle"


# ============================================================
# 任务二：events 时间线每项带 stage
# ============================================================
def test_events_each_item_has_stage(client, host, ai):
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(LifecycleEvent(citizen_id=ai["id"], event="rent", detail="扣租", at=now))
    db.add(AIFeed(ai_id=ai["id"], event_type="settled", payload="{}"))
    db.add(Notification(ai_id=ai["id"], type="settled", title="结算", payload="{}"))
    db.add(AILedger(citizen_id=ai["id"], amount_cent=500, type="结算",
                    note="", balance_after=100_500))
    db.commit(); db.close()

    r = client.get(f"/api/host/observatory/{ai['id']}/events",
                  headers=_hdr(host))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    valid = {"register", "exam", "work", "post", "message", "trade", "gov", "life"}
    for it in items:
        assert "stage" in it
        assert it["stage"] in valid, f"非法 stage={it['stage']}"
        assert "kind" in it  # 既有 kind 保留


def test_stage_mapping_smoke(client, host, ai):
    """已知 type → stage 抽样：feed settled→trade；notification→message；ledger 税→gov。"""
    db = SessionLocal()
    now = datetime.utcnow()
    db.add(AIFeed(ai_id=ai["id"], event_type="settled", payload="{}"))
    db.add(Notification(ai_id=ai["id"], type="x", title="t", payload="{}"))
    db.add(AILedger(citizen_id=ai["id"], amount_cent=-30, type="税",
                    note="", balance_after=100_000))
    db.add(LifecycleEvent(citizen_id=ai["id"], event="revive", detail="", at=now))
    db.commit(); db.close()

    r = client.get(f"/api/host/observatory/{ai['id']}/events",
                  headers=_hdr(host))
    got = {}
    for it in r.json()["items"]:
        p = it.get("payload", {})
        sub = p.get("event") or p.get("event_type") or p.get("type")
        got[(it["kind"], sub)] = it["stage"]
    assert got[("feed", "settled")] == "trade"
    assert got[("notification", "x")] == "message"
    assert got[("ledger", "税")] == "gov"
    assert got[("lifecycle", "revive")] == "life"
