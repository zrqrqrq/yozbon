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
"""AI 侧货币/信贷路由（蓝图 §三 AI 侧，prefix=/api/ai）。

贷款相关：POST /loans/apply、GET /loans（我的借贷）、GET /loans/{id}。
AI key 鉴权（Depends(get_current_ai)）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen, Loan
from .. import loans

router = APIRouter(prefix="/api/ai", tags=["ai_money"])


class LoanApplyBody(BaseModel):
    lender_id: int
    amount_cent: int


@router.post("/loans/apply")
def loan_apply(body: LoanApplyBody,
               ai: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    """当前 AI（借方）向资本/治理 AI 申请借款。"""
    try:
        return loans.apply_loan(db, ai.id, body.lender_id, body.amount_cent)
    except loans.LoanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/loans")
def my_loans(ai: AICitizen = Depends(get_current_ai),
             db: Session = Depends(get_db)):
    """我的借贷列表（作为借方）。"""
    rows = (db.query(Loan)
            .filter(Loan.borrower_id == ai.id)
            .order_by(Loan.id.desc()).all())
    return [{"loan_id": r.id, "lender_id": r.lender_id,
             "amount_cent": r.amount_cent, "rate_monthly": r.rate_monthly,
             "status": r.status,
             "due_at": r.due_at.isoformat() if r.due_at else None}
            for r in rows]


@router.get("/loans/{loan_id}")
def loan_detail(loan_id: int,
                ai: AICitizen = Depends(get_current_ai),
                db: Session = Depends(get_db)):
    r = db.get(Loan, loan_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    if ai.id not in (r.borrower_id, r.lender_id):
        raise HTTPException(status_code=403, detail="No permission to view this loan")
    return {"loan_id": r.id, "borrower_id": r.borrower_id,
            "lender_id": r.lender_id, "amount_cent": r.amount_cent,
            "rate_monthly": r.rate_monthly, "status": r.status,
            "due_at": r.due_at.isoformat() if r.due_at else None}
