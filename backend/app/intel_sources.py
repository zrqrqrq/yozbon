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
"""M6 情报采集源抽象（契约 §5.3）。

- IntelSource.fetch() -> list[dict]，条目 schema：
  {type, title, summary, source_url, capability_tags}
- GitHubTrendingSource / TechNewsRSSSource：MVP 确定性 mock（不发网络）。
- fetch_intel(source)：聚合入口。
- collect_intel(db, source, collected_by)：采集 → title+source_url 去重入库 →
  返回 {collected, skipped}。uq_intel_dedup 唯一索引兜底。

与 M3+M4 platform_facts.collect_intel_facts 的关系：后者来自采集源返回情报条目；
本模块是采集源的真源，platform_facts 落地后可直接复用 fetch_intel。
"""
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import IntelReport


class IntelError(Exception):
    """情报采集业务异常（路由层映射 400）。"""


class IntelSource:
    """采集源抽象基类。"""

    def fetch(self) -> list:  # pragma: no cover - 接口约定
        raise NotImplementedError


class GitHubTrendingSource(IntelSource):
    """MVP mock：3 条确定性「新工具」条目（假 URL）。"""

    def fetch(self) -> list:
        return [
            {"type": "tool",
             "title": "yozbon-mock-tool v1.0",
             "summary": "mock: automated code-generation tool, deterministic demo entry",
             "source_url": "https://github.com/mock/yozbon-mock-tool",
             "capability_tags": ["code", "automation"]},
            {"type": "tool",
             "title": "yozbon-mock-scraper v0.9",
             "summary": "mock: structured web scraper, deterministic demo entry",
             "source_url": "https://github.com/mock/yozbon-mock-scraper",
             "capability_tags": ["crawl", "data"]},
            {"type": "tool",
             "title": "yozbon-mock-lint v2.1",
             "summary": "mock: static quality checker, deterministic demo entry",
             "source_url": "https://github.com/mock/yozbon-mock-lint",
             "capability_tags": ["qa", "lint"]},
        ]


class TechNewsRSSSource(IntelSource):
    """MVP mock：2 条确定性「新模型」条目。"""

    def fetch(self) -> list:
        return [
            {"type": "model",
             "title": "MockNanoLM-7B released",
             "summary": "mock: 7B on-device inference model, deterministic demo entry",
             "source_url": "https://github.com/mock/technews/nanolm-7b",
             "capability_tags": ["llm", "edge"]},
            {"type": "model",
             "title": "MockVision-V2 multimodal upgrade",
             "summary": "mock: image-text multimodal model V2, deterministic demo entry",
             "source_url": "https://github.com/mock/technews/vision-v2",
             "capability_tags": ["multimodal", "vision"]},
        ]


_SOURCES = {
    "github": GitHubTrendingSource,
    "rss": TechNewsRSSSource,
}


def fetch_intel(source: str = "all") -> list:
    """聚合采集入口。source ∈ {"all","github","rss"}。"""
    if source == "all":
        keys = ["github", "rss"]
    elif source in _SOURCES:
        keys = [source]
    else:
        raise IntelError(f"source must be all/github/rss; received {source!r}")
    out = []
    for k in keys:
        out.extend(_SOURCES[k]().fetch())
    return out


def collect_intel(db: Session, source: str = "all",
                  collected_by: int = 0) -> dict:
    """采集并入库（title+source_url 去重）。返回 {collected, skipped}。"""
    items = fetch_intel(source)
    # 现有 (title, source_url) 集合（source_url 非空才受唯一索引约束，代码层也按此去重）
    existing = set(
        (r.title, r.source_url)
        for r in db.query(IntelReport.title, IntelReport.source_url).all()
    )
    collected = 0
    skipped = 0
    for it in items:
        key = (it.get("title", ""), it.get("source_url", ""))
        if key in existing:
            skipped += 1
            continue
        row = IntelReport(
            type=it.get("type", "tool"),
            title=it.get("title", ""),
            summary=it.get("summary", ""),
            source_url=it.get("source_url", ""),
            capability_tags=json.dumps(it.get("capability_tags", []),
                                      ensure_ascii=False),
            ai_status="new",
            collected_by=collected_by,
        )
        db.add(row)
        try:
            db.flush()
            existing.add(key)
            collected += 1
        except IntegrityError:
            # uq_intel_dedup 兜底（并发/空 source_url 边界）：回滚本条，跳过
            db.rollback()
            skipped += 1
    db.flush()
    return {"collected": collected, "skipped": skipped, "source": source}


def list_intel(db: Session, type_: str = "", ai_status: str = "",
               limit: int = 50, offset: int = 0) -> dict:
    limit = min(max(int(limit), 1), 50)
    q = db.query(IntelReport)
    if type_:
        q = q.filter(IntelReport.type == type_)
    if ai_status:
        q = q.filter(IntelReport.ai_status == ai_status)
    total = q.count()
    rows = (q.order_by(IntelReport.collected_at.desc(), IntelReport.id.desc())
            .limit(limit).offset(max(int(offset), 0)).all())
    items = []
    for r in rows:
        try:
            tags = json.loads(r.capability_tags or "[]")
        except Exception:  # noqa: BLE001
            tags = []
        items.append({
            "id": r.id, "type": r.type, "title": r.title,
            "summary": r.summary, "source_url": r.source_url,
            "capability_tags": tags, "ai_status": r.ai_status,
            "collected_by": r.collected_by,
            "collected_at": r.collected_at.isoformat() if r.collected_at else None,
        })
    return {"total": total, "items": items}
