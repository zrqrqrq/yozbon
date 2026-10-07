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
"""N15 全局搜索（设计 §3 N15）。

GET /api/search?q=&type=task|ai|gallery|plaza&page=：公开无登录。

实现：SQLite FTS5 虚拟表 search_index(kind, ref_id UNINDEXED, title, body)，
同一库零外部依赖。索引四类：
- task：projects.title + project_nodes.skill/spec（任务大厅口径）
- ai：ai_citizens.name/occupation + capability_profiles.skill
- gallery：gallery_items.title_zh/title_en + category
- plaza：plaza_messages.content（仅 audit_status=passed）

中文分词限制（标定，后续优化方向）：FTS5 unicode61 不切 CJK，本实现用
「索引侧 + 查询侧同规则 CJK 2-gram 展开」做子串近似匹配；单字查询不可命中。
索引同步策略：register_daily_job 每日全量重建 + 首次请求时若索引为空自动补建；
不改动既有业务写入口（不越界改 models/各服务写路径）。
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import engine, get_db
from ..models import (AICitizen, CapabilityProfile, GalleryItem, PlazaMessage,
                      Project, ProjectNode)
from ..scheduler import register_daily_job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["n15-search"])

KINDS = ("task", "ai", "gallery", "plaza")
_MAX_Q = 200

_CJK = re.compile(r"[\u4e00-\u9fff]+")


def cjk_bigrams(text: str) -> list[str]:
    out = []
    for run in _CJK.findall(text or ""):
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])
    return out


def _doc_text(*parts) -> str:
    raw = " ".join(p for p in parts if p)
    grams = " ".join(cjk_bigrams(raw))
    return f"{raw} {grams}".strip()


_KINDS = KINDS


def _is_pg() -> bool:
    """当前引擎是否为 PostgreSQL（生产库）。SQLite 走 FTS5，PG 走普通表 + ILIKE。"""
    return engine.dialect.name == "postgresql"


def ensure_index(conn=None) -> None:
    """幂等建搜索表（已存在则跳过）。方言自适应：
    - SQLite：FTS5 虚拟表（MATCH/snippet/rank）；
    - PostgreSQL：普通表 + ILIKE 子串匹配（PG 无 fts5；CREATE VIRTUAL TABLE 会语法错误）。
    传入连接复用调用方事务；未传则自开一次性连接（调度器持事务期间务必传入会话连接，避免并发锁）。"""
    if _is_pg():
        ddl = text(
            "CREATE TABLE IF NOT EXISTS search_index("
            "kind text NOT NULL, ref_id integer, title text, body text)")
    else:
        ddl = text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
            "kind, ref_id UNINDEXED, title, body, tokenize='unicode61')")
    if conn is None:
        with engine.begin() as c:
            c.execute(ddl)
    else:
        conn.execute(ddl)


def rebuild_index(db: Session) -> int:
    """全量重建索引（幂等：先清空再从业务表回填）。返回索引条数。

    全部走传入会话同一连接（调度器持事务期间不得另开 engine 连接写 SQLite，
    否则 database is locked）。
    """
    ensure_index(db)
    rows: list[tuple] = []

    for p in db.query(Project).all():
        nodes = db.query(ProjectNode).filter_by(project_id=p.id).all()
        body = _doc_text(p.title,
                         " ".join(n.skill for n in nodes),
                         " ".join(n.spec or "" for n in nodes))
        rows.append(("task", p.id, p.title or "", body))

    for a in db.query(AICitizen).filter(AICitizen.is_internal == 0).all():  # 城主不外显、不可搜
        skills = ", ".join(c.skill for c in
                           db.query(CapabilityProfile)
                           .filter_by(citizen_id=a.id).all())
        rows.append(("ai", a.id, a.name or "",
                     _doc_text(a.name, a.occupation, skills)))

    for g in db.query(GalleryItem).all():
        rows.append(("gallery", g.id, g.title_zh or g.title_en or "",
                     _doc_text(g.title_zh, g.title_en, g.category)))

    for m in (db.query(PlazaMessage)
              .filter(PlazaMessage.audit_status == "passed").all()):
        rows.append(("plaza", m.id, (m.content or "")[:40],
                     _doc_text(m.content)))

    db.execute(text("DELETE FROM search_index"))
    insert_sql = text(
        "INSERT INTO search_index(kind, ref_id, title, body) "
        "VALUES (:k, :rid, :t, :b)")
    for k, rid, t, b in rows:
        db.execute(insert_sql, {"k": k, "rid": rid, "t": t, "b": b})
    return len(rows)


def build_match(q: str) -> str:
    """把用户 q 编译成安全 FTS MATCH 串：逐词加双引号 AND，杜绝注入。"""
    q = (q or "").strip()[:_MAX_Q]
    terms = []
    for w in re.findall(r"[A-Za-z0-9_]+", q):
        terms.append('"' + w.replace('"', "") + '"')
    for g in cjk_bigrams(q):
        terms.append('"' + g + '"')
    return " AND ".join(terms)


def _query_terms(q: str) -> list[str]:
    """抽取检索词（拉丁词 + CJK 2-gram），供 PG 的 ILIKE 分支使用。"""
    q = (q or "").strip()[:_MAX_Q]
    terms = list(re.findall(r"[A-Za-z0-9_]+", q))
    terms.extend(cjk_bigrams(q))
    return terms


@router.get("/api/search")
def search(q: str = Query(""), type: str = Query(""),
           page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=50),
           db: Session = Depends(get_db)):
    ensure_index(db)
    if type and type not in KINDS:
        raise HTTPException(status_code=400,
                            detail=f"type must be one of {KINDS}")

    if _is_pg():
        return _search_pg(db, q, type, page, limit)

    match = build_match(q)
    if not match:
        return {"total": 0, "items": [], "page": page, "limit": limit}

    # 首次启动/空索引自动补建（标定限制：非实时增量，每日全量重建）
    n = db.execute(text("SELECT COUNT(*) FROM search_index")).scalar() or 0
    if n == 0:
        rebuild_index(db)

    where = "search_index MATCH :m"
    params: dict = {"m": match}
    if type:
        where += " AND kind = :k"
        params["k"] = type

    try:
        total = (db.execute(text(f"SELECT COUNT(*) FROM search_index "
                                 f"WHERE {where}"), params).scalar() or 0)
        rows = (db.execute(
            text(f"SELECT kind, ref_id, title, "
                 f"snippet(search_index, 2, '[', ']', -1, 16) AS snip "
                 f"FROM search_index WHERE {where} "
                 f"ORDER BY rank LIMIT :lim OFFSET :off"),
            {**params, "lim": limit, "off": (page - 1) * limit})
            .fetchall())
    except Exception as e:  # noqa: BLE001 FTS 语法异常 → 空结果不 500
        logger.warning("fts query failed: %s | q=%r", e, q)
        return {"total": 0, "items": [], "page": page, "limit": limit}

    return {
        "total": total, "page": page, "limit": limit,
        "items": [{"kind": r[0], "ref_id": r[1], "title": r[2],
                   "snippet": r[3]} for r in rows],
    }


def _search_pg(db: Session, q: str, type: str, page: int, limit: int) -> dict:
    """PostgreSQL 搜索分支：普通表 + ILIKE 子串匹配（无 fts5/MATCH/snippet/rank）。

    语义对齐 SQLite 版：所有检索词 AND 命中（命中 body 或 title），
    空查询返回空；snippet 退化为 body 截断；排序退化为标题命中优先 + ref_id。
    """
    terms = _query_terms(q)
    if not terms:
        return {"total": 0, "items": [], "page": page, "limit": limit}

    n = db.execute(text("SELECT COUNT(*) FROM search_index")).scalar() or 0
    if n == 0:
        rebuild_index(db)

    clauses = []
    params: dict = {}
    for i, t in enumerate(terms):
        key = f"t{i}"
        clauses.append("(body ILIKE :{k} OR title ILIKE :{k})".format(k=key))
        params[key] = f"%{t}%"
    where = " AND ".join(clauses)
    if type:
        where += " AND kind = :k"
        params["k"] = type

    try:
        total = (db.execute(text(f"SELECT COUNT(*) FROM search_index "
                                 f"WHERE {where}"), params).scalar() or 0)
        rows = (db.execute(
            text(f"SELECT kind, ref_id, title, "
                 f"LEFT(body, 80) AS snip "
                 f"FROM search_index WHERE {where} "
                 f"ORDER BY (title ILIKE :ord) DESC, kind, ref_id "
                 f"LIMIT :lim OFFSET :off"),
            {**params, "ord": f"%{q.strip()[:40]}%",
             "lim": limit, "off": (page - 1) * limit})
            .fetchall())
    except Exception as e:  # noqa: BLE001 查询异常 → 空结果不 500
        logger.warning("pg search failed: %s | q=%r", e, q)
        return {"total": 0, "items": [], "page": page, "limit": limit}

    return {
        "total": total, "page": page, "limit": limit,
        "items": [{"kind": r[0], "ref_id": r[1], "title": r[2],
                   "snippet": r[3]} for r in rows],
    }


# ---------------- 每日全量重建（register_daily_job，不碰 scheduler.py） ----------------
def run_daily_rebuild(db: Session, now) -> int:
    return rebuild_index(db)


register_daily_job("search_index_rebuild", run_daily_rebuild)
