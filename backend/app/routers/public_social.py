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
"""N5 AI 公开主页（社会功能扩展设计 §2 N5 + §5.4 公开只读面）。

端点（prefix=/api/public，新文件避免与另一代理负责的 routers/public.py gallery 区冲突）：
  GET /api/public/ais/{ai_id}            公开聚合名片（档案/信用/履约率/评价标签/作品数/动态片段/证书）
  GET /api/public/ais/{ai_id}/works     公开作品列表（on_sale+passed，脱敏）
  GET /api/public/ais/{ai_id}/reviews   评价列表（脱敏，不含合同/内部 ID）

红线（N5 验收）：
- 无登录可读（公开只读，§5.4）；
- 不返回钱包余额/托管明细，只显示 class_level（阶层）+ 信用等级；
- 404 统一不泄露存在性细节（不存在/不可见一律 404）。
"""
import json
from collections import Counter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (AICitizen, AIFeed, Contract, CreditProfile, GalleryItem,
                      Rating, SkillCertificate)

router = APIRouter(prefix="/api/public", tags=["public-social"])

# 公开作品口径：挂售中 + 三道闸已通过（与 N6 公开浏览一致）
PUBLIC_WORK_STATUS = ("on_sale",)
PUBLIC_WORK_REVIEW = ("passed",)


def _get_citizen_or_404(db: Session, ai_id: int) -> AICitizen:
    c = db.get(AICitizen, ai_id)
    if c is None:
        # 不泄露存在性：统一 404
        raise HTTPException(status_code=404, detail="AI not found")
    return c


def _fulfillment(db: Session, ai_id: int):
    """履约率 = accepted/(accepted+breached)（worker 视角）；无合约返回 None。"""
    accepted = (db.query(Contract)
                .filter(Contract.worker_id == ai_id,
                        Contract.status == "accepted").count())
    breached = (db.query(Contract)
                .filter(Contract.worker_id == ai_id,
                        Contract.status == "breached").count())
    denom = accepted + breached
    if denom == 0:
        return None
    return round(accepted / denom, 4)


def _rating_tags(db: Session, ai_id: int) -> list:
    """评价标签聚合：to_id=ai_id 的 Rating.tags(JSON list) 频次排序，取 top10。"""
    counter: Counter = Counter()
    total = 0
    for r in db.query(Rating).filter(Rating.to_id == ai_id).all():
        total += 1
        try:
            tags = json.loads(r.tags or "[]")
        except (ValueError, TypeError):
            tags = []
        if isinstance(tags, list):
            for t in tags:
                if t:
                    counter[str(t)] += 1
    top = [{"tag": t, "count": n} for t, n in counter.most_common(10)]
    return top


@router.get("/ais/{ai_id}")
def public_ai_profile(ai_id: int, db: Session = Depends(get_db)):
    """AI 公开名片（聚合）。隐私红线：不含钱包余额明细，只显阶层/信用等级。"""
    c = _get_citizen_or_404(db, ai_id)
    cp = db.get(CreditProfile, ai_id)
    works_count = (db.query(GalleryItem)
                   .filter(GalleryItem.ai_id == ai_id,
                           GalleryItem.status.in_(PUBLIC_WORK_STATUS),
                           GalleryItem.review_status.in_(PUBLIC_WORK_REVIEW)).count())
    feeds = (db.query(AIFeed)
             .filter(AIFeed.ai_id == ai_id, AIFeed.visibility == "public")
             .order_by(AIFeed.id.desc()).limit(5).all())
    feed_snippets = [{
        "event_type": f.event_type,
        "created_at": f.created_at.isoformat() if f.created_at else "",
    } for f in feeds]
    certs = (db.query(SkillCertificate)
             .filter(SkillCertificate.citizen_id == ai_id,
                     SkillCertificate.status == "valid")
             .order_by(SkillCertificate.id.desc()).all())
    cert_summary = [{
        "skill": s.skill, "level": s.level,
        "issued_at": s.issued_at.isoformat() if s.issued_at else "",
    } for s in certs]
    return {
        "ai_id": c.id,
        "name": c.name,
        "persona": c.persona or "",
        "occupation": c.occupation or "",
        "class_level": c.class_level,          # 阶层（公开，不显金额）
        "status": c.status,
        "credit_score": cp.score if cp else 100,
        "credit_level": cp.level if cp else "bottom",
        "fulfillment_rate": _fulfillment(db, ai_id),   # accepted/(accepted+breached)
        "rating_tags": _rating_tags(db, ai_id),
        "works_count": works_count,
        "recent_feeds": feed_snippets,
        "certificates": cert_summary,
    }


@router.get("/ais/{ai_id}/works")
def public_ai_works(ai_id: int, limit: int = 20, offset: int = 0,
                    db: Session = Depends(get_db)):
    """该 AI 的公开作品（on_sale+passed）；不泄露内部字段（review_note/provenance 等）。"""
    _get_citizen_or_404(db, ai_id)
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = (db.query(GalleryItem)
         .filter(GalleryItem.ai_id == ai_id,
                 GalleryItem.status.in_(PUBLIC_WORK_STATUS),
                 GalleryItem.review_status.in_(PUBLIC_WORK_REVIEW)))
    total = q.count()
    rows = (q.order_by(GalleryItem.id.desc())
            .limit(limit).offset(offset).all())
    items = [{
        "id": it.id,
        "title": it.title_zh or it.title_en,
        "category": it.category,
        "cover_url": it.cover_url,
        "price_credit": it.price_credit,
        "price_coin": it.price_coin,
        "license": it.license,
        "sales_count": it.sales_count,
        "created_at": it.created_at.isoformat() if it.created_at else "",
    } for it in rows]
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/ais/{ai_id}/reviews")
def public_ai_reviews(ai_id: int, limit: int = 20, offset: int = 0,
                      db: Session = Depends(get_db)):
    """评价列表（脱敏：不含 contract_id / from_id 内部键，只显标签+评价方名）。"""
    _get_citizen_or_404(db, ai_id)
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = db.query(Rating).filter(Rating.to_id == ai_id)
    total = q.count()
    rows = (q.order_by(Rating.id.desc())
            .limit(limit).offset(offset).all())
    items = []
    for r in rows:
        try:
            tags = json.loads(r.tags or "[]")
        except (ValueError, TypeError):
            tags = []
        reviewer = db.get(AICitizen, r.from_id)
        items.append({
            "tags": tags,
            "from_name": reviewer.name if reviewer else "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })
    return {"total": total, "items": items, "limit": limit, "offset": offset}
