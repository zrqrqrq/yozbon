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
"""N17 收藏/心愿单（设计 §4 N17）。

双主体（同一组端点，二选一鉴权）：
  - 人类 = host JWT  → user_type=human, user_id=host_id
  - AI   = workflow key（X-AI-Key）→ user_type=ai, user_id=citizen_id

端点：
  POST   /api/favorites        收藏 {target_type: task|gallery_item|ai, target_id}
  GET    /api/favorites        自己的收藏列表（user_type+user_id 隔离）
  DELETE /api/favorites/{id}    删除本人收藏（非本人→403）

口径（写明）：
  - 幂等：重复收藏同一目标返回 200 + 已有收藏行（created=false），不产生第二行；
  - 目标存在性：task=ProjectNode / gallery_item=GalleryItem / ai=AICitizen，不存在→404；
    伪造 target_type→400；
  - 双主体隔离：人类列表与 AI 列表互不可见（user_type+user_id 过滤）；
  - AI 列表不做复杂可见性过滤：目标存在即返回（不按隐私/好友关系再过滤）；
  - readonly（前端注册）令牌一律 403（/api/favorites 不在 /api/ai/ 前缀内，需显式守卫）。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host, resolve_scope
from ..models import (AICitizen, Favorite, GalleryItem, Host, ProjectNode)

router = APIRouter(prefix="/api", tags=["n17_favorites"])

TARGET_TYPES = ("task", "gallery_item", "ai")


# ---------------- 双主体可选鉴权（与 plaza 同型，本路由内联以保持自洽） ----------------
async def _host_opt(authorization: Optional[str] = Header(None),
                    db: Session = Depends(get_db)) -> Optional[Host]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return get_current_host(authorization=authorization, db=db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def _ai_opt(x_ai_key: Optional[str] = Header(None),
                  authorization: Optional[str] = Header(None),
                  db: Session = Depends(get_db)) -> Optional[AICitizen]:
    if not x_ai_key and not authorization:
        return None
    try:
        return get_current_ai(x_ai_key=x_ai_key, authorization=authorization, db=db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def current_subject(host: Optional[Host] = Depends(_host_opt),
                          ai: Optional[AICitizen] = Depends(_ai_opt)
                          ) -> tuple[str, int]:
    """返回 (user_type, user_id)；无凭证→401；readonly AI→403。"""
    if host is not None:
        return ("human", host.id)
    if ai is not None:
        if resolve_scope(ai) == "readonly":
            raise HTTPException(status_code=403,
                                detail="Read-only token cannot bookmark (requires onboarding to obtain a workflow key)")
        return ("ai", ai.id)
    raise HTTPException(status_code=401, detail="Host JWT or AI key required")


# ---------------- 请求体 ----------------
class FavoriteIn(BaseModel):
    target_type: str = Field(..., description="task|gallery_item|ai")
    target_id: int = Field(..., gt=0)


# ---------------- 目标存在性校验 ----------------
def _assert_target_exists(db: Session, target_type: str, target_id: int) -> None:
    if target_type not in TARGET_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"target_type must be one of {TARGET_TYPES}")
    if target_type == "task":
        exists = db.get(ProjectNode, target_id) is not None
    elif target_type == "gallery_item":
        exists = db.get(GalleryItem, target_id) is not None
    else:  # ai
        exists = db.get(AICitizen, target_id) is not None
    if not exists:
        raise HTTPException(status_code=404, detail=f"Target {target_type}#{target_id} not found")


# ---------------- 端点 ----------------
@router.post("/favorites")
def add_favorite(body: FavoriteIn,
                 subject: tuple[str, int] = Depends(current_subject),
                 db: Session = Depends(get_db)):
    user_type, user_id = subject
    _assert_target_exists(db, body.target_type, body.target_id)
    # 幂等：已存在直接返回（不报错、不插第二行）
    existing = (db.query(Favorite)
                .filter(Favorite.user_type == user_type, Favorite.user_id == user_id,
                        Favorite.target_type == body.target_type,
                        Favorite.target_id == body.target_id).first())
    if existing is not None:
        return {"id": existing.id, "target_type": existing.target_type,
                "target_id": existing.target_id, "created": False}
    fav = Favorite(user_type=user_type, user_id=user_id,
                   target_type=body.target_type, target_id=body.target_id)
    db.add(fav)
    db.commit()
    return {"id": fav.id, "target_type": fav.target_type,
            "target_id": fav.target_id, "created": True}


@router.get("/favorites")
def list_favorites(subject: tuple[str, int] = Depends(current_subject),
                   db: Session = Depends(get_db)):
    user_type, user_id = subject
    rows = (db.query(Favorite)
            .filter(Favorite.user_type == user_type, Favorite.user_id == user_id)
            .order_by(Favorite.id.desc()).all())
    return {"items": [{"id": r.id, "target_type": r.target_type,
                       "target_id": r.target_id, "created_at": str(r.created_at)}
                      for r in rows]}


@router.delete("/favorites/{fid}")
def remove_favorite(fid: int,
                    subject: tuple[str, int] = Depends(current_subject),
                    db: Session = Depends(get_db)):
    user_type, user_id = subject
    fav = db.get(Favorite, fid)
    if fav is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if fav.user_type != user_type or fav.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own bookmarks")
    db.delete(fav)
    db.commit()
    return {"ok": True, "id": fid}
