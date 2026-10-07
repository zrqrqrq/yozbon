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
"""长约管理 RESTful 端点（签约/记工时/结算/备用顶替/心跳）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from ..deps import get_current_host
from ..database import get_db
from ..models import Host, RetainerContract, WorkLedger
from .. import retainer
from ..retainer import RetainerError

router = APIRouter(prefix="/api/retainer", tags=["retainer"])


# ---- Request Schemas ----
class SignContractReq(BaseModel):
    post_code: str
    title: str = ""
    occupation: str = ""
    primary_ai_id: int
    backup_ai_id: int = 0
    retainer_cent: int = 0
    weekly_hours: float = 40.0
    min_verified_level: str = ""
    guarantee_level: int = 0
    is_key_post: int = 0
    ends_at: Optional[str] = None  # ISO datetime string, None=长期
    renewable: int = 1


class LogHoursReq(BaseModel):
    ai_id: int
    post_code: str
    hours: float
    contract_id: int = 0
    period: str = ""  # "yyyy-Wnn"; empty = auto from current ISO week
    tasks_done: int = 0
    source: str = "manual"
    note: str = ""


class SetBackupReq(BaseModel):
    backup_ai_id: int


class HeartbeatReq(BaseModel):
    ts: Optional[str] = None  # ISO datetime; empty = now


# ---- Endpoints ----
@router.post("/contracts")
def sign_contract_ep(req: SignContractReq, host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    """签约长约（城主/宿主发起）。"""
    try:
        ends = datetime.fromisoformat(req.ends_at) if req.ends_at else None
        c = retainer.sign_contract(
            db, req.post_code, title=req.title, occupation=req.occupation,
            primary_ai_id=req.primary_ai_id, backup_ai_id=req.backup_ai_id,
            host_id=host.id, retainer_cent=req.retainer_cent,
            weekly_hours=req.weekly_hours, min_verified_level=req.min_verified_level,
            guarantee_level=req.guarantee_level, is_key_post=bool(req.is_key_post),
            ends_at=ends, renewable=bool(req.renewable))
        db.commit()
        return {"ok": True, "contract_id": c.id, "post_code": c.post_code}
    except RetainerError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/log-hours")
def log_hours_ep(req: LogHoursReq, host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """记工时（在编 AI 或城主代记）。"""
    try:
        period = req.period if req.period else None
        wl = retainer.log_hours(
            db, req.ai_id, req.post_code, req.hours,
            contract_id=req.contract_id, period=period,
            tasks_done=req.tasks_done, source=req.source, note=req.note)
        db.commit()
        return {"ok": True, "ledger_id": wl.id, "period": wl.period, "hours": wl.hours}
    except RetainerError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/settle")
def settle_ep(host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    """周度结算（城主/调度器触发）。"""
    results = retainer.weekly_settle(db)
    return {"ok": True, "settled": len(results), "details": [
        {"contract_id": r.get("contract_id"), "payout_cent": r.get("amount_cent", 0)}
        for r in results]}


@router.put("/contracts/{contract_id}/backup")
def set_backup_ep(contract_id: int, req: SetBackupReq,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    """设置 1:1 备用顶替 AI。"""
    try:
        c = retainer.set_backup(db, contract_id, req.backup_ai_id)
        db.commit()
        return {"ok": True, "contract_id": c.id, "backup_ai_id": c.backup_ai_id}
    except RetainerError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/contracts/{contract_id}/heartbeat")
def heartbeat_ep(contract_id: int, req: HeartbeatReq,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """主 AI 心跳上报。"""
    try:
        ts = datetime.fromisoformat(req.ts) if req.ts else datetime.utcnow()
        c = retainer.heartbeat(db, contract_id, ts)
        db.commit()
        return {"ok": True, "contract_id": c.id,
                "last_heartbeat_at": c.last_heartbeat_at.isoformat()}
    except RetainerError as e:
        db.rollback()
        raise HTTPException(404, str(e))


@router.post("/contracts/{contract_id}/failover")
def failover_ep(contract_id: int, host: Host = Depends(get_current_host),
                db: Session = Depends(get_db)):
    """手动触发掉线顶替。"""
    res = retainer.failover_contract(db, contract_id)
    db.commit()
    if res is None:
        raise HTTPException(404, "Contract not found or not in failover state")
    return {"ok": True, **res}


@router.get("/contracts")
def list_contracts(host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db), status: str = ""):
    """查看本宿主的长约列表。"""
    q = db.query(RetainerContract).filter(RetainerContract.host_id == host.id)
    if status:
        q = q.filter(RetainerContract.status == status)
    items = q.order_by(RetainerContract.id.desc()).all()
    return [{"id": c.id, "post_code": c.post_code, "title": c.title,
             "primary_ai_id": c.primary_ai_id, "backup_ai_id": c.backup_ai_id,
             "status": c.status, "retainer_cent": c.retainer_cent,
             "weekly_hours": c.weekly_hours, "is_key_post": c.is_key_post,
             "started_at": c.started_at.isoformat() if c.started_at else None,
             "ends_at": c.ends_at.isoformat() if c.ends_at else None}
            for c in items]


@router.get("/ledger")
def list_ledger(host: Host = Depends(get_current_host),
                db: Session = Depends(get_db), ai_id: int = 0, period: str = ""):
    """查看工时账（仅限本宿主合同关联的岗位）。"""
    from sqlalchemy import select
    # 数据隔离：仅展示本宿主名下长约涉及的 post_code
    host_posts = (db.query(RetainerContract.post_code)
                    .filter(RetainerContract.host_id == host.id)
                    .distinct())
    q = db.query(WorkLedger).filter(WorkLedger.post_code.in_(host_posts))
    if ai_id:
        q = q.filter(WorkLedger.ai_id == ai_id)
    if period:
        q = q.filter(WorkLedger.period == period)
    items = q.order_by(WorkLedger.id.desc()).limit(100).all()
    return [{"id": w.id, "ai_id": w.ai_id, "post_code": w.post_code,
             "period": w.period, "hours": w.hours, "tasks_done": w.tasks_done,
             "source": w.source} for w in items]
