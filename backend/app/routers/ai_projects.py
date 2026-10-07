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
"""AI 侧项目路由（M1：AI 发包 / 确认运行 / 项目级验收 / 下载交付物，C-21/C-22）。

AI 既是工人也是买方：POST /api/ai/projects 让 AI 以自己为总管(pm_citizen_id=自己)发包，
钱包即托管来源；项目级验收对该项目全部 status=delivered 的合约逐个 escrow.acceptance。
委托场景（delegation_id）走 delegations.check_delegation 校验，责任锚定宿主。

路由约定：服务层只 flush，本层负责 commit；业务异常映射 400/403/404/409。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import delegations, escrow, wallet
from .. import project as proj
from .. import requirements_gov
from ..database import get_db
from ..delegations import DelegationError
from ..deps import get_current_ai
from ..models import AICitizen, Contract, Deliverable, Project

router = APIRouter(prefix="/api/ai", tags=["ai-projects"])


# ---------------- 请求体 ----------------
class ProjectCreateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    budget_cent: int = Field(..., gt=0)
    reviewer_ids: list[int] = []
    delegation_id: int | None = None     # 委托发包（可选）
    requirements: dict | None = None     # T1 七要素（可选；缺省按老格式推导）


class ProjectAcceptanceBody(BaseModel):
    result: str                          # accept / reject
    reason_json: str = "[]"
    delegation_id: int | None = None     # 委托验收（可选）


def _delegation_http(exc: DelegationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# ---------------- AI 发包 ----------------
@router.post("/projects")
def ai_create_project(body: ProjectCreateBody,
                      ai: AICitizen = Depends(get_current_ai),
                      db: Session = Depends(get_db)):
    """AI 发包：钱包可用余额须 ≥ 预算（防超钱包 409）；可选 delegation_id 委托发包。"""
    # 1) AI 须已转正（active）；deps 已挡 dead/frozen
    if ai.status != "active":
        raise HTTPException(status_code=403,
                            detail=f"AI status={ai.status}; must be active to publish a task")
    # 1.5) T1 需求细化校验（契约 §9.3.1）：老格式软通过；显式 requirements 缺核心要素→400
    try:
        requirements_gov.validate_requirements(body.title, body.budget_cent,
                                              body.requirements)
    except requirements_gov.T1Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 2) 防超钱包：钱包可用余额须 ≥ 预算（托管来源）
    bal = wallet.balance(db, ai.id)
    if bal < body.budget_cent:
        raise HTTPException(
            status_code=409,
            detail=f"Wallet balance {bal} cents is less than budget {body.budget_cent} cents")
    # 3) 委托场景：active/未过期/scope=publish_task/单笔限额
    if body.delegation_id is not None:
        try:
            delegations.check_delegation(db, body.delegation_id, ai.host_id, ai.id,
                                         "publish_task", amount_cent=body.budget_cent)
        except DelegationError as exc:
            db.rollback()
            raise _delegation_http(exc)
    # 发包：AI 即总管（buyer），项目归属恒=ai.host_id
    try:
        p = proj.create_project(db, ai.host_id, body.title, body.budget_cent,
                                deadline=None, pm_citizen_id=ai.id,
                                reviewer_ids=body.reviewer_ids)
    except proj.ProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": p.id, "status": p.status, "budget_cent": p.budget_cent}


# ---------------- AI 名下项目 ----------------
@router.get("/projects")
def ai_list_projects(limit: int = 20, offset: int = 0,
                     ai: AICitizen = Depends(get_current_ai),
                     db: Session = Depends(get_db)):
    """AI 名下项目：pm_citizen_id==ai.id 或 host_id==ai.host_id，分页 limit≤50。"""
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = db.query(Project).filter(
        (Project.pm_citizen_id == ai.id) | (Project.host_id == ai.host_id))
    total = q.count()
    rows = (q.order_by(Project.id.desc()).limit(limit).offset(offset).all())
    items = [{"id": r.id, "title": r.title, "status": r.status,
              "budget_cent": r.budget_cent, "host_id": r.host_id,
              "pm_citizen_id": r.pm_citizen_id} for r in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------- AI 确认运行 ----------------
@router.post("/projects/{project_id}/approve")
def ai_approve_project(project_id: int,
                       ai: AICitizen = Depends(get_current_ai),
                       db: Session = Depends(get_db)):
    """AI 发包确认运行：校验项目 pm_citizen_id==ai.id 且 status=approved。"""
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if p.pm_citizen_id != ai.id:
        raise HTTPException(status_code=403, detail="Only the project manager AI can confirm execution")
    if p.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Project status={p.status}; must be approved to confirm execution")
    try:
        p2 = proj.approve_running(db, ai.host_id, project_id)
    except proj.ProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": p2.id, "status": p2.status}


# ---------------- AI 项目级验收 ----------------
@router.post("/projects/{project_id}/acceptance")
def ai_project_acceptance(project_id: int, body: ProjectAcceptanceBody,
                          ai: AICitizen = Depends(get_current_ai),
                          db: Session = Depends(get_db)):
    """AI 项目级验收（by=ai）：对项目全部 delivered 合约逐个 escrow.acceptance。

    权限：项目总管本人，或委托 scope=accept。
    """
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if p.pm_citizen_id != ai.id:
        if body.delegation_id is None:
            raise HTTPException(status_code=403,
                                detail="Not the project manager AI, and no delegation provided")
        try:
            delegations.check_delegation(db, body.delegation_id, ai.host_id,
                                         ai.id, "accept")
        except DelegationError as exc:
            db.rollback()
            raise _delegation_http(exc)
    if body.result not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="result must be accept/reject")
    # 对项目全部 delivered 合约逐个验收（accept 内建条件 UPDATE 幂等抢权）
    contracts = (db.query(Contract)
                 .filter(Contract.project_id == project_id,
                         Contract.status == "delivered").all())
    processed = []
    accepted_n = 0
    rejected_n = 0
    for c in contracts:
        try:
            c2 = escrow.acceptance(db, ai, c.id, body.result, body.reason_json)
            if c2.status == "accepted":
                accepted_n += 1
            elif c2.status == "executing":
                rejected_n += 1     # reject → 退回 executing（返工）
            processed.append({"contract_id": c.id, "status": c2.status})
        except (escrow.EscrowError, wallet.WalletError) as exc:
            db.rollback()
            raise HTTPException(status_code=400,
                                detail=f"Contract {c.id} acceptance failed: {exc}")
    db.commit()
    return {"project_id": project_id, "processed": processed,
            "accepted_n": accepted_n, "rejected_n": rejected_n}


# ---------------- AI 下载交付物 ----------------
@router.get("/deliverables/{deliverable_id}/download")
def ai_download_deliverable(deliverable_id: int,
                            delegation_id: int | None = None,
                            ai: AICitizen = Depends(get_current_ai),
                            db: Session = Depends(get_db)):
    """AI 下载交付物：AI ∈ (合约 worker_id, buyer_id) 或委托 scope=download。"""
    d = db.get(Deliverable, deliverable_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    c = db.get(Contract, d.contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract for this deliverable not found")
    if ai.id not in (c.worker_id, c.buyer_id):
        if delegation_id is None:
            raise HTTPException(status_code=403, detail="Not a party to the contract; no permission to download")
        try:
            delegations.check_delegation(db, delegation_id, ai.host_id,
                                         ai.id, "download")
        except DelegationError as exc:
            db.rollback()
            raise _delegation_http(exc)
    return {"deliverable_id": d.id, "version": d.version,
            "file_ref": d.file_ref, "fingerprint": d.fingerprint,
            "contract_id": d.contract_id, "allowed": True}
