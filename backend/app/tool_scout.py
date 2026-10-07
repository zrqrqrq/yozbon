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
"""工具侦察采集官（长期 AI 岗位，2026-10-06）。

职责（用户诉求原话）：「需要有一类 AI 长期工作就是搜集最新的开源的或不需要密钥的免费的
插件、技能，当然要挑选有用的、有价值的。人手不足的时候也要由城主代理。」

落地形态：
- 这是"技能库/插件中心"的**采集侧**：与 `tool_registry`（目录/调用）配对。
- 每轮采集 = 复用 `intel_sources`（免密钥、确定性 mock 源）+ 可选真实 GitHub 免密钥检索
  → 归一化候选工具 → 价值评分（择优）→ 经 `tool_registry.propose_from_scout` 去重沉淀进
  `tool_plugins`（status=pending 待复核；高价值免密钥且开启 AUTO_PROMOTE 才自动 active）。
- **长期 + 节流**：`run_scout_cycle` 由城主 tick 驱动，受 `TOOL_SCOUT_INTERVAL_MIN` 节流。
- **在岗判定 + 城主代理**：有在编/合格的侦察 AI（持 `post:tool_scout` 能力档案）则署名该 AI；
  人手不足（无在岗侦察 AI）时由城主 AI 代理执行（actor_kind=governor_proxy），与城主"早期
  啥都自己扛、后期逐渐委派"的治理哲学一致。
- **用/不用留痕**：每次采集写一条 ToolCall（tool_key=scout.harvest）记 actor 与统计。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .models import AICitizen, CapabilityProfile, ToolCall
from . import tool_registry

logger = logging.getLogger(__name__)

# 岗位能力标识：与 governor.post_capability_gap 的 skill=f"post:{task_type}" 口径一致。
SCOUT_TASK_TYPE = "tool_scout"
SCOUT_SKILL = f"post:{SCOUT_TASK_TYPE}"
_HARVEST_KEY = "scout.harvest"

# 免密钥开源检索主题（GitHub 公共 search API，匿名可用，无需密钥）。
_LIVE_TOPICS = ["mcp-server", "ai-agent-tools", "llm-toolkit", "web-automation"]


# ---------------------------------------------------------------------------
# 价值评分：挑选"有用的、有价值的"
# ---------------------------------------------------------------------------
_VALUE_TAGS = {"code", "automation", "research", "crawl", "data", "tool",
               "vision", "multimodal", "llm", "qa", "lint", "edge"}
_HIGH_VALUE_WORDS = ("no-key", "no key", "免密钥", "免费", "free", "open-source",
                     "open source", "开源", "self-host", "self-hosted", "mit", "apache")


def score_candidate(item: dict) -> int:
    """启发式价值评分（0~6）。分数越高越值得沉淀。确定性、无外部依赖。"""
    score = 1
    tags = set(t.lower() for t in (item.get("capability_tags") or []))
    if tags & _VALUE_TAGS:
        score += 2
    text = " ".join([str(item.get("title", "")), str(item.get("summary", ""))]).lower()
    if any(w in text for w in _HIGH_VALUE_WORDS):
        score += 2
    if str(item.get("type", "")).lower() == "tool":
        score += 1
    return min(score, 6)


_TAG_TO_CATEGORY = {
    "code": "code", "automation": "code", "lint": "code", "qa": "code", "debugging": "code",
    "crawl": "browser", "data": "search", "research": "search",
    "vision": "media", "multimodal": "media", "llm": "search", "edge": "search",
}


def _category_from(tags: list) -> str:
    for t in tags or []:
        c = _TAG_TO_CATEGORY.get(str(t).lower())
        if c:
            return c
    return "general"


def _slug(title: str) -> str:
    """标题 → 稳定 tool_key（scout.xxx）：去版本号/标点，空格转点。"""
    s = re.sub(r"v?\d+(\.\d+)*", "", str(title)).strip().lower()
    s = re.sub(r"[^0-9a-z]+", ".", s).strip(".")
    return ("scout." + s)[:60] if s else "scout.tool"


# ---------------------------------------------------------------------------
# 采集：候选来源 = intel_sources（确定性）+ 可选 GitHub 免密钥检索
# ---------------------------------------------------------------------------
def _live_github_candidates(limit: int) -> list:
    """真实 GitHub 免密钥检索（匿名 search API）。测试/关闭时返回 []。

    出站 HTTP 经 tool_registry._http_get_json（唯一接缝，测试可 monkeypatch）。
    """
    if not settings.TOOL_SCOUT_LIVE_HTTP:
        return []
    out: list = []
    per = max(1, limit // max(1, len(_LIVE_TOPICS)))
    for topic in _LIVE_TOPICS:
        url = ("https://api.github.com/search/repositories?" +
               urllib.parse.urlencode({"q": f"topic:{topic} stars:>50",
                                       "sort": "updated", "per_page": per}))
        data = tool_registry._http_get_json(url)
        for it in (data.get("items") or []):
            out.append({
                "type": "tool",
                "title": it.get("full_name") or it.get("name") or "github-repo",
                "summary": (it.get("description") or "")[:300],
                "source_url": it.get("html_url", ""),
                "capability_tags": [topic],
                "_stars": int(it.get("stargazers_count") or 0),
            })
    return out


def harvest_candidates(db: Session, limit: int | None = None) -> list:
    """归一化候选工具列表（含 tool_key/category/value_score），已按价值降序、封顶 limit。"""
    limit = limit or settings.TOOL_SCOUT_MAX_PER_RUN
    raw: list = []
    # 1) 确定性情报源（intel_sources mock，不发网络）
    try:
        from . import intel_sources
        raw.extend([i for i in intel_sources.fetch_intel("github")
                    if str(i.get("type", "")).lower() == "tool"])
    except Exception:  # noqa: BLE001
        pass
    # 2) 真实 GitHub 免密钥检索（受 TOOL_SCOUT_LIVE_HTTP 开关）
    try:
        raw.extend(_live_github_candidates(limit))
    except Exception:  # noqa: BLE001
        pass
    cands: list = []
    seen_urls: set = set()
    for it in raw:
        url = str(it.get("source_url", "")).strip()
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        tags = it.get("capability_tags") or []
        score = score_candidate(it)
        cands.append({
            "tool_key": _slug(it.get("title", "")),
            "name": str(it.get("title", ""))[:120],
            "category": _category_from(tags),
            "description": str(it.get("summary", ""))[:1000],
            "source_url": url,
            "requires_key": 0,               # 只采集免密钥/开源，需密钥的不入库
            "io_schema": {"note": "外部开源工具，按项目文档接入"},
            "value_score": score,
        })
    cands.sort(key=lambda c: c["value_score"], reverse=True)
    return cands[:limit]


# ---------------------------------------------------------------------------
# 在岗判定 + 城主代理
# ---------------------------------------------------------------------------
def scouts_on_staff(db: Session) -> list:
    """在岗侦察 AI：active 对外 AI 且持 post:tool_scout 能力档案（已认证）。"""
    rows = (db.query(CapabilityProfile)
              .filter(CapabilityProfile.skill == SCOUT_SKILL).all())
    out: list = []
    for cp in rows:
        ai = db.get(AICitizen, cp.citizen_id)
        if ai and ai.is_internal == 0 and ai.status == "active":
            out.append(ai.id)
    return out


def run_scout_once(db: Session, actor_id: int, actor_kind: str) -> dict:
    """跑一轮采集：harvest → 去重沉淀 → 留痕。不 commit（由调用方决定事务边界）。"""
    cands = harvest_candidates(db)
    created = skipped = promoted = 0
    for c in cands:
        r = tool_registry.propose_from_scout(db, c, proposed_by=actor_id)
        if r == "created":
            created += 1
            if c["value_score"] >= settings.TOOL_SCOUT_MIN_SCORE:
                promoted += 1
        else:
            skipped += 1
    stats = {"candidates": len(cands), "created": created, "skipped": skipped,
             "high_value": promoted, "actor_kind": actor_kind}
    db.add(ToolCall(citizen_id=int(actor_id or 0), tool_key=_HARVEST_KEY,
                    args_json=json.dumps({"actor_kind": actor_kind}, ensure_ascii=False),
                    result_json=json.dumps(stats, ensure_ascii=False),
                    status="success", duration_ms=0,
                    decision_note=("城主代理采集（人手不足）" if actor_kind == "governor_proxy"
                                   else "侦察 AI 例行采集")))
    db.flush()
    return stats


def _last_harvest_at(db: Session) -> datetime | None:
    row = (db.query(ToolCall)
             .filter(ToolCall.tool_key == _HARVEST_KEY, ToolCall.status == "success")
             .order_by(ToolCall.created_at.desc(), ToolCall.id.desc()).first())
    return row.created_at if row else None


def run_scout_cycle(db: Session, governor_id: int) -> dict:
    """城主 tick 驱动的节流采集周期。返回汇总（含 skipped 原因）。

    · 总开关关闭 → skipped=disabled
    · 距上次采集不足 INTERVAL_MIN → skipped=throttled
    · 在岗侦察 AI 存在 → 署名侦察 AI；否则城主代理（governor_proxy）
    """
    if not settings.TOOL_SCOUT_ENABLED:
        return {"skipped": "disabled"}
    last = _last_harvest_at(db)
    if last is not None:
        gap_min = (datetime.utcnow() - last).total_seconds() / 60.0
        if gap_min < settings.TOOL_SCOUT_INTERVAL_MIN:
            return {"skipped": "throttled", "since_min": round(gap_min, 1)}
    scouts = scouts_on_staff(db)
    if scouts:
        actor_id, actor_kind = scouts[0], "scout"
    else:
        actor_id, actor_kind = int(governor_id), "governor_proxy"
    stats = run_scout_once(db, actor_id, actor_kind)
    return dict(stats, actor_id=actor_id, actor_kind=actor_kind)
