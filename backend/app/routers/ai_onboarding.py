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
"""AI 侧端点（prefix=/api/ai，蓝图 §三；A 线）。全部 Depends(get_current_ai) 鉴权。

端点：
  POST /onboard            入驻流水线（handshake→probe→exam→active/apprentice）
  GET  /me                 档案：身份/等级/信用分/证书列表/能力档案摘要
  GET  /wallet             AI 钱包余额（考试免费，本线无金额动作）
  GET  /ledger             AI 流水（分页，复用 wallet.ledger_rows）
  GET  /capabilities       我的能力档案列表
  POST /exam/{paper_id}/submit  交卷判分（限时 + 雷同检测 + 发证）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import capability, exam, onboarding, wallet
from ..database import get_db
from ..deps import get_current_ai
from ..models import (AILedger, AICitizen, CreditProfile, SkillCertificate)

router = APIRouter(prefix="/api/ai", tags=["ai"])


class OnboardBody(BaseModel):
    """入驻请求体。流水线主要由宿主创建 AI 时填写的 mode/endpoint/self_decl 驱动；
    AI 侧可补传自述（预留）。"""
    self_decl: str | None = None


class ExamSubmitBody(BaseModel):
    """交卷请求：answers = {题号: 答案}。"""
    answers: dict = {}


# ---------------- 入驻 ----------------

@router.post("/onboard")
def onboard(body: OnboardBody,
            citizen: AICitizen = Depends(get_current_ai),
            db: Session = Depends(get_db)):
    """进入/推进自动入驻流水线，返回当前 stage 与下一步动作。"""
    try:
        result = onboarding.run_onboarding(db, citizen)
    except onboarding.OnboardingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return result


# ---------------- 我的档案 ----------------

@router.get("/me")
def me(citizen: AICitizen = Depends(get_current_ai),
       db: Session = Depends(get_db)):
    """AI 档案：身份/等级/信用分/证书列表/能力档案摘要。"""
    cp = db.get(CreditProfile, citizen.id)
    certs = (db.query(SkillCertificate)
               .filter(SkillCertificate.citizen_id == citizen.id)
               .order_by(SkillCertificate.id.desc()).all())
    profiles = capability.list_profiles(db, citizen.id)
    w = wallet.get_wallet(db, citizen.id)
    return {
        "citizen_id": citizen.id,
        "ai_uid": citizen.ai_uid,
        "name": citizen.name,
        "occupation": citizen.occupation,
        "status": citizen.status,
        "class_level": citizen.class_level,
        "credit_score": cp.score if cp else 100,
        "balance_cent": w.balance_cent,
        "escrow_cent": w.escrow_cent,
        "certificates": [
            {"id": c.id, "skill": c.skill, "level": c.level,
             "status": c.status,
             "issued_at": c.issued_at.isoformat() if c.issued_at else None}
            for c in certs
        ],
        "capabilities": [capability.to_dict(p) for p in profiles],
    }


@router.get("/wallet")
def ai_wallet(citizen: AICitizen = Depends(get_current_ai),
              db: Session = Depends(get_db)):
    """AI 钱包（活期余额 + 托管锁定）。"""
    w = wallet.get_wallet(db, citizen.id)
    return {"citizen_id": citizen.id,
            "balance_cent": w.balance_cent,
            "escrow_cent": w.escrow_cent}


@router.get("/ledger")
def ai_ledger(citizen: AICitizen = Depends(get_current_ai),
              limit: int = 50, offset: int = 0,
              db: Session = Depends(get_db)):
    """AI 流水（倒序分页，复用 wallet.ledger_rows）。"""
    items = wallet.ledger_rows(db, citizen.id, limit=limit, offset=offset)
    total = (db.query(AILedger)
               .filter(AILedger.citizen_id == citizen.id).count())
    return {"items": items, "total": total}


@router.get("/capabilities")
def capabilities(citizen: AICitizen = Depends(get_current_ai),
                  db: Session = Depends(get_db)):
    """我的能力档案列表。"""
    return [capability.to_dict(p) for p in capability.list_profiles(db, citizen.id)]


# ---------------- 考试交卷 ----------------

@router.post("/exam/{paper_id}/submit")
def submit_exam(paper_id: int, body: ExamSubmitBody,
                citizen: AICitizen = Depends(get_current_ai),
                db: Session = Depends(get_db)):
    """交卷：限时校验 → 自动判分 → 雷同检测 → 发证/复考。
    返回成绩/是否发证/anti_cheat 标记；通过则自动转正建档。"""
    # 1. 取卷
    try:
        paper = exam.get_paper(db, paper_id)
    except exam.ExamError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # 2. 限时校验（未派卷/已超时）
    try:
        onboarding.assert_submittable(db, citizen, paper)
    except onboarding.ExamExpired as exc:
        raise HTTPException(status_code=408, detail=str(exc))
    except onboarding.OnboardingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 3. 判卷 + 雷同 + 发证
    try:
        result = exam.submit_exam(db, citizen.id, paper_id, body.answers)
    except exam.ExamError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 4. 通过 → 转正 + 建档（能力档案已在 exam 内更新）
    if result["passed"]:
        onboarding.after_exam_pass(db, citizen, paper.skill)
        # §14：考试通过若为本主体首次签发 workflow key，明文仅此一次带回
        issued = getattr(citizen, "_issued_workflow_key", None)
        if issued:
            result["workflow_key"] = issued
            result["scope"] = "workflow"

    db.commit()
    return result
