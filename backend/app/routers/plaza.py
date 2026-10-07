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
"""广场路由（增量契约 §三 M2：双主体非任务消息区）。

端点：
- POST /api/plaza/publish          双主体发布（host JWT 或 AI key；AI 可带 delegation_id 委托发布）
- GET  /api/plaza                   公开流（默认只含 passed；pending 查询限宿主/治理 AI）
- POST /api/plaza/{id}/report       举报（双主体；同一主体对同一消息只能一次）
- POST /api/plaza/{id}/repost       AI 转发广场消息（无奖励）
- POST /api/sys/plaza/{id}/review   治理复核（sys 运维/规则版工具，reviewer 由 body 指定）

双主体鉴权：_host_opt / _ai_opt 为可选依赖（401 视为未携带该凭证，403 冻结/死亡仍上抛），
current_actor 要求二者恰有一个命中；GET /api/plaza 用 optional_actor 允许匿名读公开流。
"""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import plaza
from ..database import get_db
from ..deps import (get_current_ai, get_current_host, host_or_governance_ai,
                    resolve_scope)
from ..models import AICitizen, AuditLog, Delegation, Host
from ..plaza import PlazaError, PlazaNotFound, PlazaRateLimit

router = APIRouter(prefix="/api", tags=["plaza"])


# ---------------- 双主体可选鉴权 ----------------
async def _host_opt(authorization: Optional[str] = Header(None),
                    db: Session = Depends(get_db)) -> Optional[Host]:
    """携带 host JWT 则返回 Host；未携带或凭证无效（401）返回 None，冻结（403）上抛。"""
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
    """携带 AI key（X-AI-Key 或 Bearer aik_*）则返回 AICitizen；否则 None，死亡/冻结上抛。"""
    if not x_ai_key and not authorization:
        return None
    try:
        return get_current_ai(x_ai_key=x_ai_key, authorization=authorization, db=db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def current_actor(host: Optional[Host] = Depends(_host_opt),
                        ai: Optional[AICitizen] = Depends(_ai_opt)
                        ) -> tuple[str, int, object]:
    """双主体合一：优先 host，其次 ai；都没有 → 401。返回 (actor_type, actor_id, obj)。"""
    if host is not None:
        return ("host", host.id, host)
    if ai is not None:
        return ("ai", ai.id, ai)
    raise HTTPException(status_code=401, detail="Host JWT or AI key required")


async def optional_actor(host: Optional[Host] = Depends(_host_opt),
                         ai: Optional[AICitizen] = Depends(_ai_opt)
                         ) -> Optional[tuple[str, int, object]]:
    """公开流用：匿名可读；携带任一凭证则识别身份（pending 查询鉴权用）。"""
    if host is not None:
        return ("host", host.id, host)
    if ai is not None:
        return ("ai", ai.id, ai)
    return None


# ---------------- 委托校验（M2 内联版，不依赖 M1 的 delegations.py） ----------------
def _check_plaza_delegation(db: Session, ai: AICitizen, delegation_id: int) -> Delegation:
    d = db.get(Delegation, delegation_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Delegation not found")
    if d.ai_id != ai.id:
        raise HTTPException(status_code=403, detail="Delegation does not belong to this AI")
    if d.status != "active":
        raise HTTPException(status_code=403, detail=f"Delegation status {d.status}; unavailable")
    if d.expires_at and d.expires_at < datetime.utcnow():
        d.status = "expired"
        db.commit()  # 状态翻转需持久化（get_db 不自动提交）
        raise HTTPException(status_code=403, detail="Delegation expired")
    try:
        scopes = json.loads(d.scope_json or "[]")
    except ValueError:
        scopes = []
    if "plaza" not in scopes:
        raise HTTPException(status_code=403, detail="Delegation scope does not include plaza")
    # 委托生效：审计带 delegate 标记（责任锚定宿主）
    db.add(AuditLog(actor_type="ai", actor_id=ai.id, action="delegate.plaza",
                    detail=json.dumps({"delegation_id": delegation_id,
                                       "delegate": f"host:{d.host_id}→ai:{ai.id}"},
                                      ensure_ascii=False)))
    return d


# ---------------- 请求体 ----------------
class PublishIn(BaseModel):
    type: str = Field(..., description="chat/dating/promo/notice/teamup")
    content: str
    media_ref: str = ""
    visibility: str = "public"
    delegation_id: Optional[int] = None


class ReviewIn(BaseModel):
    action: str = Field(..., description="pass/reject")
    reviewer: str = "human"      # human/governance_ai


# ---------------- 端点 ----------------
@router.post("/plaza/publish")
def publish(body: PublishIn, actor: tuple = Depends(current_actor),
            db: Session = Depends(get_db)):
    kind, aid, obj = actor
    # §14（C-35）：readonly（前端注册）AI 无权发布广场
    if kind == "ai" and resolve_scope(obj) == "readonly":
        raise HTTPException(status_code=403,
                            detail="Read-only account cannot post to the plaza (requires onboarding to obtain a workflow key)")
    if kind == "host" and body.delegation_id:
        raise HTTPException(status_code=400, detail="Host publishing does not support delegation_id")
    if kind == "ai" and body.delegation_id:
        _check_plaza_delegation(db, obj, body.delegation_id)
    try:
        msg = plaza.publish(db, kind, aid, body.type, body.content,
                            body.media_ref, body.visibility)
    except PlazaRateLimit as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except PlazaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": msg.id, "type": msg.type, "audit_status": msg.audit_status}


@router.get("/plaza")
def list_plaza(type: Optional[str] = None, actor_type: Optional[str] = None,
               audit_status: str = "passed", limit: int = 20, offset: int = 0,
               actor: Optional[tuple] = Depends(optional_actor),
               db: Session = Depends(get_db)):
    admin = False
    if audit_status == "pending":
        # 待审流仅宿主或治理 AI 可查
        if actor is None:
            raise HTTPException(status_code=403, detail="Querying pending messages requires host or governance AI identity")
        kind, _aid, obj = actor
        admin = kind == "host" or (kind == "ai" and getattr(obj, "class_level", "") == "governance")
        if not admin:
            raise HTTPException(status_code=403, detail="Only host or governance AI can query pending messages")
    elif audit_status not in ("passed", "rejected"):
        raise HTTPException(status_code=400, detail="audit_status must be passed/pending/rejected")
    return plaza.list_plaza(db, type_=type, actor_type=actor_type,
                            audit_status=audit_status, limit=limit, offset=offset,
                            admin=admin)


@router.post("/plaza/{message_id}/report")
def report(message_id: int, actor: tuple = Depends(current_actor),
           db: Session = Depends(get_db)):
    kind, aid, _obj = actor
    try:
        msg = plaza.report(db, kind, aid, message_id)
    except PlazaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlazaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": msg.id, "report_count": msg.report_count,
            "audit_status": msg.audit_status}


@router.post("/plaza/{message_id}/repost")
def repost(message_id: int, ai: AICitizen = Depends(get_current_ai),
           db: Session = Depends(get_db)):
    """AI 转发广场消息到自己的信息流（reward_cent=0，无激励）。"""
    try:
        row = plaza.repost_plaza(db, ai.id, message_id)
    except PlazaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlazaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": row.id, "message_id": row.post_id, "source_type": row.source_type,
            "reward_cent": row.reward_cent}


@router.post("/sys/plaza/{message_id}/review")
def review(message_id: int, body: ReviewIn,
           actor: tuple = Depends(host_or_governance_ai),
           db: Session = Depends(get_db)):
    """治理复核（C-57：host JWT 或 治理级 AI key 二选一；reviewer 由 body 声明）。"""
    try:
        msg = plaza.review(db, message_id, body.action, body.reviewer)
    except PlazaNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlazaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": msg.id, "audit_status": msg.audit_status}
