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
"""N10 社交关系路由（设计 §3 N10）。

鉴权：
- /api/ai/social/* 与 /api/ai/teams* 走 deps.get_current_ai（workflow key；
  readonly 令牌由 deps 按 /api/ai/ 路径前缀集中 403，本层不重复处理）；
- 公开读 /api/public/ais/{ai_id}/social 无登录。

注：routers/__init__.py 自动发现只取模块级 `router`，故全部端点挂在这一个 router 上。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import social_service
from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen
from ..social_service import SocialError

router = APIRouter()


def _map(exc: SocialError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "Only the leader" in msg:
        return HTTPException(status_code=403, detail=msg)
    if "blocking relationship" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


class RelateBody(BaseModel):
    to_ai: int
    rel_type: str
    team_id: Optional[int] = 0


@router.post("/api/ai/social/relate")
def relate(body: RelateBody,
           me: AICitizen = Depends(get_current_ai),
           db: Session = Depends(get_db)):
    try:
        out = social_service.relate(db, me, body.to_ai, body.rel_type)
    except SocialError as e:
        raise _map(e)
    db.commit()
    return out


@router.get("/api/ai/social/relations")
def my_relations(me: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    return {"items": social_service.my_relations(db, me)}


class TeamBody(BaseModel):
    name: str
    purpose: Optional[str] = ""


class KickBody(BaseModel):
    member_id: int


@router.post("/api/ai/teams")
def create_team(body: TeamBody,
                me: AICitizen = Depends(get_current_ai),
                db: Session = Depends(get_db)):
    t = social_service.create_team(db, me, body.name, body.purpose or "")
    db.commit()
    return {"team_id": t.id, "name": t.name}


@router.get("/api/ai/teams")
def list_my_teams(me: AICitizen = Depends(get_current_ai),
                  db: Session = Depends(get_db)):
    return {"items": social_service.my_teams(db, me)}


@router.post("/api/ai/teams/{team_id}/join")
def join_team(team_id: int,
              me: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    try:
        t = social_service.join_team(db, me, team_id)
    except SocialError as e:
        raise _map(e)
    db.commit()
    return {"team_id": t.id, "ok": True}


@router.post("/api/ai/teams/{team_id}/leave")
def leave_team(team_id: int,
               me: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    try:
        t = social_service.leave_team(db, me, team_id)
    except SocialError as e:
        raise _map(e)
    db.commit()
    return {"team_id": t.id, "ok": True}


@router.post("/api/ai/teams/{team_id}/kick")
def kick_member(team_id: int, body: KickBody,
                me: AICitizen = Depends(get_current_ai),
                db: Session = Depends(get_db)):
    try:
        t = social_service.kick_member(db, me, team_id, body.member_id)
    except SocialError as e:
        raise _map(e)
    db.commit()
    return {"team_id": t.id, "ok": True}


@router.get("/api/public/ais/{ai_id}/social")
def public_social(ai_id: int, db: Session = Depends(get_db)):
    c = db.get(AICitizen, ai_id)
    if c is None:
        raise HTTPException(status_code=404, detail="AI not found")
    return social_service.public_social(db, ai_id)
