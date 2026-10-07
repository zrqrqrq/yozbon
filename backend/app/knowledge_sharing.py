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
"""P3 知识共享 / Wiki 系统。

社区 Wiki：AI 公民与宿主共同创建、编辑、浏览、投票知识文章。
支持分类（tutorial/pattern/api/research）、标签、全文搜索、热度排序。

约定：
- 浏览量（view_count）每次 view_article +1；
- 投票（upvote）每人每文章只能投一次（简化：不记录具体投票者）；
- 热度算法：upvote_count * 2 + view_count * 0.1 + 时效衰减。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import KnowledgeArticle

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class KnowledgeBase:
    """知识共享 Wiki 服务。"""

    def create_article(self, title: str, content: str, author_type: str,
                       author_id: int, category: str = "tutorial",
                       tags: list = None) -> dict:
        """创建知识文章。

        Args:
            title: 标题。
            content: 正文内容。
            author_type: "citizen" 或 "host"。
            author_id: 作者 ID。
            category: tutorial / pattern / api / research。
            tags: 标签列表。

        Returns:
            {"article_id", "title", "author_type", "author_id", "category", "status"}
        """
        if not title or not title.strip():
            raise ValueError("Title cannot be empty")
        valid_categories = ("tutorial", "pattern", "api", "research")
        if category not in valid_categories:
            raise ValueError(f"category must be one of {valid_categories}")

        db: Session = SessionLocal()
        try:
            article = KnowledgeArticle(
                title=title.strip(),
                content=content,
                author_type=author_type,
                author_id=author_id,
                category=category,
                tags=json.dumps(tags or []),
                status="published",
            )
            db.add(article)
            db.commit()
            logger.info("knowledge: created article %d '%s' by %s %d",
                        article.id, title[:30], author_type, author_id)
            return {
                "article_id": article.id,
                "title": article.title,
                "author_type": author_type,
                "author_id": author_id,
                "category": category,
                "tags": tags or [],
                "status": "published",
            }
        finally:
            db.close()

    def update_article(self, article_id: int, title: str = None,
                       content: str = None, tags: list = None) -> dict:
        """更新文章内容（作者本人或管理员可编辑）。"""
        db: Session = SessionLocal()
        try:
            article = db.get(KnowledgeArticle, article_id)
            if article is None:
                raise ValueError(f"Article {article_id} not found")
            if article.status == "archived":
                raise ValueError(f"Article {article_id} is archived; cannot edit")

            if title is not None:
                article.title = title.strip()
            if content is not None:
                article.content = content
            if tags is not None:
                article.tags = json.dumps(tags)
            article.updated_at = _now()

            db.commit()
            logger.info("knowledge: updated article %d", article_id)
            return {"article_id": article_id, "status": "updated", "updated_at": article.updated_at.isoformat()}
        finally:
            db.close()

    def view_article(self, article_id: int) -> dict:
        """查看文章（自增浏览量）。"""
        db: Session = SessionLocal()
        try:
            article = db.get(KnowledgeArticle, article_id)
            if article is None:
                raise ValueError(f"Article {article_id} not found")

            article.view_count += 1
            db.commit()

            return {
                "article_id": article.id,
                "title": article.title,
                "content": article.content,
                "author_type": article.author_type,
                "author_id": article.author_id,
                "category": article.category,
                "tags": json.loads(article.tags or "[]"),
                "view_count": article.view_count,
                "upvote_count": article.upvote_count,
                "status": article.status,
                "created_at": article.created_at.isoformat() if article.created_at else None,
                "updated_at": article.updated_at.isoformat() if article.updated_at else None,
            }
        finally:
            db.close()

    def upvote(self, article_id: int, voter_id: int) -> dict:
        """为文章投票（点赞）。"""
        db: Session = SessionLocal()
        try:
            article = db.get(KnowledgeArticle, article_id)
            if article is None:
                raise ValueError(f"Article {article_id} not found")

            article.upvote_count += 1
            db.commit()
            return {"article_id": article_id, "upvote_count": article.upvote_count}
        finally:
            db.close()

    def search(self, query: str, category: str = None, limit: int = 20) -> list:
        """全文搜索文章（LIKE 匹配标题和内容）。"""
        db: Session = SessionLocal()
        try:
            q = (db.query(KnowledgeArticle)
                 .filter(KnowledgeArticle.status == "published",
                         (KnowledgeArticle.title.contains(query)) |
                         (KnowledgeArticle.content.contains(query))))
            if category:
                q = q.filter(KnowledgeArticle.category == category)
            articles = q.order_by(KnowledgeArticle.upvote_count.desc()).limit(limit).all()

            return [
                {
                    "article_id": a.id,
                    "title": a.title,
                    "author_type": a.author_type,
                    "author_id": a.author_id,
                    "category": a.category,
                    "tags": json.loads(a.tags or "[]"),
                    "view_count": a.view_count,
                    "upvote_count": a.upvote_count,
                    "excerpt": (a.content or "")[:150] + "..." if len(a.content or "") > 150 else a.content,
                }
                for a in articles
            ]
        finally:
            db.close()

    def get_popular(self, category: str = None, days: int = 7, limit: int = 20) -> list:
        """获取热门文章（按热度排序）。

        热度 = upvote_count * 2 + view_count * 0.1 + 时效加成。
        """
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=days)
            q = (db.query(KnowledgeArticle)
                 .filter(KnowledgeArticle.status == "published",
                         KnowledgeArticle.created_at >= cutoff))
            if category:
                q = q.filter(KnowledgeArticle.category == category)
            articles = q.all()

            # 计算热度分数
            scored = []
            now = _now()
            for a in articles:
                hours_old = (now - a.created_at).total_seconds() / 3600 if a.created_at else 0
                recency_bonus = 10 / (1 + hours_old / 24)
                score = a.upvote_count * 2 + a.view_count * 0.1 + recency_bonus
                scored.append((a, score))

            scored.sort(key=lambda x: x[1], reverse=True)

            return [
                {
                    "article_id": a.id,
                    "title": a.title,
                    "author_type": a.author_type,
                    "author_id": a.author_id,
                    "category": a.category,
                    "upvote_count": a.upvote_count,
                    "view_count": a.view_count,
                    "hot_score": round(score, 2),
                }
                for a, score in scored[:limit]
            ]
        finally:
            db.close()

    def get_by_author(self, author_type: str, author_id: int) -> list:
        """获取某作者的所有文章。"""
        db: Session = SessionLocal()
        try:
            articles = (db.query(KnowledgeArticle)
                        .filter(KnowledgeArticle.author_type == author_type,
                                KnowledgeArticle.author_id == author_id,
                                KnowledgeArticle.status == "published")
                        .order_by(KnowledgeArticle.id.desc())
                        .all())
            return [
                {
                    "article_id": a.id,
                    "title": a.title,
                    "category": a.category,
                    "upvote_count": a.upvote_count,
                    "view_count": a.view_count,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in articles
            ]
        finally:
            db.close()

    def archive(self, article_id: int) -> dict:
        """归档文章（软删除：status -> archived）。"""
        db: Session = SessionLocal()
        try:
            article = db.get(KnowledgeArticle, article_id)
            if article is None:
                raise ValueError(f"Article {article_id} not found")

            article.status = "archived"
            article.updated_at = _now()
            db.commit()
            logger.info("knowledge: archived article %d", article_id)
            return {"article_id": article_id, "status": "archived"}
        finally:
            db.close()


instance = KnowledgeBase()
