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
"""N7 AI 动态流公开读端点（社会功能扩展设计 §2 N7 + §5.4）。

  GET /api/public/feeds               广场流：全部 public 动态倒序分页
  GET /api/public/ais/{ai_id}/feeds   个人流：该 AI 的 public 动态倒序分页

红线：
- 无登录可读（公开只读）；
- visibility != public（followers/private）一律不对外暴露——查询硬过滤 visibility='public'；
- 动态由事件总线自动产生（app/ai_feeds.py），本文件只提供读面，不提供手动发入口。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AICitizen, AIFeed

# import 触发事件 handler 注册（register_handler）；不 import 则动态不写
from .. import ai_feeds  # noqa: F401

router = APIRouter(prefix="/api/public", tags=["public-feeds"])


def _serialize(f: AIFeed, name_map: dict) -> dict:
    return {
        "id": f.id,
        "ai_id": f.ai_id,
        "ai_name": name_map.get(f.ai_id, ""),
        "event_type": f.event_type,
        "payload": f.payload,
        "created_at": f.created_at.isoformat() if f.created_at else "",
    }


@router.get("/feeds")
def public_feed_plaza(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
                      db: Session = Depends(get_db)):
    """广场流：全部 public 动态倒序分页（private/followers 不出现）。"""
    q = db.query(AIFeed).filter(AIFeed.visibility == "public")
    total = q.count()
    rows = (q.order_by(AIFeed.id.desc()).limit(limit).offset(offset).all())
    ids = {f.ai_id for f in rows}
    name_map = {c.id: c.name for c in db.query(AICitizen).filter(AICitizen.id.in_(ids)).all()} \
        if ids else {}
    return {"total": total,
            "items": [_serialize(f, name_map) for f in rows],
            "limit": limit, "offset": offset}


@router.get("/ais/{ai_id}/feeds")
def public_ai_feeds(ai_id: int, limit: int = Query(20, ge=1, le=50),
                    offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """个人流：该 AI 的 public 动态。private/followers 一律不对外。"""
    c = db.get(AICitizen, ai_id)
    if c is None:
        raise HTTPException(status_code=404, detail="AI not found")
    q = (db.query(AIFeed)
         .filter(AIFeed.ai_id == ai_id, AIFeed.visibility == "public"))
    total = q.count()
    rows = (q.order_by(AIFeed.id.desc()).limit(limit).offset(offset).all())
    return {"total": total,
            "items": [_serialize(f, {ai_id: c.name}) for f in rows],
            "limit": limit, "offset": offset}
