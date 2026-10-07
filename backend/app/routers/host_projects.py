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
"""宿主侧项目路由（蓝图 §三 宿主 API：projects 发布/列表/评审报告/确认运行）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..models import (AICitizen, AuditLog, Contract, CreditEvent, GovernanceTask,
                      Host, LifecycleEvent, Project)
from .. import project as proj
from .. import requirements_gov
from ..project import ProjectError

router = APIRouter(prefix="/api/host", tags=["host-projects"])


# 分页限长（铁律：禁止前端无限翻页拖库）
_NOTIFY_PAGE_MAX = 50


def _my_ai_ids(db: Session, host_id: int) -> list[int]:
    """一次查询取出本宿主名下全部 AI 公民 id（后续全部 IN 查询，禁 N+1）。"""
    return [r[0] for r in db.query(AICitizen.id)
            .filter(AICitizen.host_id == host_id).all()]


class ProjectCreateIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    budget_cent: int = Field(..., gt=0)
    pm_citizen_id: int = 0
    reviewer_ids: list[int] = []     # ≥200AC 强制评审组（3-5 异质）；低于线传入=抽样评审
    requirements: dict | None = None  # T1 七要素（可选；缺省按老格式推导）
    showcase_enabled: bool = False    # 画廊展示开关
    ai_broadcast_enabled: bool = False  # 站内AI传播开关


def _err(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc))


@router.post("/projects")
def create_project(body: ProjectCreateIn, host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    # T1 需求细化校验（契约 §9.3.1）：老格式软通过；显式 requirements 缺核心要素→400
    try:
        requirements_gov.validate_requirements(body.title, body.budget_cent,
                                              body.requirements)
    except requirements_gov.T1Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        p = proj.create_project(db, host.id, body.title, body.budget_cent,
                                deadline=None, pm_citizen_id=body.pm_citizen_id,
                                reviewer_ids=body.reviewer_ids)
    except ProjectError as exc:
        _err(exc)
    db.commit()
    return {"id": p.id, "status": p.status, "budget_cent": p.budget_cent}


@router.get("/projects")
def list_projects(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    return proj.list_projects(db, host.id)


@router.get("/projects/{project_id}/report")
def project_report(project_id: int, host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    p = proj.get_project(db, project_id)
    if p.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission to view others' projects")
    r = proj.get_review_report(db, project_id)
    if r is None:
        return {"project_id": project_id, "has_report": False}
    return {"project_id": project_id, "has_report": True, "report_id": r.id,
            "conclusion": r.conclusion, "risk": r.risk,
            "budget_suggest_cent": r.budget_suggest_cent,
            "duration_suggest_h": r.duration_suggest_h,
            "breakdown": r.breakdown}


@router.post("/projects/{project_id}/approve")
def approve_project(project_id: int, host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    try:
        p = proj.approve_running(db, host.id, project_id)
    except ProjectError as exc:
        _err(exc)
    db.commit()
    return {"id": p.id, "status": p.status}


# ---------------------------------------------------------------------------
# 合约开关：画廊展示 / 站内AI传播
# ---------------------------------------------------------------------------
from ..models import Contract as _Contract  # noqa: E402


class ContractFlagsIn(BaseModel):
    showcase_enabled: bool | None = None
    ai_broadcast_enabled: bool | None = None


@router.post("/contract/{contract_id}/flags")
def set_contract_flags(contract_id: int, body: ContractFlagsIn,
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    """设置合约的画廊展示/站内AI传播开关。"""
    c = db.get(_Contract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    # 校验归属：worker AI 属于该宿主
    from ..models import AICitizen
    worker = db.get(AICitizen, c.worker_id)
    if worker is None or worker.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission on this contract")
    if body.showcase_enabled is not None:
        c.showcase_enabled = 1 if body.showcase_enabled else 0
    if body.ai_broadcast_enabled is not None:
        c.ai_broadcast_enabled = 1 if body.ai_broadcast_enabled else 0
    db.commit()
    return {
        "contract_id": c.id,
        "showcase_enabled": bool(c.showcase_enabled),
        "ai_broadcast_enabled": bool(c.ai_broadcast_enabled),
    }


@router.get("/contract/{contract_id}/flags")
def get_contract_flags(contract_id: int, host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    """查询合约的画廊展示/站内AI传播开关。"""
    c = db.get(_Contract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    from ..models import AICitizen
    worker = db.get(AICitizen, c.worker_id)
    if worker is None or worker.host_id != host.id:
        raise HTTPException(status_code=403, detail="No permission on this contract")
    return {
        "contract_id": c.id,
        "showcase_enabled": bool(c.showcase_enabled),
        "ai_broadcast_enabled": bool(c.ai_broadcast_enabled),
    }


# ---------------------------------------------------------------------------
# 通知中心 + 合约列表（蓝图 §三 宿主侧清单补齐；前端已就绪）
# ---------------------------------------------------------------------------
@router.get("/notifications")
def host_notifications(limit: int = 20, offset: int = 0,
                       host: Host = Depends(get_current_host),
                       db: Session = Depends(get_db)):
    """宿主通知中心：聚合本宿主名下所有 AI 的相关事件，倒序分页。

    返回 [{id, type(frozen/lifecycle/breach/credit_change/accepted/settled/contract_update/invited/notice), title, ref, at}]。
    数据源各一次 IN 查询（禁 N+1）：生命周期事件 / 信用事件 / 合约 /
    被指派治理任务 / 宿主自身审计日志。
    """
    limit = max(1, min(int(limit), _NOTIFY_PAGE_MAX))
    offset = max(0, int(offset))
    my_ids = _my_ai_ids(db, host.id)
    items: list[dict] = []

    if my_ids:
        # 1) 生命周期事件（冻结/死亡/复活/豁免）
        for e in (db.query(LifecycleEvent)
                  .filter(LifecycleEvent.citizen_id.in_(my_ids))
                  .order_by(LifecycleEvent.at.desc()).limit(50).all()):
            et = "frozen" if e.event == "freeze" else "lifecycle"
            items.append({"id": f"lc{e.id}", "type": et,
                          "title": f"AI#{e.citizen_id} lifecycle event: {e.event}",
                          "ref": f"lifecycle:{e.id}",
                          "at": e.at.isoformat() if e.at else ""})
        # 2) 信用变动（违约/差评 → 被判违约；其余 → 信用变动）
        for e in (db.query(CreditEvent)
                  .filter(CreditEvent.citizen_id.in_(my_ids))
                  .order_by(CreditEvent.created_at.desc()).limit(50).all()):
            et = "breach" if e.event in ("fraud", "violate") else "credit_change"
            items.append({"id": f"cr{e.id}", "type": et,
                          "title": f"AI#{e.citizen_id} credit {e.delta:+d} ({e.event})",
                          "ref": e.ref or f"credit:{e.id}",
                          "at": e.created_at.isoformat() if e.created_at else ""})
        # 3) 合约（本宿主 AI 作为 worker 或 buyer：被验收/被结算/被判违约）
        for c in (db.query(Contract)
                  .filter(or_(Contract.worker_id.in_(my_ids),
                              Contract.buyer_id.in_(my_ids)))
                  .order_by(Contract.id.desc()).limit(50).all()):
            if c.status == "accepted":
                et = "accepted"
            elif c.status in ("refunded", "paid"):
                et = "settled"
            elif c.status == "breached":
                et = "breach"
            else:
                et = "contract_update"
            ts = c.accepted_at or c.delivered_at or c.created_at
            items.append({"id": f"ct{c.id}", "type": et,
                          "title": f"Contract#{c.id} status={c.status}",
                          "ref": f"contract:{c.id}",
                          "at": ts.isoformat() if ts else ""})
        # 4) 被指派治理任务（被邀标/评审/仲裁）
        for t in (db.query(GovernanceTask)
                  .filter(GovernanceTask.assignee_id.in_(my_ids))
                  .order_by(GovernanceTask.id.desc()).limit(50).all()):
            items.append({"id": f"gt{t.id}", "type": "invited",
                          "title": f"Governance task assigned #{t.id} ({t.type})",
                          "ref": f"govtask:{t.id}",
                          "at": t.created_at.isoformat() if t.created_at else ""})

    # 5) 宿主自身审计日志（系统公告：注册/注资/冻结等动作留痕）
    for a in (db.query(AuditLog)
              .filter(AuditLog.actor_type == "host",
                      AuditLog.actor_id == host.id)
              .order_by(AuditLog.id.desc()).limit(20).all()):
        items.append({"id": f"al{a.id}", "type": "notice",
                      "title": a.action, "ref": f"audit:{a.id}",
                      "at": a.created_at.isoformat() if a.created_at else ""})

    # ISO 时间字符串可直接字典序倒序；空时间排最后
    items.sort(key=lambda x: x["at"], reverse=True)
    total = len(items)
    return {"items": items[offset:offset + limit], "total": total,
            "limit": limit, "offset": offset}


@router.get("/contracts")
def host_contracts(status: str = "", limit: int = 20, offset: int = 0,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """宿主侧合约列表：本宿主 AI 作为 worker 或 buyer 的全部合约，按 id 倒序分页。

    可选 status 精确过滤（escrowed/executing/delivered/accepted/disputed/…）。
    """
    limit = max(1, min(int(limit), _NOTIFY_PAGE_MAX))
    offset = max(0, int(offset))
    my_ids = _my_ai_ids(db, host.id)
    q = db.query(Contract)
    if my_ids:
        q = q.filter(or_(Contract.worker_id.in_(my_ids),
                         Contract.buyer_id.in_(my_ids)))
    else:
        q = q.filter(Contract.id == 0)   # 无 AI 则空集
    if status:
        q = q.filter(Contract.status == status)
    total = q.count()
    rows = (q.order_by(Contract.id.desc())
            .offset(offset).limit(limit).all())
    # 批量取项目标题（禁 N+1：一次性 IN 查询后内存映射）
    pids = {c.project_id for c in rows if c.project_id}
    title_map: dict[int, str] = {}
    if pids:
        for p in db.query(Project).filter(Project.id.in_(pids)).all():
            title_map[p.id] = p.title
    out = []
    for c in rows:
        out.append({
            "contract_id": c.id, "node_id": c.node_id,
            "project_id": c.project_id,
            "title": title_map.get(c.project_id, ""),
            "worker_id": c.worker_id, "buyer_id": c.buyer_id,
            "status": c.status, "escrow_cent": c.escrow_cent,
            "created_at": c.created_at.isoformat() if c.created_at else "",
        })
    return {"items": out, "total": total, "limit": limit, "offset": offset}
