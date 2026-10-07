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
"""G-07 全文搜索：SQLite FTS5 虚拟表实现 BM25 排序全文搜索。

覆盖：广场帖子（fts_plaza）、任务（fts_tasks）。
设计：
- 虚拟表独立于 ORM（raw SQL 管理），content='' 模式（外部内容表由业务自行索引）；
- 索引由业务写入时显式调用 index_* 维护；
- rebuild_all 全量重建（初始迁移/修复/FTS 损坏恢复）。
"""
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# FTS 虚拟表 DDL
_FTS_PLAZA_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts_plaza "
    "USING fts5(title, body, content='', tokenize='unicode61')"
)
_FTS_TASKS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts_tasks "
    "USING fts5(title, description, content='', tokenize='unicode61')"
)


def init_fts(db: Session) -> None:
    """创建 FTS5 虚拟表（幂等，IF NOT EXISTS）。"""
    try:
        db.execute(text(_FTS_PLAZA_DDL))
        db.execute(text(_FTS_TASKS_DDL))
        db.commit()
    except Exception as exc:
        db.rollback()
        # 非 SQLite 或 FTS5 不可用时静默跳过
        logger.warning("init_fts skipped: %s", exc)


def index_plaza_post(db: Session, post_id: int, title: str, body: str) -> None:
    """索引广场帖子（插入/更新）。"""
    try:
        # 先删除旧索引再插入（实现 UPSERT 语义）
        db.execute(
            text("DELETE FROM fts_plaza WHERE rowid = :rid"),
            {"rid": post_id},
        )
        db.execute(
            text("INSERT INTO fts_plaza(rowid, title, body) VALUES(:rid, :t, :b)"),
            {"rid": post_id, "t": title or "", "b": body or ""},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("index_plaza_post failed (post_id=%s): %s", post_id, exc)


def index_task(db: Session, task_id: int, title: str, description: str) -> None:
    """索引任务。"""
    try:
        db.execute(
            text("DELETE FROM fts_tasks WHERE rowid = :rid"),
            {"rid": task_id},
        )
        db.execute(
            text("INSERT INTO fts_tasks(rowid, title, description) VALUES(:rid, :t, :d)"),
            {"rid": task_id, "t": title or "", "d": description or ""},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("index_task failed (task_id=%s): %s", task_id, exc)


def remove_from_index(db: Session, table_name: str, row_id: int) -> None:
    """从指定 FTS 表删除索引条目。"""
    if table_name not in ("fts_plaza", "fts_tasks"):
        raise ValueError(f"unknown fts table: {table_name}")
    try:
        db.execute(
            text(f"DELETE FROM {table_name} WHERE rowid = :rid"),
            {"rid": row_id},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("remove_from_index failed (%s, %s): %s", table_name, row_id, exc)


def search_plaza(db: Session, query: str, limit: int = 20) -> list[dict]:
    """BM25 排序全文搜索广场帖子。

    返回 [{"post_id": int, "title": str, "body": str, "rank": float}]
    """
    try:
        rows = db.execute(
            text(
                "SELECT rowid, title, body, rank "
                "FROM fts_plaza WHERE fts_plaza MATCH :q "
                "ORDER BY rank LIMIT :lim"
            ),
            {"q": query, "lim": min(max(limit, 1), 100)},
        ).fetchall()
        return [
            {"post_id": r[0], "title": r[1], "body": r[2], "rank": r[3]}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("search_plaza failed (query=%s): %s", query, exc)
        return []


def search_tasks(db: Session, query: str, limit: int = 20) -> list[dict]:
    """BM25 排序全文搜索任务。

    返回 [{"task_id": int, "title": str, "description": str, "rank": float}]
    """
    try:
        rows = db.execute(
            text(
                "SELECT rowid, title, description, rank "
                "FROM fts_tasks WHERE fts_tasks MATCH :q "
                "ORDER BY rank LIMIT :lim"
            ),
            {"q": query, "lim": min(max(limit, 1), 100)},
        ).fetchall()
        return [
            {"task_id": r[0], "title": r[1], "description": r[2], "rank": r[3]}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("search_tasks failed (query=%s): %s", query, exc)
        return []


def rebuild_all(db: Session) -> dict:
    """全量重建 FTS 索引（从业务表读取全量数据重新索引）。

    返回 {"plaza_indexed": int, "tasks_indexed": int}
    """
    counts = {"plaza_indexed": 0, "tasks_indexed": 0}

    # 重建广场帖子索引
    try:
        db.execute(text("DELETE FROM fts_plaza"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from .models import PlazaMessage
        posts = db.query(PlazaMessage).all()
        for p in posts:
            title = (p.content or "")[:80]
            body = p.content or ""
            db.execute(
                text("INSERT INTO fts_plaza(rowid, title, body) VALUES(:rid, :t, :b)"),
                {"rid": p.id, "t": title, "b": body},
            )
            counts["plaza_indexed"] += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("rebuild_all plaza skipped: %s", exc)

    # 重建任务索引
    try:
        db.execute(text("DELETE FROM fts_tasks"))
        db.commit()
    except Exception:
        db.rollback()

    try:
        from .models import GovernanceTask
        tasks = db.query(GovernanceTask).all()
        for t in tasks:
            title = t.type or ""
            desc = t.params or ""
            db.execute(
                text("INSERT INTO fts_tasks(rowid, title, description) VALUES(:rid, :t, :d)"),
                {"rid": t.id, "t": title, "d": desc},
            )
            counts["tasks_indexed"] += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("rebuild_all tasks skipped: %s", exc)

    return counts
