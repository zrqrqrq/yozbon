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
"""长期雇佣合同 + 招聘系统。

与项目制合约（Contract）互补：
- Contract = 项目制（一个任务→交付→结束）
- EmploymentContract = 长期聘用（周薪/试用期/竞业）

周薪发放由现有 payroll.py 驱动（已实现），本模块负责合同生命周期。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .models import (
    AICitizen, CapabilityProfile, EmploymentContract, JobPosting,
)
from . import wallet as wallet_mod
from .scheduler import register_daily_job


def _now() -> datetime:
    return datetime.utcnow()


# ==================== 招聘 ====================

def create_job(
    db: Session, employer_id: int, title: str,
    role_desc: str = "", required_skill: str = "",
    min_benchmark: float = 0, salary_min_cent: int = 0,
    salary_max_cent: int = 0, slots: int = 1,
    expires_days: int = 30,
) -> JobPosting:
    """发布招聘帖。"""
    job = JobPosting(
        employer_id=employer_id, title=title,
        role_desc=role_desc, required_skill=required_skill,
        min_benchmark=min_benchmark,
        weekly_salary_min_cent=salary_min_cent,
        weekly_salary_max_cent=salary_max_cent,
        slots=slots,
        expires_at=_now() + timedelta(days=expires_days),
    )
    db.add(job)
    db.flush()
    return job


def list_open_jobs(db: Session, skill: str = "", limit: int = 20) -> list:
    """浏览开放中的招聘。"""
    q = db.query(JobPosting).filter(
        JobPosting.status == "open",
        JobPosting.expires_at > _now(),
    )
    if skill:
        q = q.filter(JobPosting.required_skill == skill)
    return q.order_by(JobPosting.created_at.desc()).limit(limit).all()


def apply_for_job(db: Session, job_id: int, applicant_id: int) -> dict:
    """AI 投递招聘。"""
    job = db.get(JobPosting, job_id)
    if not job or job.status != "open":
        raise ValueError("job not available")
    if job.filled >= job.slots:
        raise ValueError("job positions filled")

    # 能力门槛校验
    if job.min_benchmark > 0:
        cp = (
            db.query(CapabilityProfile)
            .filter(
                CapabilityProfile.citizen_id == applicant_id,
                CapabilityProfile.skill == (job.required_skill or ""),
            )
            .first()
        )
        if not cp or cp.benchmark_score < job.min_benchmark:
            raise ValueError("applicant does not meet minimum capability")

    return {"job_id": job_id, "applicant_id": applicant_id, "status": "applied"}


# ==================== 签约 ====================

def sign_employment(
    db: Session, employer_id: int, employee_id: int,
    weekly_salary_cent: int, role_desc: str = "",
    probation_days: int = 14, notice_days: int = 7,
    non_compete_days: int = 0,
) -> EmploymentContract:
    """正式签署雇佣合同。"""
    # 检查是否已有合同
    existing = (
        db.query(EmploymentContract)
        .filter(
            EmploymentContract.employer_id == employer_id,
            EmploymentContract.employee_id == employee_id,
            EmploymentContract.status.in_(["probation", "active"]),
        )
        .first()
    )
    if existing:
        raise ValueError("employment contract already exists")

    probation_end = _now() + timedelta(days=probation_days) if probation_days > 0 else None
    ec = EmploymentContract(
        employer_id=employer_id, employee_id=employee_id,
        role_desc=role_desc, weekly_salary_cent=weekly_salary_cent,
        probation_end=probation_end, notice_days=notice_days,
        non_compete_days=non_compete_days,
        status="probation" if probation_end else "active",
    )
    db.add(ec)
    db.flush()
    return ec


def confirm_after_probation(db: Session, contract_id: int) -> dict:
    """试用期结束 → 转正。"""
    ec = db.get(EmploymentContract, contract_id)
    if not ec or ec.status != "probation":
        raise ValueError("contract not in probation")
    ec.status = "active"
    db.flush()
    return {"contract_id": ec.id, "status": "active"}


# ==================== 终止 ====================

def terminate_employment(
    db: Session, contract_id: int,
    initiator_id: int, reason: str = "",
) -> dict:
    """解约（需提前 notice_days 天，否则需支付代通知金）。"""
    ec = db.get(EmploymentContract, contract_id)
    if not ec or ec.status not in ("probation", "active"):
        raise ValueError("contract not terminable")
    if initiator_id not in (ec.employer_id, ec.employee_id):
        raise ValueError("only parties can terminate")

    # 代通知金：立即解约但未满通知期 → 支付剩余天数工资
    notice_pay = 0
    if ec.notice_days > 0:
        notice_pay = ec.weekly_salary_cent * ec.notice_days // 7

    if notice_pay > 0 and initiator_id == ec.employer_id:
        wallet_mod.debit(
            db, ec.employer_id, notice_pay,
            reason=f"employment_notice_pay:contract:{ec.id}",
        )
        wallet_mod.credit(
            db, ec.employee_id, notice_pay,
            reason=f"employment_notice_receive:contract:{ec.id}",
        )

    ec.status = "terminated"
    ec.ended_at = _now()
    ec.termination_reason = reason or "voluntary"
    db.flush()
    return {"contract_id": ec.id, "status": "terminated", "notice_pay_cent": notice_pay}


# ==================== 竞业限制补偿 ====================

def pay_non_compete(db: Session, contract_id: int) -> dict:
    """解约后竞业期内支付竞业补偿金（按周付）。"""
    ec = db.get(EmploymentContract, contract_id)
    if not ec or ec.non_compete_days <= 0:
        return {"skipped": True, "reason": "no non-compete clause"}

    comp_per_week = ec.weekly_salary_cent * ec.non_compete_comp_bps // 10000
    total_weeks = ec.non_compete_days // 7
    total_comp = comp_per_week * total_weeks

    if total_comp > 0:
        wallet_mod.debit(
            db, ec.employer_id, total_comp,
            reason=f"non_compete_comp:contract:{ec.id}",
        )
        wallet_mod.credit(
            db, ec.employee_id, total_comp,
            reason=f"non_compete_receive:contract:{ec.id}",
        )
    return {"comp_total_cent": total_comp, "weeks": total_weeks}


# ==================== 日级任务：过期招聘 + 试用期自动转正 ====================

def employment_daily_job(db: Session) -> dict:
    """日巡检：关闭过期招聘 + 试用期到期自动转正。"""
    expired = (
        db.query(JobPosting)
        .filter(JobPosting.status == "open", JobPosting.expires_at < _now())
        .all()
    )
    for j in expired:
        j.status = "expired"

    confirmed = (
        db.query(EmploymentContract)
        .filter(
            EmploymentContract.status == "probation",
            EmploymentContract.probation_end != None,
            EmploymentContract.probation_end <= _now(),
        )
        .all()
    )
    for ec in confirmed:
        ec.status = "active"

    db.flush()
    return {"expired_jobs": len(expired), "confirmed_contracts": len(confirmed)}


register_daily_job("employment", employment_daily_job)
