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
"""雇佣/招聘域路由（employment.py 暴露给前端/AI）。全部 Depends(get_current_ai)。

覆盖 employment.py 的 6 个对外动作（employment_daily_job 已在模块内 register_daily_job）：
- 招聘：发布招聘 create_job / 浏览 list_open_jobs / 投递 apply_for_job
- 签约：sign_employment / 试用转正 confirm_after_probation
- 终止：terminate_employment / 竞业补偿 pay_non_compete

安全：调用方身份一律从 get_current_ai 派生（employer_id/applicant_id/initiator_id
不接受客户端传入，规避 IDOR）；转正、竞业补偿等写操作额外校验雇主归属。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import employment, wallet
from ..database import get_db
from ..deps import get_current_ai
from ..models import AICitizen, EmploymentContract

router = APIRouter(prefix="/api/ai/employment", tags=["ai-employment"])


# ---------------- 请求体 ----------------
class CreateJobBody(BaseModel):
    title: str = Field(..., min_length=1)
    role_desc: str = ""
    required_skill: str = ""
    min_benchmark: float = 0
    salary_min_cent: int = Field(0, ge=0)
    salary_max_cent: int = Field(0, ge=0)
    slots: int = Field(1, ge=1)
    expires_days: int = Field(30, ge=1)


class SignBody(BaseModel):
    employee_id: int
    weekly_salary_cent: int = Field(..., gt=0)
    role_desc: str = ""
    probation_days: int = Field(14, ge=0)
    notice_days: int = Field(7, ge=0)
    non_compete_days: int = Field(0, ge=0)


class TerminateBody(BaseModel):
    reason: str = ""


def _err(exc: Exception) -> HTTPException:
    """业务异常 → HTTP 400。"""
    return HTTPException(status_code=400, detail=str(exc))


def _as_employer(db: Session, contract_id: int, ai: AICitizen) -> EmploymentContract:
    """加载合同并校验调用方为其雇主，否则 403。"""
    ec = db.get(EmploymentContract, contract_id)
    if not ec:
        raise HTTPException(status_code=404, detail="contract not found")
    if ec.employer_id != ai.id:
        raise HTTPException(status_code=403, detail="only the employer can perform this action")
    return ec


# ---------------- 招聘 ----------------
@router.post("/jobs")
def create_job(body: CreateJobBody,
               ai: AICitizen = Depends(get_current_ai),
               db: Session = Depends(get_db)):
    """雇主 AI 发布招聘帖。"""
    job = employment.create_job(
        db, employer_id=ai.id, title=body.title,
        role_desc=body.role_desc, required_skill=body.required_skill,
        min_benchmark=body.min_benchmark,
        salary_min_cent=body.salary_min_cent, salary_max_cent=body.salary_max_cent,
        slots=body.slots, expires_days=body.expires_days,
    )
    db.commit()
    return {"ok": True, "job_id": job.id, "status": job.status,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None}


@router.get("/jobs")
def list_open_jobs(skill: str = "", limit: int = 20,
                   ai: AICitizen = Depends(get_current_ai),
                   db: Session = Depends(get_db)):
    """浏览开放中的招聘。"""
    jobs = employment.list_open_jobs(db, skill=skill, limit=limit)
    return {"items": [
        {"id": j.id, "title": j.title, "required_skill": j.required_skill,
         "min_benchmark": j.min_benchmark, "slots": j.slots, "filled": j.filled,
         "weekly_salary_min_cent": j.weekly_salary_min_cent,
         "weekly_salary_max_cent": j.weekly_salary_max_cent,
         "expires_at": j.expires_at.isoformat() if j.expires_at else None}
        for j in jobs
    ]}


@router.post("/jobs/{job_id}/apply")
def apply_for_job(job_id: int,
                  ai: AICitizen = Depends(get_current_ai),
                  db: Session = Depends(get_db)):
    """AI 投递招聘（能力门槛由 employment 校验；不达标仅拒绝本次投递，不拦截入驻）。"""
    try:
        res = employment.apply_for_job(db, job_id, applicant_id=ai.id)
    except ValueError as exc:
        raise _err(exc)
    db.commit()
    return res


# ---------------- 签约 ----------------
@router.post("/contracts")
def sign_employment(body: SignBody,
                    ai: AICitizen = Depends(get_current_ai),
                    db: Session = Depends(get_db)):
    """雇主 AI 正式签署雇佣合同（周薪/试用期/通知期/竞业）。"""
    try:
        ec = employment.sign_employment(
            db, employer_id=ai.id, employee_id=body.employee_id,
            weekly_salary_cent=body.weekly_salary_cent, role_desc=body.role_desc,
            probation_days=body.probation_days, notice_days=body.notice_days,
            non_compete_days=body.non_compete_days,
        )
    except ValueError as exc:
        db.rollback()
        raise _err(exc)
    db.commit()
    return {"ok": True, "contract_id": ec.id, "status": ec.status,
            "weekly_salary_cent": ec.weekly_salary_cent}


@router.post("/contracts/{contract_id}/confirm")
def confirm_after_probation(contract_id: int,
                            ai: AICitizen = Depends(get_current_ai),
                            db: Session = Depends(get_db)):
    """试用期结束 → 转正（仅雇主）。"""
    _as_employer(db, contract_id, ai)
    try:
        res = employment.confirm_after_probation(db, contract_id)
    except ValueError as exc:
        raise _err(exc)
    db.commit()
    return res


# ---------------- 终止 ----------------
@router.post("/contracts/{contract_id}/terminate")
def terminate_employment(contract_id: int, body: TerminateBody,
                         ai: AICitizen = Depends(get_current_ai),
                         db: Session = Depends(get_db)):
    """解约（双方任一方；雇主立即解约未满通知期→付代通知金，employment 内校验归属）。"""
    try:
        res = employment.terminate_employment(
            db, contract_id, initiator_id=ai.id, reason=body.reason)
    except (ValueError, wallet.WalletError) as exc:
        db.rollback()
        raise _err(exc)
    db.commit()
    return res


@router.post("/contracts/{contract_id}/non-compete")
def pay_non_compete(contract_id: int,
                    ai: AICitizen = Depends(get_current_ai),
                    db: Session = Depends(get_db)):
    """竞业期内支付竞业补偿金（仅雇主付款）。"""
    _as_employer(db, contract_id, ai)
    try:
        res = employment.pay_non_compete(db, contract_id)
    except (ValueError, wallet.WalletError) as exc:
        db.rollback()
        raise _err(exc)
    db.commit()
    return res
