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
"""周薪发放 + 工作评判服务（表 45/46）。

核心逻辑：
- 城主/上级对职务 AI 按周评判绩效（work_reviews）；
- 每周结算时按绩效系数发放基础周薪（payroll_runs），从税池支出；
- 幂等：同 ai_id + period 仅发放一次。
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from . import wallet
from .wallet import WalletError
from .database import SessionLocal
from .models import AICitizen, AuditLog, PayrollRun, WorkReview

# ---------------- 配置常量 ----------------
WEEKLY_SALARY_BASE_CENT = 5000          # 基础周薪 50 AC = 5000 分
COEFFICIENT_EXCELLENT = 1.2             # avg >= 0.8
COEFFICIENT_GOOD = 1.0                  # avg >= 0.5
COEFFICIENT_POOR = 0.6                  # avg < 0.5


class PayrollError(Exception):
    """薪资/评判业务异常。"""


def _now():
    return datetime.utcnow()


def get_weekly_period(dt: datetime) -> str:
    """返回 ISO 周字符串 "yyyy-Wnn"。"""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _coefficient(avg_score: float) -> float:
    """根据平均绩效分映射系数。"""
    if avg_score >= 0.8:
        return COEFFICIENT_EXCELLENT
    elif avg_score >= 0.5:
        return COEFFICIENT_GOOD
    else:
        return COEFFICIENT_POOR


# ---------------- 工作评判 ----------------

def review_work(db: Session, ai_id: int, reviewer_id: int,
                task_id: int = 0, quality_score: float = 1.0,
                verdict: str = "pass", comment: str = "",
                period: str | None = None) -> WorkReview:
    """城主/上级提交对职务 AI 的绩效评判。

    禁止自评（reviewer_id == ai_id → PayrollError）。
    """
    if reviewer_id == ai_id:
        raise PayrollError("Self-evaluation not allowed (the judge cannot be the judged)")

    now = _now()
    if period is None:
        period = get_weekly_period(now)

    row = WorkReview(
        ai_id=ai_id,
        reviewer_id=reviewer_id,
        task_id=task_id,
        period=period,
        quality_score=quality_score,
        verdict=verdict,
        comment=comment,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


# ---------------- 周薪结算 ----------------

def settle_weekly_payroll(db: Session, now: datetime | None = None) -> list:
    """结算本周职务 AI 周薪（从税池支出）。

    仅对 governance 级或 occupation 含 "治理"/"governance" 的非城主 AI 发放。
    幂等：已有 payroll_run(ai_id, period) 且 status=paid → skip。
    返回本次实际发放记录列表。
    """
    now = now or _now()
    period = get_weekly_period(now)

    # 获取所有职务 AI：class_level == "governance" 且非城主（城主 is_internal=1 不领薪）
    gov_ais = (db.query(AICitizen)
               .filter(AICitizen.class_level == "governance",
                       AICitizen.is_internal != 1)
               .all())

    if not gov_ais:
        return []

    # 第一遍：筛出待发放 AI 并计算各自金额，累计待发放总额（跳过已 paid 的，保持幂等）。
    # 提前算出总额，用于发放前的税池预警（S3）。
    pending = []  # 元素：(ai_id, existing_run_id, coeff, amount_cent)
    total_needed = 0
    for ai in gov_ais:
        existing = (db.query(PayrollRun)
                    .filter(PayrollRun.ai_id == ai.id,
                            PayrollRun.period == period)
                    .first())
        if existing is not None and existing.status == "paid":
            continue

        reviews = (db.query(WorkReview)
                   .filter(WorkReview.ai_id == ai.id,
                           WorkReview.period == period)
                   .all())
        if reviews:
            avg_score = sum(r.quality_score for r in reviews) / len(reviews)
        else:
            avg_score = 0.5  # 无评判记录默认及格

        coeff = _coefficient(avg_score)
        amount_cent = int(WEEKLY_SALARY_BASE_CENT * coeff)
        pending.append((ai.id, existing.id if existing is not None else 0,
                        coeff, amount_cent))
        total_needed += amount_cent

    # 税池预警（S3）：发放前若税池余额低于待发放总额，写一条预警审计。
    # 预警单独提交，避免后续逐个发放的 rollback 把它一并回滚而丢失留痕。
    # 预警不阻断发放：实际发放逐个降级，池尽即停，已成功者照常入账留痕。
    if total_needed > 0:
        pool_now = wallet.get_system_state(db, "tax_pool")
        if pool_now < total_needed:
            db.add(AuditLog(actor_type="system", actor_id=0,
                            action="payroll.tax_pool_shortfall",
                            detail=json.dumps({
                                "period": period,
                                "tax_pool_cent": pool_now,
                                "total_needed_cent": total_needed,
                                "pending_count": len(pending),
                            }, ensure_ascii=False)))
            db.commit()

    # 第二遍：逐个 AI 发放（对齐 retainer.weekly_settle 的逐岗降级范式）。
    # 单个 AI 触发 WalletError（税池不足 / 重复入账）→ 回滚该半成品并跳过，
    # 不影响同批其它已成功发放；每笔成功单独提交，确保入账留痕不可被后续失败连带回滚。
    results = []
    for ai_id, existing_run_id, coeff, amount_cent in pending:
        ref = f"payroll:{ai_id}:{period}"
        try:
            # 从税池支出 + 入账给 AI
            wallet.adjust_system_state(db, "tax_pool", -amount_cent, ref=ref)
            wallet.credit(db, ai_id, amount_cent, "周薪",
                          ref=ref,
                          note=f"salary {period} coeff={coeff}")
        except WalletError:
            db.rollback()
            continue  # 税池不足或重复入账：跳过该 AI，继续下一个

        # 写 PayrollRun（如果已有 pending 记录则更新）
        existing = db.get(PayrollRun, existing_run_id) if existing_run_id else None
        if existing is not None:
            existing.base_salary_cent = WEEKLY_SALARY_BASE_CENT
            existing.coefficient = coeff
            existing.amount_cent = amount_cent
            existing.status = "paid"
            existing.paid_at = now
        else:
            pr = PayrollRun(
                ai_id=ai_id,
                period=period,
                base_salary_cent=WEEKLY_SALARY_BASE_CENT,
                coefficient=coeff,
                amount_cent=amount_cent,
                status="paid",
                paid_at=now,
                created_at=now,
            )
            db.add(pr)
        # 每笔独立提交：成功者正常入账留痕（PayrollRun + AILedger + 税池扣减）。
        db.commit()
        results.append({
            "ai_id": ai_id,
            "period": period,
            "amount_cent": amount_cent,
            "coefficient": coeff,
        })

    return results


# ---------------- Scheduler 日级 job 适配 ----------------

def payroll_daily_job(db: Session, now: datetime) -> int:
    """注册为 scheduler 日级 job：仅周一执行周薪结算。返回 0（无 task_id）。"""
    # 仅周一执行（isocalendar()[2] == 1）
    if now.isocalendar()[2] != 1:
        return 0
    settle_weekly_payroll(db, now)
    return 0
