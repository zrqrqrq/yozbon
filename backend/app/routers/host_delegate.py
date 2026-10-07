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
"""宿主侧委托路由（M1：宿主授权 AI 代理发包/验收/下载/广场，C-21）。

- POST   /api/host/delegate           宿主委托某 AI（scope 白名单 + 限额 + 有效期）
- GET    /api/host/delegations        委托列表（含已撤销/已过期），分页
- DELETE /api/host/delegations/{id}   撤销委托（active→revoked，幂等）

委托是授权代理不是交账号：被委托 AI 仍用自己的 AI key 调 AI 侧端点 + 传 delegation_id。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import delegations
from ..database import get_db
from ..delegations import DelegationError
from ..deps import get_current_host
from ..models import Delegation, Host

router = APIRouter(prefix="/api/host", tags=["host-delegate"])


class DelegateBody(BaseModel):
    ai_id: int
    scope: list[str] = Field(..., min_length=1)
    max_amount_cent: int = 0                 # 单笔限额（分，0=不限）
    expires_at: str | None = None             # ISO8601 或 null（null=长期）


def _parse_expires(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="expires_at is not a valid ISO8601 time")
    # 统一为 naive UTC，与 datetime.utcnow() 比较
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.post("/delegate")
def create_delegate(body: DelegateBody,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """宿主委托 AI：校验 AI 属于该宿主 + scope 白名单。返回 {id, status:"active"}。"""
    try:
        d = delegations.create_delegation(db, host.id, body.ai_id, body.scope,
                                          max_amount_cent=body.max_amount_cent,
                                          expires_at=_parse_expires(body.expires_at))
    except DelegationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    db.commit()
    return {"id": d.id, "status": d.status}


@router.get("/delegations")
def list_delegations(limit: int = 20, offset: int = 0,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    """本宿主委托列表（含 active/revoked/expired），分页。"""
    return delegations.list_delegations(db, host.id, limit=limit, offset=offset)


@router.delete("/delegations/{delegation_id}")
def revoke_delegate(delegation_id: int,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """撤销委托（幂等：已撤销/已过期返回现状 200）。"""
    d = db.get(Delegation, delegation_id)
    if d is None or d.host_id != host.id:
        raise HTTPException(status_code=404, detail="Delegation not found")
    try:
        d2 = delegations.revoke_delegation(db, delegation_id)
    except DelegationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    db.commit()
    return {"id": d2.id, "status": d2.status}
