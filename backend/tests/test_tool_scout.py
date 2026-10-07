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
"""技能库 / 插件中心 + 工具侦察采集岗 回归测试（2026-10-06）。

覆盖用户诉求「有一类 AI 长期搜集最新开源/免密钥的有用插件技能，人手不足由城主代理」：
  - tool_registry：种子发现 + 发现准则 + 执行（免密钥真接 / 需密钥只可发现 / 未知工具）
                   + 决策留痕 + propose_from_scout 去重与状态；
  - tool_scout：价值评分确定性 + 采集归一（离线 mock 源）+ 在岗判定
                + run_scout_cycle（无在岗→城主代理 / 有在岗→署名侦察 AI / 节流跳过）；
  - governor.run_tick：有对外公民时触发采集周期并写 AuditLog(tool_scout.cycle)；
  - 路由：GET /api/tools、POST /api/tools/decision（AI key）、
          POST /api/tools/scout/run + GET /api/tools/scout/proposals（host JWT）。

离线确定性：monkeypatch tool_registry._http_get_json 与 settings.TOOL_SCOUT_LIVE_HTTP=False。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import governor, tool_registry, tool_scout  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (AICitizen, AuditLog, CapabilityProfile,  # noqa: E402
                        ToolCall, ToolPlugin)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def offline(monkeypatch):
    """关闭真实 GitHub 检索，保证采集只走确定性 mock 源（不发网络）。"""
    monkeypatch.setattr(settings, "TOOL_SCOUT_LIVE_HTTP", False, raising=False)


# ===========================================================================
# tool_registry：种子 / 发现 / 执行 / 决策 / 沉淀
# ===========================================================================
def test_discover_returns_seed_catalog_and_directive(db):
    out = tool_registry.discover(db)
    keys = {i["tool_key"] for i in out["items"]}
    assert {"web.search", "web.wikipedia", "legal.search", "code.run"} <= keys
    assert out["total"] == len(out["items"])
    assert "技能库优先" in out["directive"]
    # 需密钥工具应被标记 requires_key=1
    legal = next(i for i in out["items"] if i["tool_key"] == "legal.search")
    assert legal["requires_key"] == 1


def test_execute_legal_search_requires_key(db):
    """需密钥工具：本期仅可发现、不可真调，返回 requires_key 并留痕。"""
    res = tool_registry.execute(db, 999, "legal.search", {"query": "商标法"})
    assert res["ok"] is False
    assert res["status"] == "requires_key"
    row = (db.query(ToolCall)
             .filter(ToolCall.tool_key == "legal.search").first())
    assert row is not None and row.status == "requires_key"


def test_execute_web_search_success_with_mock_http(db, monkeypatch):
    """免密钥工具：经 _http_get_json 接缝真实接通（测试桩返回摘要）。"""
    def fake_get(url, timeout=None):
        return {"AbstractText": "DuckDuckGo 摘要内容", "Heading": "DDG",
                "RelatedTopics": [{"Text": "相关条目", "FirstURL": "https://x.test/a"}]}
    monkeypatch.setattr(tool_registry, "_http_get_json", fake_get)
    res = tool_registry.execute(db, 1001, "web.search", {"query": "开源工具"})
    assert res["ok"] is True
    assert res["status"] == "success"
    assert res["abstract"] == "DuckDuckGo 摘要内容"
    # usage_count 应累加
    plugin = db.query(ToolPlugin).filter(ToolPlugin.tool_key == "web.search").first()
    assert int(plugin.usage_count) >= 1


def test_execute_unknown_tool_error(db):
    res = tool_registry.execute(db, 1002, "no.such.tool", {})
    assert res["ok"] is False
    assert res["status"] == "error"


def test_record_decision_leaves_trace(db):
    tool_registry.record_decision(db, 1003, "无需工具，纯文本任务", chosen_key="")
    row = (db.query(ToolCall)
             .filter(ToolCall.citizen_id == 1003, ToolCall.status == "decision").first())
    assert row is not None
    assert row.tool_key == ""
    assert "纯文本" in row.decision_note


def test_propose_from_scout_creates_pending_then_dedup(db):
    cand = {"tool_key": "scout.demo-tool", "name": "Demo Tool", "category": "code",
            "description": "开源免费", "source_url": "https://github.com/mock/demo",
            "requires_key": 0, "io_schema": {}, "value_score": 5}
    assert tool_registry.propose_from_scout(db, cand, proposed_by=7) == "created"
    row = db.query(ToolPlugin).filter(ToolPlugin.tool_key == "scout.demo-tool").first()
    assert row.source == "scout"
    assert row.status == "pending"          # 默认 AUTO_PROMOTE 关 → pending 待复核
    assert row.proposed_by == 7
    assert row.value_score == 5
    # 同 source_url 去重
    assert tool_registry.propose_from_scout(db, cand, proposed_by=7) == "skipped"
    # 同 tool_key 去重（不同 url）
    cand2 = dict(cand, source_url="https://github.com/mock/other")
    assert tool_registry.propose_from_scout(db, cand2, proposed_by=7) == "skipped"


# ===========================================================================
# tool_scout：评分 / 采集 / 在岗 / 周期（含城主代理）
# ===========================================================================
def test_score_candidate_deterministic():
    hi = {"title": "open-source free scraper", "summary": "no-key mit",
          "capability_tags": ["crawl", "data"], "type": "tool"}
    lo = {"title": "Random Blog Post", "summary": "news",
          "capability_tags": ["opinion"], "type": "model"}
    assert tool_scout.score_candidate(hi) > tool_scout.score_candidate(lo)
    # 同输入同输出（确定性）
    assert tool_scout.score_candidate(hi) == tool_scout.score_candidate(hi)


def test_harvest_candidates_offline_uses_mock_source(db, offline):
    cands = tool_scout.harvest_candidates(db)
    assert len(cands) >= 1
    for c in cands:
        assert c["requires_key"] == 0          # 只采集免密钥
        assert c["tool_key"].startswith("scout.")
    # 按价值降序
    scores = [c["value_score"] for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_scouts_on_staff_detects_only_active_external(db, offline):
    # 造一个对外 active 公民 + 持 post:tool_scout 能力档案
    scout = AICitizen(host_id=0, ai_uid="scout_staff_1", name="ScoutA",
                      class_level="junior", status="active", is_internal=0)
    db.add(scout)
    db.flush()
    db.add(CapabilityProfile(citizen_id=scout.id, skill=tool_scout.SCOUT_SKILL,
                             profile_json="{}", declared=1))
    # 造一个不持该档案的对外公民
    other = AICitizen(host_id=0, ai_uid="scout_staff_2", name="Other",
                      class_level="junior", status="active", is_internal=0)
    db.add(other)
    db.flush()
    staff = tool_scout.scouts_on_staff(db)
    assert staff == [scout.id]


def test_run_scout_cycle_governor_proxy_when_no_scout(db, offline, monkeypatch):
    """人手不足（无在岗侦察 AI）→ 城主代理执行，署名 governor_proxy。"""
    monkeypatch.setattr(settings, "TOOL_SCOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TOOL_SCOUT_INTERVAL_MIN", 0, raising=False)
    gov = governor.ensure_governor(db)
    res = tool_scout.run_scout_cycle(db, gov.id)
    assert res["actor_kind"] == "governor_proxy"
    assert res["actor_id"] == gov.id
    # 写了采集留痕
    hc = (db.query(ToolCall)
            .filter(ToolCall.tool_key == "scout.harvest",
                    ToolCall.status == "success").first())
    assert hc is not None


def test_run_scout_cycle_signed_by_on_staff_scout(db, offline, monkeypatch):
    """有在岗侦察 AI → 署名该 AI（scout），非城主。"""
    monkeypatch.setattr(settings, "TOOL_SCOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TOOL_SCOUT_INTERVAL_MIN", 0, raising=False)
    gov = governor.ensure_governor(db)
    scout = AICitizen(host_id=0, ai_uid="scout_run_1", name="ScoutRun",
                      class_level="junior", status="active", is_internal=0)
    db.add(scout)
    db.flush()
    db.add(CapabilityProfile(citizen_id=scout.id, skill=tool_scout.SCOUT_SKILL,
                             profile_json="{}", declared=1))
    db.commit()
    res = tool_scout.run_scout_cycle(db, gov.id)
    assert res["actor_kind"] == "scout"
    assert res["actor_id"] == scout.id


def test_run_scout_cycle_throttled(db, offline, monkeypatch):
    """距上次采集不足间隔 → 节流跳过。"""
    monkeypatch.setattr(settings, "TOOL_SCOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TOOL_SCOUT_INTERVAL_MIN", 360, raising=False)
    db.add(ToolCall(citizen_id=1, tool_key="scout.harvest", status="success",
                    args_json="{}", result_json="{}", created_at=datetime.utcnow()))
    db.flush()
    gov = governor.ensure_governor(db)
    res = tool_scout.run_scout_cycle(db, gov.id)
    assert res.get("skipped") == "throttled"


def test_run_scout_cycle_disabled(db, offline, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SCOUT_ENABLED", False, raising=False)
    gov = governor.ensure_governor(db)
    res = tool_scout.run_scout_cycle(db, gov.id)
    assert res.get("skipped") == "disabled"


# ===========================================================================
# governor.run_tick 触发采集周期并写审计
# ===========================================================================
def test_run_tick_triggers_scout_audit(db, offline, monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SCOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TOOL_SCOUT_INTERVAL_MIN", 0, raising=False)
    # 对外正式公民（越过待机门）
    fc = AICitizen(host_id=0, ai_uid="first_citizen_ts", name="FirstCitizen",
                   class_level="junior", status="active", is_internal=0)
    db.add(fc)
    db.commit()
    governor.run_tick(db)
    log = (db.query(AuditLog)
             .filter(AuditLog.action == "tool_scout.cycle").first())
    assert log is not None


# ===========================================================================
# 路由：发现 / 决策 / 手动采集 / 提案列表
# ===========================================================================
def test_route_tools_list_public(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert "directive" in body


def test_route_decision_requires_ai(client, ai):
    # 无凭证 → 401
    r0 = client.post("/api/tools/decision", json={"note": "x"})
    assert r0.status_code == 401
    # 带 AI key → 200 + 留痕
    r = client.post("/api/tools/decision",
                    json={"chosen_tool": "", "note": "纯文本无需工具"},
                    headers={"X-AI-Key": ai["api_key"]})
    assert r.status_code == 200
    assert r.json()["recorded"] is True


def test_route_scout_run_and_proposals(client, host, offline):
    # host JWT 触发一轮采集
    r = client.post("/api/tools/scout/run",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 查询 pending 提案
    r2 = client.get("/api/tools/scout/proposals",
                    headers={"Authorization": f"Bearer {host['token']}"})
    assert r2.status_code == 200
    assert isinstance(r2.json()["items"], list)
    # 全部 pending 提案 source=scout
    for it in r2.json()["items"]:
        assert it["source"] == "scout"
