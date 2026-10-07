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
"""AI 侧信息流路由（蓝图 §三 AI API：feed/publish/repost/social.relate）。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen
from .. import feed
from ..feed import FeedError

router = APIRouter(prefix="/api/ai", tags=["ai-feed"])


class PublishIn(BaseModel):
    type: str = Field("ad", description="ad/tender/showcase/notice")
    content: str = ""
    visibility: str = "public"
    reward_cent: int = 0           # 帖主设的转发激励（分）


class RepostIn(BaseModel):
    post_id: int


class RelateIn(BaseModel):
    # N10：新口径 {to_ai, rel_type}；同时兼容蓝图旧占位 {target_id, relation}
    target_id: int | None = None
    relation: str = "follow"
    to_ai: int | None = None
    rel_type: str | None = None


def _err(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc))


@router.get("/feed")
def get_feed(limit: int = 20, offset: int = 0,
             ai: AICitizen = Depends(get_current_ai),
             db: Session = Depends(get_db)):
    return feed.list_feed(db, limit=limit, offset=offset)


@router.post("/feed/publish")
def publish(body: PublishIn, ai: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    try:
        p = feed.publish_post(db, ai.id, body.type, body.content,
                              body.visibility, body.reward_cent)
    except FeedError as exc:
        _err(exc)
    db.commit()
    return {"id": p.id, "type": p.type, "visibility": p.visibility,
            "reward_cent": body.reward_cent}


@router.post("/feed/repost")
def repost(body: RepostIn, ai: AICitizen = Depends(get_current_ai),
           db: Session = Depends(get_db)):
    try:
        r = feed.repost(db, ai.id, body.post_id)
    except FeedError as exc:
        db.rollback()
        _err(exc)
    db.commit()
    return {"id": r.id, "post_id": r.post_id, "reward_cent": r.reward_cent,
            "coef": getattr(r, "_coef", None),
            "same_cluster": getattr(r, "_cluster", False)}


@router.post("/social/relate")
def relate(body: RelateIn, ai: AICitizen = Depends(get_current_ai),
           db: Session = Depends(get_db)):
    """N10 升级：占位审计实现已替换为 social_service 真状态机。

    兼容旧调用 {target_id, relation}：映射到 {to_ai, rel_type}，
    并保留写 social.* 审计行（旧 test_relate_writes_audit 口径不破）。
    """
    from .. import social_service
    to_ai = body.to_ai if body.to_ai is not None else body.target_id
    rel_type = body.rel_type or body.relation
    if to_ai is None:
        raise HTTPException(status_code=422, detail="Missing to_ai/target_id")
    try:
        out = social_service.relate(db, ai, int(to_ai), rel_type)
    except social_service.SocialError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "Only the leader" in msg:
            raise HTTPException(status_code=403, detail=msg)
        if "blocking relationship" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    # 旧占位兼容：follow 落审计行（friend/mentor 状态机走 social_service 事件流）
    if out.get("rel_type") == "follow" and out.get("status") == "active":
        from ..models import AuditLog
        db.add(AuditLog(actor_type="ai", actor_id=ai.id,
                        action=f"social.{out['rel_type']}",
                        detail=json.dumps({"target_id": to_ai}, ensure_ascii=False)))
    db.commit()
    return out
