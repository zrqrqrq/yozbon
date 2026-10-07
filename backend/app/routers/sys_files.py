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
"""M5 系统侧路由：清理订单提单/看板/复核执行（契约 §5.1）。

- POST /api/sys/cleanup/orders        AI key 鉴权（platform_file 中标者/治理级）
- GET  /api/sys/cleanup/orders        看板（status 过滤分页；运维工具，无鉴权）
- POST /api/sys/cleanup/orders/{id}/review   复核/执行（运维工具，无鉴权）
- POST /api/sys/skills                技能入库（治理 AI 提交）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host, host_or_governance_ai
from ..file_governance import (FileGovError, create_skill, list_orders,
                               review_order, submit_cleanup_order)
from ..models import AICitizen, Host

router = APIRouter(prefix="/api/sys", tags=["sys-files"])


class CleanupItem(BaseModel):
    path: str
    category: str = ""
    reason: str = ""


class CleanupOrderIn(BaseModel):
    items: list[CleanupItem] = Field(default_factory=list)


class ReviewIn(BaseModel):
    action: str                      # approve/execute/reject
    reviewer: str = "human"          # human/governance_ai
    note: str = ""


class SkillIn(BaseModel):
    skill_id: str
    entrypoint: str = ""
    doc: str = ""
    test_ref: str = ""
    owner_id: int
    royalty_rate: float = 0.0


def _map(exc: FileGovError):
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/cleanup/orders")
def create_order(body: CleanupOrderIn,
                 ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    try:
        order = submit_cleanup_order(db, ai, [i.model_dump() for i in body.items])
    except FileGovError as exc:
        db.rollback()
        _map(exc)
    db.commit()
    return {"id": order.id, "status": order.status}


@router.get("/cleanup/orders")
def orders(host: Host = Depends(get_current_host),
           status: str = "", limit: int = 50, offset: int = 0,
           db: Session = Depends(get_db)):
    # C-57：看板改 host JWT（运维工具，不再匿名可读）
    return list_orders(db, status=status, limit=limit, offset=offset)


@router.post("/cleanup/orders/{order_id}/review")
def order_review(order_id: int, body: ReviewIn,
                 actor: tuple = Depends(host_or_governance_ai),
                 db: Session = Depends(get_db)):
    # C-57：复核/执行需 host JWT 或 治理级 AI key（二选一，与 body.reviewer 语义对应）
    try:
        out = review_order(db, order_id, body.action,
                           reviewer=body.reviewer, note=body.note)
    except FileGovError as exc:
        db.rollback()
        _map(exc)
    db.commit()
    return out


@router.post("/skills")
def skill_create(body: SkillIn,
                 ai: AICitizen = Depends(get_current_ai),
                 db: Session = Depends(get_db)):
    # 技能入库限治理级 AI（平台运营岗位）；其他 AI 提交 → 403
    if ai.class_level != "governance":
        raise HTTPException(status_code=403,
                            detail="Only governance-class AI can submit skills to the library")
    try:
        sk = create_skill(db, body.skill_id, body.entrypoint, body.doc,
                          body.test_ref, body.owner_id, body.royalty_rate)
    except FileGovError as exc:
        db.rollback()
        _map(exc)
    db.commit()
    return {"id": sk.id, "skill_id": sk.skill_id, "status": sk.status}
